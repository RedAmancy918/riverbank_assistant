#!/usr/bin/env python3
"""Validate both idle and active states of the lease-controlled face tracker."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path


STATE_PATH = Path("/run/riverbank-face-tracker/state.json")
SOCKET_PATH = Path("/run/riverbank-face-tracker/control.sock")


def main() -> int:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        inference = state["inference"]
        age = max(0.0, time.time() - float(state["timestamp"]))
        if age > 5.0:
            raise ValueError(f"state is stale: {age:.1f}s")
        if not SOCKET_PATH.exists() or not stat_is_socket(SOCKET_PATH):
            raise ValueError("control socket is unavailable")
        if state.get("service") != "running" or not state.get("ok"):
            raise ValueError("service state is not healthy")
        active = bool(inference.get("active"))
        requested = bool(inference.get("requested"))
        lease_count = int(inference.get("lease_count", 0))
        transitioning = active != requested
        request_age = max(
            0.0,
            time.time() - float(inference.get("request_changed_at") or 0.0),
        )
        if transitioning and request_age > 8.0:
            raise ValueError(
                f"pipeline transition is stuck: requested={requested} active={active} "
                f"age={request_age:.1f}s"
            )
        if active:
            if not transitioning and (not requested or lease_count < 1):
                raise ValueError("pipeline is active without a valid lease")
            last_frame_at = float(state.get("last_frame_at") or 0.0)
            frame_age = max(0.0, time.time() - last_frame_at)
            if not transitioning and frame_age > 5.0:
                raise ValueError(f"active pipeline has stale frames: {frame_age:.1f}s")
        elif not transitioning and (lease_count != 0 or state.get("visible")):
            raise ValueError("idle pipeline retained a lease or visible face")
        result = {
            "ok": True,
            "mode": inference.get("mode"),
            "pipeline_state": inference.get("pipeline_state"),
            "lease_count": lease_count,
            "transitioning": transitioning,
            "state_age_seconds": round(age, 3),
            "frame": state.get("frame", 0),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def stat_is_socket(path: Path) -> bool:
    import stat

    return stat.S_ISSOCK(path.stat().st_mode)


if __name__ == "__main__":
    sys.exit(main())
