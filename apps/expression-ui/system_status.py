#!/usr/bin/env python3
"""Read-only platform status snapshots for the RiverBank round display."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


RIVERBANK_HOME = Path(os.environ.get("RIVERBANK_HOME", Path.home()))
DAILY_HOME = Path(
    os.environ.get("RIVERBANK_DAILY_HOME", RIVERBANK_HOME / ".hermes/profiles/daily")
)
DAILY_STATE_DB = Path(
    os.environ.get("RIVERBANK_DAILY_STATE_DB", DAILY_HOME / "state.db")
)
VOICE_STATE_PATH = Path("/run/hermes-voice-control/state.json")
ACTIVE_VISION_LEASE_DIR = Path("/dev/shm/riverbank-active-vision")
HEALTH_STATUS_PATH = Path("/var/lib/riverbank-health-monitor/status.json")
VERSION_PATH = Path(os.environ.get("RIVERBANK_VERSION_FILE", "/etc/riverbank/VERSION"))
LEGACY_VERSION_PATH = Path(__file__).resolve().parent / "VERSION"
DEFAULT_APP_VERSION = "v0.0.0 beta"
APP_VERSION_RE = re.compile(
    r"^v?(?P<number>\d+\.\d+\.\d+)(?:\s+(?P<channel>beta|stable))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SystemStatusSnapshot:
    wifi_quality: int
    wifi_enabled: bool
    tokens_today: int
    camera_active: bool
    camera_active_sources: tuple[str, ...]
    bluetooth_connected: bool
    volume_percent: int
    wifi_ssid: str
    wifi_ipv4: str
    bluetooth_powered: bool
    bluetooth_devices: tuple[str, ...]
    hostname: str
    os_name: str
    kernel_version: str
    uptime_seconds: int
    health_healthy_count: int
    health_total_count: int
    app_version: str
    health_checks: tuple[tuple[str, str, bool, str], ...]


class SystemStatus:
    """Low-cost status model whose blocking reads can run off the UI thread."""

    def __init__(self) -> None:
        self.last_update = 0.0
        self.wifi_quality = 0
        self.wifi_enabled = False
        self.tokens_today = 0
        self.camera_active = False
        self.camera_active_sources: tuple[str, ...] = ()
        self.bluetooth_connected = False
        self.volume_percent = 0
        self.wifi_ssid = ""
        self.wifi_ipv4 = ""
        self.bluetooth_powered = False
        self.bluetooth_devices: tuple[str, ...] = ()
        self.hostname = socket.gethostname()
        self.os_name = "Linux"
        self.kernel_version = ""
        self.uptime_seconds = 0
        self.health_healthy_count = 0
        self.health_total_count = 0
        self.app_version = DEFAULT_APP_VERSION
        self.health_checks: tuple[tuple[str, str, bool, str], ...] = ()

    @staticmethod
    def read_wifi_quality() -> int:
        try:
            for line in Path("/proc/net/wireless").read_text(encoding="utf-8").splitlines():
                if "wlan0:" not in line:
                    continue
                quality = float(line.split()[2].rstrip("."))
                return max(0, min(round(quality / 70.0 * 100), 100))
        except (OSError, ValueError, IndexError):
            pass
        return 0

    @staticmethod
    def read_wifi_enabled() -> bool:
        try:
            result = subprocess.run(
                ["/usr/bin/nmcli", "radio", "wifi"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.7,
                check=False,
            )
            return result.returncode == 0 and result.stdout.strip() == "enabled"
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def read_wifi_details() -> tuple[str, str]:
        ssid = ""
        for executable in ("/usr/sbin/iwgetid", "/usr/bin/iwgetid"):
            if not Path(executable).is_file():
                continue
            try:
                result = subprocess.run(
                    [executable, "wlan0", "--raw"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=0.7,
                    check=False,
                )
                if result.returncode == 0:
                    ssid = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
            break

        ipv4 = ""
        for executable in ("/usr/sbin/ip", "/usr/bin/ip"):
            if not Path(executable).is_file():
                continue
            try:
                result = subprocess.run(
                    [executable, "-4", "-o", "addr", "show", "dev", "wlan0"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=0.7,
                    check=False,
                )
                match = re.search(r"\binet\s+([0-9.]+)(?:/\d+)?", result.stdout)
                if result.returncode == 0 and match:
                    ipv4 = match.group(1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            break
        return ssid, ipv4

    @staticmethod
    def read_tokens_today() -> int:
        if not DAILY_STATE_DB.is_file():
            return 0
        local = time.localtime()
        midnight = time.mktime(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                0,
                0,
                0,
                local.tm_wday,
                local.tm_yday,
                local.tm_isdst,
            )
        )
        try:
            connection = sqlite3.connect(
                f"file:{DAILY_STATE_DB}?mode=ro",
                uri=True,
                timeout=0.15,
            )
            try:
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                    FROM sessions
                    WHERE COALESCE(last_activity_at, started_at) >= ?
                    """,
                    (midnight,),
                ).fetchone()
            finally:
                connection.close()
            return int(row[0] or 0) + int(row[1] or 0)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return 0

    @staticmethod
    def read_camera_activity(now_wall: float) -> tuple[bool, tuple[str, ...]]:
        sources: list[str] = []
        try:
            payload = json.loads(VOICE_STATE_PATH.read_text(encoding="utf-8"))
            if bool(payload.get("visual_request_active")):
                sources.append("qwen_visual_request")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

        try:
            leases = tuple(ACTIVE_VISION_LEASE_DIR.glob("*.json"))
        except OSError:
            leases = ()
        for path in leases:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not bool(payload.get("active", True)):
                    continue
                expires_at = float(payload.get("expires_at", 0.0))
                updated_at = float(payload.get("updated_at", 0.0))
                ttl_seconds = max(
                    0.2,
                    min(float(payload.get("ttl_seconds", 3.0)), 300.0),
                )
                if expires_at > now_wall or (
                    not expires_at and updated_at and now_wall - updated_at <= ttl_seconds
                ):
                    sources.append(str(payload.get("source") or path.stem))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        unique_sources = tuple(dict.fromkeys(sources))
        return bool(unique_sources), unique_sources

    @staticmethod
    def read_bluetooth_details() -> tuple[bool, tuple[str, ...]]:
        powered = False
        devices: tuple[str, ...] = ()
        try:
            show = subprocess.run(
                ["/usr/bin/bluetoothctl", "show"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.7,
                check=False,
            )
            powered = show.returncode == 0 and bool(
                re.search(r"^\s*Powered:\s+yes\s*$", show.stdout, re.MULTILINE)
            )
            connected = subprocess.run(
                ["/usr/bin/bluetoothctl", "devices", "Connected"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.7,
                check=False,
            )
            if connected.returncode == 0:
                names = []
                for line in connected.stdout.splitlines():
                    parts = line.strip().split(maxsplit=2)
                    if len(parts) >= 3 and parts[0] == "Device":
                        names.append(parts[2])
                devices = tuple(names)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return powered, devices

    @classmethod
    def read_bluetooth_connected(cls) -> bool:
        _powered, devices = cls.read_bluetooth_details()
        return bool(devices)

    @staticmethod
    def read_platform_details() -> tuple[str, str, str, int]:
        hostname = socket.gethostname()
        os_name = "Linux"
        kernel_version = ""
        uptime_seconds = 0
        try:
            for raw_line in Path("/etc/os-release").read_text(
                encoding="utf-8"
            ).splitlines():
                if raw_line.startswith("PRETTY_NAME="):
                    os_name = raw_line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        try:
            kernel_version = Path("/proc/sys/kernel/osrelease").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            pass
        try:
            uptime_seconds = int(
                float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            )
        except (OSError, ValueError, IndexError):
            pass
        return hostname, os_name, kernel_version, uptime_seconds

    @staticmethod
    def read_health_checks() -> tuple[tuple[str, str, bool, str], ...]:
        try:
            payload = json.loads(HEALTH_STATUS_PATH.read_text(encoding="utf-8"))
            checks = payload.get("checks")
            if not isinstance(checks, list):
                return ()
            records: list[tuple[str, str, bool, str]] = []
            for record in checks:
                if not isinstance(record, dict):
                    continue
                check_id = str(record.get("id") or "unknown")
                name = str(record.get("name") or check_id)
                healthy = bool(record.get("healthy"))
                detail = record.get("detail", "")
                if not isinstance(detail, str):
                    detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                # Healthy rows only need their state. Preserve a concise reason for
                # failed rows so the round-screen detail page stays informative
                # without copying large service payloads into every UI state write.
                detail = "" if healthy else detail.strip().replace("\n", " ")[:240]
                records.append((check_id, name, healthy, detail))
            return tuple(records)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return ()

    @classmethod
    def read_health_summary(cls) -> tuple[int, int]:
        checks = cls.read_health_checks()
        return sum(1 for _check_id, _name, healthy, _detail in checks if healthy), len(checks)

    @staticmethod
    def read_app_version() -> str:
        for path in (VERSION_PATH, LEGACY_VERSION_PATH):
            try:
                raw_version = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            match = APP_VERSION_RE.fullmatch(raw_version)
            if match is not None:
                channel = (match.group("channel") or "beta").lower()
                return f"v{match.group('number')} {channel}"
        return DEFAULT_APP_VERSION

    @staticmethod
    def read_volume_percent() -> int:
        environment = os.environ.copy()
        environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            result = subprocess.run(
                ["/usr/bin/wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.7,
                check=False,
                env=environment,
            )
            match = re.search(r"Volume:\s*([0-9.]+)", result.stdout)
            if result.returncode == 0 and match:
                return max(0, min(round(float(match.group(1)) * 100), 100))
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            pass
        return 0

    def update(self, now_monotonic: float, force: bool = False) -> bool:
        if not force and now_monotonic - self.last_update < 2.0:
            return False
        return self.apply_snapshot(self.collect_snapshot(), now_monotonic)

    def snapshot(self) -> SystemStatusSnapshot:
        return SystemStatusSnapshot(
            wifi_quality=self.wifi_quality,
            wifi_enabled=self.wifi_enabled,
            tokens_today=self.tokens_today,
            camera_active=self.camera_active,
            camera_active_sources=self.camera_active_sources,
            bluetooth_connected=self.bluetooth_connected,
            volume_percent=self.volume_percent,
            wifi_ssid=self.wifi_ssid,
            wifi_ipv4=self.wifi_ipv4,
            bluetooth_powered=self.bluetooth_powered,
            bluetooth_devices=self.bluetooth_devices,
            hostname=self.hostname,
            os_name=self.os_name,
            kernel_version=self.kernel_version,
            uptime_seconds=self.uptime_seconds,
            health_healthy_count=self.health_healthy_count,
            health_total_count=self.health_total_count,
            app_version=self.app_version,
            health_checks=self.health_checks,
        )

    def snapshot_values(self) -> tuple:
        snapshot = self.snapshot()
        return tuple(getattr(snapshot, field) for field in snapshot.__dataclass_fields__)

    @classmethod
    def collect_snapshot(cls) -> SystemStatusSnapshot:
        camera_active, camera_sources = cls.read_camera_activity(time.time())
        wifi_quality = cls.read_wifi_quality()
        wifi_ssid, wifi_ipv4 = cls.read_wifi_details()
        bluetooth_powered, bluetooth_devices = cls.read_bluetooth_details()
        hostname, os_name, kernel_version, uptime_seconds = cls.read_platform_details()
        health_checks = cls.read_health_checks()
        health_total_count = len(health_checks)
        health_healthy_count = sum(
            1 for _check_id, _name, healthy, _detail in health_checks if healthy
        )
        return SystemStatusSnapshot(
            wifi_quality=wifi_quality,
            wifi_enabled=cls.read_wifi_enabled(),
            tokens_today=cls.read_tokens_today(),
            camera_active=camera_active,
            camera_active_sources=camera_sources,
            bluetooth_connected=bool(bluetooth_devices),
            volume_percent=cls.read_volume_percent(),
            wifi_ssid=wifi_ssid,
            wifi_ipv4=wifi_ipv4,
            bluetooth_powered=bluetooth_powered,
            bluetooth_devices=bluetooth_devices,
            hostname=hostname,
            os_name=os_name,
            kernel_version=kernel_version,
            uptime_seconds=uptime_seconds,
            health_healthy_count=health_healthy_count,
            health_total_count=health_total_count,
            app_version=cls.read_app_version(),
            health_checks=health_checks,
        )

    def apply_snapshot(
        self,
        snapshot: SystemStatusSnapshot | tuple,
        now_monotonic: float,
    ) -> bool:
        before = self.snapshot()
        if not isinstance(snapshot, SystemStatusSnapshot):
            snapshot = SystemStatusSnapshot(*snapshot)
        for field in snapshot.__dataclass_fields__:
            setattr(self, field, getattr(snapshot, field))
        self.last_update = now_monotonic
        return before != snapshot

    def as_dict(self) -> dict:
        snapshot = self.snapshot()
        result: dict = {}
        for field in snapshot.__dataclass_fields__:
            value = getattr(snapshot, field)
            if field == "health_checks":
                result[field] = [
                    {
                        "id": check_id,
                        "name": name,
                        "healthy": healthy,
                        "detail": detail,
                    }
                    for check_id, name, healthy, detail in value
                ]
            else:
                result[field] = list(value) if isinstance(value, tuple) else value
        return result
