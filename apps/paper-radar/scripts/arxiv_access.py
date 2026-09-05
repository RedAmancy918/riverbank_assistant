#!/usr/bin/env python3
"""Shared, persistent and rate-limit-aware access to arXiv.

Every Paper Radar process uses the same file lock and state file.  This keeps
metadata collection, HTML reading and on-demand PDF enrichment on one serial
connection even when they are started by different services.
"""

from __future__ import annotations

import email.utils
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = os.environ.get(
    "RIVERBANK_ARXIV_USER_AGENT",
    "RiverBank-Edge-PaperRadar/0.26.1 "
    "(personal research reader; https://github.com/RedAmancy918/riverbank_assistant)",
)
ALLOWED_HOSTS = frozenset({"arxiv.org", "export.arxiv.org", "oaipmh.arxiv.org"})
SAFE_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def default_state_root() -> Path:
    configured = os.environ.get("RIVERBANK_ARXIV_STATE_DIR", "").strip()
    if configured:
        return Path(configured)
    data_root = os.environ.get("RIVERBANK_DATA", "").strip()
    if data_root:
        return Path(data_root) / "paper-radar" / "data" / "arxiv-access"
    return ROOT / "data" / "arxiv-access"


class ArxivAccessError(RuntimeError):
    """Base error for the coordinated arXiv client."""


class ArxivCooldownError(ArxivAccessError):
    def __init__(self, retry_at: str, retry_after_seconds: int):
        super().__init__(f"arXiv cooling down until {retry_at}")
        self.retry_at = retry_at
        self.retry_after_seconds = retry_after_seconds


class ArxivRateLimitError(ArxivCooldownError):
    """The most recent request received HTTP 429."""


class ArxivNetworkError(ArxivAccessError):
    """The request could not reach arXiv or returned a non-429 HTTP error."""


@dataclass(frozen=True)
class ArxivPayload:
    body: bytes
    url: str
    headers: dict[str, str]
    status_code: int


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def iso_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat()


def parse_retry_after(value: str | None, now: float) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int(parsed.timestamp() - now))
    except (TypeError, ValueError, OverflowError):
        return None


