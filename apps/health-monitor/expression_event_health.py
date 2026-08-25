#!/usr/bin/env python3
"""Health check for the RiverBank structured voice-expression event chain."""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path


VOICE_STATE_PATH = Path("/run/hermes-voice-control/state.json")
EVENT_SNAPSHOT_PATH = Path("/run/hermes-voice-control/expression-event.json")
BRIDGE_STATE_PATH = Path("/run/riverbank-expression/voice-event-bridge.json")
EVENT_SOCKET_PATH = Path("/run/riverbank-expression/voice-events.sock")
SCHEMA = "riverbank.expression.event/v1"


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload


def inspect() -> tuple[bool, dict]:
    if not EVENT_SOCKET_PATH.exists():
        return False, {"error": "event socket missing"}
    try:
        if not stat.S_ISSOCK(EVENT_SOCKET_PATH.stat().st_mode):
            return False, {"error": "event path is not a socket"}
        voice = read_json(VOICE_STATE_PATH)
        snapshot = read_json(EVENT_SNAPSHOT_PATH)
        bridge = read_json(BRIDGE_STATE_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, {"error": str(exc)}

    publisher = voice.get("expression_events")
    consumer = bridge.get("consumer")
    if not isinstance(publisher, dict) or not isinstance(consumer, dict):
        return False, {"error": "publisher or consumer diagnostics missing"}
    checks = {
        "voice_service_running": voice.get("service") == "running",
        "schema": (
            publisher.get("schema")
            == snapshot.get("schema")
            == SCHEMA
            == consumer.get("schema")
        ),
        "architecture": bridge.get("architecture") == "structured-event-v1",
        "legacy_parser_disabled": bridge.get("legacy_journal_parser") is False,
        "delivery_ok": bridge.get("last_delivery_ok") is True,
        "publisher_id": (
            publisher.get("publisher_id")
            and publisher.get("publisher_id")
            == snapshot.get("publisher_id")
            == consumer.get("publisher_id")
        ),
        "sequence": snapshot.get("sequence") == consumer.get("last_sequence"),
        "state": snapshot.get("state") == consumer.get("last_state"),
    }
    detail = {
        "ok": all(checks.values()),
        "checks": checks,
        "publisher_id": publisher.get("publisher_id"),
        "sequence": snapshot.get("sequence"),
        "interaction_index": snapshot.get("interaction_index"),
        "state": snapshot.get("state"),
        "stage": snapshot.get("stage"),
        "accepted": consumer.get("accepted"),
        "rejected": consumer.get("rejected"),
    }
    return bool(detail["ok"]), detail


def main() -> int:
    detail: dict = {}
    for attempt in range(4):
        ok, detail = inspect()
        if ok:
            print(json.dumps(detail, ensure_ascii=False))
            return 0
        if attempt < 3:
            time.sleep(0.08)
    print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
