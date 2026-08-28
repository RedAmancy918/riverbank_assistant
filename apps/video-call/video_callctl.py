#!/usr/bin/env python3
"""Local control client for riverbank-video-call.service."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:19734"


def request(method: str, path: str) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "activate", "hangup"))
    args = parser.parse_args()
    if args.command == "status":
        payload = request("GET", "/api/v1/status")
    elif args.command == "activate":
        payload = request("POST", "/api/v1/activate")
    else:
        payload = request("POST", "/api/v1/hangup")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
