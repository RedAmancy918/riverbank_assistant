#!/usr/bin/env python3
"""Privileged, allow-listed recovery controller for RiverBank services."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import math
from datetime import datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


API_VERSION = 1
DEFAULT_CONFIG = Path("/etc/riverbank/recovery.json")
DEFAULT_SOCKET = Path("/run/riverbank-recovery/control.sock")
DEFAULT_STATUS = Path("/var/lib/riverbank-recovery/status.json")
DEFAULT_LOCK = Path("/run/riverbank-recovery/recover.lock")
MAX_REQUEST_BYTES = 4096


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any], mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    required_strings = (
        "user",
        "home",
        "hermes_bin",
        "paper_job_name",
        "paper_report_path",
        "paper_jobs_path",
    )
    for key in required_strings:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"config.{key} must be a non-empty string")
    services = config.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("config.services must be a non-empty list")
    for unit in services:
        if (
            not isinstance(unit, str)
            or not unit.endswith(".service")
            or "/" in unit
            or ".." in unit
        ):
            raise ValueError(f"invalid allow-listed service: {unit!r}")
    config["services"] = list(dict.fromkeys(services))
    config.setdefault("timezone", "Asia/Shanghai")
    config.setdefault("report_expected_after", "08:45")
    config.setdefault("command_timeout_seconds", 25)
    config.setdefault("settle_seconds", 0.35)
    config.setdefault(
        "paper_executions_path",
        str(Path(config["paper_jobs_path"]).with_name("executions.db")),
    )
    return config


def expected_report_date(
    now: datetime,
    expected_after: wall_time,
) -> str:
    target = now.date() if now.time() >= expected_after else now.date() - timedelta(days=1)
    return target.isoformat()


def report_date(path: Path) -> str:
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    return str(payload.get("date") or "")


def matching_job(path: Path, name: str) -> dict[str, Any] | None:
    try:
        jobs = load_json(path).get("jobs", [])
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(jobs, list):
        return None
    normalized = name.casefold()
    return next(
        (
            job
            for job in jobs
            if isinstance(job, dict)
            and str(job.get("name") or "").casefold() == normalized
        ),
        None,
    )


def active_execution(path: Path, job_id: str) -> dict[str, Any] | None:
    """Read Hermes' durable execution ledger without mutating scheduler state."""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT id, pid, process_started_at, status, started_at
                FROM executions
                WHERE job_id = ? AND status IN ('claimed', 'running')
                ORDER BY claimed_at DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    record = dict(row)
    try:
        stat = Path(f"/proc/{int(record['pid'])}/stat").read_text(encoding="utf-8")
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        expected_ticks = int(record.get("process_started_at") or 0)
    except (OSError, TypeError, ValueError, IndexError):
        return None
    return record if expected_ticks > 0 and start_ticks == expected_ticks else None


def stale_claim_delay(
    job: dict[str, Any] | None,
    now: datetime,
    claim_ttl_seconds: int = 300,
) -> int:
    """Delay a retry until Hermes' own fire-claim lease has safely expired."""
    claim = (job or {}).get("fire_claim")
    if not isinstance(claim, dict):
        return 0
    try:
        claimed_at = datetime.fromisoformat(str(claim.get("at") or ""))
        age = (now - claimed_at.astimezone(now.tzinfo)).total_seconds()
    except (TypeError, ValueError):
        return 0
    if age < 0 or age >= claim_ttl_seconds:
        return 0
    return max(1, math.ceil(claim_ttl_seconds - age + 5))


