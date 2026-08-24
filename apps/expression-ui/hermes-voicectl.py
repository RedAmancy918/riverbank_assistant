#!/usr/bin/env python3
"""Inspect or manually trigger the RiverBank Hermes voice input bridge."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path


SOCKET_PATH = Path("/run/hermes-voice-control/control.sock")
STATE_PATH = Path("/run/hermes-voice-control/state.json")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "status":
        try:
            print(STATE_PATH.read_text(encoding="utf-8"), end="")
            return 0
        except OSError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if command != "trigger":
        print("usage: hermes-voicectl [status|trigger]", file=sys.stderr)
        return 2
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(b'{"command":"trigger"}', str(SOCKET_PATH))
        client.close()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print('{"ok": true, "command": "trigger"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
