"""Hermes CLI implementation of the provider-neutral Agent Runtime contract."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from .protocol import (
    MAX_RESPONSE_CHARS,
    AgentCancelled,
    AgentRunRequest,
    AgentRuntimeError,
    AgentTimeout,
)


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SESSION_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:Session ID|Session|session_id)\s*:\s*[^\n]+\s*$",
    re.I,
)
EMPTY_SESSION_RE = re.compile(
    r"^\s*Session\s+\S+\s+found but has no messages\.\s+Starting fresh\.\s*",
    re.I,
)
RESUMED_SESSION_RE = re.compile(r"^\s*(?:↻\s*)?Resumed session[^\n]*(?:\n|$)", re.I)


def clean_hermes_output(value: str) -> str:
    cleaned = ANSI_RE.sub("", value).replace("\r", "").strip()
    cleaned = EMPTY_SESSION_RE.sub("", cleaned).strip()
    cleaned = RESUMED_SESSION_RE.sub("", cleaned).strip()
    cleaned = SESSION_LINE_RE.sub("", cleaned).strip()
    return cleaned


class HermesCLIAdapter:
    """Translate validated runtime requests into one constrained Hermes CLI turn."""

    name = "hermes-cli"

    def __init__(
        self,
        *,
        hermes_bin: Path,
        profile_home: Path,
        workspaces: dict[str, Path],
        allowed_toolsets: set[str] | frozenset[str],
        image_roots: tuple[Path, ...] = (),
    ) -> None:
        self.hermes_bin = Path(hermes_bin)
        self.profile_home = Path(profile_home)
        self.workspaces = {
            str(name): Path(path).resolve(strict=False) for name, path in workspaces.items()
        }
        self.allowed_toolsets = frozenset(allowed_toolsets)
        self.image_roots = tuple(Path(path).resolve(strict=False) for path in image_roots)

    def available(self) -> bool:
        return self.hermes_bin.is_file() and os.access(self.hermes_bin, os.X_OK)

    def _image(self, request: AgentRunRequest) -> Path | None:
        if not request.image_path:
            return None
        try:
            image = Path(request.image_path).resolve(strict=True)
        except OSError as exc:
            raise AgentRuntimeError("image attachment is unavailable", code="invalid_attachment") from exc
        if not image.is_file() or not any(image.is_relative_to(root) for root in self.image_roots):
            raise AgentRuntimeError("image attachment is outside trusted roots", code="policy_denied")
        return image

    def command(self, request: AgentRunRequest) -> tuple[list[str], Path, dict[str, str]]:
        if not self.available():
            raise AgentRuntimeError("configured agent backend is unavailable", code="backend_unavailable")
        workspace = self.workspaces.get(request.workspace)
        if workspace is None:
            raise AgentRuntimeError("workspace is not configured", code="policy_denied")
        denied = sorted(set(request.toolsets).difference(self.allowed_toolsets))
        if denied:
            raise AgentRuntimeError(
                f"toolsets are not allowed: {', '.join(denied)}",
                code="policy_denied",
            )
        workspace.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.hermes_bin),
            "chat",
            "--query",
            request.prompt,
            "--quiet",
            "--toolsets",
            ",".join(request.toolsets),
            "--reasoning",
            request.reasoning,
            "--max-turns",
            str(request.max_turns),
            "--source",
            request.source,
            "--in",
            str(workspace),
        ]
        if request.session:
            command.extend(["--continue", request.session])
            if request.create_session:
                command.append("--create-if-missing")
        image = self._image(request)
        if image is not None:
            command.extend(["--image", str(image)])
        if request.autonomy:
            command.append("--yolo")
        environment = os.environ.copy()
        environment.setdefault("HERMES_ACCEPT_HOOKS", "1")
        environment.setdefault("HERMES_HOME", str(self.profile_home))
        if request.autonomy:
            environment.setdefault("HERMES_YOLO_MODE", "1")
        return command, workspace, environment

    async def run(
        self,
        request: AgentRunRequest,
        emit_snapshot: Callable[[str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> str:
        command, workspace, environment = self.command(request)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        chunks: list[str] = []
        response_chars = 0
        last_snapshot = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        try:
            while True:
                if cancel_event.is_set():
                    await self._terminate(process)
                    raise AgentCancelled()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._terminate(process)
                    raise AgentTimeout()
                try:
                    payload = await asyncio.wait_for(
                        process.stdout.read(512), timeout=min(0.25, remaining)
                    )
                except asyncio.TimeoutError:
                    continue
                if not payload:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        chunks.append(tail)
                        response_chars += len(tail)
                    break
                decoded = decoder.decode(payload, final=False)
                if decoded:
                    chunks.append(decoded)
                    response_chars += len(decoded)
                if response_chars > MAX_RESPONSE_CHARS:
                    await self._terminate(process)
                    raise AgentRuntimeError(
                        "agent backend response exceeded the safety limit",
                        code="response_too_large",
                    )
                snapshot = clean_hermes_output("".join(chunks))
                if snapshot and snapshot != last_snapshot:
                    last_snapshot = snapshot
                    await emit_snapshot(snapshot)
            return_code = await process.wait()
            response = clean_hermes_output("".join(chunks))
            if return_code != 0:
                raise AgentRuntimeError(
                    response[-4000:] or f"agent backend exited {return_code}",
                    code="backend_failed",
                )
            if not response:
                raise AgentRuntimeError("agent backend returned an empty response", code="empty_response")
            return response
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except Exception:
            await self._terminate(process)
            raise

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=4.0)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2.0)
