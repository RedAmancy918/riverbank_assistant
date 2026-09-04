#!/usr/bin/env python3
"""Local provider-neutral Agent Runtime broker for RiverBank Edge OS."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import struct
from pathlib import Path
from typing import Any

from riverbank_agent import AgentCancelled, AgentRunRequest, AgentRuntimeError, AgentTimeout
from riverbank_agent.hermes import HermesCLIAdapter
from riverbank_agent.protocol import DEFAULT_SOCKET, MAX_WIRE_BYTES, PROTOCOL_SCHEMA, event


LOGGER = logging.getLogger("riverbank-agent-runtime")
DEFAULT_TOOLSETS = "browser,clarify,cronjob,file,memory,session_search,skills,todo,vision,web"


def parse_mapping(values: list[str], label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not Path(raw_path).is_absolute():
            raise ValueError(f"{label} must use name=/absolute/path")
        result[name] = Path(raw_path)
    if not result:
        raise ValueError(f"at least one {label} is required")
    return result


class AgentRuntimeServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        adapter: HermesCLIAdapter,
        max_concurrency: int = 3,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.adapter = adapter
        self.semaphore = asyncio.Semaphore(max(1, min(int(max_concurrency), 8)))
        self.active: dict[str, asyncio.Event] = {}
        self.active_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self.handle,
            path=str(self.socket_path),
            limit=MAX_WIRE_BYTES + 1,
        )
        os.chmod(self.socket_path, 0o660)

    async def close(self) -> None:
        async with self.active_lock:
            for cancelled in self.active.values():
                cancelled.set()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    @staticmethod
    def peer_allowed(writer: asyncio.StreamWriter) -> bool:
        transport_socket = writer.get_extra_info("socket")
        if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
            return True
        try:
            raw = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _pid, uid, _gid = struct.unpack("3i", raw)
        except OSError:
            return False
        return uid in {0, os.getuid()}

    async def send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_WIRE_BYTES:
            raise AgentRuntimeError("runtime event exceeds wire limit", code="response_too_large")
        writer.write(encoded)
        await writer.drain()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id = ""
        registered_request = False
        try:
            if not self.peer_allowed(writer):
                raise AgentRuntimeError("runtime peer is not authorized", code="peer_denied")
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw or len(raw) > MAX_WIRE_BYTES:
                raise AgentRuntimeError("invalid runtime request size", code="invalid_request")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentRuntimeError("invalid runtime JSON", code="invalid_request") from exc
            if not isinstance(payload, dict) or payload.get("schema") != PROTOCOL_SCHEMA:
                raise AgentRuntimeError("unsupported runtime protocol", code="invalid_request")
            operation = payload.get("operation")
            request_id = str(payload.get("requestId") or "")
            if operation == "health":
                await self.send(
                    writer,
                    event(
                        "health",
                        ok=True,
                        backend=self.adapter.name,
                        backendAvailable=self.adapter.available(),
                        activeRequests=len(self.active),
                        protocol=PROTOCOL_SCHEMA,
                    ),
                )
                return
            if operation == "cancel":
                cancelled = self.active.get(request_id)
                if cancelled is not None:
                    cancelled.set()
                await self.send(writer, event("cancelled", request_id, found=cancelled is not None))
                return
            request = AgentRunRequest.from_wire(payload)
            request_id = request.request_id
            async with self.active_lock:
                if request_id in self.active:
                    raise AgentRuntimeError("duplicate request_id", code="duplicate_request")
                cancelled = asyncio.Event()
                self.active[request_id] = cancelled
                registered_request = True
            await self.send(writer, event("accepted", request_id, backend=self.adapter.name))
            async with self.semaphore:
                if cancelled.is_set():
                    raise AgentCancelled()

                async def snapshot(text: str) -> None:
                    await self.send(writer, event("snapshot", request_id, text=text))

                response = await self.adapter.run(request, snapshot, cancelled)
            await self.send(
                writer,
                event("final", request_id, text=response, backend=self.adapter.name),
            )
        except AgentCancelled as exc:
            with contextlib.suppress(OSError, ConnectionError):
                await self.send(writer, event("error", request_id, code=exc.code, message=str(exc)))
        except AgentTimeout as exc:
            with contextlib.suppress(OSError, ConnectionError):
                await self.send(writer, event("error", request_id, code=exc.code, message=str(exc)))
        except AgentRuntimeError as exc:
            with contextlib.suppress(OSError, ConnectionError):
                await self.send(writer, event("error", request_id, code=exc.code, message=str(exc)))
        except Exception as exc:
            LOGGER.exception("unhandled runtime request failure id=%s", request_id)
            with contextlib.suppress(OSError, ConnectionError):
                await self.send(
                    writer,
                    event("error", request_id, code="internal_error", message=str(exc)[:500]),
                )
        finally:
            if request_id and registered_request:
                async with self.active_lock:
                    self.active.pop(request_id, None)
            writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await writer.wait_closed()


async def serve(args: argparse.Namespace) -> int:
    workspaces = parse_mapping(args.workspace, "workspace")
    adapter = HermesCLIAdapter(
        hermes_bin=args.hermes_bin,
        profile_home=args.profile_home,
        workspaces=workspaces,
        allowed_toolsets={item for item in args.allowed_toolsets.split(",") if item},
        image_roots=tuple(args.image_root),
    )
    server = AgentRuntimeServer(
        socket_path=args.socket,
        adapter=adapter,
        max_concurrency=args.max_concurrency,
    )
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    LOGGER.info(
        "Agent Runtime started protocol=%s backend=%s socket=%s",
        PROTOCOL_SCHEMA,
        adapter.name,
        args.socket,
    )
    await stop.wait()
    await server.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--backend", choices=("hermes-cli",), default="hermes-cli")
    parser.add_argument("--hermes-bin", type=Path, default=Path("/home/geo/.local/bin/hermes"))
    parser.add_argument("--profile-home", type=Path, default=Path("/home/geo/.hermes/profiles/daily"))
    parser.add_argument("--workspace", action="append", default=[])
    parser.add_argument("--image-root", type=Path, action="append", default=[])
    parser.add_argument("--allowed-toolsets", default=DEFAULT_TOOLSETS)
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.self_test:
        workspaces = parse_mapping(args.workspace, "workspace")
        print(
            json.dumps(
                {
                    "ok": True,
                    "schema": PROTOCOL_SCHEMA,
                    "backend": args.backend,
                    "workspaces": sorted(workspaces),
                    "maxConcurrency": max(1, min(args.max_concurrency, 8)),
                },
                ensure_ascii=False,
            )
        )
        return 0
    return asyncio.run(serve(args))


if __name__ == "__main__":
    raise SystemExit(main())
