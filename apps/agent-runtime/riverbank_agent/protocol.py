"""Versioned, provider-neutral RiverBank Agent Runtime contract."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROTOCOL_SCHEMA = "riverbank.agent/v1"
DEFAULT_SOCKET = Path("/run/riverbank-agent/runtime.sock")
MAX_WIRE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_CHARS = 180_000
MAX_RESPONSE_CHARS = 500_000
PURPOSES = frozenset({"voice", "chat", "paper-qa", "task", "workshop", "daily-report"})
REASONING_LEVELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOOLSET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


class AgentRuntimeError(RuntimeError):
    """A provider-neutral runtime or protocol failure."""

    def __init__(self, message: str, *, code: str = "runtime_error") -> None:
        super().__init__(message)
        self.code = code


class AgentCancelled(AgentRuntimeError):
    def __init__(self, message: str = "agent request cancelled") -> None:
        super().__init__(message, code="cancelled")


class AgentTimeout(AgentRuntimeError):
    def __init__(self, message: str = "agent request timed out") -> None:
        super().__init__(message, code="timeout")


def _safe_name(value: Any, label: str, *, default: str = "") -> str:
    text = str(value or default).strip()
    if not text or not NAME_RE.fullmatch(text):
        raise AgentRuntimeError(f"invalid {label}", code="invalid_request")
    return text


@dataclass(frozen=True)
class AgentRunRequest:
    """One bounded runtime turn; the adapter never receives arbitrary commands."""

    prompt: str
    purpose: str
    workspace: str = "daily"
    toolsets: tuple[str, ...] = field(default_factory=tuple)
    reasoning: str = "medium"
    max_turns: int = 24
    timeout_seconds: float = 90.0
    source: str = "riverbank-runtime"
    session: str = ""
    create_session: bool = False
    image_path: str = ""
    autonomy: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        prompt = str(self.prompt).replace("\x00", "").strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise AgentRuntimeError("invalid prompt length", code="invalid_request")
        purpose = str(self.purpose).strip()
        if purpose not in PURPOSES:
            raise AgentRuntimeError("unsupported purpose", code="invalid_request")
        workspace = _safe_name(self.workspace, "workspace")
        source = _safe_name(self.source, "source")
        request_id = _safe_name(self.request_id, "request_id")
        session = str(self.session or "").strip()
        if session and not NAME_RE.fullmatch(session):
            raise AgentRuntimeError("invalid session", code="invalid_request")
        toolsets = tuple(dict.fromkeys(str(item).strip() for item in self.toolsets))
        if any(not TOOLSET_RE.fullmatch(item) for item in toolsets):
            raise AgentRuntimeError("invalid toolset", code="invalid_request")
        reasoning = str(self.reasoning).strip().lower()
        if reasoning not in REASONING_LEVELS:
            raise AgentRuntimeError("invalid reasoning level", code="invalid_request")
        max_turns = int(self.max_turns)
        if not 1 <= max_turns <= 64:
            raise AgentRuntimeError("max_turns must be between 1 and 64", code="invalid_request")
        timeout_seconds = float(self.timeout_seconds)
        if not 10.0 <= timeout_seconds <= 3600.0:
            raise AgentRuntimeError(
                "timeout_seconds must be between 10 and 3600",
                code="invalid_request",
            )
        image_path = str(self.image_path or "").strip()
        if image_path and not Path(image_path).is_absolute():
            raise AgentRuntimeError("image_path must be absolute", code="invalid_request")
        if self.autonomy and purpose not in {"task", "daily-report"}:
            raise AgentRuntimeError(
                "autonomy is restricted to trusted background work",
                code="policy_denied",
            )
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "toolsets", toolsets)
        object.__setattr__(self, "reasoning", reasoning)
        object.__setattr__(self, "max_turns", max_turns)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "image_path", image_path)

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": PROTOCOL_SCHEMA,
            "operation": "run",
            "requestId": self.request_id,
            "purpose": self.purpose,
            "workspace": self.workspace,
            "prompt": self.prompt,
            "toolsets": list(self.toolsets),
            "reasoning": self.reasoning,
            "maxTurns": self.max_turns,
            "timeoutSeconds": self.timeout_seconds,
            "source": self.source,
            "session": self.session,
            "createSession": bool(self.create_session),
            "imagePath": self.image_path,
            "autonomy": bool(self.autonomy),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "AgentRunRequest":
        if payload.get("schema") != PROTOCOL_SCHEMA or payload.get("operation") != "run":
            raise AgentRuntimeError("unsupported protocol request", code="invalid_request")
        raw_toolsets = payload.get("toolsets", [])
        if not isinstance(raw_toolsets, list):
            raise AgentRuntimeError("toolsets must be a list", code="invalid_request")
        return cls(
            prompt=payload.get("prompt", ""),
            purpose=payload.get("purpose", ""),
            workspace=payload.get("workspace", "daily"),
            toolsets=tuple(raw_toolsets),
            reasoning=payload.get("reasoning", "medium"),
            max_turns=payload.get("maxTurns", 24),
            timeout_seconds=payload.get("timeoutSeconds", 90.0),
            source=payload.get("source", "riverbank-runtime"),
            session=payload.get("session", ""),
            create_session=bool(payload.get("createSession", False)),
            image_path=payload.get("imagePath", ""),
            autonomy=bool(payload.get("autonomy", False)),
            request_id=payload.get("requestId", ""),
        )


@dataclass(frozen=True)
class AgentResult:
    request_id: str
    text: str
    backend: str


def event(event_type: str, request_id: str = "", **values: Any) -> dict[str, Any]:
    return {
        "schema": PROTOCOL_SCHEMA,
        "event": event_type,
        "requestId": request_id,
        **values,
    }
