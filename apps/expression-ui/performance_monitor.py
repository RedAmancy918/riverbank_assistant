#!/usr/bin/env python3
"""Low-overhead live performance snapshots for the round display."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


HEALTH_STATUS_PATH = Path("/var/lib/riverbank-health-monitor/status.json")
NVME_PATH = Path("/mnt/nvme64")


@dataclass(frozen=True)
class PerformanceSnapshot:
    sampled_at: float = 0.0
    cpu_percent: float = 0.0
    cpu_temp_c: float = 0.0
    cpu_freq_mhz: int = 0
    load_1: float = 0.0
    memory_used_bytes: int = 0
    memory_total_bytes: int = 0
    memory_percent: float = 0.0
    disk_used_bytes: int = 0
    disk_total_bytes: int = 0
    disk_percent: float = 0.0
    network_rx_bps: float = 0.0
    network_tx_bps: float = 0.0
    hailo_ready: bool = False
    hailo_active: bool = False
    health_healthy_count: int = 0
    health_total_count: int = 0
    uptime_seconds: int = 0
    process_rss_bytes: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class PerformanceMonitor:
    """Collect deltas from procfs/sysfs without blocking the UI thread."""

    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_network: tuple[int, int, float] | None = None

    @staticmethod
    def _read_cpu_counters() -> tuple[int, int]:
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
            values = [int(value) for value in fields[1:]]
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return total, idle
        except (OSError, ValueError, IndexError):
            return 0, 0

    @staticmethod
    def _read_default_network_interface() -> str | None:
        try:
            for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return fields[0]
        except (OSError, ValueError, IndexError):
            pass
        return None

    @classmethod
    def _read_network_counters(cls) -> tuple[int, int]:
        preferred = cls._read_default_network_interface()
        counters: dict[str, tuple[int, int]] = {}
        try:
            for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
                name, raw_values = line.split(":", 1)
                interface = name.strip()
                values = raw_values.split()
                counters[interface] = (int(values[0]), int(values[8]))
        except (OSError, ValueError, IndexError):
            return 0, 0
        if preferred in counters:
            return counters[preferred]
        selected = [value for name, value in counters.items() if name != "lo"]
        return tuple(map(sum, zip(*selected))) if selected else (0, 0)

    @staticmethod
    def _read_memory() -> tuple[int, int, float]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return 0, 0, 0.0
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        percent = used / total * 100.0 if total else 0.0
        return used, total, percent

    @staticmethod
    def _read_disk() -> tuple[int, int, float]:
        try:
            stat = os.statvfs(NVME_PATH)
            total = stat.f_blocks * stat.f_frsize
            available = stat.f_bavail * stat.f_frsize
            used = max(0, total - available)
            percent = used / total * 100.0 if total else 0.0
            return used, total, percent
        except OSError:
            return 0, 0, 0.0

    @staticmethod
    def _read_number(path: Path, divisor: float = 1.0) -> float:
        try:
            return float(path.read_text(encoding="utf-8").strip()) / divisor
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _read_hailo_and_health() -> tuple[bool, bool, int, int]:
        ready = Path("/dev/hailo0").exists()
        active = False
        healthy = 0
        total = 0
        try:
            payload = json.loads(HEALTH_STATUS_PATH.read_text(encoding="utf-8"))
            checks = payload.get("checks", [])
            if not isinstance(checks, list):
                checks = []
            total = len(checks)
            healthy = sum(
                1
                for check in checks
                if isinstance(check, dict) and bool(check.get("healthy"))
            )
            by_id = {
                str(check.get("id")): check
                for check in checks
                if isinstance(check, dict)
            }
            if "hailo-device" in by_id:
                ready = bool(by_id["hailo-device"].get("healthy"))
            face_detail = by_id.get("face-tracker-state", {}).get("detail", "")
            if isinstance(face_detail, str) and face_detail.startswith("{"):
                face_state = json.loads(face_detail)
                active = (
                    str(face_state.get("pipeline_state", "")).lower()
                    not in {"", "paused", "stopped", "idle"}
                    or int(face_state.get("lease_count", 0) or 0) > 0
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return ready, active, healthy, total

    @staticmethod
    def _read_uptime() -> int:
        try:
            return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        except (OSError, ValueError, IndexError):
            return 0

    @staticmethod
    def _read_process_rss() -> int:
        try:
            pages = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return 0

    def collect(self) -> PerformanceSnapshot:
        now = time.monotonic()
        cpu_total, cpu_idle = self._read_cpu_counters()
        if self._previous_cpu is None:
            previous_total, previous_idle = cpu_total, cpu_idle
            time.sleep(0.08)
            cpu_total, cpu_idle = self._read_cpu_counters()
        else:
            previous_total, previous_idle = self._previous_cpu
        delta_total = max(0, cpu_total - previous_total)
        delta_idle = max(0, cpu_idle - previous_idle)
        cpu_percent = (
            max(0.0, min((1.0 - delta_idle / delta_total) * 100.0, 100.0))
            if delta_total
            else 0.0
        )
        self._previous_cpu = (cpu_total, cpu_idle)

        rx_bytes, tx_bytes = self._read_network_counters()
        if self._previous_network is None:
            rx_bps = tx_bps = 0.0
        else:
            old_rx, old_tx, old_at = self._previous_network
            elapsed = max(0.001, now - old_at)
            rx_bps = max(0.0, (rx_bytes - old_rx) / elapsed)
            tx_bps = max(0.0, (tx_bytes - old_tx) / elapsed)
        self._previous_network = (rx_bytes, tx_bytes, now)

        memory_used, memory_total, memory_percent = self._read_memory()
        disk_used, disk_total, disk_percent = self._read_disk()
        hailo_ready, hailo_active, health_healthy, health_total = self._read_hailo_and_health()
        try:
            load_1 = float(os.getloadavg()[0])
        except (OSError, AttributeError):
            load_1 = 0.0
        return PerformanceSnapshot(
            sampled_at=time.time(),
            cpu_percent=cpu_percent,
            cpu_temp_c=self._read_number(
                Path("/sys/class/thermal/thermal_zone0/temp"),
                1000.0,
            ),
            cpu_freq_mhz=round(
                self._read_number(
                    Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"),
                    1000.0,
                )
            ),
            load_1=load_1,
            memory_used_bytes=memory_used,
            memory_total_bytes=memory_total,
            memory_percent=memory_percent,
            disk_used_bytes=disk_used,
            disk_total_bytes=disk_total,
            disk_percent=disk_percent,
            network_rx_bps=rx_bps,
            network_tx_bps=tx_bps,
            hailo_ready=hailo_ready,
            hailo_active=hailo_active,
            health_healthy_count=health_healthy,
            health_total_count=health_total,
            uptime_seconds=self._read_uptime(),
            process_rss_bytes=self._read_process_rss(),
        )


def format_bytes(value: float, *, per_second: bool = False) -> str:
    amount = max(0.0, float(value))
    suffixes = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while amount >= 1024.0 and index < len(suffixes) - 1:
        amount /= 1024.0
        index += 1
    precision = 0 if amount >= 100 or index == 0 else 1
    suffix = suffixes[index] + ("/s" if per_second else "")
    return f"{amount:.{precision}f} {suffix}"
