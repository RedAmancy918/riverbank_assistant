#!/usr/bin/env python3
"""Regression checks for the privileged recovery state machine."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from recovery_manager import RecoveryManager, active_execution, expected_report_date, matching_job


def completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, "ok\n", "")


def main() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    assert expected_report_date(
        datetime(2026, 8, 31, 9, 0, tzinfo=timezone), time(8, 45)
    ) == "2026-08-31"
    assert expected_report_date(
        datetime(2026, 8, 31, 7, 0, tzinfo=timezone), time(8, 45)
    ) == "2026-08-30"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        jobs = root / "jobs.json"
        jobs.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "paper-123",
                            "name": "具身智讯日报",
                            "enabled": True,
                            "state": "scheduled",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert matching_job(jobs, "具身智讯日报")["id"] == "paper-123"
        executions = root / "executions.db"
        connection = sqlite3.connect(executions)
        connection.execute(
            "CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, status TEXT, started_at TEXT, claimed_at TEXT)"
        )
        connection.execute(
            "INSERT INTO executions VALUES ('run-1', 'another-job', 42, 'running', 'now', 'now')"
        )
        connection.commit()
        connection.close()
        assert active_execution(executions, "paper-123") is None

        report = root / "report.json"
        report.write_text('{"date":"2000-01-01"}', encoding="utf-8")
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[:3] == ["/usr/bin/systemctl", "is-active", "--quiet"]:
                return completed(command, returncode=3)
            return completed(command)

        config = {
            "user": "geo",
            "home": str(root),
            "hermes_bin": "/home/geo/.local/bin/hermes",
            "paper_job_name": "具身智讯日报",
            "paper_report_path": str(report),
            "paper_jobs_path": str(jobs),
            "services": ["alpha.service", "expression-display.service"],
            "display_service": "expression-display.service",
            "settle_seconds": 0,
        }
        manager = RecoveryManager(
            config,
            socket_path=root / "control.sock",
            status_path=root / "status.json",
            lock_path=root / "recover.lock",
            runner=runner,
        )
        manager.recover()
        assert manager.state["state"] == "complete"
        assert manager.state["paper"]["action"] == "queued"
        restart_units = [
            command[-1]
            for command in calls
            if command[:3] == ["/usr/bin/systemctl", "restart", "--no-block"]
        ]
        assert restart_units == ["alpha.service", "expression-display.service"]
        assert any(command[-3:] == ["cron", "run", "paper-123"] for command in calls)
        persisted = json.loads((root / "status.json").read_text(encoding="utf-8"))
        assert persisted["state"] == "complete"
        assert "DEEPSEEK" not in json.dumps(persisted)
    print("recovery manager: regression checks passed")


if __name__ == "__main__":
    main()
