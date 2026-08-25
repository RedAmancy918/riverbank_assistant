#!/usr/bin/env python3
"""Thread-safe, expiring leases for on-demand RiverBank vision inference."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable


class VisionLeaseManager:
    def __init__(
        self,
        *,
        minimum_ttl_seconds: float = 0.5,
        maximum_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.minimum_ttl_seconds = max(0.1, float(minimum_ttl_seconds))
        self.maximum_ttl_seconds = max(
            self.minimum_ttl_seconds,
            float(maximum_ttl_seconds),
        )
        self.clock = clock
        self.lock = threading.Lock()
        self.leases: dict[str, dict] = {}

    def _ttl(self, value: object) -> float:
        try:
            ttl = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be numeric") from exc
        return max(self.minimum_ttl_seconds, min(ttl, self.maximum_ttl_seconds))

    def acquire(self, source: str, ttl_seconds: object = 30.0) -> dict:
        source_value = str(source or "unknown").strip()[:80] or "unknown"
        ttl = self._ttl(ttl_seconds)
        now = self.clock()
        lease_id = uuid.uuid4().hex
        lease = {
            "lease_id": lease_id,
            "source": source_value,
            "created_monotonic": now,
            "renewed_monotonic": now,
            "expires_monotonic": now + ttl,
            "ttl_seconds": ttl,
        }
        with self.lock:
            self._expire_locked(now)
            self.leases[lease_id] = lease
        return self._public(lease, now)

    def renew(self, lease_id: str, ttl_seconds: object = 30.0) -> dict | None:
        ttl = self._ttl(ttl_seconds)
        now = self.clock()
        with self.lock:
            self._expire_locked(now)
            lease = self.leases.get(str(lease_id))
            if lease is None:
                return None
            lease["renewed_monotonic"] = now
            lease["expires_monotonic"] = now + ttl
            lease["ttl_seconds"] = ttl
            return self._public(lease, now)

    def release(self, lease_id: str) -> bool:
        with self.lock:
            return self.leases.pop(str(lease_id), None) is not None

    def expire(self) -> list[dict]:
        now = self.clock()
        with self.lock:
            expired = self._expire_locked(now)
            return [self._public(lease, now) for lease in expired]

    def active(self) -> bool:
        now = self.clock()
        with self.lock:
            self._expire_locked(now)
            return bool(self.leases)

    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            self._expire_locked(now)
            leases = [
                self._public(lease, now)
                for lease in sorted(
                    self.leases.values(),
                    key=lambda item: item["expires_monotonic"],
                )
            ]
        return {
            "active": bool(leases),
            "lease_count": len(leases),
            "leases": leases,
            "next_expiry_seconds": (
                min(lease["remaining_seconds"] for lease in leases)
                if leases
                else None
            ),
        }

    def _expire_locked(self, now: float) -> list[dict]:
        expired_ids = [
            lease_id
            for lease_id, lease in self.leases.items()
            if lease["expires_monotonic"] <= now
        ]
        return [self.leases.pop(lease_id) for lease_id in expired_ids]

    @staticmethod
    def _public(lease: dict, now: float) -> dict:
        return {
            "lease_id": lease["lease_id"],
            "source": lease["source"],
            "ttl_seconds": round(float(lease["ttl_seconds"]), 3),
            "remaining_seconds": round(
                max(0.0, float(lease["expires_monotonic"]) - now),
                3,
            ),
        }
