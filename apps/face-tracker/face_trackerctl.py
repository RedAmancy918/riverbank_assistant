#!/usr/bin/env python3
"""Control lease-based RiverBank Hailo face tracking."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path


SOCKET_PATH = Path("/run/riverbank-face-tracker/control.sock")


def request(payload: dict) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3.0)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65535)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    return json.loads(b"".join(chunks).decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage on-demand face-tracker leases")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status")
    acquire = subparsers.add_parser("acquire")
    acquire.add_argument("--source", default="manual")
    acquire.add_argument("--ttl", type=float, default=30.0)
    renew = subparsers.add_parser("renew")
    renew.add_argument("lease_id")
    renew.add_argument("--ttl", type=float, default=30.0)
    release = subparsers.add_parser("release")
    release.add_argument("lease_id")
    args = parser.parse_args()
    command = args.command or "status"
    payload: dict = {"command": command}
    if command == "acquire":
        payload.update({"source": args.source, "ttl_seconds": args.ttl})
    elif command == "renew":
        payload.update({"lease_id": args.lease_id, "ttl_seconds": args.ttl})
    elif command == "release":
        payload.update({"lease_id": args.lease_id})
    try:
        response = request(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        response = {"ok": False, "error": str(exc)}
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
