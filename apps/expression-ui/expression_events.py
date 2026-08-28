#!/usr/bin/env python3
"""Structured expression events shared by RiverBank voice and display services."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path


SCHEMA = "riverbank.expression.event/v1"
SOURCE = "hermes-voice"
DEFAULT_SOCKET_PATH = Path("/run/riverbank-expression/voice-events.sock")
DEFAULT_SNAPSHOT_PATH = Path("/run/hermes-voice-control/expression-event.json")
ALLOWED_STATES = {
    "idle",
    "listening",
    "thinking",
    "happy",
    "love",
    "proud",
    "cool",
    "sad",
    "cry",
    "afraid",
    "angry",
    "error",
}
MAX_EVENT_AGE_SECONDS = 30.0
MAX_FUTURE_SKEW_SECONDS = 5.0


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot"


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class ExpressionEventPublisher:
    """Publish ordered lifecycle events and keep a restart-safe latest snapshot."""

    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
        *,
        source: str = SOURCE,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.snapshot_path = Path(snapshot_path)
        self.source = source
        self.publisher_started_at = time.time()
        self.publisher_id = (
            f"{_boot_id()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self._lock = threading.Lock()
        self._sequence = 0
        self._interaction_index = 0
        self._interaction_id = "system"
        self._interaction_source = "service"
        self._last_event: dict | None = None
        self._last_error: str | None = None

    def begin_interaction(self, source: str) -> tuple[str, int]:
        with self._lock:
            self._interaction_index += 1
            self._interaction_id = uuid.uuid4().hex
            self._interaction_source = str(source or "unknown")
            return self._interaction_id, self._interaction_index

    def publish(
        self,
        state: str,
        ttl: float | None = None,
        *,
        stage: str | None = None,
    ) -> dict:
        state_value = str(state).strip()
        if state_value not in ALLOWED_STATES:
            raise ValueError(f"unsupported expression state: {state_value}")
        ttl_value = None if ttl is None else max(0.0, min(float(ttl), 300.0))
        with self._lock:
            self._sequence += 1
            event = {
                "schema": SCHEMA,
                "source": self.source,
                "publisher_id": self.publisher_id,
                "publisher_started_at": self.publisher_started_at,
                "event_id": uuid.uuid4().hex,
                "sequence": self._sequence,
                "sent_at": time.time(),
                "monotonic_ns": time.monotonic_ns(),
                "interaction_id": self._interaction_id,
                "interaction_index": self._interaction_index,
                "interaction_source": self._interaction_source,
                "stage": str(stage or state_value),
                "state": state_value,
                "ttl": ttl_value,
            }
            self._last_event = event
            self._last_error = None
            try:
                atomic_write_json(self.snapshot_path, event)
            except OSError as exc:
                self._last_error = f"snapshot: {exc}"
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    client.sendto(
                        json.dumps(event, ensure_ascii=False).encode("utf-8"),
                        str(self.socket_path),
                    )
                finally:
                    client.close()
            except OSError as exc:
                self._last_error = f"event socket: {exc}"
            return dict(event)

    def state(self) -> dict:
        with self._lock:
            last_event = self._last_event or {}
            return {
                "schema": SCHEMA,
                "transport": "unix-datagram-with-atomic-snapshot",
                "socket_path": str(self.socket_path),
                "snapshot_path": str(self.snapshot_path),
                "publisher_id": self.publisher_id,
                "publisher_started_at": self.publisher_started_at,
                "sequence": self._sequence,
                "interaction_id": self._interaction_id,
                "interaction_index": self._interaction_index,
                "last_event_id": last_event.get("event_id"),
                "last_state": last_event.get("state"),
                "last_stage": last_event.get("stage"),
                "last_sent_at": last_event.get("sent_at"),
                "last_error": self._last_error,
            }


class ExpressionEventConsumer:
    """Validate event order across interactions and voice-service restarts."""

    def __init__(
        self,
        *,
        max_event_age_seconds: float = MAX_EVENT_AGE_SECONDS,
    ) -> None:
        self.max_event_age_seconds = max(1.0, float(max_event_age_seconds))
        self.publisher_id: str | None = None
        self.publisher_started_at = 0.0
        self.last_sequence = 0
        self.latest_interaction_index = 0
        self.accepted = 0
        self.rejected = 0
        self.last_decision = "waiting"
        self.last_event: dict | None = None

    def validate(
        self,
        event: object,
        *,
        now: float | None = None,
        allow_stale_idle: bool = False,
    ) -> tuple[bool, str]:
        now_value = time.time() if now is None else float(now)
        if not isinstance(event, dict):
            return self._reject("not-an-object")
        if event.get("schema") != SCHEMA:
            return self._reject("schema-mismatch")
        if event.get("source") != SOURCE:
            return self._reject("source-mismatch")
        state = str(event.get("state") or "")
        if state not in ALLOWED_STATES:
            return self._reject("unsupported-state")
        try:
            publisher_started_at = float(event["publisher_started_at"])
            sequence = int(event["sequence"])
            interaction_index = int(event["interaction_index"])
            sent_at = float(event["sent_at"])
        except (KeyError, TypeError, ValueError):
            return self._reject("invalid-ordering-fields")
        publisher_id = str(event.get("publisher_id") or "")
        event_id = str(event.get("event_id") or "")
        interaction_id = str(event.get("interaction_id") or "")
        if not publisher_id or not event_id or not interaction_id:
            return self._reject("missing-identity")
        if sequence <= 0 or interaction_index < 0:
            return self._reject("invalid-ordering-values")
        age = now_value - sent_at
        if age < -MAX_FUTURE_SKEW_SECONDS:
            return self._reject("future-event")
        if age > self.max_event_age_seconds and not (
            allow_stale_idle and state == "idle"
        ):
            return self._reject("stale-event")

        if self.publisher_id is None:
            self._activate_publisher(publisher_id, publisher_started_at)
        elif publisher_id != self.publisher_id:
            if publisher_started_at <= self.publisher_started_at:
                return self._reject("retired-publisher")
            self._activate_publisher(publisher_id, publisher_started_at)

        if sequence <= self.last_sequence:
            return self._reject("duplicate-or-out-of-order-sequence")
        if interaction_index < self.latest_interaction_index:
            return self._reject("retired-interaction")

        self.last_sequence = sequence
        self.latest_interaction_index = max(
            self.latest_interaction_index,
            interaction_index,
        )
        self.accepted += 1
        self.last_decision = "accepted"
        self.last_event = dict(event)
        return True, "accepted"

    def _activate_publisher(self, publisher_id: str, started_at: float) -> None:
        self.publisher_id = publisher_id
        self.publisher_started_at = started_at
        self.last_sequence = 0
        self.latest_interaction_index = 0

    def _reject(self, reason: str) -> tuple[bool, str]:
        self.rejected += 1
        self.last_decision = reason
        return False, reason

    def state(self) -> dict:
        event = self.last_event or {}
        return {
            "schema": SCHEMA,
            "publisher_id": self.publisher_id,
            "publisher_started_at": self.publisher_started_at or None,
            "last_sequence": self.last_sequence,
            "latest_interaction_index": self.latest_interaction_index,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "last_decision": self.last_decision,
            "last_event_id": event.get("event_id"),
            "last_interaction_id": event.get("interaction_id"),
            "last_state": event.get("state"),
            "last_stage": event.get("stage"),
            "last_sent_at": event.get("sent_at"),
        }
