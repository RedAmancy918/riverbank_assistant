#!/usr/bin/env python3
"""Atomic one-shot special-focus queue shared by the web UI and daily job."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(
    os.environ.get(
        "PAPER_RADAR_SPECIAL_FOCUS_DIR",
        str(ROOT / "data" / "special-focus"),
    )
)
QUEUE_PATH = DATA_DIR / "requests.json"
LOCK_PATH = DATA_DIR / ".requests.lock"
MAX_DESCRIPTION_LENGTH = 800


def normalize_description(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    if len(normalized) < 2:
        raise ValueError("描述至少需要 2 个字符")
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"描述不能超过 {MAX_DESCRIPTION_LENGTH} 个字符")
    return normalized


@contextmanager
def queue_lock() -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_queue_unlocked() -> dict[str, Any]:
    if not QUEUE_PATH.exists():
        return {"version": 1, "requests": []}
    try:
        payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"特别关注队列无法读取：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("requests"), list):
        raise RuntimeError("特别关注队列格式无效")
    return payload


def write_queue_unlocked(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=DATA_DIR,
        prefix=".requests-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(QUEUE_PATH)


def public_request(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if request is None:
        return None
    return {
        key: request.get(key)
        for key in (
            "id",
            "description",
            "target_date",
            "created_at",
            "edited_at",
            "status",
            "consumed_at",
            "consumed_for",
            "result_count",
        )
        if request.get(key) is not None
    }


def submit_request(description: str, timezone_name: str) -> dict[str, Any]:
    description = normalize_description(description)
    now = datetime.now(ZoneInfo(timezone_name))
    target_date = (now.date() + timedelta(days=1)).isoformat()
    new_request = {
        "id": uuid.uuid4().hex[:16],
        "description": description,
        "target_date": target_date,
        "created_at": now.isoformat(timespec="seconds"),
        "status": "pending",
    }
    with queue_lock():
        payload = read_queue_unlocked()
        for request in payload["requests"]:
            if request.get("status") == "pending" and request.get("target_date") == target_date:
                request["status"] = "superseded"
                request["superseded_at"] = now.isoformat(timespec="seconds")
                request["superseded_by"] = new_request["id"]
        payload["requests"].append(new_request)
        payload["requests"] = payload["requests"][-100:]
        payload["updated_at"] = now.isoformat(timespec="seconds")
        write_queue_unlocked(payload)
    return public_request(new_request) or {}


def get_due_request(report_date: str) -> dict[str, Any] | None:
    date.fromisoformat(report_date)
    with queue_lock():
        payload = read_queue_unlocked()
        due = [
            request
            for request in payload["requests"]
            if request.get("status") == "pending"
            and str(request.get("target_date", "")) <= report_date
        ]
        if not due:
            return None
        selected = min(
            due,
            key=lambda request: (
                str(request.get("target_date", "")),
                str(request.get("created_at", "")),
            ),
        )
        return public_request(selected)


def queue_status(timezone_name: str) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(timezone_name))
    with queue_lock():
        payload = read_queue_unlocked()
        pending = sorted(
            (
                request
                for request in payload["requests"]
                if request.get("status") == "pending"
            ),
            key=lambda request: (
                str(request.get("target_date", "")),
                str(request.get("created_at", "")),
            ),
        )
        consumed = sorted(
            (
                request
                for request in payload["requests"]
                if request.get("status") == "consumed"
            ),
            key=lambda request: str(request.get("consumed_at", "")),
            reverse=True,
        )
    return {
        "pending": public_request(pending[0]) if pending else None,
        "pending_requests": [public_request(request) for request in pending],
        "pending_count": len(pending),
        "last_consumed": public_request(consumed[0]) if consumed else None,
        "tomorrow": (now.date() + timedelta(days=1)).isoformat(),
        "server_time": now.isoformat(timespec="seconds"),
    }


def cancel_request(request_id: str, timezone_name: str) -> bool:
    now = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    with queue_lock():
        payload = read_queue_unlocked()
        original_count = len(payload["requests"])
        payload["requests"] = [
            request
            for request in payload["requests"]
            if not (
                request.get("id") == request_id
                and request.get("status") == "pending"
            )
        ]
        changed = len(payload["requests"]) != original_count
        if changed:
            payload["updated_at"] = now
            write_queue_unlocked(payload)
    return changed


def update_request(
    request_id: str,
    description: str,
    timezone_name: str,
) -> dict[str, Any] | None:
    """Edit a pending prompt without changing its target date or identity."""
    description = normalize_description(description)
    now = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    updated: dict[str, Any] | None = None
    with queue_lock():
        payload = read_queue_unlocked()
        for request in payload["requests"]:
            if request.get("id") != request_id or request.get("status") != "pending":
                continue
            request["description"] = description
            request["edited_at"] = now
            payload["updated_at"] = now
            write_queue_unlocked(payload)
            updated = public_request(request)
            break
    return updated


def clear_unconsumed_requests(timezone_name: str) -> int:
    """Permanently remove every prompt that has not produced a report."""
    now = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    with queue_lock():
        payload = read_queue_unlocked()
        retained = [
            request
            for request in payload["requests"]
            if request.get("status") == "consumed"
        ]
        cleared_count = len(payload["requests"]) - len(retained)
        if cleared_count:
            payload["requests"] = retained
            payload["updated_at"] = now
            write_queue_unlocked(payload)
    return cleared_count


def mark_consumed(
    request_id: str,
    report_date: str,
    result_count: int,
    timezone_name: str,
) -> bool:
    date.fromisoformat(report_date)
    now = datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
    changed = False
    with queue_lock():
        payload = read_queue_unlocked()
        for request in payload["requests"]:
            if request.get("id") != request_id:
                continue
            if request.get("status") == "consumed":
                return True
            if request.get("status") != "pending":
                return False
            request["status"] = "consumed"
            request["consumed_at"] = now
            request["consumed_for"] = report_date
            request["result_count"] = max(0, int(result_count))
            payload["updated_at"] = now
            write_queue_unlocked(payload)
            changed = True
            break
    return changed
