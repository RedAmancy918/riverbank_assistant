#!/usr/bin/env python3
"""Small unprivileged client for the allow-listed recovery controller."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


DEFAULT_SOCKET = Path("/run/riverbank-recovery/control.sock")


def request_recovery(
    action: str = "recover_all",
    *,
    socket_path: Path = DEFAULT_SOCKET,
    timeout: float = 2.0,
) -> dict[str, Any]:
    if action not in {"recover_all", "status"}:
        raise ValueError("unsupported recovery action")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(
            (json.dumps({"version": 1, "action": action}) + "\n").encode("utf-8")
        )
        response = client.recv(65535)
    finally:
        client.close()
    payload = json.loads(response.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid recovery response")
    return payload
