"""Socket and compatibility transports for RiverBank Agent Runtime v1."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import select
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .hermes import HermesCLIAdapter
from .protocol import (
    AgentCancelled,
    AgentResult,
    AgentRunRequest,
    AgentRuntimeError,
    AgentTimeout,
    DEFAULT_SOCKET,
    MAX_WIRE_BYTES,
    PROTOCOL_SCHEMA,
)


SnapshotCallback = Callable[[str], Any]
CancelCallback = Callable[[], Any]


async def _invoke(callback: SnapshotCallback | None, value: str) -> None:
    if callback is None:
        return
    result = callback(value)
    if inspect.isawaitable(result):
        await result


async def _cancel_requested(callback: CancelCallback | None) -> bool:
    if callback is None:
        return False
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


class SocketAgentRuntime:
    """Provider-neutral client used by RiverBank product services."""

    def __init__(self, socket_path: Path = DEFAULT_SOCKET) -> None:
        self.socket_path = Path(socket_path)
        self._active: set[str] = set()
        self._lock = threading.Lock()

    async def run_async(
        self,
        request: AgentRunRequest,
        *,
        on_snapshot: SnapshotCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AgentResult:
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.socket_path),
                limit=MAX_WIRE_BYTES + 1,
            )
        except OSError as exc:
            raise AgentRuntimeError(
                f"agent runtime unavailable: {exc}", code="runtime_unavailable"
            ) from exc
        with self._lock:
            self._active.add(request.request_id)
        writer.write((json.dumps(request.to_wire(), ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds + 8.0
        try:
            while True:
                if await _cancel_requested(should_cancel):
                    await self.cancel_async(request.request_id)
                    raise AgentCancelled()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self.cancel_async(request.request_id)
                    raise AgentTimeout()
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=min(0.3, remaining))
                except asyncio.TimeoutError:
                    continue
                if not line:
                    raise AgentRuntimeError("agent runtime closed the stream", code="runtime_disconnected")
                payload = self._decode_event(line)
                kind = payload.get("event")
                if kind == "snapshot":
                    await _invoke(on_snapshot, str(payload.get("text") or ""))
                elif kind == "final":
                    return AgentResult(
                        request_id=request.request_id,
                        text=str(payload.get("text") or ""),
                        backend=str(payload.get("backend") or "unknown"),
                    )
                elif kind == "error":
                    self._raise_remote(payload)
        finally:
            with self._lock:
                self._active.discard(request.request_id)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass

    def run_sync(
        self,
        request: AgentRunRequest,
        *,
        on_snapshot: SnapshotCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AgentResult:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self.socket_path))
        except OSError as exc:
            client.close()
            raise AgentRuntimeError(
                f"agent runtime unavailable: {exc}", code="runtime_unavailable"
            ) from exc
        with self._lock:
            self._active.add(request.request_id)
        client.sendall((json.dumps(request.to_wire(), ensure_ascii=False) + "\n").encode("utf-8"))
        buffer = bytearray()
        deadline = __import__("time").monotonic() + request.timeout_seconds + 8.0
        try:
            while True:
                if should_cancel is not None and bool(should_cancel()):
                    self.cancel(request.request_id)
                    raise AgentCancelled()
                remaining = deadline - __import__("time").monotonic()
                if remaining <= 0:
                    self.cancel(request.request_id)
                    raise AgentTimeout()
                ready, _, _ = select.select([client], [], [], min(0.3, remaining))
                if not ready:
                    continue
                payload = client.recv(8192)
                if not payload:
                    raise AgentRuntimeError("agent runtime closed the stream", code="runtime_disconnected")
                buffer.extend(payload)
                if len(buffer) > MAX_WIRE_BYTES:
                    raise AgentRuntimeError("agent runtime event is too large", code="invalid_response")
                while b"\n" in buffer:
                    raw, _, tail = buffer.partition(b"\n")
                    buffer = bytearray(tail)
                    message = self._decode_event(raw)
                    kind = message.get("event")
                    if kind == "snapshot" and on_snapshot is not None:
                        on_snapshot(str(message.get("text") or ""))
                    elif kind == "final":
                        return AgentResult(
                            request_id=request.request_id,
                            text=str(message.get("text") or ""),
                            backend=str(message.get("backend") or "unknown"),
                        )
                    elif kind == "error":
                        self._raise_remote(message)
        finally:
            with self._lock:
                self._active.discard(request.request_id)
            client.close()

    async def cancel_async(self, request_id: str) -> bool:
        return await asyncio.to_thread(self.cancel, request_id)

    def cancel(self, request_id: str) -> bool:
        payload = {
            "schema": PROTOCOL_SCHEMA,
            "operation": "cancel",
            "requestId": request_id,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(self.socket_path))
                client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                response = client.recv(4096)
        except OSError:
            return False
        try:
            decoded = self._decode_event(response.splitlines()[0])
        except (AgentRuntimeError, IndexError):
            return False
        return decoded.get("event") == "cancelled"

    def cancel_all(self) -> None:
        with self._lock:
            active = tuple(self._active)
        for request_id in active:
            self.cancel(request_id)

    def health(self) -> dict[str, Any]:
        payload = {"schema": PROTOCOL_SCHEMA, "operation": "health"}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(self.socket_path))
                client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                response = client.recv(16 * 1024)
        except OSError as exc:
            raise AgentRuntimeError(str(exc), code="runtime_unavailable") from exc
        return self._decode_event(response.splitlines()[0])

    def available(self) -> bool:
        try:
            return self.health().get("event") == "health"
        except AgentRuntimeError:
            return False

    @staticmethod
    def _decode_event(raw: bytes) -> dict[str, Any]:
        if len(raw) > MAX_WIRE_BYTES:
            raise AgentRuntimeError("agent runtime event is too large", code="invalid_response")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentRuntimeError("invalid agent runtime response", code="invalid_response") from exc
        if not isinstance(payload, dict) or payload.get("schema") != PROTOCOL_SCHEMA:
            raise AgentRuntimeError("unsupported agent runtime response", code="invalid_response")
        return payload

    @staticmethod
    def _raise_remote(payload: dict[str, Any]) -> None:
        code = str(payload.get("code") or "runtime_error")
        message = str(payload.get("message") or "agent runtime failed")
        if code == "cancelled":
            raise AgentCancelled(message)
        if code == "timeout":
            raise AgentTimeout(message)
        raise AgentRuntimeError(message, code=code)


class DirectAgentRuntime:
    """Explicit compatibility transport; Hermes details remain inside the adapter."""

    def __init__(self, adapter: HermesCLIAdapter) -> None:
        self.adapter = adapter

    def available(self) -> bool:
        return self.adapter.available()

    async def run_async(
        self,
        request: AgentRunRequest,
        *,
        on_snapshot: SnapshotCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AgentResult:
        cancelled = asyncio.Event()

        async def monitor() -> None:
            while not cancelled.is_set():
                if await _cancel_requested(should_cancel):
                    cancelled.set()
                    return
                await asyncio.sleep(0.2)

        monitor_task = asyncio.create_task(monitor())
        try:
            text = await self.adapter.run(
                request,
                lambda value: _invoke(on_snapshot, value),
                cancelled,
            )
            return AgentResult(request.request_id, text, self.adapter.name)
        finally:
            cancelled.set()
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

    def run_sync(
        self,
        request: AgentRunRequest,
        *,
        on_snapshot: SnapshotCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AgentResult:
        return asyncio.run(
            self.run_async(
                request,
                on_snapshot=on_snapshot,
                should_cancel=should_cancel,
            )
        )

    def cancel_all(self) -> None:
        # Direct calls are cancelled through the per-request callback.
        return None
