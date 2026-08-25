#!/usr/bin/env python3
"""Regression tests for the RiverBank structured expression event protocol."""

from __future__ import annotations

import json
import socket
import tempfile
import time
import uuid
from pathlib import Path

import hermes_expression_bridge as bridge_module
from expression_events import ExpressionEventConsumer, ExpressionEventPublisher


def changed(event: dict, **values: object) -> dict:
    result = dict(event)
    result.update(values)
    result["event_id"] = uuid.uuid4().hex
    return result


def receive(server: socket.socket) -> dict:
    return json.loads(server.recv(65535).decode("utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="riverbank-expression-test-") as tmp:
        root = Path(tmp)
        event_socket_path = root / "events.sock"
        snapshot_path = root / "latest.json"
        event_server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        event_server.bind(str(event_socket_path))
        event_server.settimeout(0.25)

        publisher = ExpressionEventPublisher(event_socket_path, snapshot_path)
        interaction_id, interaction_index = publisher.begin_interaction("test")
        first = publisher.publish("listening", stage="recording")
        received = receive(event_server)
        assert received == first
        assert json.loads(snapshot_path.read_text(encoding="utf-8")) == first
        assert first["interaction_id"] == interaction_id
        assert first["interaction_index"] == interaction_index == 1

        consumer = ExpressionEventConsumer()
        assert consumer.validate(first) == (True, "accepted")
        assert consumer.validate(first) == (
            False,
            "duplicate-or-out-of-order-sequence",
        )

        second = changed(first, sequence=2, state="thinking", stage="transcribing")
        assert consumer.validate(second) == (True, "accepted")
        newer_interaction = changed(
            second,
            sequence=3,
            interaction_index=2,
            interaction_id=uuid.uuid4().hex,
            state="listening",
            stage="recording",
        )
        assert consumer.validate(newer_interaction) == (True, "accepted")
        late_old_interaction = changed(
            second,
            sequence=4,
            interaction_index=1,
            state="idle",
            stage="completed",
        )
        assert consumer.validate(late_old_interaction) == (
            False,
            "retired-interaction",
        )
        stale = changed(
            newer_interaction,
            sequence=5,
            sent_at=time.time() - 60,
        )
        assert consumer.validate(stale) == (False, "stale-event")

        replacement = changed(
            newer_interaction,
            publisher_id="replacement-publisher",
            publisher_started_at=first["publisher_started_at"] + 1,
            sequence=1,
            interaction_index=0,
            interaction_id="system",
            state="idle",
            stage="service_started",
            sent_at=time.time(),
        )
        assert consumer.validate(replacement) == (True, "accepted")
        old_publisher_returns = changed(
            newer_interaction,
            sequence=6,
            sent_at=time.time(),
        )
        assert consumer.validate(old_publisher_returns) == (
            False,
            "retired-publisher",
        )

        display_socket_path = root / "display.sock"
        display_server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        display_server.bind(str(display_socket_path))
        display_server.settimeout(0.15)
        bridge_module.DISPLAY_SOCKET_PATH = display_socket_path
        bridge_module.BRIDGE_STATE_PATH = root / "bridge-state.json"
        bridge = bridge_module.VoiceExpressionBridge()
        assert bridge.process(first) == "accepted"
        assert receive(display_server)["state"] == "listening"
        assert bridge.process(first) == "duplicate-or-out-of-order-sequence"
        try:
            receive(display_server)
        except socket.timeout:
            pass
        else:
            raise AssertionError("duplicate event reached the display")

        stale_snapshot = changed(first, sequence=2, sent_at=time.time() - 60)
        assert bridge.process(stale_snapshot, from_snapshot=True) == "stale-event"
        assert receive(display_server)["state"] == "idle"
        state = json.loads(
            bridge_module.BRIDGE_STATE_PATH.read_text(encoding="utf-8")
        )
        assert state["architecture"] == "structured-event-v1"
        assert state["legacy_journal_parser"] is False
        assert state["consumer"]["rejected"] >= 2

        late_display_path = root / "late-display.sock"
        bridge_module.DISPLAY_SOCKET_PATH = late_display_path
        recovering_bridge = bridge_module.VoiceExpressionBridge()
        assert recovering_bridge.process(first, from_snapshot=True) == "accepted"
        assert recovering_bridge.last_delivery_ok is False
        late_display = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        late_display.bind(str(late_display_path))
        late_display.settimeout(0.15)
        assert recovering_bridge.retry_pending_delivery() is True
        assert receive(late_display)["state"] == "listening"
        assert recovering_bridge.delivery_attempts == 2
        assert recovering_bridge.delivery_recoveries == 1
        late_display.close()

        display_server.close()
        event_server.close()
    print("structured expression events: all regression checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
