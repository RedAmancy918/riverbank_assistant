#!/usr/bin/env python3
"""Verify that the local ViewTurbo HTTP proxy can reach the public internet."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


PROXY = os.environ.get("RIVERBANK_HTTP_PROXY", "http://127.0.0.1:15732")
TARGET = os.environ.get("RIVERBANK_PROXY_TEST_URL", "https://export.arxiv.org/")


def main() -> int:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    request = urllib.request.Request(
        TARGET,
        method="HEAD",
        headers={"User-Agent": "riverbank-health-monitor/1.1"},
    )
    try:
        with opener.open(request, timeout=12) as response:
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"proxy egress failed: {exc}")
        return 1
    if status < 500:
        print(f"proxy egress ok: HTTP {status}")
        return 0
    print(f"proxy egress failed: HTTP {status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
