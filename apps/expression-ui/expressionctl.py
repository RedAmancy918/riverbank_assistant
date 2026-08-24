#!/usr/bin/env python3
"""Command-line control for the RiverBank expression display."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path


SOCKET_PATH = Path("/run/riverbank-expression/control.sock")
STATE_PATH = Path("/run/riverbank-expression/state.json")


def read_status() -> int:
    try:
        print(STATE_PATH.read_text(encoding="utf-8"), end="")
        return 0
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Set or inspect the small-screen expression")
    parser.add_argument("state", nargs="?", help="expression state, or 'status'")
    parser.add_argument("--ttl", type=float, help="seconds before returning to idle")
    parser.add_argument("--force", action="store_true", help="restart the current animation")
    args = parser.parse_args()

    if not args.state or args.state == "status":
        return read_status()
    payload = {"state": args.state, "ttl": args.ttl, "force": args.force}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(json.dumps(payload).encode("utf-8"), str(SOCKET_PATH))
        client.close()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