class RecoveryManager:
    """Runs a single recovery transaction and exposes status over a Unix socket."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        socket_path: Path = DEFAULT_SOCKET,
        status_path: Path = DEFAULT_STATUS,
        lock_path: Path = DEFAULT_LOCK,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config = validate_config(dict(config))
        self.socket_path = socket_path
        self.status_path = status_path
        self.lock_path = lock_path
        self.runner = runner
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.guard = threading.Lock()
        self.state: dict[str, Any] = {
            "version": API_VERSION,
            "state": "idle",
            "message": "等待恢复",
            "started_at": None,
            "finished_at": None,
            "paper": {"action": "pending", "expected_date": None, "report_date": None},
            "services": [],
        }
        self.write_status()

    def write_status(self) -> None:
        payload = {**self.state, "updated_at": iso_now()}
        atomic_json(self.status_path, payload)

    def command(self, command: list[str], timeout: float | None = None) -> tuple[bool, str]:
        limit = float(timeout or self.config["command_timeout_seconds"])
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        detail = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, detail or f"exit={result.returncode}"

    def user_command(self, arguments: list[str]) -> tuple[bool, str]:
        user = self.config["user"]
        home = self.config["home"]
        return self.command(
            [
                "/usr/sbin/runuser",
                "-u",
                user,
                "--",
                "/usr/bin/env",
                f"HOME={home}",
                f"USER={user}",
                f"LOGNAME={user}",
                *arguments,
            ]
        )

    def detached_user_command(
        self,
        arguments: list[str],
        *,
        delay_seconds: int = 0,
    ) -> tuple[bool, str]:
        """Queue a long Hermes job in an independent, resource-bounded unit."""
        user = self.config["user"]
        home = self.config["home"]
        command = [
                "/usr/bin/systemd-run",
                "--unit=riverbank-paper-catchup",
                "--uid",
                user,
                "--gid",
                user,
                "--collect",
                "--no-block",
                "--property=Slice=riverbank-apps.slice",
                "--property=TimeoutStartSec=infinity",
                f"--setenv=HOME={home}",
                f"--setenv=USER={user}",
                f"--setenv=LOGNAME={user}",
                *arguments,
            ]
        if delay_seconds > 0:
            command.insert(2, f"--on-active={int(delay_seconds)}s")
        return self.command(
            command,
            timeout=10,
        )

    def trigger_paper_catchup(self) -> dict[str, Any]:
        timezone = ZoneInfo(str(self.config["timezone"]))
        now = datetime.now(timezone)
        hour, minute = (
            int(part) for part in str(self.config["report_expected_after"]).split(":", 1)
        )
        expected = expected_report_date(now, wall_time(hour, minute))
        current = report_date(Path(self.config["paper_report_path"]))
        result: dict[str, Any] = {
            "expected_date": expected,
            "report_date": current or None,
            "action": "current" if current == expected else "needed",
        }
        if current == expected:
            result["detail"] = "日报已经是最新日期"
            return result

        job = matching_job(
            Path(self.config["paper_jobs_path"]),
            self.config["paper_job_name"],
        )
        reference = str((job or {}).get("id") or self.config["paper_job_name"])
        execution = active_execution(
            Path(self.config["paper_executions_path"]),
            reference,
        )
        if execution or (job and str(job.get("state") or "").lower() == "running"):
            result.update(
                action="already_running",
                detail="日报任务正在运行",
                execution_id=(execution or {}).get("id"),
            )
            return result
        for suffix in ("service", "timer"):
            active, _detail = self.command(
                [
                    "/usr/bin/systemctl",
                    "is-active",
                    "--quiet",
                    f"riverbank-paper-catchup.{suffix}",
                ],
                timeout=3,
            )
            if active:
                result.update(action="already_running", detail="日报补跑已在队列中")
                return result
        if job and not bool(job.get("enabled", True)):
            ok, detail = self.user_command(
                [self.config["hermes_bin"], "cron", "resume", reference]
            )
            if not ok:
                result.update(action="failed", detail=f"恢复日报计划失败：{detail}")
                return result

        delay = stale_claim_delay(job, now)
        ok, detail = self.detached_user_command(
            [self.config["hermes_bin"], "cron", "run", reference],
            delay_seconds=delay,
        )
        result.update(
            action="queued" if ok else "failed",
            detail=(
                (f"已进入补跑队列，将在约 {delay} 秒后启动" if delay else "已进入补跑队列")
                if ok
                else f"触发日报失败：{detail}"
            ),
            job_id=reference if job else None,
        )
        return result

    def restart_services(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        display_unit = str(self.config.get("display_service", "expression-display.service"))
        ordered = [unit for unit in self.config["services"] if unit != display_unit]
        if display_unit in self.config["services"]:
            ordered.append(display_unit)
        for unit in ordered:
            ok, detail = self.command(
                ["/usr/bin/systemctl", "restart", "--no-block", unit]
            )
            records.append(
                {
                    "unit": unit,
                    "ok": ok,
                    "detail": detail[:240],
                }
            )
            self.state["services"] = records
            self.write_status()
            if float(self.config["settle_seconds"]) > 0:
                time.sleep(float(self.config["settle_seconds"]))
        return records

    def recover(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.state.update(state="busy", message="另一个恢复任务正在运行")
                self.write_status()
                return
            self.state = {
                "version": API_VERSION,
                "state": "running",
                "message": "正在恢复服务",
                "started_at": iso_now(),
                "finished_at": None,
                "paper": {"action": "checking"},
                "services": [],
            }
            self.write_status()
            try:
                services = self.restart_services()
                paper = self.trigger_paper_catchup()
                failed = [record for record in services if not record["ok"]]
                if paper.get("action") == "failed":
                    failed.append({"unit": "paper-radar-catchup", "ok": False})
                state = "failed" if failed else "complete"
                if paper.get("action") == "queued":
                    message = "服务已恢复，日报已进入补跑队列"
                elif paper.get("action") == "already_running":
                    message = "服务已恢复，日报正在补跑"
                elif state == "complete":
                    message = "服务已恢复，日报无需补跑"
                else:
                    message = f"恢复完成，但有 {len(failed)} 项失败"
                self.state.update(
                    state=state,
                    message=message,
                    finished_at=iso_now(),
                    paper=paper,
                    services=services,
                )
            except Exception as exc:
                self.state.update(
                    state="failed",
                    message=f"恢复异常：{type(exc).__name__}",
                    finished_at=iso_now(),
                    error=str(exc)[:500],
                )
            finally:
                self.write_status()

    def start_recovery(self) -> tuple[bool, dict[str, Any]]:
        with self.guard:
            if self.worker is not None and self.worker.is_alive():
                return False, {**self.state, "accepted": False}
            self.worker = threading.Thread(
                target=self.recover,
                name="riverbank-recovery-worker",
                daemon=True,
            )
            self.worker.start()
            return True, {**self.state, "accepted": True, "state": "running"}

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("version", API_VERSION) != API_VERSION:
            return {"ok": False, "error": "unsupported API version"}
        action = request.get("action")
        if action == "status":
            return {"ok": True, **self.state}
        if action == "recover_all":
            accepted, status = self.start_recovery()
            return {"ok": accepted, **status}
        return {"ok": False, "error": "unsupported action"}

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o660)
        server.listen(8)
        server.settimeout(1.0)
        try:
            while not self.stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(2.0)
                    try:
                        payload = connection.recv(MAX_REQUEST_BYTES)
                        request = json.loads(payload.decode("utf-8"))
                        if not isinstance(request, dict):
                            raise ValueError("request must be an object")
                        response = self.handle(request)
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        response = {"ok": False, "error": str(exc)}
                    connection.sendall(
                        (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                    )
        finally:
            server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--validate-config", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        config = validate_config(load_json(arguments.config))
        pwd.getpwnam(config["user"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"configuration error: {exc}")
        return 2
    if arguments.validate_config:
        print(f"configuration valid: {len(config['services'])} services")
        return 0
    manager = RecoveryManager(
        config,
        socket_path=arguments.socket,
        status_path=arguments.status,
        lock_path=arguments.lock,
    )
    signal.signal(signal.SIGTERM, manager.request_stop)
    signal.signal(signal.SIGINT, manager.request_stop)
    manager.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
