#!/usr/bin/env python3
"""Short live transport check; the caller restores hermes-voice afterwards."""

from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path

from expression_events import ExpressionEventPublisher


EVENT_SOCKET = Path("/run/riverbank-expression/voice-events.sock")
SNAPSHOT = Path("/private/tmp/riverbank-expression-integration-latest.json")


def send_raw(event: dict) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(json.dumps(event).encode("utf-8"), str(EVENT_SOCKET))
    finally:
        client.close()


def main() -> int:
    publisher = ExpressionEventPublisher(EVENT_SOCKET, SNAPSHOT)
    publisher.begin_interaction("integration-old")
    old = publisher.publish("thinking", stage="integration-old-thinking")
    time.sleep(0.15)
    publisher.begin_interaction("integration-new")
    publisher.publish("listening", stage="integration-new-listening")
    time.sleep(0.15)
    delayed_old = dict(old)
    delayed_old.update(
        {
            "event_id": uuid.uuid4().hex,
            "sequence": 3,
            "sent_at": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "state": "idle",
            "stage": "integration-delayed-old-idle",
        }
    )
    send_raw(delayed_old)
    time.sleep(0.15)
    publisher.publish("idle", stage="integration-complete")
    print(publisher.publisher_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
