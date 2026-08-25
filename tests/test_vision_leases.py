#!/usr/bin/env python3
"""Regression tests for expiring Hailo inference leases."""

from __future__ import annotations

from vision_leases import VisionLeaseManager


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def main() -> int:
    clock = Clock()
    manager = VisionLeaseManager(
        minimum_ttl_seconds=0.5,
        maximum_ttl_seconds=10.0,
        clock=clock,
    )
    assert manager.snapshot()["active"] is False
    first = manager.acquire("voice-vision", 3)
    assert first["remaining_seconds"] == 3.0
    assert manager.snapshot()["lease_count"] == 1

    clock.value += 2
    renewed = manager.renew(first["lease_id"], 5)
    assert renewed is not None and renewed["remaining_seconds"] == 5.0
    second = manager.acquire("gimbal", 20)
    assert second["ttl_seconds"] == 10.0
    assert manager.snapshot()["lease_count"] == 2

    assert manager.release("missing") is False
    assert manager.release(first["lease_id"]) is True
    assert manager.snapshot()["lease_count"] == 1
    clock.value += 11
    expired = manager.expire()
    assert [item["lease_id"] for item in expired] == [second["lease_id"]]
    assert manager.snapshot()["active"] is False
    assert manager.renew(second["lease_id"], 1) is None

    try:
        manager.acquire("invalid", "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid TTL was accepted")
    print("vision lease manager: all regression checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