class ArxivAccess:
    """Serialize requests and persist cooldown across processes and restarts."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        min_interval_seconds: float = 4.0,
        default_cooldown_seconds: int = 30 * 60,
        max_cooldown_seconds: int = 2 * 60 * 60,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.root = Path(root) if root is not None else default_state_root()
        self.lock_path = self.root / "access.lock"
        self.state_path = self.root / "state.json"
        self.source_status_path = self.root / "source-status.json"
        self.min_interval_seconds = max(3.0, float(min_interval_seconds))
        self.default_cooldown_seconds = max(60, int(default_cooldown_seconds))
        self.max_cooldown_seconds = max(
            self.default_cooldown_seconds,
            int(max_cooldown_seconds),
        )
        self.clock = clock
        self.sleeper = sleeper

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, {"version": 1, **state})

    def status(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._load_state()
        now = self.clock()
        cooldown_until = float(state.get("cooldown_until_epoch") or 0)
        return {
            **state,
            "cooldown_active": cooldown_until > now,
            "retry_after_seconds": max(0, int(cooldown_until - now)),
        }

    def fetch_bytes(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 60,
        max_bytes: int = 40 * 1024 * 1024,
        headers: dict[str, str] | None = None,
    ) -> ArxivPayload:
        requested_host = (urlparse(url).hostname or "").lower()
        if requested_host not in ALLOWED_HOSTS:
            raise ValueError(f"untrusted arXiv host: {requested_host or 'missing'}")
        client = session or requests.Session()
        owns_session = session is None
        self.root.mkdir(parents=True, exist_ok=True)
        response: requests.Response | None = None
        try:
            with self.lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                state = self._load_state()
                now = self.clock()
                cooldown_until = float(state.get("cooldown_until_epoch") or 0)
                if cooldown_until > now:
                    raise ArxivCooldownError(
                        iso_timestamp(cooldown_until),
                        max(1, int(cooldown_until - now)),
                    )

                last_request = float(state.get("last_request_epoch") or 0)
                wait_seconds = max(0.0, last_request + self.min_interval_seconds - now)
                if wait_seconds:
                    self.sleeper(wait_seconds)
                started = self.clock()
                state.update(
                    last_request_epoch=started,
                    last_request_at=iso_timestamp(started),
                    last_url_host=requested_host,
                    last_status="requesting",
                )
                self._save_state(state)

                request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
                if headers:
                    request_headers.update(headers)
                try:
                    response = client.get(
                        url,
                        timeout=timeout,
                        stream=True,
                        headers=request_headers,
                    )
                except requests.RequestException as exc:
                    state.update(
                        last_status="network_error",
                        last_error=str(exc)[:500],
                        last_finished_at=iso_timestamp(self.clock()),
                    )
                    self._save_state(state)
                    raise ArxivNetworkError(str(exc)) from exc

                if response.status_code == 429:
                    now = self.clock()
                    strikes = int(state.get("consecutive_rate_limits") or 0) + 1
                    retry_after = parse_retry_after(response.headers.get("Retry-After"), now)
                    if retry_after is None:
                        retry_after = min(
                            self.max_cooldown_seconds,
                            self.default_cooldown_seconds * (2 ** (strikes - 1)),
                        )
                    cooldown_until = now + max(1, retry_after)
                    state.update(
                        consecutive_rate_limits=strikes,
                        cooldown_until_epoch=cooldown_until,
                        cooldown_until=iso_timestamp(cooldown_until),
                        last_status="rate_limited",
                        last_http_status=429,
                        last_error="HTTP 429 Too Many Requests",
                        last_finished_at=iso_timestamp(now),
                    )
                    self._save_state(state)
                    raise ArxivRateLimitError(
                        iso_timestamp(cooldown_until),
                        max(1, retry_after),
                    )

                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    state.update(
                        last_status="http_error",
                        last_http_status=response.status_code,
                        last_error=str(exc)[:500],
                        last_finished_at=iso_timestamp(self.clock()),
                    )
                    self._save_state(state)
                    raise ArxivNetworkError(str(exc)) from exc

                final_host = (urlparse(response.url).hostname or "").lower()
                if final_host not in ALLOWED_HOSTS:
                    state.update(
                        last_status="rejected_redirect",
                        last_error=f"redirected to {final_host or 'missing host'}",
                        last_finished_at=iso_timestamp(self.clock()),
                    )
                    self._save_state(state)
                    raise ArxivAccessError("arXiv redirected to an untrusted host")

                announced = int(response.headers.get("Content-Length", "0") or 0)
                if announced > max_bytes:
                    raise ArxivAccessError(f"arXiv response exceeds {max_bytes} bytes")
                parts: list[bytes] = []
                consumed = 0
                for part in response.iter_content(chunk_size=128 * 1024):
                    if not part:
                        continue
                    consumed += len(part)
                    if consumed > max_bytes:
                        raise ArxivAccessError(f"arXiv response exceeds {max_bytes} bytes")
                    parts.append(part)
                finished = self.clock()
                state.update(
                    consecutive_rate_limits=0,
                    cooldown_until_epoch=0,
                    cooldown_until="",
                    last_status="ok",
                    last_http_status=response.status_code,
                    last_error="",
                    last_finished_at=iso_timestamp(finished),
                )
                self._save_state(state)
                return ArxivPayload(
                    body=b"".join(parts),
                    url=response.url,
                    headers={str(key): str(value) for key, value in response.headers.items()},
                    status_code=response.status_code,
                )
        finally:
            if response is not None:
                response.close()
            if owns_session:
                client.close()

    def cache_path(self, namespace: str, cache_date: str, key: str) -> Path:
        if not SAFE_NAMESPACE_RE.fullmatch(namespace):
            raise ValueError("invalid arXiv cache namespace")
        datetime.strptime(cache_date, "%Y-%m-%d")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / "daily-cache" / cache_date / namespace / f"{digest}.json"

    def read_daily_cache(
        self,
        namespace: str,
        cache_date: str,
        key: str,
    ) -> Any | None:
        path = self.cache_path(namespace, cache_date, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("key") != key:
            return None
        return payload.get("value")

    def write_daily_cache(
        self,
        namespace: str,
        cache_date: str,
        key: str,
        value: Any,
    ) -> None:
        atomic_write_json(
            self.cache_path(namespace, cache_date, key),
            {
                "version": 1,
                "date": cache_date,
                "key": key,
                "stored_at": iso_timestamp(self.clock()),
                "value": value,
            },
        )

    def prune_daily_cache(self, keep_dates: set[str]) -> None:
        cache_root = self.root / "daily-cache"
        if not cache_root.is_dir():
            return
        for child in cache_root.iterdir():
            if not child.is_dir() or child.name in keep_dates:
                continue
            try:
                datetime.strptime(child.name, "%Y-%m-%d")
            except ValueError:
                continue
            for path in sorted(child.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            child.rmdir()

    def write_source_status(self, payload: dict[str, Any]) -> None:
        safe = {
            "version": 1,
            "updated_at": iso_timestamp(self.clock()),
            **payload,
        }
        atomic_write_json(self.source_status_path, safe)

    def read_source_status(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.source_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "state": "unknown", "message": "论文源状态尚未建立"}
        return payload if isinstance(payload, dict) else {"version": 1, "state": "unknown"}


def cache_dates_around(value: date, days: int = 2) -> set[str]:
    from datetime import timedelta

    return {(value - timedelta(days=offset)).isoformat() for offset in range(max(1, days))}
