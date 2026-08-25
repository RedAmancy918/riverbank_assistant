#!/usr/bin/env python3
"""Consume ordered Hermes voice events and apply them to the expression display."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from expression_events import (
    DEFAULT_SNAPSHOT_PATH,
    DEFAULT_SOCKET_PATH,
    ExpressionEventConsumer,
    atomic_write_json,
)


DISPLAY_SOCKET_PATH = Path("/run/riverbank-expression/control.sock")
EVENT_SOCKET_PATH = DEFAULT_SOCKET_PATH
SNAPSHOT_PATH = DEFAULT_SNAPSHOT_PATH
BRIDGE_STATE_PATH = Path("/run/riverbank-expression/voice-event-bridge.json")


def log(message: str) -> None:
    print(message, flush=True)


def send_display(state: str, ttl: float | None = None) -> tuple[bool, str | None]:
    payload = {"state": state, "ttl": ttl}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                str(DISPLAY_SOCKET_PATH),
            )
        finally:
            client.close()
        return True, None
    except OSError as exc:
        return False, str(exc)


class VoiceExpressionBridge:
    def __init__(self) -> None:
        self.consumer = ExpressionEventConsumer()
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.last_delivery_ok = False
        self.last_delivery_error: str | None = None
        self.last_received_at: float | None = None
        self.last_rejection_reason: str | None = None
        self.reconciled_from_snapshot = False
        self.delivery_attempts = 0
        self.delivery_recoveries = 0
        self.last_delivery_at: float | None = None
        self.last_retry_at: float | None = None

    def write_state(self) -> None:
        payload = {
            "ok": self.last_delivery_error is None,
            "service": "running",
            "architecture": "structured-event-v1",
            "legacy_journal_parser": False,
            "transport": "unix-datagram-with-atomic-snapshot",
            "event_socket_path": str(EVENT_SOCKET_PATH),
            "snapshot_path": str(SNAPSHOT_PATH),
            "display_socket_path": str(DISPLAY_SOCKET_PATH),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_received_at": self.last_received_at,
            "last_delivery_ok": self.last_delivery_ok,
            "last_delivery_error": self.last_delivery_error,
            "last_delivery_at": self.last_delivery_at,
            "last_retry_at": self.last_retry_at,
            "delivery_attempts": self.delivery_attempts,
            "delivery_recoveries": self.delivery_recoveries,
            "last_rejection_reason": self.last_rejection_reason,
            "reconciled_from_snapshot": self.reconciled_from_snapshot,
            "consumer": self.consumer.state(),
        }
        try:
            atomic_write_json(BRIDGE_STATE_PATH, payload)
        except OSError as exc:
            log(f"bridge state write failed: {exc}")

    def deliver(self, event: dict) -> None:
        previously_failed = self.last_delivery_error is not None
        self.delivery_attempts += 1
        delivered, error = send_display(event["state"], event.get("ttl"))
        self.last_delivery_ok = delivered
        self.last_delivery_error = error
        self.last_delivery_at = time.time()
        self.updated_at = self.last_delivery_at
        if delivered:
            if previously_failed:
                self.delivery_recoveries += 1
            log(
                "expression event accepted "
                f"publisher={event['publisher_id']} seq={event['sequence']} "
                f"interaction={event['interaction_index']} "
                f"stage={event.get('stage')} state={event['state']}"
            )
        else:
            log(f"expression delivery failed: {error}")

    def retry_pending_delivery(self) -> bool:
        """Replay the accepted snapshot after a renderer startup race."""
        if self.last_delivery_ok:
            return True
        self.last_retry_at = time.time()
        event = self.consumer.last_event
        if not isinstance(event, dict):
            delivered, error = send_display("idle")
            self.delivery_attempts += 1
            self.last_delivery_ok = delivered
            self.last_delivery_error = error
            self.last_delivery_at = self.last_retry_at
            self.updated_at = self.last_retry_at
        else:
            self.deliver(event)
        self.write_state()
        return self.last_delivery_ok

    def process(self, event: object, *, from_snapshot: bool = False) -> str:
        self.updated_at = time.time()
        if not from_snapshot:
            self.last_received_at = self.updated_at
        accepted, reason = self.consumer.validate(
            event,
            now=self.updated_at,
            allow_stale_idle=from_snapshot,
        )
        if not accepted:
            self.last_rejection_reason = reason
            if from_snapshot:
                self.last_delivery_ok, self.last_delivery_error = send_display("idle")
                log(f"snapshot rejected reason={reason}; display reconciled to idle")
            else:
                log(f"expression event rejected reason={reason}")
            self.write_state()
            return reason
        assert isinstance(event, dict)
        self.last_rejection_reason = None
        self.reconciled_from_snapshot = bool(from_snapshot)
        self.deliver(event)
        self.write_state()
        return "accepted"

    def reconcile_snapshot(self) -> None:
        try:
            event = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_delivery_ok, self.last_delivery_error = send_display("idle")
            self.last_rejection_reason = "snapshot-unavailable"
            self.updated_at = time.time()
            log(f"snapshot unavailable; display reconciled to idle: {exc}")
            self.write_state()
            return
        self.process(event, from_snapshot=True)


def main() -> int:
    bridge = VoiceExpressionBridge()
    try:
        EVENT_SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    event_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    event_socket.bind(str(EVENT_SOCKET_PATH))
    os.chmod(EVENT_SOCKET_PATH, 0o660)
    event_socket.settimeout(1.0)
    bridge.reconcile_snapshot()
    log(f"structured expression event bridge ready socket={EVENT_SOCKET_PATH}")
    try:
        while True:
            try:
                raw = event_socket.recv(65535)
            except socket.timeout:
                bridge.retry_pending_delivery()
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                bridge.consumer.rejected += 1
                bridge.consumer.last_decision = "invalid-json"
                bridge.last_rejection_reason = "invalid-json"
                bridge.updated_at = time.time()
                bridge.last_received_at = bridge.updated_at
                bridge.write_state()
                log(f"expression event rejected reason=invalid-json error={exc}")
                continue
            bridge.process(event)
    finally:
        event_socket.close()
        try:
            EVENT_SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
