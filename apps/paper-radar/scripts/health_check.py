#!/usr/bin/env python3
"""Validate daily report freshness, collection integrity, and Hermes cron state."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "generated" / "latest-report.json"
CRON_JOBS = Path(
    os.environ.get(
        "RIVERBANK_HERMES_CRON_JOBS",
        Path.home() / ".hermes/cron/jobs.json",
    )
)
TIMEZONE = ZoneInfo("Asia/Shanghai")
EXPECTED_BY = time(8, 45)
DAILY_JOB_ID = os.environ.get("RIVERBANK_PAPER_RADAR_JOB_ID", "")
DAILY_JOB_NAME = os.environ.get("RIVERBANK_PAPER_RADAR_JOB_NAME", "具身智讯日报")


def fail(message: str) -> int:
    print(message)
    return 1


def normalized_title(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def main() -> int:
    now = datetime.now(TIMEZONE)
    if not REPORT.is_file():
        return fail("latest report is missing")
    try:
        report = load_json(REPORT)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(f"latest report is invalid: {exc}")

    expected_dates = {now.date().isoformat()}
    if now.time() < EXPECTED_BY:
        expected_dates.add((now.date() - timedelta(days=1)).isoformat())
    report_date = str(report.get("date") or "")
    if report_date not in expected_dates:
        return fail(f"report is stale: date={report_date}, expected={sorted(expected_dates)}")

    required_arrays = ("papers", "potential_methods", "special_focus", "industry_updates")
    for key in required_arrays:
        if not isinstance(report.get(key), list):
            return fail(f"report field {key} is not an array")
    selected = report["papers"] + report["potential_methods"]
    if len(selected) > 10:
        return fail(f"selected paper cap exceeded: {len(selected)}")
    if len(report["special_focus"]) > 5:
        return fail(f"special-focus cap exceeded: {len(report['special_focus'])}")
    if len(report["industry_updates"]) > 9:
        return fail(f"industry cap exceeded: {len(report['industry_updates'])}")
    titles = [normalized_title(item.get("title")) for item in selected]
    if any(not title for title in titles) or len(titles) != len(set(titles)):
        return fail("selected papers contain an empty or duplicate title")
    for item in selected:
        score = item.get("relevance_score")
        if not isinstance(score, (int, float)) or score < 40:
            return fail(f"selected paper below threshold: {item.get('title')}")

    candidate_path = ROOT / "data" / "candidates" / f"{report_date}.json"
    try:
        candidates = load_json(candidate_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(f"candidate file is invalid: {exc}")
    items = candidates.get("candidates")
    if not isinstance(items, list) or len(items) > 30:
        return fail(f"candidate list is invalid or exceeds cap: {len(items or [])}")
    missing_summaries = [
        item.get("title")
        for item in items
        if not str(item.get("summary_zh") or "").strip()
    ]
    if missing_summaries:
        return fail(f"candidate summaries missing: {len(missing_summaries)}")

    try:
        jobs = load_json(CRON_JOBS).get("jobs", [])
        if DAILY_JOB_ID:
            job = next(item for item in jobs if item.get("id") == DAILY_JOB_ID)
        else:
            job = next(item for item in jobs if item.get("name") == DAILY_JOB_NAME)
    except (OSError, ValueError, json.JSONDecodeError, StopIteration) as exc:
        return fail(f"daily cron state unavailable: {exc}")
    if not job.get("enabled") or job.get("last_status") != "ok":
        return fail(
            f"daily cron unhealthy: enabled={job.get('enabled')} "
            f"status={job.get('last_status')}"
        )
    if now.time() >= EXPECTED_BY:
        last_run = str(job.get("last_run_at") or "")
        if not last_run.startswith(now.date().isoformat()):
            return fail(f"daily cron did not finish today: {last_run or 'never'}")

    print(
        f"daily report ok: date={report_date} selected={len(selected)} "
        f"candidates={len(items)} industry={len(report['industry_updates'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
