#!/usr/bin/env python3
"""Regression checks for the Paper Radar offline circuit breaker."""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "apps/paper-radar/scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from collect import NetworkUnavailableError, fetch_feed  # noqa: E402


def main() -> None:
    attempts = 0

    def offline(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError("offline")

    with patch("urllib.request.urlopen", side_effect=offline), patch("time.sleep") as sleep:
        try:
            fetch_feed("cat:cs.RO", 1)
        except NetworkUnavailableError:
            pass
        else:
            raise AssertionError("offline fetch must raise NetworkUnavailableError")
    assert attempts == 4
    assert [call.args[0] for call in sleep.call_args_list] == [5, 15, 30]
    print("paper radar: offline retry checks passed")


if __name__ == "__main__":
    main()
