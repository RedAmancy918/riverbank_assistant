#!/usr/bin/env python3
"""Extensible local health monitor with Raspberry Pi desktop alerts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_TYPES = {
    "systemd_service",
    "http",
    "http_json",
    "tcp",
    "mount",
    "path",
    "fresh_path",
    "command",
}
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
DEFAULT_HOME = Path.home()


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def current_boot_id() -> str:
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    checks = config.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("config.checks must be a non-empty list")

    seen: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ValueError(f"checks[{index}] must be an object")
        check_id = check.get("id")
        check_type = check.get("type")
        if not isinstance(check_id, str) or not check_id:
            raise ValueError(f"checks[{index}].id must be a non-empty string")
        if check_id in seen:
            raise ValueError(f"duplicate check id: {check_id}")
        if check_type not in SUPPORTED_TYPES:
            raise ValueError(f"unsupported check type for {check_id}: {check_type}")
        if check_type == "command":
            command = check.get("command")
            if not isinstance(command, list) or not command or not all(
                isinstance(part, str) and part for part in command
            ):
                raise ValueError(f"{check_id}.command must be a non-empty string list")
        elif not isinstance(check.get("target"), str) or not check["target"]:
            raise ValueError(f"{check_id}.target must be a non-empty string")
        if check_type == "fresh_path":
            max_age = check.get("max_age_seconds")
            if not isinstance(max_age, (int, float)) or max_age <= 0:
                raise ValueError(f"{check_id}.max_age_seconds must be positive")
        check_interval = check.get("interval_seconds", 0)
        if not isinstance(check_interval, (int, float)) or check_interval < 0:
            raise ValueError(f"{check_id}.interval_seconds must be non-negative")
        seen.add(check_id)

    for key in (
        "interval_seconds",
        "startup_grace_seconds",
        "failure_threshold",
        "repeat_alert_seconds",
        "popup_timeout_seconds",
    ):
        value = config.get(key)
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"config.{key} must be a non-negative number")
    return config


def run_command(command: list[str], timeout: float) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr).strip()
    if result.returncode == 0:
        return True, output or "command succeeded"
    return False, output or f"exit code {result.returncode}"


def fetch_url(url: str, timeout: float) -> tuple[bool, str, bytes | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "riverbank-health-monitor/1.0"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(1024 * 1024)
            status = int(getattr(response, "status", 200))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc), None
    if 200 <= status < 400:
        return True, f"HTTP {status}", body
    return False, f"HTTP {status}", body


def nested_value(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(dotted_path)
    return current


def mounted_filesystem_for(target: str) -> Path | None:
    """Return the nearest mount containing an existing data path.

    Data is intentionally stored below the filesystem root (for example
    ``/mnt/nvme64/riverbank-user``), so checking only the final directory with
    ``ismount`` reports a false failure.  The root filesystem does not count as
    an external data mount.
    """
    path = Path(target)
    if not path.exists():
        return None
    for candidate in (path, *path.parents):
        if os.path.ismount(candidate):
            return None if candidate == Path("/") else candidate
    return None


def run_check(check: dict[str, Any]) -> tuple[bool, str]:
    check_type = check["type"]
    target = check.get("target", "")
    timeout = float(check.get("timeout_seconds", 5))

    if check_type == "systemd_service":
        ok, detail = run_command(
            ["/usr/bin/systemctl", "is-active", target], timeout
        )
        return ok and detail == "active", detail

    if check_type in {"http", "http_json"}:
        ok, detail, body = fetch_url(target, timeout)
        if not ok or check_type == "http":
            return ok, detail
        try:
            payload = json.loads((body or b"").decode("utf-8"))
            actual = nested_value(payload, check["json_path"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            return False, f"invalid JSON response: {exc}"
        expected = check.get("equals", True)
        return actual == expected, f"{check['json_path']}={actual!r}"

    if check_type == "tcp":
        host, separator, port_text = target.rpartition(":")
        if not separator or not host:
            return False, "target must use host:port"
        try:
            with socket.create_connection((host, int(port_text)), timeout=timeout):
                return True, f"connected to {target}"
        except (OSError, ValueError) as exc:
            return False, str(exc)

    if check_type == "mount":
        mountpoint = mounted_filesystem_for(target)
        if mountpoint is None:
            return False, "not on a dedicated mounted filesystem"
        return True, f"mounted via {mountpoint}"

    if check_type == "path":
        exists = Path(target).exists()
        return exists, "present" if exists else "missing"

    if check_type == "fresh_path":
        path = Path(target)
        if not path.exists():
            return False, "missing"
        age = max(0.0, time.time() - path.stat().st_mtime)
        max_age = float(check["max_age_seconds"])
        return age <= max_age, f"age={age:.1f}s max={max_age:.1f}s"

    if check_type == "command":
        return run_command(check["command"], timeout)

    return False, f"unhandled check type: {check_type}"


class HealthMonitor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.tracking: dict[str, dict[str, Any]] = {}
        self.startup_announced = False
        self.started_at = timestamp()
        self.boot_id = current_boot_id()
        self.cycle_count = 0
        self.status_file = Path(config["status_file"])
        self.check_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.last_logged_health: dict[str, bool] = {}

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_event.set()

    def desktop_environment(self) -> dict[str, str] | None:
        desktop = self.config.get("desktop", {})
        display = str(desktop.get("display", ":0"))
        display_number = display.lstrip(":").split(".", 1)[0]
        x_socket = Path(f"/tmp/.X11-unix/X{display_number}")
        xauthority = Path(
            str(desktop.get("xauthority", DEFAULT_HOME / ".Xauthority"))
        )
        runtime_dir = Path(
            str(desktop.get("runtime_dir", f"/run/user/{os.getuid()}"))
        )
        bus_socket = runtime_dir / "bus"
        zenity = Path(str(desktop.get("zenity", "/usr/bin/zenity")))
        if not (x_socket.exists() and xauthority.exists() and bus_socket.exists() and zenity.exists()):
            return None
        environment = os.environ.copy()
        environment.update(
            {
                "DISPLAY": display,
                "XAUTHORITY": str(xauthority),
                "XDG_RUNTIME_DIR": str(runtime_dir),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus_socket}",
                "HOME": str(desktop.get("home", DEFAULT_HOME)),
            }
        )
        return environment

    def popup(self, title: str, body: str, warning: bool) -> bool:
        environment = self.desktop_environment()
        if environment is None:
            log("desktop session is not ready; notification will be retried")
            return False
        timeout = int(self.config["popup_timeout_seconds"])
        zenity = str(self.config.get("desktop", {}).get("zenity", "/usr/bin/zenity"))
        mode = "--warning" if warning else "--info"
        command = [
            zenity,
            mode,
            f"--title={title}",
            f"--text={body}",
            "--width=560",
            f"--timeout={timeout}",
        ]
        try:
            result = subprocess.run(
                command,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"desktop notification failed: {exc}")
            return False
        displayed = result.returncode in {0, 5}
        log(
            f"desktop notification {'displayed' if displayed else 'failed'}: "
            f"{title} (exit={result.returncode})"
        )
        return displayed

    def collect(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for check in self.config["checks"]:
            if check.get("enabled", True) is False:
                continue
            now = time.monotonic()
            check_interval = float(check.get("interval_seconds", 0))
            cached = self.check_cache.get(check["id"])
            if cached and check_interval > 0 and now - cached[0] < check_interval:
                record = dict(cached[1])
                record["cached"] = True
                records.append(record)
                continue
            started = time.monotonic()
            healthy, detail = run_check(check)
            duration_ms = round((time.monotonic() - started) * 1000)
            record = {
                "id": check["id"],
                "name": check.get("name", check["id"]),
                "type": check["type"],
                "target": check.get("target", check.get("command")),
                "healthy": healthy,
                "detail": detail,
                "duration_ms": duration_ms,
                "checked_at": timestamp(),
                "cached": False,
            }
            self.check_cache[check["id"]] = (now, record)
            records.append(record)
        return records

    def update_tracking(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now = time.time()
        default_threshold = max(1, int(self.config["failure_threshold"]))
        repeat_seconds = float(self.config["repeat_alert_seconds"])
        alert_due: list[dict[str, Any]] = []
        recovery_due: list[dict[str, Any]] = []

        for record in records:
            check = next(item for item in self.config["checks"] if item["id"] == record["id"])
            threshold = max(1, int(check.get("failure_threshold", default_threshold)))
            state = self.tracking.setdefault(
                record["id"],
                {
                    "status": "unknown",
                    "consecutive_failures": 0,
                    "last_alert": 0.0,
                    "alerted": False,
                    "pending_recovery": False,
                },
            )
            if record["healthy"]:
                state["consecutive_failures"] = 0
                if state["status"] == "down" and state["alerted"]:
                    state["pending_recovery"] = True
                state["status"] = "up"
                if state["pending_recovery"]:
                    recovery_due.append(record)
                continue

            state["consecutive_failures"] += 1
            if state["consecutive_failures"] >= threshold:
                state["status"] = "down"
                if (
                    not state["alerted"]
                    or now - float(state["last_alert"]) >= repeat_seconds
                ):
                    alert_due.append(record)
        return alert_due, recovery_due

    def mark_alerted(self, records: list[dict[str, Any]]) -> None:
        now = time.time()
        for record in records:
            state = self.tracking[record["id"]]
            state["alerted"] = True
            state["last_alert"] = now

    def mark_recovered(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            state = self.tracking[record["id"]]
            state["alerted"] = False
            state["pending_recovery"] = False
            state["last_alert"] = 0.0

    def write_status(self, records: list[dict[str, Any]]) -> None:
        self.cycle_count += 1
        payload = {
            "version": 2,
            "boot_id": self.boot_id,
            "monitor_started_at": self.started_at,
            "cycle_count": self.cycle_count,
            "startup_complete": True,
            "updated_at": timestamp(),
            "healthy": all(record["healthy"] for record in records),
            "checks": records,
        }
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.status_file)

    def notify_events(
        self,
        records: list[dict[str, Any]],
        alert_due: list[dict[str, Any]],
        recovery_due: list[dict[str, Any]],
    ) -> None:
        if alert_due:
            lines = [f"• {record['name']}：{record['detail']}" for record in alert_due]
            title = "RiverBank 开机自检发现故障" if not self.startup_announced else "RiverBank 服务异常"
            body = "以下项目未通过健康检查：\n\n" + "\n".join(lines)
            if self.popup(title, body, warning=True):
                self.mark_alerted(alert_due)
                self.startup_announced = True

        if recovery_due:
            lines = [f"• {record['name']}" for record in recovery_due]
            body = "以下项目已经恢复：\n\n" + "\n".join(lines)
            if self.popup("RiverBank 服务已恢复", body, warning=False):
                self.mark_recovered(recovery_due)

        if (
            not self.startup_announced
            and not alert_due
            and all(record["healthy"] for record in records)
            and self.config.get("notify_startup_success", True)
        ):
            body = f"开机自检通过：{len(records)} 个检查项全部正常。"
            if self.popup("RiverBank 系统就绪", body, warning=False):
                self.startup_announced = True

    def run_once(self, notify: bool = False) -> bool:
        records = self.collect()
        self.write_status(records)
        for record in records:
            marker = "OK" if record["healthy"] else "FAIL"
            log(f"[{marker}] {record['name']}: {record['detail']}")
        if notify:
            failures = [record for record in records if not record["healthy"]]
            if failures:
                body = "以下项目未通过健康检查：\n\n" + "\n".join(
                    f"• {record['name']}：{record['detail']}" for record in failures
                )
                self.popup("RiverBank 自检测试：发现故障", body, warning=True)
            else:
                self.popup(
                    "RiverBank 自检测试：全部正常",
                    f"{len(records)} 个检查项全部通过。",
                    warning=False,
                )
        return all(record["healthy"] for record in records)

    def run(self) -> None:
        grace = float(self.config["startup_grace_seconds"])
        interval = max(1.0, float(self.config["interval_seconds"]))
        log(f"monitor started; startup grace={grace}s interval={interval}s")
        if self.stop_event.wait(grace):
            return
        while not self.stop_event.is_set():
            cycle_started = time.monotonic()
            records = self.collect()
            alert_due, recovery_due = self.update_tracking(records)
            self.write_status(records)
            self.notify_events(records, alert_due, recovery_due)
            for record in records:
                current = bool(record["healthy"])
                previous = self.last_logged_health.get(record["id"])
                if previous is not None and previous != current:
                    marker = "RECOVERED" if current else "FAILED"
                    log(f"[{marker}] {record['name']}: {record['detail']}")
                self.last_logged_health[record["id"]] = current
            remaining = max(0.0, interval - (time.monotonic() - cycle_started))
            self.stop_event.wait(remaining)
        log("monitor stopped")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/riverbank/health-monitor.json"),
    )
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument(
        "--test-notification",
        choices=("success", "failure"),
        help="show a desktop test popup and exit",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        config = load_config(arguments.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if arguments.validate_config:
        print(f"configuration valid: {len(config['checks'])} checks")
        return 0

    monitor = HealthMonitor(config)
    if arguments.test_notification:
        warning = arguments.test_notification == "failure"
        title = "RiverBank 故障弹窗测试" if warning else "RiverBank 正常弹窗测试"
        body = "这是健康守护器的测试通知；显示此窗口说明桌面弹窗通道正常。"
        return 0 if monitor.popup(title, body, warning) else 1
    if arguments.once:
        return 0 if monitor.run_once(notify=arguments.notify) else 1

    signal.signal(signal.SIGTERM, monitor.request_stop)
    signal.signal(signal.SIGINT, monitor.request_stop)
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
