#!/usr/bin/env python3
"""Run at most two guarded Paper Radar catch-ups after the daily job."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from arxiv_access import ArxivAccess


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data/generated/latest-report.json"
JOBS = Path(os.environ.get("RIVERBANK_HERMES_CRON_JOBS", Path.home() / ".hermes/cron/jobs.json"))
JOB_NAME = os.environ.get("RIVERBANK_PAPER_RADAR_JOB_NAME", "具身智讯日报")
TIMEZONE = ZoneInfo("Asia/Shanghai")


def load_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def report_is_current_and_complete(report: dict, source: dict, today: str) -> bool:
    """Only a fully ready source consumes the remaining recovery windows."""
    return report.get("date") == today and source.get("state") == "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", default=str(Path.home() / ".local/bin/hermes"))
    parser.add_argument("--job-id", default=os.environ.get("RIVERBANK_PAPER_RADAR_JOB_ID", ""))
    args = parser.parse_args()
    local_now = datetime.now(TIMEZONE)
    today = local_now.date().isoformat()
    if local_now.time() < time(8, 20):
        print("paper radar catch-up window has not opened")
        return 0
    access = ArxivAccess()
    source = access.read_source_status()
    if report_is_current_and_complete(load_object(REPORT), source, today):
        print(f"paper radar already current: {today}")
        return 0
    if source.get("attempted_report_date") == today and int(source.get("automatic_attempts") or 0) >= 3:
        print("paper radar automatic catch-up limit reached")
        return 0
    state = access.status()
    if state.get("cooldown_active"):
        print(f"paper radar catch-up deferred until {state.get('cooldown_until')}")
        return 0

    jobs = load_object(JOBS).get("jobs", [])
    job = next(
        (
            item
            for item in jobs
            if isinstance(item, dict)
            and ((args.job_id and item.get("id") == args.job_id) or (not args.job_id and item.get("name") == JOB_NAME))
        ),
        None,
    )
    if not job or not job.get("enabled"):
        print("paper radar Hermes job is unavailable or disabled")
        return 1
    hermes = args.hermes if Path(args.hermes).is_file() else (shutil.which("hermes") or args.hermes)
    lock_path = access.root / "catchup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("paper radar catch-up is already running")
            return 0
        result = subprocess.run(
            [hermes, "cron", "run", str(job["id"])],
            stdin=subprocess.DEVNULL,
            timeout=45 * 60,
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
