#!/usr/bin/env python3
"""Translate hands-free Hermes journal events into display states."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


SOCKET_PATH = Path("/run/riverbank-expression/control.sock")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def send(state: str, ttl: float | None = None) -> None:
    payload = {"state": state, "ttl": ttl}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(json.dumps(payload).encode("utf-8"), str(SOCKET_PATH))
        client.close()
    except OSError:
        pass


def state_for_line(line: str) -> tuple[str, float | None] | None:
    clean = ANSI_RE.sub("", line).replace("\r", " ").strip()
    lowered = clean.lower()
    if not clean:
        return None
    if "wake word detected" in lowered or "● recording" in lowered or "voice recording started" in lowered:
        return "listening", None
    if "transcribing" in lowered or "voice recording stopped" in lowered:
        return "thinking", None
    if "tts cut" in lowered:
        return "listening", 8
    if "tts playback failed" in lowered or "traceback" in lowered or "failed to start wake word" in lowered:
        return "error", 8
    if "wake word listening" in lowered or "wake word stopped" in lowered:
        return "idle", None
    return None


def main() -> int:
    send("idle")
    while True:
        process = subprocess.Popen(
            [
                "/usr/bin/journalctl",
                "-f",
                "-n",
                "0",
                "-u",
                "hermes-voice.service",
                "-o",
                "cat",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            result = state_for_line(line)
            if result:
                send(*result)
        process.wait()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
