#!/usr/bin/env python3
"""Persistent, double-buffered expression renderer for the RiverBank DSI display."""

from __future__ import annotations

import io
import json
import math
import os
import re
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from collections import Counter, OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageSequence


APP_DIR = Path(__file__).resolve().parent
RIVERBANK_HOME = Path(os.environ.get("RIVERBANK_HOME", Path.home()))
RIVERBANK_DATA = Path(
    os.environ.get("RIVERBANK_DATA", RIVERBANK_HOME / ".local/share/riverbank")
)
DAILY_HOME = Path(
    os.environ.get(
        "RIVERBANK_DAILY_HOME",
        RIVERBANK_HOME / ".hermes/profiles/daily",
    )
)
CONFIG_PATH = Path(os.environ.get("RIVERBANK_EXPRESSION_CONFIG", APP_DIR / "expressions.json"))
DEFAULT_UI_THEME_PATH = APP_DIR / "ui-theme.json"
if not DEFAULT_UI_THEME_PATH.is_file() and (APP_DIR.parent / "ui-theme.json").is_file():
    DEFAULT_UI_THEME_PATH = APP_DIR.parent / "ui-theme.json"
UI_THEME_PATH = Path(
    os.environ.get("RIVERBANK_UI_THEME", DEFAULT_UI_THEME_PATH)
)
RUNTIME_DIR = Path(os.environ.get("RIVERBANK_EXPRESSION_RUNTIME", "/run/riverbank-expression"))
SOCKET_PATH = RUNTIME_DIR / "control.sock"
STATE_PATH = RUNTIME_DIR / "state.json"
REBOOT_REQUEST_PATH = RUNTIME_DIR / "reboot.request"
MENU_EVENT_PATH = RUNTIME_DIR / "menu-selection.json"
VOICE_STATE_PATH = Path("/run/hermes-voice-control/state.json")
ACTIVE_VISION_LEASE_DIR = Path("/dev/shm/riverbank-active-vision")
HEALTH_STATUS_PATH = Path("/var/lib/riverbank-health-monitor/status.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
# The service RuntimeDirectory is removed whenever this one service restarts.
# /dev/shm survives service restarts but is cleared by an actual OS reboot.
BOOT_ANIMATION_MARKER_PATH = Path(
    "/dev/shm/riverbank-expression-boot-animation-boot-id"
)
VERSION_PATH = Path(
    os.environ.get("RIVERBANK_VERSION_FILE", APP_DIR / "VERSION")
)
DAILY_STATE_DB = Path(os.environ.get("RIVERBANK_DAILY_STATE_DB", DAILY_HOME / "state.db"))
DAILY_ENV_PATH = Path(os.environ.get("RIVERBANK_DAILY_ENV", DAILY_HOME / ".env"))
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
VOICE_SOCKET_PATH = Path("/run/hermes-voice-control/control.sock")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
WINDOW_TITLE = "RiverBank Expression"
DEFAULT_DISPLAY_REFRESH_HZ = 60.0
MAX_DISPLAY_REFRESH_HZ = 120.0
UI_AA_SCALE = 4
DEFAULT_PRELOAD = ("idle", "listening", "thinking", "happy")
STATUS_ITEM_ANGLES = (
    # Left capsule: measurable status. Right capsule: compact controls.
    ("wifi", -158),
    ("token", -140),
    ("volume", -122),
    ("bluetooth", -78),
    ("screensaver", -60),
    ("restart", -42),
    ("camera", -24),
)
BOOT_CHECK_GROUPS = (
    (
        "system",
        ("expression-display", "hermes-expression-bridge", "reboot-request-path"),
        True,
    ),
    ("storage", ("nvme-storage",), True),
    ("display", ("dsi-touch-hardware",), True),
    (
        "network",
        ("tailscale", "viewturbo-service", "vpn-proxy-port", "vpn-proxy-egress"),
        False,
    ),
    ("camera", ("camera-hub-service", "camera-stream"), False),
    (
        "hailo",
        ("hailo-device", "face-tracker-service", "face-tracker-state"),
        False,
    ),
    ("audio", ("listengo-mic-service", "listengo-mic-hardware"), False),
    (
        "hermes",
        ("hermes-gateway", "hermes-voice", "hermes-voice-caption"),
        True,
    ),
    (
        "research",
        ("paper-radar-service", "paper-radar-page", "paper-radar-daily-integrity"),
        False,
    ),
)
DEFAULT_RADIAL_MENU = (
    {"id": "voice", "label": "对话", "glyph": "声", "action": {"type": "voice_trigger"}},
    {
        "id": "vision",
        "label": "相机",
        "glyph": "眼",
        "action": {"type": "camera_voice"},
    },
    {
        "id": "daily",
        "label": "日报",
        "glyph": "报",
        "action": {
            "type": "voice_query",
            "text": "请用口语简短告诉我今天的具身智讯日报里最值得关注的内容。",
        },
    },
    {
        "id": "settings",
        "label": "设置",
        "glyph": "设",
        "action": {"type": "open_settings"},
    },
    {
        "id": "happy",
        "label": "开心",
        "glyph": "笑",
        "action": {"type": "expression", "state": "happy", "ttl": 5},
    },
    {
        "id": "sleep",
        "label": "休眠",
        "glyph": "眠",
        "action": {"type": "expression", "state": "sleep", "ttl": 30},
    },
)


def log(message: str) -> None:
    print(message, flush=True)


def current_boot_id() -> str:
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def claim_boot_animation(boot_id: str, enabled: bool) -> bool:
    """Return True only for the first renderer start in this OS boot."""
    if not enabled:
        return False
    try:
        if BOOT_ANIMATION_MARKER_PATH.read_text(encoding="utf-8").strip() == boot_id:
            return False
    except OSError:
        pass
    try:
        BOOT_ANIMATION_MARKER_PATH.write_text(boot_id + "\n", encoding="utf-8")
    except OSError as exc:
        # If the volatile marker cannot be written, still show the animation for
        # this start rather than hiding the boot self-check completely.
        log(f"boot animation marker warning: {exc}")
    return True


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("expressions"), dict) or not data["expressions"]:
        raise ValueError("expressions.json must contain a non-empty expressions map")
    return data


def load_ui_theme() -> dict:
    try:
        with UI_THEME_PATH.open("r", encoding="utf-8") as handle:
            theme = json.load(handle)
        if isinstance(theme, dict):
            return theme
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def fetch_camera_frame(url: str, target_size: tuple[int, int]) -> bytes:
    """Fetch, decode and resize one camera-hub frame off the UI thread."""
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=0.8) as response:
        payload = response.read(4 * 1024 * 1024 + 1)
    if not payload or len(payload) > 4 * 1024 * 1024:
        raise ValueError("camera snapshot is empty or too large")
    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGB")
    source_width, source_height = image.size
    target_width, target_height = target_size
    target_ratio = target_width / target_height
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_width = round(source_height * target_ratio)
        left = max(0, (source_width - crop_width) // 2)
        image = image.crop((left, 0, left + crop_width, source_height))
    elif source_ratio < target_ratio:
        crop_height = round(source_width / target_ratio)
        top = max(0, (source_height - crop_height) // 2)
        image = image.crop((0, top, source_width, top + crop_height))
    if image.size != target_size:
        image = image.resize(target_size, Image.Resampling.BILINEAR)
    return image.tobytes()


def capture_camera_photo(url: str, output_path: Path) -> str:
    """Fetch and atomically save a full-resolution camera-hub JPEG."""
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with urllib.request.urlopen(request, timeout=2.0) as response:
        payload = response.read(8 * 1024 * 1024 + 1)
    if not payload or len(payload) > 8 * 1024 * 1024:
        raise ValueError("camera snapshot is empty or too large")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".part")
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert("RGB")
            image.save(temporary, format="JPEG", quality=94, optimize=True)
        os.replace(temporary, output_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return str(output_path)


def compact_token_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def request_system_reboot() -> tuple[bool, str]:
    """Hand a one-shot reboot request to the privileged systemd path unit."""
    temporary = REBOOT_REQUEST_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "requested_at": time.time(),
                    "renderer_pid": os.getpid(),
                    "source": "dsi-restart-slider",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, REBOOT_REQUEST_PATH)
        return True, str(REBOOT_REQUEST_PATH)
    except OSError as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        return False, str(exc)


class SystemStatus:
    """Low-cost, read-only status snapshot for the DSI overlay."""

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
        self.app_version = "0.0.0"

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
            (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst)
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

        # Future active consumers (for example pan/tilt face alignment) can
        # publish an expiring JSON lease here. Merely keeping camera-hub or the
        # Hailo face tracker alive does not create a lease and does not light the
        # privacy indicator.
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
                ttl_seconds = max(0.2, min(float(payload.get("ttl_seconds", 3.0)), 300.0))
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
                float(
                    Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
                )
            )
        except (OSError, ValueError, IndexError):
            pass
        return hostname, os_name, kernel_version, uptime_seconds

    @staticmethod
    def read_health_summary() -> tuple[int, int]:
        try:
            payload = json.loads(HEALTH_STATUS_PATH.read_text(encoding="utf-8"))
            checks = payload.get("checks")
            if not isinstance(checks, list):
                return 0, 0
            total = len(checks)
            healthy = sum(
                1
                for record in checks
                if isinstance(record, dict) and bool(record.get("healthy"))
            )
            return healthy, total
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0, 0

    @staticmethod
    def read_app_version() -> str:
        try:
            version = VERSION_PATH.read_text(encoding="utf-8").strip()
            return version or "0.0.0"
        except OSError:
            return "0.0.0"

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

    def snapshot_values(self) -> tuple:
        return (
            self.wifi_quality,
            self.wifi_enabled,
            self.tokens_today,
            self.camera_active,
            self.camera_active_sources,
            self.bluetooth_connected,
            self.volume_percent,
            self.wifi_ssid,
            self.wifi_ipv4,
            self.bluetooth_powered,
            self.bluetooth_devices,
            self.hostname,
            self.os_name,
            self.kernel_version,
            self.uptime_seconds,
            self.health_healthy_count,
            self.health_total_count,
            self.app_version,
        )

    @classmethod
    def collect_snapshot(cls) -> tuple:
        camera_active, camera_sources = cls.read_camera_activity(time.time())
        wifi_quality = cls.read_wifi_quality()
        wifi_ssid, wifi_ipv4 = cls.read_wifi_details()
        bluetooth_powered, bluetooth_devices = cls.read_bluetooth_details()
        hostname, os_name, kernel_version, uptime_seconds = (
            cls.read_platform_details()
        )
        health_healthy_count, health_total_count = cls.read_health_summary()
        return (
            wifi_quality,
            cls.read_wifi_enabled(),
            cls.read_tokens_today(),
            camera_active,
            camera_sources,
            bool(bluetooth_devices),
            cls.read_volume_percent(),
            wifi_ssid,
            wifi_ipv4,
            bluetooth_powered,
            bluetooth_devices,
            hostname,
            os_name,
            kernel_version,
            uptime_seconds,
            health_healthy_count,
            health_total_count,
            cls.read_app_version(),
        )

    def apply_snapshot(self, snapshot: tuple, now_monotonic: float) -> bool:
        before = self.snapshot_values()
        (
            self.wifi_quality,
            self.wifi_enabled,
            self.tokens_today,
            self.camera_active,
            self.camera_active_sources,
            self.bluetooth_connected,
            self.volume_percent,
            self.wifi_ssid,
            self.wifi_ipv4,
            self.bluetooth_powered,
            self.bluetooth_devices,
            self.hostname,
            self.os_name,
            self.kernel_version,
            self.uptime_seconds,
            self.health_healthy_count,
            self.health_total_count,
            self.app_version,
        ) = snapshot
        self.last_update = now_monotonic
        return before != snapshot

    def as_dict(self) -> dict:
        return {
            "wifi_quality": self.wifi_quality,
            "wifi_enabled": self.wifi_enabled,
            "tokens_today": self.tokens_today,
            "camera_active": self.camera_active,
            "camera_active_sources": list(self.camera_active_sources),
            "bluetooth_connected": self.bluetooth_connected,
            "volume_percent": self.volume_percent,
            "wifi_ssid": self.wifi_ssid,
            "wifi_ipv4": self.wifi_ipv4,
            "bluetooth_powered": self.bluetooth_powered,
            "bluetooth_devices": list(self.bluetooth_devices),
            "hostname": self.hostname,
            "os_name": self.os_name,
            "kernel_version": self.kernel_version,
            "uptime_seconds": self.uptime_seconds,
            "health_healthy_count": self.health_healthy_count,
            "health_total_count": self.health_total_count,
            "app_version": self.app_version,
        }


def read_profile_env_value(name: str) -> str:
    """Read one profile secret without importing or logging the complete env file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        for raw_line in DAILY_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, raw_value = line.partition("=")
            if separator and key.strip() == name:
                return raw_value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def fetch_deepseek_balance() -> dict:
    """Fetch a small, sanitized balance snapshot from DeepSeek's official API."""
    api_key = read_profile_env_value("DEEPSEEK_API_KEY")
    if not api_key:
        return {"ok": False, "error": "未配置 DeepSeek 密钥"}
    request = urllib.request.Request(
        DEEPSEEK_BALANCE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "RiverBank-Status/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        balances = payload.get("balance_infos") or []
        preferred = next(
            (item for item in balances if item.get("currency") == "CNY"),
            balances[0] if balances else None,
        )
        if not isinstance(preferred, dict):
            return {"ok": False, "error": "余额数据为空"}
        return {
            "ok": True,
            "available": bool(payload.get("is_available")),
            "currency": str(preferred.get("currency") or "CNY"),
            "total_balance": str(preferred.get("total_balance") or "0.00"),
            "checked_at": time.time(),
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"远端返回 {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "error": "远端暂不可用"}


def display_geometry(output_name: str) -> tuple[int, int, int, int, float] | None:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    try:
        result = subprocess.run(
            ["/usr/bin/wlr-randr"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    current_output = None
    position = None
    size = None
    for raw_line in ANSI_RE.sub("", result.stdout).splitlines():
        if raw_line and not raw_line.startswith(" "):
            current_output = raw_line.split()[0]
            position = None
            size = None
            continue
        if current_output != output_name:
            continue
        pos_match = re.search(r"Position:\s*(-?\d+),(-?\d+)", raw_line)
        if pos_match:
            position = (int(pos_match.group(1)), int(pos_match.group(2)))
        mode_match = re.search(
            r"(\d+)x(\d+)\s+px,\s*([0-9.]+)\s+Hz.*\(.*current.*\)",
            raw_line,
        )
        if mode_match:
            size = (int(mode_match.group(1)), int(mode_match.group(2)))
            refresh_hz = float(mode_match.group(3))
        if position and size:
            return position[0], position[1], size[0], size[1], refresh_hz
    return None


def app_path(value: object) -> Path:
    """Resolve relative configuration paths from the expression UI directory."""
    path = Path(os.path.expandvars(str(value))).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def dominant_edge_color(frame: Image.Image) -> tuple[int, int, int]:
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    band = max(4, min(width, height) // 40)
    boxes = (
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, band, band, height - band),
        (width - band, band, width, height - band),
    )
    colors: Counter[tuple[int, int, int]] = Counter()
    for box in boxes:
        for red, green, blue, alpha in rgba.crop(box).getdata():
            if alpha >= 128:
                colors[(red, green, blue)] += 1
    return colors.most_common(1)[0][0] if colors else (0, 0, 0)


def black_chroma_matte(
    image: Image.Image,
    background: tuple[int, int, int],
    soft_distance: int,
) -> Image.Image:
    """Replace a uniform matte with black and decontaminate antialiased edges."""
    rgb = image.convert("RGB")
    matte = Image.new("RGB", rgb.size, background)
    red_delta, green_delta, blue_delta = ImageChops.difference(rgb, matte).split()
    distance = ImageChops.lighter(ImageChops.lighter(red_delta, green_delta), blue_delta)
    threshold = max(4, min(int(soft_distance), 96))
    alpha_lut = [
        0 if value <= 2 else 255 if value >= threshold else round(255 * value / threshold)
        for value in range(256)
    ]
    alpha = distance.point(alpha_lut)
    channels = []
    for channel, matte_value in zip(rgb.split(), background):
        residual_lut = [round((255 - value) * matte_value / 255) for value in range(256)]
        channels.append(ImageChops.subtract(channel, alpha.point(residual_lut)))
    return Image.merge("RGB", channels)


def apply_background_mode(
    image: Image.Image,
    background: tuple[int, int, int],
    mode: str,
    soft_distance: int,
) -> tuple[Image.Image, tuple[int, int, int]]:
    if mode == "original":
        return image.convert("RGB"), background
    if mode == "black_chroma":
        return black_chroma_matte(image, background, soft_distance), (0, 0, 0)
    raise ValueError(f"unsupported background_mode: {mode}")


def fitted_size(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    source_width, source_height = source
    target_width, target_height = target
    scale = min(target_width / source_width, target_height / source_height)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))


def render_viewport(
    image: Image.Image,
    target_size: tuple[int, int],
    background: tuple[int, int, int],
    display_scale: float,
) -> Image.Image:
    """Pre-render the exact centered/cropped viewport shown on the DSI panel."""
    rendered_size = (
        max(1, round(image.size[0] * display_scale)),
        max(1, round(image.size[1] * display_scale)),
    )
    if image.size != rendered_size:
        image = image.resize(rendered_size, Image.Resampling.LANCZOS)
    viewport = Image.new("RGB", target_size, background)
    left = (target_size[0] - rendered_size[0]) // 2
    top = (target_size[1] - rendered_size[1]) // 2
    viewport.paste(image, (left, top))
    return viewport


@dataclass
class RawAnimation:
    state: str
    size: tuple[int, int]
    frames: list[bytes]
    durations: list[float]
    background: tuple[int, int, int]


@dataclass
class Animation:
    state: str
    frames: list[object]
    durations: list[float]
    background: tuple[int, int, int]
    memory_bytes: int


def decode_animation(
    state: str,
    path: Path,
    target_size: tuple[int, int],
    background_mode: str,
    chroma_soft_distance: int,
    display_scale: float,
    animation_fps: float,
) -> RawAnimation:
    source_frames: list[Image.Image] = []
    source_times: list[float] = []
    source_durations: list[float] = []
    source_time = 0.0
    sample_interval = 1.0 / animation_fps
    with Image.open(path) as source:
        default_duration = max(0.001, float(source.info.get("duration", 100)) / 1000.0)
        background = dominant_edge_color(source.copy())
        fitted = fitted_size(source.size, target_size)
        for frame in ImageSequence.Iterator(source):
            duration = max(0.001, float(frame.info.get("duration", default_duration * 1000)) / 1000.0)
            image = frame.convert("RGB")
            if image.size != fitted:
                image = image.resize(fitted, Image.Resampling.LANCZOS)
            image, output_background = apply_background_mode(
                image,
                background,
                background_mode,
                chroma_soft_distance,
            )
            image = render_viewport(
                image,
                target_size,
                output_background,
                display_scale,
            )
            source_frames.append(image)
            source_times.append(source_time)
            source_durations.append(duration)
            source_time += duration
    if not source_frames:
        raise ValueError(f"no frames decoded from {path}")
    target_frame_count = max(1, round(source_time * animation_fps))
    frames: list[bytes] = []
    for target_index in range(target_frame_count):
        target_time = min(target_index * sample_interval, source_time - 1e-9)
        source_index = max(0, bisect_right(source_times, target_time) - 1)
        next_index = (source_index + 1) % len(source_frames)
        frame_duration = max(source_durations[source_index], 0.001)
        blend = max(
            0.0,
            min((target_time - source_times[source_index]) / frame_duration, 1.0),
        )
        if blend <= 0.001:
            rendered = source_frames[source_index]
        else:
            rendered = Image.blend(
                source_frames[source_index],
                source_frames[next_index],
                blend,
            )
        frames.append(rendered.tobytes())
    durations = [sample_interval] * target_frame_count
    return RawAnimation(state, target_size, frames, durations, output_background)


class PersistentExpressionDisplay:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.ui_theme = load_ui_theme()
        theme_effects = self.ui_theme.get("effects", {})
        theme_motion = self.ui_theme.get("motion", {})
        self.expressions = {
            key: app_path(value) for key, value in config["expressions"].items()
        }
        self.default_state = str(config.get("default_state", "idle"))
        self.output = str(config.get("output", "DSI-2"))
        geometry = display_geometry(self.output)
        if geometry is None:
            raise RuntimeError(f"display output not ready: {self.output}")
        self.left, self.top, self.width, self.height, detected_refresh_hz = geometry
        refresh_setting = config.get("display_refresh_hz", "auto")
        try:
            configured_refresh_hz = (
                detected_refresh_hz
                if str(refresh_setting).strip().lower() == "auto"
                else float(refresh_setting)
            )
        except (TypeError, ValueError):
            configured_refresh_hz = detected_refresh_hz or DEFAULT_DISPLAY_REFRESH_HZ
        self.display_refresh_hz = max(
            24.0,
            min(configured_refresh_hz, MAX_DISPLAY_REFRESH_HZ),
        )
        try:
            animation_fps_cap = float(
                config.get("animation_fps_cap", self.display_refresh_hz)
            )
        except (TypeError, ValueError):
            animation_fps_cap = self.display_refresh_hz
        self.target_render_fps = max(
            24.0,
            min(self.display_refresh_hz, animation_fps_cap),
        )
        self.render_interval = 1.0 / self.target_render_fps
        self.target_size = (self.width, self.height)
        self.boot_animation_enabled = bool(config.get("boot_animation_enabled", True))
        self.boot_id = current_boot_id()
        self.boot_active = claim_boot_animation(
            self.boot_id,
            self.boot_animation_enabled,
        )
        self.boot_replay_suppressed = (
            self.boot_animation_enabled and not self.boot_active
        )
        self.boot_logo_path = app_path(
            config.get("boot_logo_path", "assets/branding/RiverBankco.png")
        )
        self.boot_min_seconds = max(
            2.0, min(float(config.get("boot_min_seconds", 4.0)), 20.0)
        )
        self.boot_settle_seconds = max(
            0.2, min(float(config.get("boot_settle_seconds", 0.4)), 2.0)
        )
        self.boot_static_seconds = max(
            0.2, min(float(config.get("boot_static_seconds", 0.6)), 3.0)
        )
        self.boot_fade_seconds = max(
            0.3, min(float(config.get("boot_fade_seconds", 1.0)), 4.0)
        )
        self.boot_failure_timeout = max(
            10.0, min(float(config.get("boot_failure_timeout", 45.0)), 180.0)
        )
        self.boot_phase = "checking" if self.boot_active else "complete"
        self.boot_started_at = time.monotonic()
        self.boot_phase_started_at = self.boot_started_at
        self.boot_health_current = False
        self.boot_health_healthy = False
        self.boot_last_health_read = 0.0
        self.boot_health_check_count = 0
        self.boot_health_failed: list[str] = []
        self.boot_group_states = ["pending"] * len(BOOT_CHECK_GROUPS)
        self.boot_warning = False
        self.boot_wordmark_surface: object | None = None
        self.boot_chinese_surface: object | None = None
        self.boot_surface: object | None = None
        self.background_mode = str(config.get("background_mode", "original"))
        if self.background_mode not in {"original", "black_chroma"}:
            raise ValueError(f"unsupported background_mode: {self.background_mode}")
        self.chroma_soft_distance = max(
            4, min(int(config.get("chroma_soft_distance", 36)), 96)
        )
        self.display_scale = max(0.5, min(float(config.get("display_scale", 1.0)), 2.5))
        self.touch_enabled = bool(config.get("touch_enabled", True))
        self.touch_state = str(config.get("touch_state", "poke_mouth"))
        if self.touch_state not in self.expressions:
            raise ValueError(f"unknown touch_state: {self.touch_state}")
        self.touch_ttl = max(0.2, min(float(config.get("touch_ttl_seconds", 2.5)), 30.0))
        self.touch_debounce = max(
            0.05, min(float(config.get("touch_debounce_seconds", 0.4)), 5.0)
        )
        self.long_press_seconds = max(
            0.35, min(float(config.get("long_press_seconds", 0.7)), 2.0)
        )
        self.menu_deadzone = max(
            35.0, min(float(config.get("menu_deadzone_px", 72)), 180.0)
        )
        configured_menu = config.get("radial_menu", DEFAULT_RADIAL_MENU)
        if not isinstance(configured_menu, list) or len(configured_menu) != 6:
            configured_menu = list(DEFAULT_RADIAL_MENU)
        self.radial_menu = configured_menu
        self.display_diameter_mm = max(
            40.0, min(float(config.get("display_diameter_mm", 86.36)), 200.0)
        )
        self.radial_menu_offset_mm = max(
            -15.0, min(float(config.get("radial_menu_offset_mm", 3.0)), 15.0)
        )
        self.radial_menu_offset_px = round(
            min(self.width, self.height)
            * self.radial_menu_offset_mm
            / self.display_diameter_mm
        )
        self.last_touch_at = 0.0
        self.touch_count = 0
        self.pointer_down = False
        self.pointer_started_at = 0.0
        self.pointer_start = (self.width // 2, self.height // 2)
        self.pointer_position = self.pointer_start
        self.pointer_moved = False
        self.menu_active = False
        self.menu_selected: int | None = None
        self.menu_background_surface: object | None = None
        self.menu_opened_at = 0.0
        self.menu_last_interaction_at = 0.0
        self.menu_idle_seconds = max(
            2.0,
            min(float(config.get("radial_menu_idle_seconds", 2.0)), 10.0),
        )
        self.menu_blur_radius = max(
            0.0,
            min(
                float(
                    config.get(
                        "menu_background_blur",
                        theme_effects.get("backgroundBlur", 50),
                    )
                ),
                80.0,
            ),
        )
        self.ui_edge_softness = max(
            3, min(int(theme_effects.get("edgeSoftness", 9)), 18)
        )
        self.volume_mode = False
        self.volume_mode_started_at = 0.0
        self.volume_return_started_at = 0.0
        self.volume_last_interaction_at = 0.0
        self.volume_last_set_at = 0.0
        self.volume_dragging = False
        self.screensaver_panel_mode = False
        self.screensaver_panel_started_at = 0.0
        self.screensaver_panel_return_started_at = 0.0
        self.screensaver_panel_last_interaction_at = 0.0
        self.volume_idle_seconds = max(
            0.5,
            min(
                float(
                    config.get(
                        "volume_slider_idle_seconds",
                        float(theme_motion.get("idleReturnMs", 1000)) / 1000.0,
                    )
                ),
                5.0,
            ),
        )
        self.volume_transition_seconds = max(
            0.12,
            min(
                float(
                    config.get(
                        "volume_slider_transition_seconds",
                        float(theme_motion.get("transitionMs", 280)) / 1000.0,
                    )
                ),
                1.0,
            ),
        )
        self.restart_mode = False
        self.restart_mode_started_at = 0.0
        self.restart_return_started_at = 0.0
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_last_interaction_at = 0.0
        self.restart_requested_at = 0.0
        self.restart_future: Future[tuple[bool, str]] | None = None
        self.restart_error_until = 0.0
        self.restart_slider_cache_key: tuple[float, float, bool] | None = None
        self.restart_slider_cache_surface: object | None = None
        self.volume_slider_cache_key: tuple[int] | None = None
        self.volume_slider_cache_surface: object | None = None
        self.radial_menu_ring_cache: dict[int | None, object] = {}
        self.radial_menu_content_cache: dict[int | None, object] = {}
        self.status_chrome_cache: dict[tuple, object] = {}
        self.status_meter_cache: dict[tuple[str, int], tuple[object, tuple[int, int]]] = {}
        self.status_composite_cache: OrderedDict[tuple, object] = OrderedDict()
        self.status_composite_cache_limit = 24
        self.status_bar_cache_key: tuple | None = None
        self.status_bar_cache_surface: object | None = None
        self.token_popup_visible = False
        self.token_popup_last_interaction_at = 0.0
        self.token_popup_idle_seconds = max(
            0.5,
            min(float(config.get("token_popup_idle_seconds", 2.0)), 10.0),
        )
        self.token_popup_cache_key: tuple | None = None
        self.token_popup_cache_surface: object | None = None
        self.token_balance_result: dict | None = None
        self.token_balance_error: str | None = None
        self.token_balance_future: Future[dict] | None = None
        self.token_balance_last_requested_at = 0.0
        self.token_balance_cache_seconds = max(
            15.0,
            min(float(config.get("token_balance_cache_seconds", 60.0)), 600.0),
        )
        self.control_overlay_cache: OrderedDict[tuple, object] = OrderedDict()
        self.control_overlay_cache_limit = 12
        self.transition_frame_ms = 0.0
        self.transition_frame_peak_ms = 0.0
        self.last_menu_selection: dict | None = None
        self.status_visible_until = 0.0
        self.settings_active = False
        self.settings_section: str | None = None
        self.settings_pointer_target: str | None = None
        self.settings_last_interaction_at = 0.0
        self.settings_card_cache: OrderedDict[tuple, object] = OrderedDict()
        self.settings_card_cache_limit = 16
        self.settings_background_cache: object | None = None
        self.settings_transition_seconds = max(
            0.16,
            min(float(config.get("settings_transition_seconds", 0.26)), 0.45),
        )
        self.settings_transition_active = False
        self.settings_transition_started_at = 0.0
        self.settings_transition_direction = 0
        self.settings_transition_from_section: str | None = None
        self.settings_transition_to_section: str | None = None
        self.settings_transition_from_surface: object | None = None
        self.settings_transition_to_surface: object | None = None
        self.settings_transition_exits_settings = False
        self.speech_bubble_active = False
        self.speech_bubble_text = ""
        self.speech_bubble_stable_chars = 0
        self.speech_bubble_final = False
        self.speech_bubble_expires_at = 0.0
        self.speech_bubble_hide_started_at = 0.0
        self.speech_bubble_hide_seconds = max(
            0.16,
            min(float(config.get("speech_bubble_fade_seconds", 0.24)), 0.5),
        )
        self.speech_bubble_cache: OrderedDict[tuple, object] = OrderedDict()
        self.speech_bubble_cache_limit = 24
        self.last_overlay_redraw = 0.0
        self.camera_indicator_angle_degrees = max(
            -170.0,
            min(float(config.get("camera_indicator_angle_degrees", -45.0)), -10.0),
        )
        self.camera_indicator_edge_inset_px = max(
            20.0,
            min(float(config.get("camera_indicator_edge_inset_px", 44.0)), 120.0),
        )
        self.token_meter_capacity = max(
            1_000,
            min(int(config.get("token_meter_capacity", 100_000)), 10_000_000),
        )
        self.render_fps = 0.0
        self.render_frame_count = 0
        self.render_fps_window_started = time.monotonic()
        self.frame_render_ms = 0.0
        self.frame_render_peak_ms = 0.0
        self.camera_view_active = False
        self.runtime_vision_sources: dict[str, float] = {}
        self.camera_snapshot_url = str(
            config.get("camera_snapshot_url", "http://127.0.0.1:19733/snapshot")
        )
        self.camera_view_fps = max(
            1.0,
            min(
                float(config.get("camera_view_fps", 30.0)),
                self.target_render_fps,
                30.0,
            ),
        )
        self.camera_frame_interval = 1.0 / self.camera_view_fps
        self.camera_frame_surface: object | None = None
        self.camera_frame_future: Future[bytes] | None = None
        self.camera_next_fetch_at = 0.0
        self.camera_last_success_at = 0.0
        self.camera_last_error: str | None = None
        self.camera_error_logged_at = 0.0
        self.camera_measured_fps = 0.0
        self.camera_frame_count = 0
        self.camera_fps_window_started = time.monotonic()
        self.camera_gallery_dir = Path(
            str(
                config.get(
                    "camera_gallery_dir",
                    RIVERBANK_DATA / "camera/gallery",
                )
            )
        )
        self.camera_gallery_page_size = max(
            1,
            min(int(config.get("camera_gallery_page_size", 6)), 6),
        )
        self.camera_controls_inward_mm = max(
            0.0,
            min(float(config.get("camera_controls_inward_mm", 5.0)), 15.0),
        )
        self.camera_controls_inward_px = round(
            min(self.width, self.height)
            * self.camera_controls_inward_mm
            / self.display_diameter_mm
        )
        self.camera_capture_future: Future[str] | None = None
        self.camera_capture_started_at = 0.0
        self.camera_capture_flash_until = 0.0
        self.camera_capture_notice_until = 0.0
        self.camera_capture_error_until = 0.0
        self.camera_capture_error: str | None = None
        self.camera_last_photo: Path | None = None
        self.camera_pointer_target: str | None = None
        self.gallery_active = False
        self.gallery_page = 0
        self.gallery_entries: list[Path] = []
        self.gallery_selected: Path | None = None
        self.gallery_pointer_control: tuple[str, Path | None] | None = None
        self.gallery_long_press_triggered = False
        self.gallery_delete_candidate: Path | None = None
        self.gallery_notice: str | None = None
        self.gallery_notice_until = 0.0
        self.gallery_swipe_threshold_px = max(
            48.0,
            min(float(config.get("gallery_swipe_threshold_px", 84.0)), 160.0),
        )
        self.gallery_page_transition_seconds = max(
            0.18,
            min(
                float(config.get("gallery_page_transition_ms", 340.0)) / 1000.0,
                0.8,
            ),
        )
        self.gallery_page_transition_active = False
        self.gallery_page_transition_started_at = 0.0
        self.gallery_page_transition_from = 0
        self.gallery_page_transition_to = 0
        self.gallery_page_transition_direction = 0
        self.gallery_long_press_seconds = max(
            0.45,
            min(
                float(
                    config.get("gallery_long_press_seconds", self.long_press_seconds)
                ),
                1.5,
            ),
        )
        self.screensaver_config_path = self.camera_gallery_dir / ".screensaver.json"
        self.screensaver_idle_seconds = 0
        self.screensaver_photo: Path | None = None
        self.screensaver_current_photo: Path | None = None
        self.screensaver_active = False
        self.screensaver_last_activity_at = time.monotonic()
        self.load_screensaver_preferences()
        self.gallery_surface_cache: OrderedDict[
            tuple[str, int, int, int, bool], object
        ] = OrderedDict()
        self.gallery_page_surface_cache: OrderedDict[tuple, object] = OrderedDict()
        self.gallery_page_surface_cache_limit = 8
        self.camera_exit_edge_ratio = max(
            0.7, min(float(config.get("camera_exit_edge_ratio", 0.8)), 0.95)
        )
        self.camera_exit_swipe_px = max(
            60.0, min(float(config.get("camera_exit_swipe_px", 100.0)), 220.0)
        )
        self.edge_exit_candidate = False
        self.edge_exit_ready = False
        self.edge_exit_progress = 0.0
        self.system_status = SystemStatus()
        self.status_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-status",
        )
        self.control_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-control",
        )
        self.balance_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-balance",
        )
        self.status_future: Future[tuple] | None = None
        self.status_next_update_at = 0.0
        self.wifi_toggle_future: Future[tuple[bool, str]] | None = None
        self.wifi_toggle_target: bool | None = None
        self.wifi_toggle_error = ""
        self.wifi_toggle_notice_until = 0.0
        self.cache_limit = int(float(config.get("cache_limit_mb", 512)) * 1024 * 1024)
        configured_preload = config.get("preload_states", DEFAULT_PRELOAD)
        self.preload_states = tuple(
            state for state in configured_preload if state in self.expressions
        )
        self.running = True
        self.state = self.default_state
        self.deadline: float | None = None
        self.frame_index = 0
        self.frame_started_at = time.monotonic()
        self.next_frame_at = self.frame_started_at
        self.needs_redraw = True
        self.first_frames: dict[str, Animation] = {}
        self.cache: OrderedDict[str, Animation] = OrderedDict()
        self.cache_bytes = 0
        self.pending: dict[str, Future[RawAnimation]] = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="expression-decode")
        self.camera_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="camera-view",
        )
        self.last_above_refresh = 0.0

        os.environ.setdefault("DISPLAY", ":0")
        os.environ.setdefault("XAUTHORITY", str(RIVERBANK_HOME / ".Xauthority"))
        os.environ.setdefault("SDL_VIDEODRIVER", "x11")
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{self.left},{self.top}"
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame

        self.pygame = pygame
        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption(WINDOW_TITLE)
        self.screen = pygame.display.set_mode(
            self.target_size,
            pygame.NOFRAME | pygame.DOUBLEBUF,
        )
        pygame.mouse.set_visible(False)
        font_path = str(app_path(
            config.get(
                "font_path",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            )
        ))
        self.font_small = pygame.font.Font(font_path, 21)
        self.font_medium = pygame.font.Font(font_path, 26)
        self.font_large = pygame.font.Font(font_path, 34)
        self.font_camera_label = pygame.font.Font(font_path, 21)
        self.font_speech_bubble_high = pygame.font.Font(
            font_path,
            25 * UI_AA_SCALE,
        )
        status_font_path = str(app_path(
            config.get(
                "status_font_path",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
        ))
        self.font_status = pygame.font.Font(status_font_path, 20)
        self.camera_icon_cache: dict[tuple[str, bool], object] = {}
        self.overlay_surface = pygame.Surface(self.target_size, pygame.SRCALPHA)
        self.boot_surface = pygame.Surface(self.target_size, pygame.SRCALPHA)
        self.window_id = int(pygame.display.get_wm_info().get("window", 0) or 0)
        self.refresh_always_on_top(force=True)

        self.prepare_boot_assets()
        if self.boot_active:
            self.draw()
        self.load_first_frame(self.default_state)
        if not self.boot_active:
            self.draw()
        for state in self.expressions:
            if state != self.default_state:
                self.load_first_frame(state)
        for state in self.preload_states:
            self.schedule_decode(state)
        # A renderer restart necessarily closes its camera view. Keep the voice
        # controller in sync so an old visual-mode flag cannot survive by itself.
        self.send_voice_command({"command": "visual_mode", "active": False})
        self.system_status.app_version = SystemStatus.read_app_version()
        self.request_system_status_refresh(time.monotonic())
        self.request_token_balance(time.monotonic())
        self.prewarm_control_overlays()
        self.write_state()
        log(
            f"persistent renderer ready output={self.output} geometry="
            f"{self.left},{self.top} {self.width}x{self.height} "
            f"refresh={self.display_refresh_hz:.3f}Hz "
            f"target={self.target_render_fps:.3f}fps window={self.window_id}"
        )

    def refresh_always_on_top(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_above_refresh < 30:
            return
        self.last_above_refresh = now
        if not self.window_id:
            return
        try:
            result = subprocess.run(
                [
                    "/usr/bin/xprop",
                    "-id",
                    str(self.window_id),
                    "-f",
                    "_NET_WM_STATE",
                    "32a",
                    "-set",
                    "_NET_WM_STATE",
                    "_NET_WM_STATE_ABOVE",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0:
                log(f"always-on-top warning: {result.stderr.strip()}")
        except Exception as exc:
            log(f"always-on-top warning: {exc}")

    def update_system_status_async(self, now: float) -> bool:
        """Collect slow system status without ever blocking a rendered frame."""
        changed = False
        if self.status_future is not None and self.status_future.done():
            try:
                snapshot = self.status_future.result()
                changed = self.system_status.apply_snapshot(snapshot, now)
            except Exception as exc:
                log(f"system status update warning: {exc}")
            finally:
                self.status_future = None
                self.status_next_update_at = now + 2.0
        if self.status_future is None and now >= self.status_next_update_at:
            self.status_future = self.status_executor.submit(
                SystemStatus.collect_snapshot
            )
        return changed

    def prepare_boot_assets(self) -> None:
        """Prepare the compact bilingual mark while drawing the tiles live."""
        try:
            with Image.open(self.boot_logo_path) as source:
                logo = source.convert("RGBA")
            split_x = min(logo.width, logo.height)
            wordmark_end = min(logo.width, round(logo.height * 3.23))
            wordmark = logo.crop((split_x, 0, wordmark_end, logo.height))
            mask = wordmark.getchannel("A")
            bounds = mask.getbbox()
            if bounds:
                wordmark = wordmark.crop(bounds)
            max_width = round(self.width * 0.63)
            scale = min(max_width / max(wordmark.width, 1), 1.0)
            scaled_size = (
                max(1, round(wordmark.width * scale)),
                max(1, round(wordmark.height * scale)),
            )
            if wordmark.size != scaled_size:
                wordmark = wordmark.resize(scaled_size, Image.Resampling.LANCZOS)
            white_wordmark = Image.new("RGBA", wordmark.size, (255, 255, 255, 0))
            white_wordmark.putalpha(wordmark.getchannel("A"))
            self.boot_wordmark_surface = self.pygame.image.fromstring(
                white_wordmark.tobytes(),
                white_wordmark.size,
                "RGBA",
            ).convert_alpha()
        except Exception as exc:
            self.boot_wordmark_surface = self.font_large.render(
                "RiverBank",
                True,
                (238, 246, 250),
            )
            log(f"boot logo warning: {exc}")
        self.boot_chinese_surface = self.font_large.render(
            "灰 度 流 动",
            True,
            (174, 194, 202),
        )

    def read_boot_health(self) -> None:
        try:
            payload = json.loads(HEALTH_STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.boot_health_current = False
            self.boot_health_healthy = False
            self.boot_group_states = ["pending"] * len(BOOT_CHECK_GROUPS)
            return
        self.boot_health_current = (
            str(payload.get("boot_id", "")) == self.boot_id
            and bool(payload.get("startup_complete"))
        )
        if not self.boot_health_current:
            self.boot_health_healthy = False
            self.boot_group_states = ["pending"] * len(BOOT_CHECK_GROUPS)
            return
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        records = {
            str(record.get("id")): record
            for record in checks
            if isinstance(record, dict) and record.get("id")
        }
        self.boot_health_check_count = len(records)
        self.boot_health_failed = [
            check_id
            for check_id, record in records.items()
            if not bool(record.get("healthy"))
        ]
        group_states: list[str] = []
        for _, check_ids, _ in BOOT_CHECK_GROUPS:
            group_records = [records.get(check_id) for check_id in check_ids]
            if any(record is None for record in group_records):
                group_states.append("pending")
            elif any(not bool(record.get("healthy")) for record in group_records):
                group_states.append("failed")
            else:
                group_states.append("healthy")
        self.boot_group_states = group_states
        self.boot_health_healthy = bool(records) and all(
            bool(record.get("healthy")) for record in records.values()
        )

    def boot_preload_ready(self) -> bool:
        return all(state in self.cache for state in self.preload_states)

    def boot_has_critical_failure(self) -> bool:
        return any(
            critical and self.boot_group_states[index] != "healthy"
            for index, (_, _, critical) in enumerate(BOOT_CHECK_GROUPS)
        )

    def begin_boot_phase(self, phase: str, now: float) -> None:
        if phase == self.boot_phase:
            return
        self.boot_phase = phase
        self.boot_phase_started_at = now
        self.needs_redraw = True
        log(
            f"boot phase={phase} health={self.boot_health_healthy} "
            f"checks={self.boot_health_check_count} failed={self.boot_health_failed}"
        )
        self.write_state()

    def update_boot_state(self, now: float) -> None:
        if now - self.boot_last_health_read >= 0.5:
            self.read_boot_health()
            self.boot_last_health_read = now
        elapsed = now - self.boot_started_at
        ready = (
            elapsed >= self.boot_min_seconds
            and self.boot_health_current
            and self.boot_health_healthy
            and self.boot_preload_ready()
        )
        noncritical_ready = (
            elapsed >= self.boot_min_seconds
            and self.boot_health_current
            and self.boot_preload_ready()
            and not self.boot_has_critical_failure()
        )
        if self.boot_phase in {"checking", "critical_hold"}:
            if ready:
                self.boot_warning = False
                self.begin_boot_phase("settle", now)
            elif noncritical_ready:
                self.boot_warning = bool(self.boot_health_failed)
                self.begin_boot_phase("settle", now)
            elif (
                elapsed >= self.boot_failure_timeout
                and self.boot_health_current
                and self.boot_preload_ready()
            ):
                if self.boot_has_critical_failure():
                    self.begin_boot_phase("critical_hold", now)
                else:
                    self.boot_warning = bool(self.boot_health_failed)
                    self.begin_boot_phase("settle", now)
            return
        phase_elapsed = now - self.boot_phase_started_at
        if self.boot_phase == "settle" and phase_elapsed >= self.boot_settle_seconds:
            self.begin_boot_phase("static", now)
        elif self.boot_phase == "static" and phase_elapsed >= self.boot_static_seconds:
            self.begin_boot_phase("fade", now)
        elif self.boot_phase == "fade" and phase_elapsed >= self.boot_fade_seconds:
            self.boot_active = False
            self.boot_phase = "complete"
            self.send_voice_command({"command": "visual_mode", "active": False})
            self.set_state(self.default_state, force=True)
            self.needs_redraw = True
            log("boot animation complete; expression renderer visible")

    @staticmethod
    def mix_color(
        first: tuple[int, int, int],
        second: tuple[int, int, int],
        ratio: float,
    ) -> tuple[int, int, int]:
        ratio = max(0.0, min(ratio, 1.0))
        return tuple(
            round(first[index] * (1.0 - ratio) + second[index] * ratio)
            for index in range(3)
        )

    def draw_boot_animation(self, now: float) -> None:
        canvas = self.boot_surface
        if canvas is None:
            return
        canvas.fill((0, 0, 0, 255))
        tile_size = round(self.width * 0.12)
        gap = round(self.width * 0.018)
        grid_size = tile_size * 3 + gap * 2
        grid_left = (self.width - grid_size) // 2
        grid_top = round(self.height * 0.15)
        elapsed = now - self.boot_started_at
        settling = self.boot_phase == "settle"
        settle_ratio = (
            min(1.0, (now - self.boot_phase_started_at) / self.boot_settle_seconds)
            if settling
            else 1.0
        )
        grayscale = (218, 178, 132, 178, 132, 88, 132, 88, 35)
        for index, base_value in enumerate(grayscale):
            row, column = divmod(index, 3)
            phase = elapsed * 1.05 - index * 0.09
            wave = 0.5 + 0.5 * math.sin(phase * math.tau)
            state = self.boot_group_states[index]
            if self.boot_phase in {"static", "fade"}:
                amplitude = 0.0
            elif self.boot_phase == "critical_hold":
                amplitude = 8.0 if state == "failed" else 2.0
            else:
                amplitude = 18.0 if state != "healthy" else 7.0
            if settling:
                amplitude *= 1.0 - settle_ratio
            jump = round(-amplitude * wave)
            base_color = (base_value, base_value, base_value)
            # Keep the live self-check in the same neutral palette as the
            # finished mark.  A white luminance mask supplies the motion cue,
            # then dissolves continuously into the static grayscale tiles.
            highlight_fade = (
                1.0 - settle_ratio
                if settling
                else (0.0 if self.boot_phase in {"static", "fade"} else 1.0)
            )
            white_strength = (0.28 + 0.58 * wave) * highlight_fade
            if state == "failed":
                alert = (232, 71, 77) if self.boot_phase == "critical_hold" else (228, 159, 66)
                alert_strength = (0.42 + 0.30 * wave) * highlight_fade
                color = self.mix_color(base_color, alert, alert_strength)
            else:
                color = self.mix_color(base_color, (255, 255, 255), white_strength)
            rect = self.pygame.Rect(
                grid_left + column * (tile_size + gap),
                grid_top + row * (tile_size + gap) + jump,
                tile_size,
                tile_size,
            )
            glow_alpha = round((36 + 82 * wave) * highlight_fade)
            glow_rect = rect.inflate(14, 14)
            self.pygame.draw.rect(
                canvas,
                (255, 255, 255, glow_alpha),
                glow_rect,
                border_radius=round(tile_size * 0.25),
            )
            self.pygame.draw.rect(
                canvas,
                (*color, 255),
                rect,
                border_radius=round(tile_size * 0.22),
            )

        if self.boot_wordmark_surface is not None:
            wordmark = self.boot_wordmark_surface
            if settling:
                wordmark_alpha = round(210 + 45 * settle_ratio)
            else:
                wordmark_alpha = (
                    210
                    if self.boot_phase in {"checking", "critical_hold"}
                    else 255
                )
            wordmark.set_alpha(wordmark_alpha)
            self.screen.blit(
                canvas,
                (0, 0),
            )
            self.screen.blit(
                wordmark,
                wordmark.get_rect(center=(self.width // 2, round(self.height * 0.69))),
            )
            if self.boot_chinese_surface is not None:
                chinese = self.boot_chinese_surface
                chinese.set_alpha(wordmark_alpha)
                self.screen.blit(
                    chinese,
                    chinese.get_rect(
                        center=(self.width // 2, round(self.height * 0.80))
                    ),
                )
        else:
            self.screen.blit(canvas, (0, 0))

        if self.boot_phase == "fade":
            fade_ratio = min(
                1.0,
                (now - self.boot_phase_started_at) / self.boot_fade_seconds,
            )
            veil = self.overlay_surface
            veil.fill((0, 0, 0, round(255 * fade_ratio)))
            self.screen.blit(veil, (0, 0))

    def load_first_frame(self, state: str) -> None:
        path = self.expressions[state]
        if not path.is_file():
            raise FileNotFoundError(path)
        with Image.open(path) as source:
            background = dominant_edge_color(source.copy())
            image = source.convert("RGB")
            fitted = fitted_size(image.size, self.target_size)
            if image.size != fitted:
                image = image.resize(fitted, Image.Resampling.LANCZOS)
            image, background = apply_background_mode(
                image,
                background,
                self.background_mode,
                self.chroma_soft_distance,
            )
            image = render_viewport(
                image,
                self.target_size,
                background,
                self.display_scale,
            )
            surface = self.pygame.image.fromstring(
                image.tobytes(), self.target_size, "RGB"
            )
            duration = max(
                self.render_interval,
                float(source.info.get("duration", 100)) / 1000.0,
            )
        memory_bytes = self.target_size[0] * self.target_size[1] * 4
        self.first_frames[state] = Animation(
            state=state,
            frames=[surface],
            durations=[duration],
            background=background,
            memory_bytes=memory_bytes,
        )

    def schedule_decode(self, state: str) -> None:
        if state in self.cache or state in self.pending:
            return
        path = self.expressions[state]
        if not path.is_file():
            log(f"expression asset missing: {path}")
            return
        self.pending[state] = self.executor.submit(
            decode_animation,
            state,
            path,
            self.target_size,
            self.background_mode,
            self.chroma_soft_distance,
            self.display_scale,
            self.target_render_fps,
        )

    def install_completed_decodes(self) -> None:
        for state, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[state]
            try:
                raw = future.result()
                surfaces = [
                    self.pygame.image.fromstring(frame, raw.size, "RGB")
                    for frame in raw.frames
                ]
                memory_bytes = raw.size[0] * raw.size[1] * 4 * len(surfaces)
                animation = Animation(
                    state=state,
                    frames=surfaces,
                    durations=raw.durations,
                    background=raw.background,
                    memory_bytes=memory_bytes,
                )
                previous = self.cache.pop(state, None)
                if previous:
                    self.cache_bytes -= previous.memory_bytes
                self.cache[state] = animation
                self.cache_bytes += memory_bytes
                self.evict_cache()
                if self.state == state:
                    self.frame_index = 0
                    self.frame_started_at = time.monotonic()
                    self.next_frame_at = (
                        self.frame_started_at + animation.durations[0]
                    )
                    self.needs_redraw = True
                log(
                    f"animation cached state={state} frames={len(surfaces)} "
                    f"cache={self.cache_bytes / 1024 / 1024:.1f}MB"
                )
                self.write_state()
            except Exception as exc:
                log(f"animation decode failed state={state}: {exc}")

    def evict_cache(self) -> None:
        protected = set(self.preload_states) | {self.state}
        while self.cache_bytes > self.cache_limit:
            victim = next((state for state in self.cache if state not in protected), None)
            if victim is None:
                break
            animation = self.cache.pop(victim)
            self.cache_bytes -= animation.memory_bytes
            log(f"animation evicted state={victim}")

    def current_animation(self) -> Animation:
        animation = self.cache.get(self.state)
        if animation is not None:
            self.cache.move_to_end(self.state)
            return animation
        return self.first_frames[self.state]

    def prepare_menu_background(self) -> None:
        """Freeze and blur the current expression or camera frame once."""
        base = self.pygame.Surface(self.target_size)
        if self.camera_view_active and self.camera_frame_surface is not None:
            base.fill((0, 0, 0))
            base.blit(self.camera_frame_surface, (0, 0))
        else:
            animation = self.current_animation()
            frame = animation.frames[self.frame_index % len(animation.frames)]
            base.fill(animation.background)
            base.blit(frame, (0, 0))
        if self.menu_blur_radius <= 0:
            self.menu_background_surface = base
            return
        pixels = self.pygame.image.tostring(base, "RGB")
        blurred = Image.frombytes("RGB", self.target_size, pixels).filter(
            ImageFilter.GaussianBlur(radius=self.menu_blur_radius)
        )
        self.menu_background_surface = self.pygame.image.fromstring(
            blurred.tobytes(), self.target_size, "RGB"
        )

    def set_camera_view(self, active: bool) -> dict:
        self.note_screensaver_activity()
        self.camera_view_active = bool(active)
        self.edge_exit_candidate = False
        self.edge_exit_ready = False
        self.edge_exit_progress = 0.0
        if self.camera_view_active:
            self.camera_next_fetch_at = 0.0
            self.camera_last_error = None
            self.camera_measured_fps = 0.0
            self.camera_frame_count = 0
            self.camera_fps_window_started = time.monotonic()
        else:
            self.gallery_active = False
            self.gallery_selected = None
            self.cancel_gallery_page_transition()
            self.gallery_pointer_control = None
            self.gallery_long_press_triggered = False
            self.gallery_delete_candidate = None
            self.camera_pointer_target = None
            if self.camera_frame_future is not None:
                self.camera_frame_future.cancel()
            self.camera_frame_future = None
        self.needs_redraw = True
        self.write_state()
        log(f"camera view active={self.camera_view_active}")
        return {"ok": True, "camera_view_active": self.camera_view_active}

    def camera_control_centers(self) -> dict[str, tuple[int, int]]:
        base_left = round(self.width * 0.19)
        base_right = round(self.width * 0.81)
        return {
            "capture": (
                base_left + self.camera_controls_inward_px,
                round(self.height * 0.80),
            ),
            "gallery": (
                base_right - self.camera_controls_inward_px,
                round(self.height * 0.80),
            ),
        }

    def camera_control_at(self, position: tuple[int, int]) -> str | None:
        for name, center in self.camera_control_centers().items():
            if math.dist(position, center) <= 58:
                return name
        return None

    def refresh_gallery(self) -> None:
        try:
            entries = [
                path
                for pattern in ("*.jpg", "*.jpeg", "*.png")
                for path in self.camera_gallery_dir.glob(pattern)
                if path.is_file()
            ]
            self.gallery_entries = sorted(
                entries,
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError as exc:
            self.gallery_entries = []
            self.camera_capture_error = str(exc)
            self.camera_capture_error_until = time.monotonic() + 3.0
        page_count = self.gallery_page_count()
        self.gallery_page = min(self.gallery_page, max(0, page_count - 1))

    def gallery_page_count(self) -> int:
        return max(
            1,
            math.ceil(len(self.gallery_entries) / self.camera_gallery_page_size),
        )

    def current_gallery_entries(self, page: int | None = None) -> list[Path]:
        page_index = self.gallery_page if page is None else max(0, int(page))
        start = page_index * self.camera_gallery_page_size
        return self.gallery_entries[start : start + self.camera_gallery_page_size]

    def load_screensaver_preferences(self) -> None:
        try:
            payload = json.loads(
                self.screensaver_config_path.read_text(encoding="utf-8")
            )
            idle_seconds = int(payload.get("idle_seconds", 0))
            self.screensaver_idle_seconds = (
                idle_seconds if idle_seconds in {180, 300, 600} else 0
            )
            raw_photo = str(payload.get("photo", "")).strip()
            if raw_photo:
                photo = Path(raw_photo).resolve()
                gallery_root = self.camera_gallery_dir.resolve()
                if (
                    photo.parent == gallery_root
                    and photo.suffix.lower() in {".jpg", ".jpeg", ".png"}
                    and photo.is_file()
                ):
                    self.screensaver_photo = photo
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log(f"screensaver preferences warning: {exc}")

    def save_screensaver_preferences(self) -> None:
        payload = {
            "idle_seconds": self.screensaver_idle_seconds,
            "photo": str(self.screensaver_photo) if self.screensaver_photo else None,
            "updated_at": time.time(),
        }
        try:
            self.camera_gallery_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.screensaver_config_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.screensaver_config_path)
        except OSError as exc:
            log(f"screensaver preferences save warning: {exc}")

    def note_screensaver_activity(self, now: float | None = None) -> None:
        activity_at = time.monotonic() if now is None else now
        self.screensaver_last_activity_at = activity_at
        if self.screensaver_active:
            self.screensaver_active = False
            self.screensaver_current_photo = None
            self.needs_redraw = True
            log("photo screensaver exited by activity")

    def set_screensaver_timeout(self, seconds: int) -> None:
        seconds = seconds if seconds in {180, 300, 600} else 0
        self.screensaver_idle_seconds = seconds
        self.note_screensaver_activity()
        self.save_screensaver_preferences()
        self.needs_redraw = True
        label = "关闭" if seconds == 0 else f"{seconds // 60} 分钟"
        log(f"photo screensaver timeout={label}")
        self.write_state()

    def set_screensaver_photo(self, path: Path) -> bool:
        try:
            gallery_root = self.camera_gallery_dir.resolve(strict=True)
            photo = path.resolve(strict=True)
            if (
                photo.parent != gallery_root
                or photo.suffix.lower() not in {".jpg", ".jpeg", ".png"}
            ):
                raise ValueError("photo is outside the managed gallery")
            self.screensaver_photo = photo
            self.screensaver_current_photo = photo if self.screensaver_active else None
            self.gallery_delete_candidate = None
            self.gallery_notice = "已设为屏保图片"
            self.gallery_notice_until = time.monotonic() + 1.6
            self.save_screensaver_preferences()
            self.needs_redraw = True
            self.write_state()
            log(f"photo screensaver selected path={photo}")
            return True
        except (OSError, ValueError) as exc:
            self.gallery_delete_candidate = None
            self.gallery_notice = "设置屏保失败"
            self.gallery_notice_until = time.monotonic() + 2.0
            self.needs_redraw = True
            self.write_state()
            log(f"photo screensaver selection warning: {exc}")
            return False

    def screensaver_photo_path(self) -> Path | None:
        if self.screensaver_photo is not None and self.screensaver_photo.is_file():
            return self.screensaver_photo
        self.refresh_gallery()
        return self.gallery_entries[0] if self.gallery_entries else None

    def start_screensaver(self, now: float) -> bool:
        photo = self.screensaver_photo_path()
        if photo is None:
            self.screensaver_last_activity_at = now
            log("photo screensaver skipped: gallery is empty")
            return False
        self.screensaver_current_photo = photo
        self.screensaver_active = True
        self.needs_redraw = True
        self.write_state()
        log(f"photo screensaver entered path={photo}")
        return True

    def update_screensaver(self, now: float) -> None:
        if self.screensaver_active:
            if (
                self.screensaver_current_photo is None
                or not self.screensaver_current_photo.is_file()
            ):
                self.note_screensaver_activity(now)
                self.write_state()
            return
        if (
            self.screensaver_idle_seconds <= 0
            or now - self.screensaver_last_activity_at
            < self.screensaver_idle_seconds
            or self.state != self.default_state
            or self.deadline is not None
            or self.pointer_down
            or self.menu_active
            or self.settings_active
            or now < self.status_visible_until
            or self.token_popup_visible
            or self.camera_view_active
            or self.gallery_active
            or self.camera_indicator_active()
            or self.camera_capture_future is not None
            or self.restart_requested_at
            or self.speech_bubble_opacity(now) > 0.0
        ):
            return
        self.start_screensaver(now)

    def draw_screensaver(self) -> None:
        self.screen.fill((0, 0, 0))
        if self.screensaver_current_photo is None:
            return
        surface = self.gallery_image_surface(
            self.screensaver_current_photo,
            self.target_size,
            cover=True,
        )
        if surface is None:
            return
        tile = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        tile.blit(surface, (0, 0))
        mask = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        mask.fill((0, 0, 0, 0))
        self.pygame.draw.circle(
            mask,
            (255, 255, 255, 255),
            (self.width // 2, self.height // 2),
            min(self.width, self.height) // 2,
        )
        tile.blit(mask, (0, 0), special_flags=self.pygame.BLEND_RGBA_MULT)
        self.screen.blit(tile, (0, 0))

    def enter_gallery(self) -> None:
        if not self.camera_view_active:
            return
        self.refresh_gallery()
        self.cancel_gallery_page_transition()
        self.gallery_active = True
        self.gallery_selected = None
        self.gallery_pointer_control = None
        self.gallery_long_press_triggered = False
        self.gallery_delete_candidate = None
        self.camera_pointer_target = None
        self.needs_redraw = True
        self.write_state()
        log(f"camera gallery opened photos={len(self.gallery_entries)}")

    def exit_gallery(self) -> None:
        self.cancel_gallery_page_transition()
        self.gallery_delete_candidate = None
        self.gallery_long_press_triggered = False
        self.gallery_pointer_control = None
        if self.gallery_selected is not None:
            self.gallery_selected = None
            log("camera gallery returned to grid")
        else:
            self.gallery_active = False
            self.camera_next_fetch_at = 0.0
            log("camera gallery returned to viewfinder")
        self.camera_pointer_target = None
        self.needs_redraw = True
        self.write_state()

    def capture_photo(self) -> None:
        if (
            not self.camera_view_active
            or self.gallery_active
            or self.camera_capture_future is not None
        ):
            return
        now_wall = time.time()
        milliseconds = int((now_wall % 1.0) * 1000)
        filename = time.strftime("RB_%Y%m%d_%H%M%S", time.localtime(now_wall))
        output_path = self.camera_gallery_dir / f"{filename}_{milliseconds:03d}.jpg"
        self.camera_capture_started_at = time.monotonic()
        self.camera_capture_flash_until = self.camera_capture_started_at + 0.18
        self.camera_capture_error = None
        self.camera_capture_future = self.executor.submit(
            capture_camera_photo,
            self.camera_snapshot_url,
            output_path,
        )
        self.needs_redraw = True
        self.write_state()
        log(f"camera capture requested path={output_path}")

    def update_camera_capture(self, now: float) -> None:
        if self.camera_capture_future is None or not self.camera_capture_future.done():
            return
        try:
            self.camera_last_photo = Path(self.camera_capture_future.result())
            self.camera_capture_notice_until = now + 1.2
            self.camera_capture_error = None
            self.refresh_gallery()
            log(f"camera photo saved path={self.camera_last_photo}")
        except Exception as exc:
            self.camera_capture_error = str(exc)
            self.camera_capture_error_until = now + 3.0
            log(f"camera capture warning: {exc}")
        finally:
            self.camera_capture_future = None
            self.needs_redraw = True
            self.write_state()

    def gallery_image_surface(
        self,
        path: Path,
        target_size: tuple[int, int],
        cover: bool,
    ) -> object | None:
        try:
            modified = path.stat().st_mtime_ns
            key = (str(path), modified, target_size[0], target_size[1], cover)
            cached = self.gallery_surface_cache.get(key)
            if cached is not None:
                self.gallery_surface_cache.move_to_end(key)
                return cached
            with Image.open(path) as source:
                image = source.convert("RGB")
            source_width, source_height = image.size
            target_width, target_height = target_size
            scale = (
                max(target_width / source_width, target_height / source_height)
                if cover
                else min(target_width / source_width, target_height / source_height)
            )
            rendered_size = (
                max(1, round(source_width * scale)),
                max(1, round(source_height * scale)),
            )
            image = image.resize(rendered_size, Image.Resampling.LANCZOS)
            if cover:
                left = max(0, (image.width - target_width) // 2)
                top = max(0, (image.height - target_height) // 2)
                image = image.crop((left, top, left + target_width, top + target_height))
            surface = self.pygame.image.fromstring(
                image.tobytes(),
                image.size,
                "RGB",
            )
            self.gallery_surface_cache[key] = surface
            while len(self.gallery_surface_cache) > 24:
                self.gallery_surface_cache.popitem(last=False)
            return surface
        except (OSError, ValueError):
            return None

    def decode_camera_frame(self, payload: bytes) -> object:
        expected_size = self.target_size[0] * self.target_size[1] * 3
        if len(payload) != expected_size:
            raise ValueError(
                f"camera RGB frame has {len(payload)} bytes, expected {expected_size}"
            )
        return self.pygame.image.fromstring(payload, self.target_size, "RGB")

    def update_camera_view(self, now: float) -> None:
        if not self.camera_view_active or self.gallery_active:
            return
        if self.camera_frame_future is not None and self.camera_frame_future.done():
            try:
                self.camera_frame_surface = self.decode_camera_frame(
                    self.camera_frame_future.result()
                )
                self.camera_last_success_at = time.time()
                self.camera_last_error = None
                self.camera_frame_count += 1
                camera_fps_elapsed = now - self.camera_fps_window_started
                if camera_fps_elapsed >= 2.0:
                    self.camera_measured_fps = (
                        self.camera_frame_count / camera_fps_elapsed
                    )
                    self.camera_frame_count = 0
                    self.camera_fps_window_started = now
                self.needs_redraw = True
            except Exception as exc:
                self.camera_last_error = str(exc)
                if now - self.camera_error_logged_at >= 5.0:
                    log(f"camera view warning: {exc}")
                    self.camera_error_logged_at = now
            finally:
                self.camera_frame_future = None
        if self.camera_frame_future is None and now >= self.camera_next_fetch_at:
            self.camera_next_fetch_at = now + self.camera_frame_interval
            self.camera_frame_future = self.camera_executor.submit(
                fetch_camera_frame,
                self.camera_snapshot_url,
                self.target_size,
            )

    def set_state(self, state: str, ttl: float | None = None, force: bool = False) -> dict:
        if state not in self.expressions:
            return {"ok": False, "error": f"unknown state: {state}", "states": sorted(self.expressions)}
        ttl_value = None
        if ttl is not None:
            try:
                ttl_value = max(0.0, min(float(ttl), 300.0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "ttl must be numeric"}
        changed = state != self.state
        now = time.monotonic()
        if changed or force or state != self.default_state:
            self.note_screensaver_activity(now)
        self.state = state
        self.deadline = now + ttl_value if ttl_value else None
        if changed or force:
            self.frame_index = 0
            animation = self.current_animation()
            self.frame_started_at = now
            self.next_frame_at = self.frame_started_at + animation.durations[0]
            self.needs_redraw = True
        self.schedule_decode(state)
        self.write_state()
        log(
            f"expression={state} renderer=persistent cached={state in self.cache} "
            f"ttl={ttl_value}"
        )
        return {"ok": True, "state": self.state, "ttl": ttl_value}

    def advance_animation(self) -> None:
        animation = self.current_animation()
        if len(animation.frames) <= 1:
            return
        now = time.monotonic()
        advanced = False
        for _ in range(len(animation.frames)):
            if now < self.next_frame_at:
                break
            self.frame_index = (self.frame_index + 1) % len(animation.frames)
            self.frame_started_at = self.next_frame_at
            self.next_frame_at = (
                self.frame_started_at + animation.durations[self.frame_index]
            )
            advanced = True
        if advanced:
            self.needs_redraw = True

    def camera_control_icon(self, name: str, active: bool) -> object:
        key = (name, active)
        cached = self.camera_icon_cache.get(key)
        if cached is not None:
            return cached

        supersample = 4
        final_size = 52
        canvas_size = final_size * supersample
        center = canvas_size // 2
        surface = self.pygame.Surface(
            (canvas_size, canvas_size), self.pygame.SRCALPHA
        )
        white = (229, 250, 255, 255) if active else (221, 248, 255, 255)
        blue = (122, 229, 253, 255) if active else (99, 221, 251, 255)
        if name == "capture":
            self.pygame.draw.circle(
                surface,
                white,
                (center, center),
                20 * supersample,
                3 * supersample,
            )
            self.pygame.draw.circle(
                surface,
                blue,
                (center, center),
                13 * supersample,
            )
        else:
            rect = self.pygame.Rect(
                center - 20 * supersample,
                center - 16 * supersample,
                40 * supersample,
                32 * supersample,
            )
            self.pygame.draw.rect(
                surface,
                white,
                rect,
                width=3 * supersample,
                border_radius=6 * supersample,
            )
            self.pygame.draw.circle(
                surface,
                blue,
                (center + 10 * supersample, center - 7 * supersample),
                4 * supersample,
            )
            mountains = [
                (center - 15 * supersample, center + 10 * supersample),
                (center - 5 * supersample, center - 1 * supersample),
                (center + 2 * supersample, center + 6 * supersample),
                (center + 8 * supersample, center + 1 * supersample),
                (center + 16 * supersample, center + 10 * supersample),
            ]
            self.pygame.draw.lines(
                surface,
                blue,
                False,
                mountains,
                3 * supersample,
            )
        cached = self.pygame.transform.smoothscale(
            surface, (final_size, final_size)
        )
        self.camera_icon_cache[key] = cached
        return cached

    def draw_camera_controls(self, now: float) -> None:
        layer = self.overlay_surface
        layer.fill((0, 0, 0, 0))
        centers = self.camera_control_centers()
        pressed = self.camera_pointer_target if self.pointer_down else None
        for name, center in centers.items():
            active = pressed == name
            radius = 43 if active else 40
            self.draw_aa_circle(layer, (4, 22, 31, 205), center, radius + 7)
            self.draw_aa_circle(
                layer,
                (15, 56, 72, 238) if active else (7, 34, 46, 232),
                center,
                radius,
            )
            self.draw_aa_ring(
                layer,
                (91, 213, 247, 170),
                center,
                radius,
                2,
            )
            if name == "capture":
                label_text = "拍照"
            else:
                label_text = "相册"
            icon = self.camera_control_icon(name, active)
            layer.blit(icon, icon.get_rect(center=center))
            label = self.font_camera_label.render(
                label_text, True, (215, 241, 249)
            )
            layer.blit(label, label.get_rect(center=(center[0], center[1] + 59)))
        self.screen.blit(layer, (0, 0))

        if now < self.camera_capture_flash_until:
            duration = max(0.001, self.camera_capture_flash_until - self.camera_capture_started_at)
            remaining = max(0.0, self.camera_capture_flash_until - now)
            flash = self.overlay_surface
            flash.fill((232, 250, 255, round(150 * remaining / duration)))
            self.screen.blit(flash, (0, 0))
        if now < self.camera_capture_notice_until:
            notice = self.font_medium.render("已保存到相册", True, (216, 249, 231))
            shadow = notice.get_rect(center=(self.width // 2, round(self.height * 0.72)))
            bubble = shadow.inflate(38, 22)
            self.pygame.draw.rect(
                self.screen,
                (3, 25, 31),
                bubble,
                border_radius=bubble.height // 2,
            )
            self.screen.blit(notice, shadow)
        elif now < self.camera_capture_error_until:
            notice = self.font_small.render("照片保存失败", True, (255, 183, 166))
            self.screen.blit(
                notice,
                notice.get_rect(center=(self.width // 2, round(self.height * 0.72))),
            )

    def gallery_thumbnail_layout(
        self,
        page: int | None = None,
    ) -> list[tuple[Path, object]]:
        centers_x = (170, 400, 630)
        centers_y = (305, 500)
        entries = self.current_gallery_entries(page)
        layout: list[tuple[Path, object]] = []
        for index, path in enumerate(entries):
            column = index % 3
            row = index // 3
            rect = self.pygame.Rect(0, 0, 194, 142)
            rect.center = (centers_x[column], centers_y[row])
            layout.append((path, rect))
        return layout

    def gallery_page_surface_key(self, page: int) -> tuple:
        signature: list[tuple[str, int]] = []
        for path in self.current_gallery_entries(page):
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            signature.append((str(path), modified))
        return (page, tuple(signature), str(self.screensaver_photo or ""))

    def gallery_page_surface(self, page: int) -> object:
        """Return one precomposed thumbnail page for low-cost animation frames."""
        key = self.gallery_page_surface_key(page)
        cached = self.gallery_page_surface_cache.get(key)
        if cached is not None:
            self.gallery_page_surface_cache.move_to_end(key)
            return cached
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        for path, rect in self.gallery_thumbnail_layout(page):
            thumbnail = self.gallery_image_surface(path, rect.size, cover=True)
            self.pygame.draw.rect(
                surface,
                (10, 48, 62),
                rect.inflate(8, 8),
                border_radius=18,
            )
            if thumbnail is None:
                self.pygame.draw.rect(
                    surface,
                    (20, 35, 41),
                    rect,
                    border_radius=14,
                )
                continue
            tile = self.pygame.Surface(rect.size, self.pygame.SRCALPHA)
            tile.blit(thumbnail, (0, 0))
            mask = self.pygame.Surface(rect.size, self.pygame.SRCALPHA)
            mask.fill((0, 0, 0, 0))
            self.pygame.draw.rect(
                mask,
                (255, 255, 255, 255),
                mask.get_rect(),
                border_radius=14,
            )
            tile.blit(mask, (0, 0), special_flags=self.pygame.BLEND_RGBA_MULT)
            surface.blit(tile, rect)
            if self.screensaver_photo == path:
                badge_center = (rect.right - 17, rect.top + 17)
                self.draw_aa_circle(surface, (24, 154, 194), badge_center, 15)
                badge = self.font_status.render("屏", True, (229, 250, 255))
                surface.blit(
                    badge,
                    badge.get_rect(center=(badge_center[0], badge_center[1] - 1)),
                )
        self.gallery_page_surface_cache[key] = surface
        self.gallery_page_surface_cache.move_to_end(key)
        while len(self.gallery_page_surface_cache) > self.gallery_page_surface_cache_limit:
            self.gallery_page_surface_cache.popitem(last=False)
        return surface

    def gallery_page_transition_progress(self, now: float) -> tuple[float, float]:
        if not self.gallery_page_transition_active:
            return 1.0, 1.0
        raw = max(
            0.0,
            min(
                (now - self.gallery_page_transition_started_at)
                / self.gallery_page_transition_seconds,
                1.0,
            ),
        )
        eased = raw * raw * (3.0 - 2.0 * raw)
        return raw, eased

    def start_gallery_page_transition(self, direction: int) -> bool:
        if (
            self.gallery_page_transition_active
            or self.gallery_selected is not None
            or self.gallery_delete_candidate is not None
        ):
            return False
        page_count = self.gallery_page_count()
        if page_count <= 1:
            return False
        direction = 1 if direction >= 0 else -1
        target_page = (self.gallery_page + direction) % page_count
        # Precompose both pages before the clock starts. The transition itself then
        # only performs two cached blits and remains close to the panel refresh rate.
        self.gallery_page_surface(self.gallery_page)
        self.gallery_page_surface(target_page)
        now = time.monotonic()
        self.gallery_page_transition_active = True
        self.gallery_page_transition_started_at = now
        self.gallery_page_transition_from = self.gallery_page
        self.gallery_page_transition_to = target_page
        self.gallery_page_transition_direction = direction
        self.needs_redraw = True
        log(
            "camera gallery page transition started "
            f"from={self.gallery_page + 1} to={target_page + 1} "
            f"duration={self.gallery_page_transition_seconds:.3f}s"
        )
        return True

    def update_gallery_page_transition(self, now: float) -> bool:
        if not self.gallery_page_transition_active:
            return False
        raw, _eased = self.gallery_page_transition_progress(now)
        if raw < 1.0:
            return False
        self.gallery_page = self.gallery_page_transition_to
        self.gallery_page_transition_active = False
        self.gallery_page_transition_started_at = 0.0
        self.gallery_page_transition_from = self.gallery_page
        self.gallery_page_transition_to = self.gallery_page
        self.gallery_page_transition_direction = 0
        self.needs_redraw = True
        self.write_state()
        log(
            "camera gallery page transition completed "
            f"page={self.gallery_page + 1}/{self.gallery_page_count()}"
        )
        return True

    def cancel_gallery_page_transition(self) -> None:
        self.gallery_page_transition_active = False
        self.gallery_page_transition_started_at = 0.0
        self.gallery_page_transition_from = self.gallery_page
        self.gallery_page_transition_to = self.gallery_page
        self.gallery_page_transition_direction = 0

    def draw_gallery_page_content(self, now: float) -> None:
        if not self.gallery_page_transition_active:
            self.screen.blit(self.gallery_page_surface(self.gallery_page), (0, 0))
            return
        _raw, eased = self.gallery_page_transition_progress(now)
        direction = self.gallery_page_transition_direction
        travel = self.width - 140
        outgoing = self.gallery_page_surface(self.gallery_page_transition_from)
        incoming = self.gallery_page_surface(self.gallery_page_transition_to)
        previous_clip = self.screen.get_clip()
        self.screen.set_clip(self.pygame.Rect(70, 220, self.width - 140, 365))
        self.screen.blit(outgoing, (-round(direction * travel * eased), 0))
        self.screen.blit(incoming, (round(direction * travel * (1.0 - eased)), 0))
        self.screen.set_clip(previous_clip)

    def gallery_back_button_center(self) -> tuple[int, int]:
        return round(self.width * 0.22), round(self.height * 0.175)

    def draw_gallery_back_button(self) -> None:
        center = self.gallery_back_button_center()
        self.draw_aa_circle(self.screen, (5, 27, 37), center, 37)
        self.draw_aa_ring(self.screen, (70, 178, 213), center, 37, 2)
        chevron = self.pygame.Surface((32 * UI_AA_SCALE, 42 * UI_AA_SCALE), self.pygame.SRCALPHA)
        chevron.fill((0, 0, 0, 0))
        self.pygame.draw.lines(
            chevron,
            (203, 240, 250),
            False,
            [
                (25 * UI_AA_SCALE, 5 * UI_AA_SCALE),
                (8 * UI_AA_SCALE, 21 * UI_AA_SCALE),
                (25 * UI_AA_SCALE, 37 * UI_AA_SCALE),
            ],
            4 * UI_AA_SCALE,
        )
        chevron = self.pygame.transform.smoothscale(chevron, (32, 42))
        self.screen.blit(chevron, chevron.get_rect(center=center))

    def gallery_delete_button_rects(self) -> dict[str, object]:
        screensaver = self.pygame.Rect(0, 0, 340, 66)
        cancel = self.pygame.Rect(0, 0, 150, 62)
        delete = self.pygame.Rect(0, 0, 150, 62)
        screensaver.center = (400, 398)
        cancel.center = (310, 486)
        delete.center = (490, 486)
        return {"screensaver": screensaver, "cancel": cancel, "delete": delete}

    def gallery_delete_control_at(self, position: tuple[int, int]) -> str | None:
        for action, rect in self.gallery_delete_button_rects().items():
            if rect.inflate(14, 14).collidepoint(position):
                return action
        return None

    def draw_gallery_delete_prompt(self) -> None:
        shade = self.overlay_surface
        shade.fill((0, 0, 0, 174))
        self.screen.blit(shade, (0, 0))

        card = self.pygame.Rect(0, 0, 470, 306)
        card.center = (self.width // 2, 400)
        self.pygame.draw.rect(
            self.screen,
            (4, 25, 34),
            card,
            border_radius=34,
        )
        self.pygame.draw.rect(
            self.screen,
            (30, 92, 112),
            card,
            width=2,
            border_radius=34,
        )
        title = self.font_medium.render("照片操作", True, (231, 247, 251))
        hint = self.font_small.render(
            "选择屏保，或移入回收站", True, (128, 174, 188)
        )
        self.screen.blit(title, title.get_rect(center=(400, 294)))
        self.screen.blit(hint, hint.get_rect(center=(400, 335)))

        buttons = self.gallery_delete_button_rects()
        screensaver_rect = buttons["screensaver"]
        cancel_rect = buttons["cancel"]
        delete_rect = buttons["delete"]
        self.pygame.draw.rect(
            self.screen,
            (16, 91, 116),
            screensaver_rect,
            border_radius=33,
        )
        self.pygame.draw.rect(
            self.screen,
            (12, 52, 66),
            cancel_rect,
            border_radius=31,
        )
        self.pygame.draw.rect(
            self.screen,
            (118, 38, 48),
            delete_rect,
            border_radius=31,
        )
        screensaver_label = self.font_small.render(
            "设为屏保", True, (218, 247, 255)
        )
        cancel_label = self.font_small.render("取消", True, (199, 228, 237))
        delete_label = self.font_small.render("删除", True, (255, 227, 229))
        self.screen.blit(
            screensaver_label,
            screensaver_label.get_rect(center=screensaver_rect.center),
        )
        self.screen.blit(cancel_label, cancel_label.get_rect(center=cancel_rect.center))
        self.screen.blit(delete_label, delete_label.get_rect(center=delete_rect.center))

    def draw_gallery_notice(self, now: float) -> None:
        if not self.gallery_notice or now >= self.gallery_notice_until:
            return
        label = self.font_small.render(self.gallery_notice, True, (214, 244, 250))
        bubble = label.get_rect(center=(self.width // 2, 640)).inflate(42, 24)
        self.pygame.draw.rect(
            self.screen,
            (7, 42, 54),
            bubble,
            border_radius=bubble.height // 2,
        )
        self.screen.blit(label, label.get_rect(center=bubble.center))

    def draw_gallery(self, now: float) -> None:
        self.screen.fill((0, 0, 0))
        self.pygame.draw.circle(
            self.screen,
            (3, 19, 27),
            (self.width // 2, self.height // 2),
            round(min(self.width, self.height) * 0.43),
        )
        self.draw_gallery_back_button()

        if self.gallery_selected is not None:
            surface = self.gallery_image_surface(
                self.gallery_selected,
                (700, 560),
                cover=False,
            )
            if surface is not None:
                self.screen.blit(
                    surface,
                    surface.get_rect(center=(self.width // 2, self.height // 2 + 15)),
                )
            name = self.font_small.render(
                self.gallery_selected.stem[-19:],
                True,
                (180, 214, 225),
            )
            self.screen.blit(name, name.get_rect(center=(self.width // 2, 704)))
            self.draw_gallery_back_button()
            self.draw_gallery_notice(now)
            return

        title = self.font_large.render("相册", True, (220, 245, 252))
        self.screen.blit(title, title.get_rect(center=(self.width // 2, 112)))
        count = self.font_small.render(
            f"{len(self.gallery_entries)} 张照片",
            True,
            (118, 178, 198),
        )
        self.screen.blit(count, count.get_rect(center=(self.width // 2, 151)))

        if not self.gallery_entries:
            empty = self.font_medium.render("还没有照片", True, (143, 186, 201))
            hint = self.font_small.render("返回相机拍一张吧", True, (88, 133, 150))
            self.screen.blit(empty, empty.get_rect(center=(self.width // 2, 377)))
            self.screen.blit(hint, hint.get_rect(center=(self.width // 2, 418)))
        else:
            self.draw_gallery_page_content(now)

        page_count = self.gallery_page_count()
        if page_count > 1:
            for center, direction in (((310, 682), -1), ((490, 682), 1)):
                self.pygame.draw.circle(self.screen, (6, 35, 47), center, 31)
                offset = 6 * direction
                points = [
                    (center[0] - offset, center[1] - 10),
                    (center[0] + offset, center[1]),
                    (center[0] - offset, center[1] + 10),
                ]
                self.pygame.draw.lines(
                    self.screen,
                    (125, 220, 246),
                    False,
                    points,
                    3,
                )
            if self.gallery_page_transition_active:
                _raw, eased = self.gallery_page_transition_progress(now)
                old_page = self.font_small.render(
                    f"{self.gallery_page_transition_from + 1} / {page_count}",
                    True,
                    (155, 203, 218),
                )
                new_page = self.font_small.render(
                    f"{self.gallery_page_transition_to + 1} / {page_count}",
                    True,
                    (155, 203, 218),
                )
                old_page.set_alpha(round(255 * (1.0 - eased)))
                new_page.set_alpha(round(255 * eased))
                self.screen.blit(old_page, old_page.get_rect(center=(400, 682)))
                self.screen.blit(new_page, new_page.get_rect(center=(400, 682)))
            else:
                page = self.font_small.render(
                    f"{self.gallery_page + 1} / {page_count}",
                    True,
                    (155, 203, 218),
                )
                self.screen.blit(page, page.get_rect(center=(400, 682)))

        if (
            self.pointer_down
            and self.camera_pointer_target == "gallery_ui"
            and self.gallery_pointer_control is not None
            and self.gallery_pointer_control[0] == "photo"
            and not self.pointer_moved
            and not self.gallery_long_press_triggered
            and self.gallery_delete_candidate is None
        ):
            self.draw_hold_progress(now, self.gallery_long_press_seconds)
        self.draw_gallery_notice(now)
        if self.gallery_delete_candidate is not None:
            self.draw_gallery_delete_prompt()

    def gallery_control_at(self, position: tuple[int, int]) -> tuple[str, Path | None] | None:
        if self.gallery_page_transition_active:
            return None
        if math.dist(position, self.gallery_back_button_center()) <= 48:
            return "back", None
        if self.gallery_selected is not None:
            return None
        for path, rect in self.gallery_thumbnail_layout():
            if rect.inflate(12, 12).collidepoint(position):
                return "photo", path
        if self.gallery_page_count() > 1:
            if math.dist(position, (310, 682)) <= 43:
                return "previous", None
            if math.dist(position, (490, 682)) <= 43:
                return "next", None
        return None

    def handle_gallery_control(self, position: tuple[int, int]) -> None:
        if self.gallery_delete_candidate is not None:
            action = self.gallery_delete_control_at(position)
            if action == "delete":
                self.delete_gallery_photo(self.gallery_delete_candidate)
            elif action == "screensaver":
                self.set_screensaver_photo(self.gallery_delete_candidate)
            else:
                self.gallery_delete_candidate = None
                self.needs_redraw = True
                self.write_state()
                log("camera gallery delete cancelled")
            return
        control = self.gallery_control_at(position)
        if control is None:
            return
        action, path = control
        if action == "back":
            self.exit_gallery()
        elif action == "photo" and path is not None:
            self.gallery_selected = path
            self.needs_redraw = True
            self.write_state()
            log(f"camera gallery photo opened path={path}")
        elif action == "previous":
            self.start_gallery_page_transition(-1)
        elif action == "next":
            self.start_gallery_page_transition(1)

    def delete_gallery_photo(self, path: Path) -> None:
        now = time.monotonic()
        try:
            gallery_root = self.camera_gallery_dir.resolve(strict=True)
            target = path.resolve(strict=True)
            if target.parent != gallery_root or target not in {
                entry.resolve() for entry in self.gallery_entries
            }:
                raise ValueError("photo is outside the managed gallery")
            trash_directory = gallery_root / ".trash"
            trash_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            trash_target = trash_directory / target.name
            if trash_target.exists():
                trash_target = trash_directory / (
                    f"{target.stem}_{time.time_ns()}{target.suffix}"
                )
            os.replace(target, trash_target)
            self.gallery_surface_cache = OrderedDict(
                (key, surface)
                for key, surface in self.gallery_surface_cache.items()
                if key[0] != str(target)
            )
            if self.camera_last_photo is not None:
                try:
                    if self.camera_last_photo.resolve() == target:
                        self.camera_last_photo = None
                except OSError:
                    self.camera_last_photo = None
            if self.gallery_selected is not None:
                try:
                    if self.gallery_selected.resolve() == target:
                        self.gallery_selected = None
                except OSError:
                    self.gallery_selected = None
            if self.screensaver_photo is not None:
                try:
                    if self.screensaver_photo.resolve() == target:
                        self.screensaver_photo = None
                        self.screensaver_current_photo = None
                        self.save_screensaver_preferences()
                except OSError:
                    self.screensaver_photo = None
                    self.screensaver_current_photo = None
                    self.save_screensaver_preferences()
            self.gallery_delete_candidate = None
            self.refresh_gallery()
            self.gallery_notice = "照片已删除"
            self.gallery_notice_until = now + 1.4
            log(
                "camera gallery photo moved to trash "
                f"path={target} trash={trash_target}"
            )
        except (OSError, ValueError) as exc:
            self.gallery_delete_candidate = None
            self.gallery_notice = "删除失败"
            self.gallery_notice_until = now + 2.0
            log(f"camera gallery delete warning: {exc}")
        self.needs_redraw = True
        self.write_state()

    def handle_gallery_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> bool:
        if (
            self.gallery_delete_candidate is not None
            or self.gallery_page_transition_active
        ):
            return False
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if (
            abs(dx) < self.gallery_swipe_threshold_px
            or abs(dx) < abs(dy) * 1.2
        ):
            return False
        direction = 1 if dx < 0 else -1
        if self.gallery_selected is not None:
            if len(self.gallery_entries) <= 1:
                return True
            try:
                index = self.gallery_entries.index(self.gallery_selected)
            except ValueError:
                self.refresh_gallery()
                index = 0
            index = (index + direction) % len(self.gallery_entries)
            self.gallery_selected = self.gallery_entries[index]
            self.gallery_page = index // self.camera_gallery_page_size
            log(
                "camera gallery photo swiped "
                f"index={index + 1}/{len(self.gallery_entries)}"
            )
        else:
            page_count = self.gallery_page_count()
            if page_count <= 1:
                return True
            self.start_gallery_page_transition(direction)
            return True
        self.needs_redraw = True
        self.write_state()
        return True

    def save_runtime_screenshot(self, name: str = "display-preview") -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-.")
        if not safe_name:
            safe_name = "display-preview"
        path = RUNTIME_DIR / f"{safe_name}.png"
        self.pygame.image.save(self.screen, str(path))
        log(f"runtime screenshot saved path={path}")
        return path

    def set_speech_bubble(
        self,
        active: bool,
        text: str = "",
        stable_chars: int = 0,
        final: bool = False,
        ttl: float = 0.0,
    ) -> None:
        """Update the in-memory wake-session caption without persisting its text."""
        now = time.monotonic()
        value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
        value = re.sub(r"\s+", " ", value).strip()[:180]
        if active:
            self.speech_bubble_active = True
            self.speech_bubble_text = value
            self.speech_bubble_stable_chars = max(
                0,
                min(int(stable_chars), len(value)),
            )
            self.speech_bubble_final = bool(final)
            self.speech_bubble_expires_at = (
                now + max(0.0, min(float(ttl), 5.0))
                if final and ttl > 0
                else 0.0
            )
            self.speech_bubble_hide_started_at = 0.0
            self.note_screensaver_activity(now)
        elif self.speech_bubble_active or self.speech_bubble_hide_started_at:
            self.speech_bubble_active = False
            self.speech_bubble_expires_at = 0.0
            self.speech_bubble_hide_started_at = now
        else:
            self.speech_bubble_text = ""
            self.speech_bubble_stable_chars = 0
            self.speech_bubble_final = False
        self.needs_redraw = True
        log(
            "speech bubble updated "
            f"active={active} final={final} chars={len(value)}"
        )

    def update_speech_bubble(self, now: float) -> bool:
        """Advance final-hold and fade state; return True when state changed."""
        changed = False
        if (
            self.speech_bubble_active
            and self.speech_bubble_expires_at
            and now >= self.speech_bubble_expires_at
        ):
            self.speech_bubble_active = False
            self.speech_bubble_expires_at = 0.0
            self.speech_bubble_hide_started_at = now
            changed = True
        if (
            self.speech_bubble_hide_started_at
            and now - self.speech_bubble_hide_started_at
            >= self.speech_bubble_hide_seconds
        ):
            self.speech_bubble_hide_started_at = 0.0
            self.speech_bubble_text = ""
            self.speech_bubble_stable_chars = 0
            self.speech_bubble_final = False
            changed = True
        if changed:
            self.needs_redraw = True
        return changed

    def speech_bubble_opacity(self, now: float) -> float:
        if self.speech_bubble_active:
            return 1.0
        if not self.speech_bubble_hide_started_at:
            return 0.0
        elapsed = max(0.0, now - self.speech_bubble_hide_started_at)
        return max(0.0, 1.0 - elapsed / self.speech_bubble_hide_seconds)

    def speech_bubble_lines(
        self,
        text: str,
        max_width: int,
    ) -> list[tuple[str, int]]:
        if not text:
            return []
        scale = UI_AA_SCALE
        limit = max_width * scale
        lines: list[tuple[str, int]] = []
        current = ""
        current_start = 0
        for index, character in enumerate(text):
            candidate = current + character
            if current and self.font_speech_bubble_high.size(candidate)[0] > limit:
                lines.append((current, current_start))
                current = character
                current_start = index
            else:
                current = candidate
        if current:
            lines.append((current, current_start))
        return lines[-3:]

    def speech_bubble_surface(self) -> object:
        lines = self.speech_bubble_lines(self.speech_bubble_text, 362)
        key = (
            tuple(lines),
            self.speech_bubble_stable_chars,
            self.speech_bubble_final,
        )
        cached = self.speech_bubble_cache.get(key)
        if cached is not None:
            self.speech_bubble_cache.move_to_end(key)
            return cached

        scale = UI_AA_SCALE
        width = 410
        line_height = 31
        body_height = 58 if not lines else 28 + line_height * len(lines)
        tail_height = 18
        height = body_height + tail_height
        high = self.pygame.Surface(
            (width * scale, height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        body = self.pygame.Rect(
            2 * scale,
            2 * scale,
            (width - 4) * scale,
            (body_height - 4) * scale,
        )
        shadow = body.move(0, 3 * scale)
        self.pygame.draw.rect(
            high,
            (0, 0, 0, 118),
            shadow,
            border_radius=24 * scale,
        )
        self.pygame.draw.polygon(
            high,
            (0, 0, 0, 118),
            (
                (41 * scale, (body_height - 3) * scale),
                (80 * scale, (body_height - 3) * scale),
                (31 * scale, height * scale),
            ),
        )
        self.pygame.draw.rect(
            high,
            (5, 29, 42, 242),
            body,
            border_radius=24 * scale,
        )
        self.pygame.draw.polygon(
            high,
            (5, 29, 42, 242),
            (
                (43 * scale, (body_height - 5) * scale),
                (79 * scale, (body_height - 5) * scale),
                (32 * scale, (height - 2) * scale),
            ),
        )
        self.pygame.draw.rect(
            high,
            (83, 200, 229, 112),
            body,
            width=scale,
            border_radius=24 * scale,
        )

        if lines:
            bright = (226, 245, 250)
            muted = (123, 161, 174)
            text_x = 24 * scale
            text_y = 12 * scale
            for line_index, (line, line_start) in enumerate(lines):
                stable_in_line = max(
                    0,
                    min(
                        len(line),
                        self.speech_bubble_stable_chars - line_start,
                    ),
                )
                if self.speech_bubble_final:
                    stable_in_line = len(line)
                stable_text = line[:stable_in_line]
                draft_text = line[stable_in_line:]
                cursor_x = text_x
                line_y = text_y + line_index * line_height * scale
                if stable_text:
                    rendered = self.font_speech_bubble_high.render(
                        stable_text,
                        True,
                        bright,
                    )
                    high.blit(rendered, (cursor_x, line_y))
                    cursor_x += rendered.get_width()
                if draft_text:
                    rendered = self.font_speech_bubble_high.render(
                        draft_text,
                        True,
                        muted,
                    )
                    high.blit(rendered, (cursor_x, line_y))

        surface = self.pygame.transform.smoothscale(high, (width, height))
        self.speech_bubble_cache[key] = surface
        self.speech_bubble_cache.move_to_end(key)
        while len(self.speech_bubble_cache) > self.speech_bubble_cache_limit:
            self.speech_bubble_cache.popitem(last=False)
        return surface

    def draw_speech_bubble(self, now: float) -> None:
        opacity = self.speech_bubble_opacity(now)
        if opacity <= 0.0:
            return
        surface = self.speech_bubble_surface()
        if not self.speech_bubble_text:
            surface = surface.copy()
            pulse = 0.5 + 0.5 * math.sin(now * math.tau * 1.15)
            for index in range(3):
                phase = 0.5 + 0.5 * math.sin(
                    now * math.tau * 1.15 - index * 0.75
                )
                color = (72, 215, 244, round(105 + 140 * phase))
                self.draw_aa_circle(
                    surface,
                    color,
                    (36 + index * 17, 29),
                    round(3 + 1.5 * (pulse if index == 1 else phase)),
                )
        surface.set_alpha(round(255 * opacity))
        self.screen.blit(surface, (105, 660 - surface.get_height()))
        surface.set_alpha(None)

    def draw(self) -> None:
        render_started = time.perf_counter()
        now = time.monotonic()
        if self.boot_active:
            self.screen.fill((0, 0, 0))
            self.draw_boot_animation(now)
        elif self.screensaver_active:
            self.draw_screensaver()
        elif self.settings_active:
            self.draw_settings(now)
        else:
            animation = self.current_animation()
            frame = animation.frames[self.frame_index % len(animation.frames)]
            if self.gallery_active:
                self.draw_gallery(now)
            elif self.camera_view_active and self.camera_frame_surface is not None:
                self.screen.fill((0, 0, 0))
                self.screen.blit(self.camera_frame_surface, (0, 0))
            else:
                self.screen.fill(animation.background)
                self.screen.blit(frame, (0, 0))
            if self.menu_active:
                self.draw_radial_menu(now)
            elif self.camera_view_active and not self.gallery_active:
                self.draw_camera_controls(now)
                if self.pointer_down and self.camera_pointer_target is None:
                    self.draw_hold_progress(now)
            elif now < self.status_visible_until or self.token_popup_visible:
                self.draw_status_bar(now)
                if self.token_popup_visible:
                    self.draw_token_popup()
            elif self.pointer_down and not self.gallery_active:
                self.draw_hold_progress(now)
            if (
                self.camera_indicator_active()
                and not self.menu_active
                and not self.token_popup_visible
                and now >= self.status_visible_until
            ):
                # The panel is physically circular. Keep the compact indicator on the
                # visible upper-right arc instead of placing it in a clipped corner.
                self.draw_camera_indicator(
                    now,
                    self.expression_camera_indicator_position(),
                    compact=True,
                )
        if (
            not self.boot_active
            and not self.screensaver_active
            and self.speech_bubble_opacity(now) > 0.0
        ):
            self.draw_speech_bubble(now)
        self.pygame.display.flip()
        render_elapsed_ms = (time.perf_counter() - render_started) * 1000.0
        self.frame_render_ms = (
            render_elapsed_ms
            if self.frame_render_ms <= 0
            else self.frame_render_ms * 0.88 + render_elapsed_ms * 0.12
        )
        self.frame_render_peak_ms = max(
            self.frame_render_peak_ms,
            render_elapsed_ms,
        )
        if render_elapsed_ms >= 30.0:
            log(
                "slow display frame "
                f"elapsed={render_elapsed_ms:.1f}ms "
                f"menu={self.menu_active} selected={self.menu_selected} "
                f"volume={self.volume_mode} restart={self.restart_mode} "
                f"screensaver_panel={self.screensaver_panel_mode} "
                f"status_visible={now < self.status_visible_until}"
            )
        self.last_overlay_redraw = now
        self.render_frame_count += 1
        fps_elapsed = now - self.render_fps_window_started
        if fps_elapsed >= 2.0:
            self.render_fps = self.render_frame_count / fps_elapsed
            self.render_frame_count = 0
            self.render_fps_window_started = now
        self.needs_redraw = False

    def draw_centered_text(
        self,
        text: str,
        font: object,
        color: tuple[int, int, int],
        center: tuple[int, int],
    ) -> None:
        surface = font.render(text, True, color)
        self.screen.blit(surface, surface.get_rect(center=center))

    def request_system_status_refresh(self, now: float | None = None) -> bool:
        requested_at = time.monotonic() if now is None else now
        if self.status_future is not None:
            return False
        self.status_future = self.status_executor.submit(
            SystemStatus.collect_snapshot
        )
        self.status_next_update_at = requested_at + 2.0
        self.needs_redraw = True
        return True

    @staticmethod
    def set_wifi_radio(active: bool) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["/usr/bin/nmcli", "radio", "wifi", "on" if active else "off"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=6.0,
                check=False,
            )
            if result.returncode == 0:
                return True, ""
            message = (result.stderr or result.stdout).strip()
            return False, message or "NetworkManager 拒绝了操作"
        except subprocess.TimeoutExpired:
            return False, "Wi-Fi 切换超时"
        except OSError as exc:
            return False, str(exc)

    def request_wifi_toggle(self, active: bool) -> bool:
        if self.wifi_toggle_future is not None:
            return False
        self.wifi_toggle_target = bool(active)
        self.wifi_toggle_error = ""
        self.wifi_toggle_notice_until = 0.0
        self.wifi_toggle_future = self.control_executor.submit(
            self.set_wifi_radio,
            bool(active),
        )
        self.needs_redraw = True
        self.write_state()
        log(f"wifi radio toggle requested active={bool(active)}")
        return True

    def update_wifi_toggle(self, now: float) -> bool:
        if self.wifi_toggle_future is None or not self.wifi_toggle_future.done():
            return False
        target = bool(self.wifi_toggle_target)
        try:
            success, error = self.wifi_toggle_future.result()
        except Exception as exc:
            success, error = False, str(exc)
        self.wifi_toggle_future = None
        self.wifi_toggle_target = None
        if success:
            self.system_status.wifi_enabled = target
            self.wifi_toggle_error = ""
            log(f"wifi radio toggle completed active={target}")
        else:
            self.wifi_toggle_error = error or "Wi-Fi 切换失败"
            log(f"wifi radio toggle failed error={self.wifi_toggle_error}")
        self.wifi_toggle_notice_until = now + 2.0
        self.status_next_update_at = 0.0
        self.request_system_status_refresh(now)
        self.needs_redraw = True
        return True

    def open_settings(self) -> None:
        now = time.monotonic()
        source_surface = self.screen.copy()
        animate_entry = not self.settings_active
        self.clear_settings_transition()
        self.settings_active = True
        self.settings_section = None
        self.settings_pointer_target = None
        self.settings_last_interaction_at = now
        self.status_visible_until = 0.0
        self.token_popup_visible = False
        self.note_screensaver_activity(now)
        self.request_system_status_refresh(now)
        if animate_entry:
            target_surface = self.render_settings_page_surface(None)
            self.begin_settings_transition(
                source_surface,
                target_surface,
                "expression",
                None,
                1,
            )
        self.needs_redraw = True
        self.write_state()
        log("settings page opened")

    def close_settings(self, animated: bool = True) -> None:
        if not self.settings_active:
            return
        if animated and not self.settings_transition_active:
            self.begin_settings_transition(
                self.screen.copy(),
                self.render_expression_surface(),
                self.settings_section,
                "expression",
                -1,
                exits_settings=True,
            )
            self.settings_pointer_target = None
            self.settings_last_interaction_at = time.monotonic()
            self.needs_redraw = True
            self.write_state()
            log("settings page exit transition started")
            return
        self.clear_settings_transition()
        self.settings_active = False
        self.settings_section = None
        self.settings_pointer_target = None
        self.settings_last_interaction_at = time.monotonic()
        self.note_screensaver_activity(self.settings_last_interaction_at)
        self.needs_redraw = True
        self.write_state()
        log("settings page closed")

    def clear_settings_transition(self) -> None:
        self.settings_transition_active = False
        self.settings_transition_started_at = 0.0
        self.settings_transition_direction = 0
        self.settings_transition_from_section = None
        self.settings_transition_to_section = None
        self.settings_transition_from_surface = None
        self.settings_transition_to_surface = None
        self.settings_transition_exits_settings = False

    def begin_settings_transition(
        self,
        source_surface: object,
        target_surface: object,
        source_label: str | None,
        target_label: str | None,
        direction: int,
        exits_settings: bool = False,
    ) -> None:
        self.settings_transition_active = True
        self.settings_transition_started_at = time.monotonic()
        self.settings_transition_direction = 1 if direction >= 0 else -1
        self.settings_transition_from_section = source_label
        self.settings_transition_to_section = target_label
        self.settings_transition_from_surface = source_surface
        self.settings_transition_to_surface = target_surface
        self.settings_transition_exits_settings = exits_settings
        self.settings_pointer_target = None
        self.needs_redraw = True

    def start_settings_transition(
        self,
        target_section: str | None,
        direction: int,
    ) -> bool:
        if (
            self.settings_transition_active
            or target_section == self.settings_section
        ):
            return False
        source_section = self.settings_section
        source_surface = self.screen.copy()
        target_surface = self.render_settings_page_surface(target_section)
        self.settings_section = target_section
        self.begin_settings_transition(
            source_surface,
            target_surface,
            source_section,
            target_section,
            direction,
        )
        log(
            "settings transition started "
            f"from={source_section or 'root'} "
            f"to={target_section or 'root'} "
            f"direction={self.settings_transition_direction}"
        )
        return True

    def settings_card_rects(self) -> dict[str, object]:
        left = round(self.width * 0.15)
        width = round(self.width * 0.70)
        height = round(self.height * 0.115)
        gap = round(self.height * 0.020)
        top = round(self.height * 0.2625)
        names = ("wifi", "bluetooth", "version", "system")
        return {
            name: self.pygame.Rect(left, top + index * (height + gap), width, height)
            for index, name in enumerate(names)
        }

    def settings_detail_rects(self) -> tuple[object, ...]:
        left = round(self.width * 0.15)
        width = round(self.width * 0.70)
        row_height = round(self.height * 0.105)
        gap = round(self.height * 0.018)
        top = round(self.height * 0.2625)
        return tuple(
            self.pygame.Rect(
                left,
                top + index * (row_height + gap),
                width,
                row_height,
            )
            for index in range(4)
        )

    def settings_wifi_switch_rect(self) -> object:
        first_row = self.settings_detail_rects()[0]
        switch = self.pygame.Rect(0, 0, 88, 46)
        switch.center = (first_row.right - 74, first_row.centery)
        return switch

    def settings_back_center(self) -> tuple[int, int]:
        return self.gallery_back_button_center()

    def settings_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.settings_back_center()) <= 48:
            return "back"
        if self.settings_section is None:
            for name, rect in self.settings_card_rects().items():
                if rect.collidepoint(position):
                    return name
        elif (
            self.settings_section == "wifi"
            and self.settings_wifi_switch_rect().inflate(18, 18).collidepoint(position)
        ):
            return "wifi_toggle"
        return None

    def settings_back_swipe_detected(
        self,
        position: tuple[int, int],
    ) -> bool:
        dx = position[0] - self.pointer_start[0]
        dy = position[1] - self.pointer_start[1]
        minimum_distance = max(80, round(self.width * 0.12))
        return dx >= minimum_distance and abs(dy) <= max(48, dx * 0.55)

    def handle_settings_target(self, target: str | None) -> None:
        if target is None:
            return
        now = time.monotonic()
        self.settings_last_interaction_at = now
        self.note_screensaver_activity(now)
        if target == "back":
            if self.settings_section is None:
                self.close_settings()
                return
            self.start_settings_transition(None, -1)
            log("settings detail returned to root")
        elif target == "wifi_toggle":
            current = (
                self.wifi_toggle_target
                if self.wifi_toggle_target is not None
                else self.system_status.wifi_enabled
            )
            self.request_wifi_toggle(not current)
        elif target in {"wifi", "bluetooth", "version", "system"}:
            self.start_settings_transition(target, 1)
            log(f"settings detail opened section={target}")
        self.settings_pointer_target = None
        self.needs_redraw = True
        self.write_state()

    @staticmethod
    def format_uptime(seconds: int) -> str:
        seconds = max(0, int(seconds))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分钟"
        return f"{minutes} 分钟"

    @staticmethod
    def ellipsize_text(text: str, font: object, max_width: int) -> str:
        value = str(text or "—")
        if font.size(value)[0] <= max_width:
            return value
        suffix = "…"
        while value and font.size(value + suffix)[0] > max_width:
            value = value[:-1]
        return (value + suffix) if value else suffix

    def settings_card_surface(
        self,
        size: tuple[int, int],
        pressed: bool,
        active: bool,
    ) -> object:
        key = (size, pressed, active)
        cached = self.settings_card_cache.get(key)
        if cached is not None:
            self.settings_card_cache.move_to_end(key)
            return cached
        width, height = size
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (width * scale, height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        rect = self.pygame.Rect(
            2 * scale,
            2 * scale,
            (width - 4) * scale,
            (height - 4) * scale,
        )
        fill = (17, 48, 63, 248) if pressed else (8, 29, 40, 244)
        border = (98, 215, 242, 160) if pressed else (75, 145, 167, 94)
        self.pygame.draw.rect(
            high,
            (0, 0, 0, 110),
            rect.move(0, 3 * scale),
            border_radius=28 * scale,
        )
        self.pygame.draw.rect(
            high,
            fill,
            rect,
            border_radius=28 * scale,
        )
        self.pygame.draw.rect(
            high,
            border,
            rect,
            width=scale,
            border_radius=28 * scale,
        )
        accent = (87, 224, 248, 230) if active else (93, 137, 151, 170)
        accent_rect = self.pygame.Rect(
            18 * scale,
            20 * scale,
            7 * scale,
            (height - 40) * scale,
        )
        self.pygame.draw.rect(
            high,
            accent,
            accent_rect,
            border_radius=4 * scale,
        )
        surface = self.pygame.transform.smoothscale(high, size)
        self.settings_card_cache[key] = surface
        self.settings_card_cache.move_to_end(key)
        while len(self.settings_card_cache) > self.settings_card_cache_limit:
            self.settings_card_cache.popitem(last=False)
        return surface

    def settings_wifi_switch_surface(
        self,
        enabled: bool,
        pending: bool,
        pressed: bool,
    ) -> object:
        size = (88, 46)
        key = ("wifi-switch", enabled, pending, pressed)
        cached = self.settings_card_cache.get(key)
        if cached is not None:
            self.settings_card_cache.move_to_end(key)
            return cached
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (size[0] * scale, size[1] * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        track = self.pygame.Rect(
            1 * scale,
            1 * scale,
            (size[0] - 2) * scale,
            (size[1] - 2) * scale,
        )
        if enabled:
            fill = (38, 177, 215, 230) if not pending else (44, 139, 165, 220)
            border = (102, 231, 255, 210)
            knob_x = size[0] - 23
        else:
            fill = (40, 65, 75, 230) if not pending else (44, 71, 81, 220)
            border = (102, 137, 148, 185)
            knob_x = 23
        if pressed:
            fill = tuple(min(255, channel + 16) for channel in fill[:3]) + (fill[3],)
        self.pygame.draw.rect(
            high,
            fill,
            track,
            border_radius=22 * scale,
        )
        self.pygame.draw.rect(
            high,
            border,
            track,
            width=scale,
            border_radius=22 * scale,
        )
        self.pygame.draw.circle(
            high,
            (224, 246, 251, 245),
            (knob_x * scale, size[1] * scale // 2),
            17 * scale,
        )
        surface = self.pygame.transform.smoothscale(high, size)
        self.settings_card_cache[key] = surface
        self.settings_card_cache.move_to_end(key)
        while len(self.settings_card_cache) > self.settings_card_cache_limit:
            self.settings_card_cache.popitem(last=False)
        return surface

    def draw_settings_navigation(self, title: str) -> None:
        self.draw_gallery_back_button()
        self.draw_centered_text(
            title,
            self.font_large,
            (225, 243, 248),
            (self.width // 2, 98),
        )

    def settings_root_items(self) -> tuple[tuple[str, str, str, bool], ...]:
        status = self.system_status
        wifi_active = bool(status.wifi_ssid or status.wifi_ipv4)
        if not status.wifi_enabled:
            wifi_summary = "已关闭"
        elif wifi_active:
            wifi_summary = (
                f"{status.wifi_ssid or '已连接'} · {status.wifi_quality}%"
            )
        else:
            wifi_summary = "已开启 · 未连接"
        if status.bluetooth_devices:
            bluetooth_summary = f"已连接 · {status.bluetooth_devices[0]}"
        elif status.bluetooth_powered:
            bluetooth_summary = "已开启 · 未连接设备"
        else:
            bluetooth_summary = "已关闭"
        if status.health_total_count:
            health = (
                f"自检 {status.health_healthy_count}/{status.health_total_count}"
            )
        else:
            health = "自检读取中"
        return (
            ("wifi", "Wi-Fi", wifi_summary, status.wifi_enabled),
            (
                "bluetooth",
                "蓝牙",
                bluetooth_summary,
                status.bluetooth_powered,
            ),
            (
                "version",
                "版本",
                f"RiverBank Edge · v{status.app_version}",
                True,
            ),
            ("system", "系统", f"{status.hostname} · {health}", True),
        )

    def settings_detail_rows(self, section: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        status = self.system_status
        if section == "wifi":
            connected = bool(status.wifi_ssid or status.wifi_ipv4)
            return "Wi-Fi", (
                ("状态", "已开启" if status.wifi_enabled else "已关闭"),
                ("网络", status.wifi_ssid or "—"),
                ("IPv4", status.wifi_ipv4 or "—"),
                (
                    "信号",
                    f"{status.wifi_quality}%"
                    if status.wifi_enabled and connected
                    else "—",
                ),
            )
        if section == "bluetooth":
            device_text = "、".join(status.bluetooth_devices) or "—"
            return "蓝牙", (
                ("电源", "已开启" if status.bluetooth_powered else "已关闭"),
                ("连接", f"{len(status.bluetooth_devices)} 台设备"),
                ("设备", device_text),
                ("模式", "只读状态"),
            )
        if section == "version":
            return "版本", (
                ("产品", "RiverBank Edge"),
                ("版本", f"v{status.app_version}"),
                ("系统", status.os_name),
                ("内核", status.kernel_version or "—"),
            )
        health = (
            f"{status.health_healthy_count}/{status.health_total_count} 正常"
            if status.health_total_count
            else "读取中"
        )
        return "系统", (
            ("主机", status.hostname),
            ("运行", self.format_uptime(status.uptime_seconds)),
            ("自检", health),
            ("界面", f"{self.render_fps:.1f} FPS" if self.render_fps else "启动中"),
        )

    def render_settings_page_surface(self, section: str | None) -> object:
        original_screen = self.screen
        original_section = self.settings_section
        original_pointer_down = self.pointer_down
        original_pointer_target = self.settings_pointer_target
        surface = self.pygame.Surface(self.target_size)
        try:
            self.screen = surface
            self.settings_section = section
            self.pointer_down = False
            self.settings_pointer_target = None
            self.draw_settings_page(time.monotonic())
        finally:
            self.screen = original_screen
            self.settings_section = original_section
            self.pointer_down = original_pointer_down
            self.settings_pointer_target = original_pointer_target
        return surface

    def render_expression_surface(self) -> object:
        animation = self.current_animation()
        frame = animation.frames[self.frame_index % len(animation.frames)]
        surface = self.pygame.Surface(self.target_size)
        surface.fill(animation.background)
        surface.blit(frame, (0, 0))
        return surface

    def settings_background_surface(self) -> object:
        if self.settings_background_cache is None:
            background = self.pygame.Surface(self.target_size)
            background.fill((0, 0, 0))
            self.draw_aa_ring(
                background,
                (42, 120, 148, 32),
                (self.width // 2, self.height // 2),
                min(self.width, self.height) // 2 - 10,
                width=3,
            )
            self.settings_background_cache = background
        return self.settings_background_cache

    def draw_settings(self, now: float) -> None:
        if not self.settings_transition_active:
            self.draw_settings_page(now)
            return
        source = self.settings_transition_from_surface
        target = self.settings_transition_to_surface
        if source is None or target is None:
            self.clear_settings_transition()
            self.draw_settings_page(now)
            return
        raw = min(
            1.0,
            max(
                0.0,
                (now - self.settings_transition_started_at)
                / self.settings_transition_seconds,
            ),
        )
        if raw >= 1.0:
            exits_settings = self.settings_transition_exits_settings
            source_label = self.settings_transition_from_section
            target_label = self.settings_transition_to_section
            self.screen.blit(target, (0, 0))
            self.clear_settings_transition()
            if exits_settings:
                self.settings_active = False
                self.settings_section = None
                self.settings_pointer_target = None
                self.settings_last_interaction_at = now
                self.note_screensaver_activity(now)
                self.write_state()
                log("settings page exit transition completed")
            else:
                self.write_state()
                log(
                    "settings transition completed "
                    f"from={source_label or 'root'} "
                    f"to={target_label or 'root'}"
                )
            return
        eased = raw * raw * (3.0 - 2.0 * raw)
        travel = self.width
        direction = self.settings_transition_direction
        source_x = -round(direction * travel * eased)
        target_x = round(direction * travel * (1.0 - eased))
        self.screen.fill((0, 0, 0))
        self.screen.blit(source, (source_x, 0))
        self.screen.blit(target, (target_x, 0))

    def draw_settings_page(self, _now: float) -> None:
        self.screen.blit(self.settings_background_surface(), (0, 0))
        if self.settings_section is None:
            self.draw_settings_navigation("设置")
            rects = self.settings_card_rects()
            for name, label, summary, active in self.settings_root_items():
                rect = rects[name]
                pressed = (
                    self.pointer_down and self.settings_pointer_target == name
                )
                self.screen.blit(
                    self.settings_card_surface(rect.size, pressed, active),
                    rect.topleft,
                )
                label_surface = self.font_medium.render(
                    label,
                    True,
                    (218, 239, 245),
                )
                summary_surface = self.font_small.render(
                    self.ellipsize_text(summary, self.font_small, rect.width - 150),
                    True,
                    (117, 180, 198) if active else (126, 143, 150),
                )
                self.screen.blit(label_surface, (rect.left + 48, rect.top + 17))
                self.screen.blit(summary_surface, (rect.left + 48, rect.top + 58))
                self.draw_centered_text(
                    ">",
                    self.font_medium,
                    (103, 173, 191),
                    (rect.right - 42, rect.centery),
                )
            return

        title, rows = self.settings_detail_rows(self.settings_section)
        self.draw_settings_navigation(title)
        for index, ((label, value), rect) in enumerate(
            zip(rows, self.settings_detail_rects())
        ):
            self.screen.blit(
                self.settings_card_surface(rect.size, False, True),
                rect.topleft,
            )
            label_surface = self.font_small.render(label, True, (111, 177, 195))
            self.screen.blit(label_surface, (rect.left + 48, rect.centery - 12))
            if self.settings_section == "wifi" and index == 0:
                enabled = (
                    self.wifi_toggle_target
                    if self.wifi_toggle_target is not None
                    else self.system_status.wifi_enabled
                )
                pending = self.wifi_toggle_future is not None
                pressed = (
                    self.pointer_down
                    and self.settings_pointer_target == "wifi_toggle"
                )
                switch_rect = self.settings_wifi_switch_rect()
                self.screen.blit(
                    self.settings_wifi_switch_surface(enabled, pending, pressed),
                    switch_rect.topleft,
                )
            else:
                value_surface = self.font_medium.render(
                    self.ellipsize_text(value, self.font_medium, rect.width - 185),
                    True,
                    (218, 239, 245),
                )
                self.screen.blit(
                    value_surface,
                    value_surface.get_rect(midright=(rect.right - 38, rect.centery)),
                )
        if (
            self.settings_section == "wifi"
            and self.wifi_toggle_error
            and time.monotonic() < self.wifi_toggle_notice_until
        ):
            self.draw_centered_text(
                "切换失败，请检查网络权限",
                self.font_small,
                (224, 139, 145),
                (self.width // 2, round(self.height * 0.80)),
            )
        self.draw_centered_text(
            "右滑或点击左上角返回",
            self.font_small,
            (91, 139, 152),
            (self.width // 2, round(self.height * 0.86)),
        )

    def draw_aa_circle(
        self,
        target: object,
        color: tuple[int, ...],
        center: tuple[int, int],
        radius: int,
    ) -> None:
        """Draw a circle with true supersampled edge coverage."""
        radius = max(1, round(radius))
        padding = 2
        native_size = (radius + padding) * 2
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_size * scale, native_size * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        high_center = (native_size * scale // 2, native_size * scale // 2)
        self.pygame.draw.circle(high, color, high_center, radius * scale)
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_size, native_size),
        )
        target.blit(
            smooth,
            (
                round(center[0]) - native_size // 2,
                round(center[1]) - native_size // 2,
            ),
        )

    def draw_aa_ring(
        self,
        target: object,
        color: tuple[int, ...],
        center: tuple[int, int],
        radius: int,
        width: int,
        start_angle: float = 0.0,
        end_angle: float = math.tau,
    ) -> None:
        """Draw a full or partial circular stroke with 4x antialiasing."""
        padding = max(3, width + 1)
        native_size = (radius + padding) * 2
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_size * scale, native_size * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        rect = self.pygame.Rect(
            padding * scale,
            padding * scale,
            radius * 2 * scale,
            radius * 2 * scale,
        )
        if end_angle - start_angle >= math.tau - 0.001:
            self.pygame.draw.circle(
                high,
                color,
                (native_size * scale // 2, native_size * scale // 2),
                radius * scale,
                width=max(1, width * scale),
            )
        else:
            self.pygame.draw.arc(
                high,
                color,
                rect,
                start_angle,
                end_angle,
                width=max(1, width * scale),
            )
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_size, native_size),
        )
        target.blit(
            smooth,
            (
                round(center[0]) - native_size // 2,
                round(center[1]) - native_size // 2,
            ),
        )

    def draw_aa_polygon(
        self,
        target: object,
        color: tuple[int, ...],
        points: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    ) -> None:
        """Fill a polygon on a tight 4x surface and downsample its edges."""
        if len(points) < 3:
            return
        padding = 3
        left = max(0, math.floor(min(point[0] for point in points) - padding))
        top = max(0, math.floor(min(point[1] for point in points) - padding))
        right = min(
            target.get_width(),
            math.ceil(max(point[0] for point in points) + padding),
        )
        bottom = min(
            target.get_height(),
            math.ceil(max(point[1] for point in points) + padding),
        )
        native_width = max(1, right - left)
        native_height = max(1, bottom - top)
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_width * scale, native_height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        high_points = [
            (round((x - left) * scale), round((y - top) * scale))
            for x, y in points
        ]
        self.pygame.draw.polygon(high, color, high_points)
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_width, native_height),
        )
        target.blit(smooth, (left, top))

    def arc_native_bounds(
        self,
        center: tuple[int, int],
        inner_radius: float,
        outer_radius: float,
        start_angle: float,
        end_angle: float,
        expansion: float = 0.0,
        canvas_size: tuple[int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        """Tight native-pixel bounds for a rounded annular arc."""
        span_steps = max(24, math.ceil(abs(end_angle - start_angle) * 96))
        angles = (
            start_angle + (end_angle - start_angle) * index / span_steps
            for index in range(span_steps + 1)
        )
        sample = list(angles)
        radius = outer_radius + expansion
        cap_radius = (outer_radius - inner_radius) / 2 + expansion
        middle_radius = (inner_radius + outer_radius) / 2
        xs = [center[0] + math.cos(angle) * radius for angle in sample]
        ys = [center[1] + math.sin(angle) * radius for angle in sample]
        for angle in (start_angle, end_angle):
            cap_x = center[0] + math.cos(angle) * middle_radius
            cap_y = center[1] + math.sin(angle) * middle_radius
            xs.extend((cap_x - cap_radius, cap_x + cap_radius))
            ys.extend((cap_y - cap_radius, cap_y + cap_radius))
        padding = 3
        canvas_width, canvas_height = canvas_size or (self.width, self.height)
        left = max(0, math.floor(min(xs) - padding))
        top = max(0, math.floor(min(ys) - padding))
        right = min(canvas_width, math.ceil(max(xs) + padding))
        bottom = min(canvas_height, math.ceil(max(ys) + padding))
        return left, top, max(left + 1, right), max(top + 1, bottom)

    def draw_aa_gradient_arc(
        self,
        target: object,
        center: tuple[int, int],
        inner_radius: float,
        outer_radius: float,
        start_angle: float,
        end_angle: float,
        start_color: tuple[int, int, int, int],
        end_color: tuple[int, int, int, int],
    ) -> None:
        """Render a continuous-looking angular gradient at 4x resolution."""
        if end_angle <= start_angle:
            return
        left, top, right, bottom = self.arc_native_bounds(
            center,
            inner_radius,
            outer_radius,
            start_angle,
            end_angle,
            expansion=2,
        )
        native_width = right - left
        native_height = bottom - top
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_width * scale, native_height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        high_center = (
            round((center[0] - left) * scale),
            round((center[1] - top) * scale),
        )
        angular_steps = max(
            8,
            math.ceil(abs(math.degrees(end_angle - start_angle)) * 4),
        )
        for index in range(angular_steps):
            ratio = index / max(angular_steps - 1, 1)
            color = tuple(
                round(start_color[channel] + (end_color[channel] - start_color[channel]) * ratio)
                for channel in range(4)
            )
            part_start = start_angle + (end_angle - start_angle) * index / angular_steps
            part_end = start_angle + (end_angle - start_angle) * (index + 1) / angular_steps
            points = self.annular_sector_points(
                high_center,
                inner_radius * scale,
                outer_radius * scale,
                part_start,
                part_end,
                steps=2,
            )
            self.pygame.draw.polygon(high, color, points)
        middle_radius = (inner_radius + outer_radius) / 2
        cap_radius = max(1, round((outer_radius - inner_radius) * scale / 2))
        for angle, color in (
            (start_angle, start_color),
            (end_angle, end_color),
        ):
            cap_center = (
                round(high_center[0] + math.cos(angle) * middle_radius * scale),
                round(high_center[1] + math.sin(angle) * middle_radius * scale),
            )
            self.pygame.draw.circle(high, color, cap_center, cap_radius)
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_width, native_height),
        )
        target.blit(smooth, (left, top))

    def draw_camera_indicator(
        self,
        now: float,
        center: tuple[int, int],
        compact: bool = False,
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        period = 3.2
        wave = 0.5 + 0.5 * math.cos(2.0 * math.pi * (now % period) / period)
        brightness = round(70 + 185 * wave)
        radius = (5 if compact else 7) * scale
        glow = round(22 + 35 * wave)
        if scale > 1:
            self.pygame.draw.circle(
                canvas,
                (8, glow, 26),
                center,
                radius + 7 * scale,
            )
            self.pygame.draw.circle(canvas, (35, brightness, 100), center, radius)
        else:
            self.draw_aa_circle(canvas, (8, glow, 26), center, radius + 7)
            self.draw_aa_circle(canvas, (35, brightness, 100), center, radius)

    def camera_indicator_sources(self) -> tuple[str, ...]:
        sources = list(self.system_status.camera_active_sources)
        sources.extend(self.runtime_vision_sources)
        if self.camera_view_active and not self.gallery_active:
            sources.append("screen_visual_mode")
        return tuple(dict.fromkeys(sources))

    def camera_indicator_active(self) -> bool:
        return bool(self.camera_indicator_sources())

    def set_vision_activity(
        self,
        source: str,
        active: bool,
        ttl_seconds: float = 180.0,
    ) -> dict:
        source = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(source)).strip("_")
        if not source:
            return {"ok": False, "error": "vision activity source is required"}
        if active:
            ttl_seconds = max(0.5, min(float(ttl_seconds), 300.0))
            self.runtime_vision_sources[source] = time.monotonic() + ttl_seconds
        else:
            self.runtime_vision_sources.pop(source, None)
        self.needs_redraw = True
        self.write_state()
        log(f"vision activity source={source} active={active}")
        return {
            "ok": True,
            "source": source,
            "active": active,
            "camera_indicator_active": self.camera_indicator_active(),
        }

    def prune_vision_activity(self, now: float) -> None:
        expired = [
            source
            for source, deadline in self.runtime_vision_sources.items()
            if now >= deadline
        ]
        if not expired:
            return
        for source in expired:
            self.runtime_vision_sources.pop(source, None)
        self.needs_redraw = True
        self.write_state()
        log(f"vision activity expired sources={expired}")

    def expression_camera_indicator_position(self) -> tuple[int, int]:
        display_radius = min(self.width, self.height) / 2.0
        safe_radius = max(0.0, display_radius - self.camera_indicator_edge_inset_px)
        angle = math.radians(self.camera_indicator_angle_degrees)
        return (
            round(self.width / 2.0 + math.cos(angle) * safe_radius),
            round(self.height / 2.0 + math.sin(angle) * safe_radius),
        )

    def draw_wifi_icon(
        self,
        origin: tuple[int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = origin
        quality = self.system_status.wifi_quality
        active_bars = 0 if quality <= 0 else max(1, min(4, math.ceil(quality / 25)))
        for index in range(4):
            height = (7 + index * 6) * scale
            rect = self.pygame.Rect(
                x + index * 10 * scale,
                y + 24 * scale - height,
                7 * scale,
                height,
            )
            color = (105, 220, 255) if index < active_bars else (65, 78, 88)
            self.pygame.draw.rect(canvas, color, rect, border_radius=2 * scale)

    def draw_bluetooth_icon(
        self,
        center: tuple[int, int],
        color: tuple[int, int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = center
        self.pygame.draw.line(
            canvas,
            color,
            (x, y - 14 * scale),
            (x, y + 14 * scale),
            width=2 * scale,
        )
        self.pygame.draw.lines(
            canvas,
            color,
            False,
            (
                (x, y - 14 * scale),
                (x + 9 * scale, y - 6 * scale),
                (x - 7 * scale, y + 5 * scale),
            ),
            width=2 * scale,
        )
        self.pygame.draw.lines(
            canvas,
            color,
            False,
            (
                (x - 7 * scale, y - 5 * scale),
                (x + 9 * scale, y + 6 * scale),
                (x, y + 14 * scale),
            ),
            width=2 * scale,
        )

    def draw_speaker_icon(
        self,
        center: tuple[int, int],
        color: tuple[int, int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = center
        self.pygame.draw.rect(
            canvas,
            color,
            self.pygame.Rect(
                x - 11 * scale,
                y - 5 * scale,
                7 * scale,
                10 * scale,
            ),
            border_radius=2 * scale,
        )
        self.pygame.draw.polygon(
            canvas,
            color,
            (
                (x - 4 * scale, y - 6 * scale),
                (x + 4 * scale, y - 12 * scale),
                (x + 4 * scale, y + 12 * scale),
                (x - 4 * scale, y + 6 * scale),
            ),
        )
        arc = self.pygame.Rect(
            x - 2 * scale,
            y - 13 * scale,
            24 * scale,
            26 * scale,
        )
        self.pygame.draw.arc(
            canvas,
            color,
            arc,
            -math.pi / 3,
            math.pi / 3,
            width=2 * scale,
        )

    def draw_restart_icon(
        self,
        center: tuple[int, int],
        color: tuple[int, int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = center
        arc = self.pygame.Rect(
            x - 12 * scale,
            y - 12 * scale,
            24 * scale,
            24 * scale,
        )
        self.pygame.draw.arc(
            canvas,
            color,
            arc,
            math.radians(-55),
            math.radians(250),
            width=3 * scale,
        )
        tip = (
            (x + 12 * scale, y - 8 * scale),
            (x + 3 * scale, y - 8 * scale),
            (x + 10 * scale, y + 1 * scale),
        )
        self.pygame.draw.polygon(canvas, color, tip)

    def draw_screensaver_icon(
        self,
        center: tuple[int, int],
        color: tuple[int, int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = center
        frame = self.pygame.Rect(
            x - 13 * scale,
            y - 10 * scale,
            26 * scale,
            20 * scale,
        )
        self.pygame.draw.rect(
            canvas,
            color,
            frame,
            width=2 * scale,
            border_radius=5 * scale,
        )
        self.pygame.draw.circle(
            canvas,
            color,
            (x + 6 * scale, y - 4 * scale),
            2 * scale,
        )
        self.pygame.draw.lines(
            canvas,
            color,
            False,
            (
                (x - 9 * scale, y + 6 * scale),
                (x - 3 * scale, y),
                (x + 2 * scale, y + 4 * scale),
                (x + 7 * scale, y - scale),
                (x + 11 * scale, y + 5 * scale),
            ),
            width=2 * scale,
        )

    def draw_token_icon(
        self,
        center: tuple[int, int],
        color: tuple[int, int, int],
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        canvas = target or self.screen
        x, y = center
        points = (
            (x, y - 13 * scale),
            (x + 11 * scale, y - 7 * scale),
            (x + 11 * scale, y + 7 * scale),
            (x, y + 13 * scale),
            (x - 11 * scale, y + 7 * scale),
            (x - 11 * scale, y - 7 * scale),
        )
        self.pygame.draw.polygon(canvas, color, points, width=2 * scale)
        self.pygame.draw.circle(canvas, color, (x, y), 3 * scale)

    def top_status_geometry(
        self,
    ) -> tuple[tuple[int, int], float, float, float, float]:
        display_center = (self.width // 2, self.height // 2)
        display_radius = min(self.width, self.height) / 2
        inner_radius = display_radius - 80
        outer_radius = display_radius - 22
        start_angle = math.radians(-158)
        end_angle = math.radians(-22)
        return display_center, inner_radius, outer_radius, start_angle, end_angle

    def draw_capsule_arc(
        self,
        target: object,
        center: tuple[int, int],
        inner_radius: float,
        outer_radius: float,
        start_angle: float,
        end_angle: float,
        fill: tuple[int, ...],
        border: tuple[int, ...] | None = None,
        border_width: int = 0,
        steps: int = 56,
        soft_edge: bool = True,
    ) -> None:
        """Draw a rounded annular bar on a 4x surface, then downsample."""

        maximum_expansion = self.ui_edge_softness if soft_edge else 0
        left, top, right, bottom = self.arc_native_bounds(
            center,
            inner_radius,
            outer_radius,
            start_angle,
            end_angle,
            expansion=maximum_expansion,
            canvas_size=target.get_size(),
        )
        native_width = right - left
        native_height = bottom - top
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_width * scale, native_height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        high_center = (
            round((center[0] - left) * scale),
            round((center[1] - top) * scale),
        )

        def draw_shape(
            shape_inner: float,
            shape_outer: float,
            color: tuple[int, ...],
        ) -> None:
            points = self.annular_sector_points(
                high_center,
                shape_inner * scale,
                shape_outer * scale,
                start_angle,
                end_angle,
                steps=max(steps * 2, 48),
            )
            self.pygame.draw.polygon(high, color, points)
            middle_radius = (shape_inner + shape_outer) / 2
            cap_radius = max(
                1,
                round((shape_outer - shape_inner) * scale / 2),
            )
            for angle in (start_angle, end_angle):
                cap_center = (
                    round(high_center[0] + math.cos(angle) * middle_radius * scale),
                    round(high_center[1] + math.sin(angle) * middle_radius * scale),
                )
                self.pygame.draw.circle(high, color, cap_center, cap_radius)

        if soft_edge:
            glow_source = border or fill
            glow_rgb = tuple(glow_source[:3])
            softness = self.ui_edge_softness
            glow_layers = (
                (softness, 14),
                (max(2, round(softness * 2 / 3)), 22),
                (max(1, round(softness / 3)), 34),
            )
            for expansion, alpha in glow_layers:
                draw_shape(
                    max(1.0, inner_radius - expansion),
                    outer_radius + expansion,
                    (*glow_rgb, alpha),
                )

        if border is not None and border_width > 0:
            draw_shape(inner_radius, outer_radius, border)
            draw_shape(
                inner_radius + border_width,
                outer_radius - border_width,
                fill,
            )
        else:
            draw_shape(inner_radius, outer_radius, fill)

        smooth = self.pygame.transform.smoothscale(
            high,
            (native_width, native_height),
        )
        target.blit(smooth, (left, top))

    def draw_oriented_status_item(
        self,
        now: float,
        kind: str,
        angle: float,
        radius: float,
        target: object | None = None,
        camera_active_override: bool | None = None,
    ) -> None:
        specs = {
            "wifi": (116, 52),
            "bluetooth": (100, 52),
            "screensaver": (100, 52),
            "token": (112, 52),
            "volume": (116, 52),
            "restart": (94, 52),
            "camera": (100, 52),
        }
        item_size = specs[kind]
        scale = UI_AA_SCALE
        high_item_size = (item_size[0] * scale, item_size[1] * scale)
        item = self.pygame.Surface(high_item_size, self.pygame.SRCALPHA)
        item.fill((0, 0, 0, 0))
        middle_x = high_item_size[0] // 2
        middle_y = high_item_size[1] // 2
        if kind == "wifi":
            self.draw_wifi_icon(
                (middle_x - 18 * scale, middle_y - 13 * scale),
                target=item,
                scale=scale,
            )
        elif kind == "bluetooth":
            color = (
                (112, 241, 166)
                if self.system_status.bluetooth_connected
                else (88, 163, 193)
            )
            self.draw_bluetooth_icon(
                (middle_x, middle_y),
                color,
                target=item,
                scale=scale,
            )
        elif kind == "token":
            self.draw_token_icon(
                (middle_x, middle_y),
                (220, 231, 236),
                target=item,
                scale=scale,
            )
        elif kind == "screensaver":
            enabled = self.screensaver_idle_seconds > 0
            color = (107, 222, 250) if enabled else (91, 132, 146)
            self.draw_screensaver_icon(
                (middle_x, middle_y),
                color,
                target=item,
                scale=scale,
            )
        elif kind == "volume":
            color = (111, 218, 249)
            self.draw_speaker_icon(
                (middle_x, middle_y),
                color,
                target=item,
                scale=scale,
            )
        elif kind == "restart":
            color = (120, 215, 244)
            self.draw_restart_icon(
                (middle_x, middle_y),
                color,
                target=item,
                scale=scale,
            )
        elif kind == "camera":
            camera_active = (
                self.camera_indicator_active()
                if camera_active_override is None
                else camera_active_override
            )
            if camera_active:
                self.draw_camera_indicator(
                    now,
                    (middle_x, middle_y),
                    compact=True,
                    target=item,
                    scale=scale,
                )
            else:
                self.pygame.draw.circle(
                    item,
                    (55, 65, 68),
                    (middle_x, middle_y),
                    6 * scale,
                )

        rotation = math.degrees(angle) + 90.0
        oriented_high = self.pygame.transform.rotozoom(item, -rotation, 1.0)
        oriented = self.pygame.transform.smoothscale(
            oriented_high,
            (
                max(1, round(oriented_high.get_width() / scale)),
                max(1, round(oriented_high.get_height() / scale)),
            ),
        )
        anchor = (
            round(self.width / 2 + math.cos(angle) * radius),
            round(self.height / 2 + math.sin(angle) * radius),
        )
        canvas = target or self.screen
        canvas.blit(oriented, oriented.get_rect(center=anchor))

    def radial_menu_center(self) -> tuple[int, int]:
        return (
            self.width // 2,
            self.height // 2 + self.radial_menu_offset_px,
        )

    def draw_volume_slider_overlay(self, now: float, transition: float) -> None:
        display_center, inner_radius, outer_radius, start_angle, end_angle = (
            self.top_status_geometry()
        )
        transition = max(0.0, min(transition, 1.0))
        eased = 1.0 - (1.0 - transition) ** 3
        collapse_angle = math.radians(-63)
        visible_start = collapse_angle + (start_angle - collapse_angle) * eased
        visible_end = collapse_angle + (end_angle - collapse_angle) * eased
        layer = self.overlay_surface
        volume_ratio = max(0.0, min(self.system_status.volume_percent / 100.0, 1.0))
        volume_angle = start_angle + (end_angle - start_angle) * volume_ratio
        active_end = min(volume_angle, visible_end)
        cacheable = transition >= 0.98
        cache_key = (round(self.system_status.volume_percent),)
        if (
            cacheable
            and self.volume_slider_cache_surface is not None
            and self.volume_slider_cache_key == cache_key
        ):
            self.screen.blit(self.volume_slider_cache_surface, (0, 0))
        else:
            layer.fill((0, 0, 0, 0))
            self.draw_capsule_arc(
                layer,
                display_center,
                inner_radius,
                outer_radius,
                visible_start,
                visible_end,
                (4, 18, 27, 255),
                border=(62, 161, 198, 115),
                border_width=2,
                steps=64,
            )
            if active_end > visible_start:
                self.draw_aa_gradient_arc(
                    layer,
                    display_center,
                    inner_radius,
                    outer_radius,
                    visible_start,
                    active_end,
                    (32, 116, 188, 255),
                    (72, 216, 249, 255),
                )

            if transition > 0.68 and visible_start <= volume_angle <= visible_end:
                slider_radius = (inner_radius + outer_radius) / 2
                thumb = (
                    round(display_center[0] + math.cos(volume_angle) * slider_radius),
                    round(display_center[1] + math.sin(volume_angle) * slider_radius),
                )
                self.draw_aa_circle(layer, (13, 57, 73, 220), thumb, 19)
                self.draw_aa_circle(layer, (194, 247, 255, 255), thumb, 11)
            if cacheable:
                self.volume_slider_cache_key = cache_key
                self.volume_slider_cache_surface = layer.copy()
            self.screen.blit(layer, (0, 0))

        if transition > 0.55:
            label_color = (226, 248, 255)
            chinese = self.font_small.render("音量", True, label_color)
            value = self.font_status.render(
                f"  {self.system_status.volume_percent}%", True, label_color
            )
            alpha = round(255 * min(1.0, (transition - 0.55) / 0.3))
            chinese.set_alpha(alpha)
            value.set_alpha(alpha)
            label_width = chinese.get_width() + value.get_width()
            label_left = display_center[0] - label_width // 2
            label_center_y = round(
                display_center[1] - (inner_radius + outer_radius) / 2
            )
            self.screen.blit(
                chinese,
                chinese.get_rect(midleft=(label_left, label_center_y)),
            )
            self.screen.blit(
                value,
                value.get_rect(
                    midleft=(label_left + chinese.get_width(), label_center_y)
                ),
            )

    def restart_slider_geometry(
        self,
    ) -> tuple[tuple[int, int], float, float, float, float]:
        display_center, inner_radius, outer_radius, _, _ = self.top_status_geometry()
        return (
            display_center,
            inner_radius,
            outer_radius,
            math.radians(-122),
            math.radians(-58),
        )

    def draw_restart_slider_overlay(self, now: float, transition: float) -> None:
        display_center, inner_radius, outer_radius, start_angle, end_angle = (
            self.restart_slider_geometry()
        )
        transition = max(0.0, min(transition, 1.0))
        eased = 1.0 - (1.0 - transition) ** 3
        collapse_angle = math.radians(-56)
        visible_start = collapse_angle + (start_angle - collapse_angle) * eased
        visible_end = collapse_angle + (end_angle - collapse_angle) * eased
        progress = max(0.0, min(self.restart_progress, 1.0))
        progress_angle = start_angle + (end_angle - start_angle) * progress
        active_end = min(progress_angle, visible_end)
        cache_key = (
            round(transition, 3),
            round(progress, 3),
            bool(self.restart_requested_at),
        )
        if (
            self.restart_slider_cache_surface is None
            or self.restart_slider_cache_key != cache_key
        ):
            # Capsule and circular primitives already supersample locally at 4x.
            scale = 1
            high_size = (self.width * scale, self.height * scale)
            high_layer = self.pygame.Surface(high_size, self.pygame.SRCALPHA)
            high_layer.fill((0, 0, 0, 0))
            scaled_center = (
                display_center[0] * scale,
                display_center[1] * scale,
            )
            for expansion, alpha in ((7, 14), (4, 23), (2, 34)):
                self.draw_capsule_arc(
                    high_layer,
                    scaled_center,
                    (inner_radius - expansion) * scale,
                    (outer_radius + expansion) * scale,
                    visible_start,
                    visible_end,
                    (62, 161, 198, alpha),
                    steps=96,
                    soft_edge=False,
                )
            self.draw_capsule_arc(
                high_layer,
                scaled_center,
                inner_radius * scale,
                outer_radius * scale,
                visible_start,
                visible_end,
                (4, 18, 27, 255),
                border=(62, 161, 198, 115),
                border_width=2 * scale,
                steps=96,
                soft_edge=False,
            )
            if transition > 0.68 and active_end > visible_start + math.radians(1.0):
                self.draw_capsule_arc(
                    high_layer,
                    scaled_center,
                    (inner_radius + 4) * scale,
                    (outer_radius - 4) * scale,
                    visible_start,
                    active_end,
                    (53, 184, 232, 245),
                    steps=72,
                    soft_edge=False,
                )

            if transition > 0.68:
                slider_radius = (inner_radius + outer_radius) / 2
                thumb_angle = min(max(progress_angle, visible_start), visible_end)
                thumb = (
                    round(
                        (display_center[0] + math.cos(thumb_angle) * slider_radius)
                        * scale
                    ),
                    round(
                        (display_center[1] + math.sin(thumb_angle) * slider_radius)
                        * scale
                    ),
                )
                glow_color = (
                    42,
                    188,
                    234,
                    80 if not self.restart_requested_at else 130,
                )
                self.draw_aa_circle(high_layer, glow_color, thumb, 25 * scale)
                self.draw_aa_circle(
                    high_layer, (13, 57, 73, 245), thumb, 20 * scale
                )
                self.draw_aa_circle(
                    high_layer, (205, 249, 255, 255), thumb, 12 * scale
                )
            self.restart_slider_cache_surface = high_layer.copy()
            self.restart_slider_cache_key = cache_key
        self.screen.blit(self.restart_slider_cache_surface, (0, 0))

        if transition > 0.55:
            if self.restart_requested_at:
                label_text = "正在重启"
                label_color = (226, 248, 255)
            elif now < self.restart_error_until:
                label_text = "重启失败"
                label_color = (255, 173, 159)
            else:
                label_text = "滑动以重启"
                label_color = (190, 230, 242)
            label = self.font_small.render(label_text, True, label_color)
            alpha = round(255 * min(1.0, (transition - 0.55) / 0.3))
            label.set_alpha(alpha)
            middle_angle = (start_angle + end_angle) / 2
            label_radius = inner_radius - 30
            anchor = (
                round(display_center[0] + math.cos(middle_angle) * label_radius),
                round(display_center[1] + math.sin(middle_angle) * label_radius),
            )
            rotation = math.degrees(middle_angle) + 90.0
            oriented = self.pygame.transform.rotozoom(label, -rotation, 1.0)
            self.screen.blit(oriented, oriented.get_rect(center=anchor))

    def draw_screensaver_settings_overlay(
        self,
        now: float,
        transition: float,
    ) -> None:
        del now
        display_center, inner_radius, outer_radius, start_angle, end_angle = (
            self.top_status_geometry()
        )
        transition = max(0.0, min(transition, 1.0))
        eased = 1.0 - (1.0 - transition) ** 3
        collapse_angle = math.radians(-112)
        visible_start = collapse_angle + (start_angle - collapse_angle) * eased
        visible_end = collapse_angle + (end_angle - collapse_angle) * eased
        layer = self.overlay_surface
        layer.fill((0, 0, 0, 0))
        self.draw_capsule_arc(
            layer,
            display_center,
            inner_radius,
            outer_radius,
            visible_start,
            visible_end,
            (4, 18, 27, 255),
            border=(62, 161, 198, 115),
            border_width=2,
            steps=72,
        )
        options = ((180, "3分"), (300, "5分"), (600, "10分"), (0, "关闭"))
        sector_width = (end_angle - start_angle) / len(options)
        if transition > 0.62:
            for index, (seconds, text) in enumerate(options):
                option_start = start_angle + sector_width * index
                option_end = option_start + sector_width
                center_angle = (option_start + option_end) / 2
                if self.screensaver_idle_seconds == seconds:
                    self.draw_capsule_arc(
                        layer,
                        display_center,
                        inner_radius + 5,
                        outer_radius - 5,
                        option_start + math.radians(2.2),
                        option_end - math.radians(2.2),
                        (38, 157, 199, 238),
                        steps=18,
                        soft_edge=False,
                    )
                label = self.font_status.render(
                    text,
                    True,
                    (230, 250, 255)
                    if self.screensaver_idle_seconds == seconds
                    else (154, 201, 216),
                )
                label.set_alpha(
                    round(255 * min(1.0, (transition - 0.62) / 0.25))
                )
                label_radius = (inner_radius + outer_radius) / 2
                anchor = (
                    round(
                        display_center[0]
                        + math.cos(center_angle) * label_radius
                    ),
                    round(
                        display_center[1]
                        + math.sin(center_angle) * label_radius
                    ),
                )
                rotation = math.degrees(center_angle) + 90.0
                oriented = self.pygame.transform.rotozoom(label, -rotation, 1.0)
                layer.blit(oriented, oriented.get_rect(center=anchor))
        self.screen.blit(layer, (0, 0))

    def control_overlay_key(self, mode: str, now: float) -> tuple:
        if mode == "volume":
            return (mode, round(self.system_status.volume_percent))
        if mode == "restart":
            return (
                mode,
                round(self.restart_progress, 3),
                bool(self.restart_requested_at),
                bool(now < self.restart_error_until),
            )
        if mode == "screensaver":
            return (mode, int(self.screensaver_idle_seconds))
        raise ValueError(f"unsupported control overlay mode: {mode}")

    def control_overlay_surface(
        self,
        mode: str,
        now: float,
    ) -> tuple[object, object]:
        """Render each final control panel once; transitions reuse native frames."""
        key = self.control_overlay_key(mode, now)
        cached = self.control_overlay_cache.get(key)
        if cached is not None:
            self.control_overlay_cache.move_to_end(key)
            return cached
        scratch = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        scratch.fill((0, 0, 0, 0))
        original_screen = self.screen
        self.screen = scratch
        try:
            if mode == "volume":
                self.draw_volume_slider_overlay(now, 1.0)
            elif mode == "restart":
                self.draw_restart_slider_overlay(now, 1.0)
            else:
                self.draw_screensaver_settings_overlay(now, 1.0)
        finally:
            self.screen = original_screen
        bounds = scratch.get_bounding_rect(min_alpha=1)
        cached = (scratch, bounds)
        self.control_overlay_cache[key] = cached
        self.control_overlay_cache.move_to_end(key)
        while len(self.control_overlay_cache) > self.control_overlay_cache_limit:
            self.control_overlay_cache.popitem(last=False)
        return cached

    def prewarm_control_overlays(self) -> None:
        """Build controls and all menu states before the first touch."""
        started = time.perf_counter()
        now = time.monotonic()
        for mode in ("volume", "restart", "screensaver"):
            self.control_overlay_surface(mode, now)
        for selected_index in (None, *range(len(self.radial_menu))):
            self.radial_menu_content_surface(selected_index)
        for meter in ("wifi", "token", "volume"):
            for level in range(21):
                self.status_meter_surface(meter, level)
        self.status_bar_surface(now)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log(
            "control and radial-menu transition cache ready "
            f"entries={3 + 1 + len(self.radial_menu) + 63} "
            f"elapsed={elapsed_ms:.1f}ms"
        )

    def draw_control_transition(
        self,
        mode: str,
        transition: float,
        now: float,
    ) -> None:
        """Composite a cached control panel with a refresh-synchronised ease."""
        started = time.perf_counter()
        transition = max(0.0, min(float(transition), 1.0))
        eased = transition * transition * (3.0 - 2.0 * transition)
        surface, bounds = self.control_overlay_surface(mode, now)
        if eased >= 0.999:
            self.screen.blit(surface, (0, 0))
        elif bounds.width > 0 and bounds.height > 0:
            crop = surface.subsurface(bounds)
            scale = 0.965 + 0.035 * eased
            target_size = (
                max(1, round(bounds.width * scale)),
                max(1, round(bounds.height * scale)),
            )
            frame = self.pygame.transform.smoothscale(crop, target_size)
            frame.set_alpha(round(255 * eased))
            anchor = (
                bounds.centerx,
                bounds.centery + round((1.0 - eased) * 8),
            )
            self.screen.blit(frame, frame.get_rect(center=anchor))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.transition_frame_ms = (
            elapsed_ms
            if self.transition_frame_ms <= 0
            else self.transition_frame_ms * 0.88 + elapsed_ms * 0.12
        )
        self.transition_frame_peak_ms = max(
            self.transition_frame_peak_ms,
            elapsed_ms,
        )

    def volume_transition_amount(self, now: float) -> float:
        if self.volume_mode:
            return min(
                1.0,
                max(0.0, now - self.volume_mode_started_at)
                / self.volume_transition_seconds,
            )
        if self.volume_return_started_at:
            elapsed = max(0.0, now - self.volume_return_started_at)
            if elapsed < self.volume_transition_seconds:
                return 1.0 - elapsed / self.volume_transition_seconds
            self.volume_return_started_at = 0.0
        return 0.0

    def restart_transition_amount(self, now: float) -> float:
        if self.restart_mode:
            return min(
                1.0,
                max(0.0, now - self.restart_mode_started_at)
                / self.volume_transition_seconds,
            )
        if self.restart_return_started_at:
            elapsed = max(0.0, now - self.restart_return_started_at)
            if elapsed < self.volume_transition_seconds:
                return 1.0 - elapsed / self.volume_transition_seconds
            self.restart_return_started_at = 0.0
        return 0.0

    def screensaver_panel_transition_amount(self, now: float) -> float:
        if self.screensaver_panel_mode:
            return min(
                1.0,
                max(0.0, now - self.screensaver_panel_started_at)
                / self.volume_transition_seconds,
            )
        if self.screensaver_panel_return_started_at:
            elapsed = max(0.0, now - self.screensaver_panel_return_started_at)
            if elapsed < self.volume_transition_seconds:
                return 1.0 - elapsed / self.volume_transition_seconds
            self.screensaver_panel_return_started_at = 0.0
        return 0.0

    def draw_status_meter_arc(
        self,
        target: object,
        center: tuple[int, int],
        inner_radius: float,
        start_degrees: float,
        end_degrees: float,
        progress: float,
        color: tuple[int, int, int, int],
    ) -> None:
        """Draw one reference-style thin status meter below the outer icon band."""
        start_angle = math.radians(start_degrees)
        end_angle = math.radians(end_degrees)
        progress = max(0.0, min(float(progress), 1.0))
        meter_inner = inner_radius + 7
        meter_outer = inner_radius + 13
        self.draw_capsule_arc(
            target,
            center,
            meter_inner,
            meter_outer,
            start_angle,
            end_angle,
            (23, 52, 62, 205),
            steps=22,
            soft_edge=False,
        )
        if progress <= 0.01:
            return
        progress_end = start_angle + (end_angle - start_angle) * progress
        self.draw_capsule_arc(
            target,
            center,
            meter_inner,
            meter_outer,
            start_angle,
            progress_end,
            color,
            steps=22,
            soft_edge=True,
        )

    @staticmethod
    def status_meter_spec(
        kind: str,
    ) -> tuple[float, float, tuple[int, int, int, int]]:
        specs = {
            "wifi": (-164.0, -152.0, (74, 224, 247, 242)),
            "token": (-146.0, -134.0, (232, 239, 242, 242)),
            "volume": (-128.0, -116.0, (102, 233, 148, 242)),
        }
        return specs[kind]

    def status_meter_surface(
        self,
        kind: str,
        level: int,
    ) -> tuple[object, tuple[int, int]]:
        level = max(0, min(int(level), 20))
        key = (kind, level)
        cached = self.status_meter_cache.get(key)
        if cached is not None:
            return cached
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        if level > 0:
            center, inner_radius, _outer_radius, _start, _end = (
                self.top_status_geometry()
            )
            start_degrees, end_degrees, color = self.status_meter_spec(kind)
            start_angle = math.radians(start_degrees)
            progress_end = start_angle + (
                math.radians(end_degrees) - start_angle
            ) * (level / 20.0)
            self.draw_capsule_arc(
                surface,
                center,
                inner_radius + 7,
                inner_radius + 13,
                start_angle,
                progress_end,
                color,
                steps=22,
                soft_edge=True,
            )
        bounds = surface.get_bounding_rect(min_alpha=1)
        if bounds.width and bounds.height:
            compact = surface.subsurface(bounds).copy()
            cached = (compact, bounds.topleft)
        else:
            cached = (
                self.pygame.Surface((1, 1), self.pygame.SRCALPHA),
                (0, 0),
            )
        self.status_meter_cache[key] = cached
        return cached

    def status_chrome_surface(self, now: float) -> tuple[object, tuple]:
        quality = self.system_status.wifi_quality
        active_bars = 0 if quality <= 0 else max(1, min(4, math.ceil(quality / 25)))
        key = (
            active_bars,
            bool(self.system_status.bluetooth_connected),
            bool(self.screensaver_idle_seconds > 0),
        )
        cached = self.status_chrome_cache.get(key)
        if cached is not None:
            return cached, key
        center, inner_radius, outer_radius, _start, _end = (
            self.top_status_geometry()
        )
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        for capsule_start, capsule_end in ((-170.0, -110.0), (-90.0, -12.0)):
            self.draw_capsule_arc(
                surface,
                center,
                inner_radius,
                outer_radius,
                math.radians(capsule_start),
                math.radians(capsule_end),
                (4, 11, 16, 232),
                border=(112, 137, 147, 68),
                border_width=2,
                steps=28,
                soft_edge=True,
            )
        for kind in ("wifi", "token", "volume"):
            start_degrees, end_degrees, _color = self.status_meter_spec(kind)
            self.draw_capsule_arc(
                surface,
                center,
                inner_radius + 7,
                inner_radius + 13,
                math.radians(start_degrees),
                math.radians(end_degrees),
                (23, 52, 62, 205),
                steps=22,
                soft_edge=False,
            )
        status_radius = inner_radius + (outer_radius - inner_radius) * 0.47
        for kind, angle_degrees in STATUS_ITEM_ANGLES:
            self.draw_oriented_status_item(
                now,
                kind,
                math.radians(angle_degrees),
                status_radius,
                target=surface,
                camera_active_override=False,
            )
        self.status_chrome_cache[key] = surface
        return surface, key

    def status_bar_surface(self, now: float) -> object:
        chrome, chrome_key = self.status_chrome_surface(now)
        levels = {
            "wifi": round(max(0, min(self.system_status.wifi_quality, 100)) / 5),
            "token": round(
                max(
                    0.0,
                    min(
                        self.system_status.tokens_today / self.token_meter_capacity,
                        1.0,
                    ),
                )
                * 20
            ),
            "volume": round(
                max(0, min(self.system_status.volume_percent, 100)) / 5
            ),
        }
        key = (chrome_key, levels["wifi"], levels["token"], levels["volume"])
        cached = self.status_composite_cache.get(key)
        if cached is None:
            cached = chrome.copy()
            for kind in ("wifi", "token", "volume"):
                meter, position = self.status_meter_surface(kind, levels[kind])
                cached.blit(meter, position)
            self.status_composite_cache[key] = cached
            self.status_composite_cache.move_to_end(key)
            while len(self.status_composite_cache) > self.status_composite_cache_limit:
                self.status_composite_cache.popitem(last=False)
        else:
            self.status_composite_cache.move_to_end(key)
        if not self.camera_indicator_active():
            return cached
        animated = cached.copy()
        center, inner_radius, outer_radius, _start, _end = (
            self.top_status_geometry()
        )
        status_radius = inner_radius + (outer_radius - inner_radius) * 0.47
        camera_angle = dict(STATUS_ITEM_ANGLES)["camera"]
        self.draw_oriented_status_item(
            now,
            "camera",
            math.radians(camera_angle),
            status_radius,
            target=animated,
            camera_active_override=True,
        )
        return animated

    def draw_status_bar(self, now: float) -> None:
        volume_transition = self.volume_transition_amount(now)
        restart_transition = self.restart_transition_amount(now)
        screensaver_transition = self.screensaver_panel_transition_amount(now)
        active_control: str | None = None
        control_transition = 0.0
        if volume_transition > 0:
            active_control = "volume"
            control_transition = volume_transition
        elif restart_transition > 0:
            active_control = "restart"
            control_transition = restart_transition
        elif screensaver_transition > 0:
            active_control = "screensaver"
            control_transition = screensaver_transition

        status_surface = self.status_bar_surface(now)
        if active_control is None:
            self.screen.blit(status_surface, (0, 0))
            return

        eased = control_transition * control_transition * (
            3.0 - 2.0 * control_transition
        )
        if eased < 0.999:
            faded_status = status_surface.copy()
            faded_status.set_alpha(round(255 * (1.0 - eased)))
            self.screen.blit(faded_status, (0, 0))
        self.draw_control_transition(
            active_control,
            control_transition,
            now,
        )

    def token_usage_percent(self) -> float:
        return max(
            0.0,
            self.system_status.tokens_today / max(self.token_meter_capacity, 1) * 100.0,
        )

    def request_token_balance(self, now: float) -> None:
        if self.token_balance_future is not None:
            return
        if (
            self.token_balance_result is not None
            and now - self.token_balance_last_requested_at
            < self.token_balance_cache_seconds
        ):
            return
        if (
            self.token_balance_error is not None
            and now - self.token_balance_last_requested_at < 15.0
        ):
            return
        self.token_balance_last_requested_at = now
        self.token_balance_error = None
        self.token_balance_future = self.balance_executor.submit(fetch_deepseek_balance)
        self.token_popup_cache_key = None
        self.token_popup_cache_surface = None
        self.needs_redraw = True

    def update_token_balance(self, now: float) -> bool:
        if self.token_balance_future is None or not self.token_balance_future.done():
            return False
        try:
            result = self.token_balance_future.result()
        except Exception:
            result = {"ok": False, "error": "远端暂不可用"}
        self.token_balance_future = None
        if bool(result.get("ok")):
            self.token_balance_result = result
            self.token_balance_error = None
        else:
            self.token_balance_error = str(result.get("error") or "远端暂不可用")
        self.token_popup_cache_key = None
        self.token_popup_cache_surface = None
        self.needs_redraw = True
        return True

    def begin_token_popup(self, now: float) -> None:
        self.token_popup_visible = True
        self.token_popup_last_interaction_at = now
        self.menu_selected = None
        if self.menu_active:
            self.menu_last_interaction_at = now
        else:
            self.status_visible_until = max(self.status_visible_until, now + 8.0)
        self.request_token_balance(now)
        self.needs_redraw = True
        log(
            "token usage popup opened "
            f"today={self.token_usage_percent():.1f}% "
            f"tokens={self.system_status.tokens_today}"
        )

    def dismiss_token_popup(self, now: float | None = None) -> None:
        if not self.token_popup_visible:
            return
        self.token_popup_visible = False
        self.token_popup_last_interaction_at = 0.0
        if now is not None and self.menu_active:
            self.menu_last_interaction_at = now
        self.needs_redraw = True
        log("token usage popup dismissed")

    def token_popup_surface(self) -> object:
        usage_percent = self.token_usage_percent()
        if self.token_balance_result is not None:
            currency = str(self.token_balance_result.get("currency") or "CNY")
            symbol = "¥" if currency == "CNY" else f"{currency} "
            balance_state = "ready"
            balance_text = (
                f"{symbol}{self.token_balance_result.get('total_balance', '0.00')}"
            )
        elif self.token_balance_future is not None:
            balance_state = "loading"
            balance_text = "读取中…"
        else:
            balance_state = "error"
            balance_text = self.token_balance_error or "暂不可用"
        key = (round(usage_percent, 1), balance_state, balance_text)
        if self.token_popup_cache_key == key and self.token_popup_cache_surface is not None:
            return self.token_popup_cache_surface

        width, height = 270, 112
        scale = UI_AA_SCALE
        high = self.pygame.Surface((width * scale, height * scale), self.pygame.SRCALPHA)
        high.fill((0, 0, 0, 0))
        shadow_rect = self.pygame.Rect(5 * scale, 8 * scale, (width - 10) * scale, (height - 12) * scale)
        card_rect = self.pygame.Rect(5 * scale, 4 * scale, (width - 10) * scale, (height - 12) * scale)
        self.pygame.draw.rect(
            high,
            (0, 0, 0, 105),
            shadow_rect,
            border_radius=25 * scale,
        )
        self.pygame.draw.rect(
            high,
            (5, 18, 26, 246),
            card_rect,
            border_radius=25 * scale,
        )
        self.pygame.draw.rect(
            high,
            (105, 176, 199, 92),
            card_rect,
            width=1 * scale,
            border_radius=25 * scale,
        )
        surface = self.pygame.transform.smoothscale(high, (width, height))

        label_color = (184, 218, 228)
        value_color = (104, 225, 249)
        remote_color = (160, 231, 187) if balance_state == "ready" else (157, 190, 201)
        usage_label = self.font_small.render("今日已使用", True, label_color)
        usage_value = self.font_medium.render(f"{usage_percent:.1f}%", True, value_color)
        balance_label = self.font_small.render("DeepSeek 余额", True, label_color)
        balance_value = self.font_small.render(balance_text, True, remote_color)
        surface.blit(usage_label, (18, 16))
        surface.blit(usage_value, usage_value.get_rect(midright=(width - 18, 31)))
        surface.blit(balance_label, (18, 63))
        surface.blit(balance_value, balance_value.get_rect(midright=(width - 18, 75)))
        self.token_popup_cache_key = key
        self.token_popup_cache_surface = surface
        return surface

    def draw_token_popup(self) -> None:
        popup = self.token_popup_surface()
        self.screen.blit(popup, popup.get_rect(center=self.radial_menu_center()))

    def draw_hold_progress(self, now: float, duration: float | None = None) -> None:
        elapsed = max(0.0, now - self.pointer_started_at)
        progress = min(1.0, elapsed / (duration or self.long_press_seconds))
        center = tuple(round(value) for value in self.pointer_start)
        radius = 25
        self.draw_aa_ring(self.screen, (45, 65, 72), center, radius, width=3)
        if progress > 0:
            self.draw_aa_ring(
                self.screen,
                (95, 226, 255),
                center,
                radius,
                4,
                -math.pi / 2,
                -math.pi / 2 + math.tau * progress,
            )

    def menu_item_center(self, index: int) -> tuple[int, int]:
        center = self.radial_menu_center()
        angle = -math.pi / 2 + index * math.tau / 6
        radius = min(self.width, self.height) * 0.25
        return (
            round(center[0] + math.cos(angle) * radius),
            round(center[1] + math.sin(angle) * radius),
        )

    @staticmethod
    def annular_sector_points(
        center: tuple[int, int],
        inner_radius: float,
        outer_radius: float,
        start_angle: float,
        end_angle: float,
        steps: int = 22,
    ) -> list[tuple[int, int]]:
        angles = [
            start_angle + (end_angle - start_angle) * index / steps
            for index in range(steps + 1)
        ]
        outer = [
            (
                round(center[0] + math.cos(angle) * outer_radius),
                round(center[1] + math.sin(angle) * outer_radius),
            )
            for angle in angles
        ]
        inner = [
            (
                round(center[0] + math.cos(angle) * inner_radius),
                round(center[1] + math.sin(angle) * inner_radius),
            )
            for angle in reversed(angles)
        ]
        return outer + inner

    def radial_menu_ring_surface(self, selected_index: int | None) -> object:
        """Return a cached 4x-supersampled menu ring for one selection state."""
        cached = self.radial_menu_ring_cache.get(selected_index)
        if cached is not None:
            return cached
        center = self.radial_menu_center()
        inner_radius = min(self.width, self.height) * 0.175
        outer_radius = min(self.width, self.height) * 0.325
        softness = self.ui_edge_softness
        native_extent = math.ceil(outer_radius + 16 + softness + 8)
        left = max(0, center[0] - native_extent)
        top = max(0, center[1] - native_extent)
        right = min(self.width, center[0] + native_extent)
        bottom = min(self.height, center[1] + native_extent)
        native_width = right - left
        native_height = bottom - top
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_width * scale, native_height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        high_center = (
            round((center[0] - left) * scale),
            round((center[1] - top) * scale),
        )
        for expansion, alpha in (
            (softness + 3, 12),
            (max(3, softness - 1), 20),
            (max(2, round(softness / 2)), 32),
        ):
            self.pygame.draw.circle(
                high,
                (47, 146, 181, alpha),
                high_center,
                round((outer_radius + expansion) * scale),
                width=max(1, expansion * scale),
            )
        sector_width = math.tau / 6
        for index, _item in enumerate(self.radial_menu):
            selected = index == selected_index
            middle_angle = -math.pi / 2 + index * sector_width
            start_angle = middle_angle - sector_width / 2
            end_angle = middle_angle + sector_width / 2
            item_outer_radius = outer_radius + 16 if selected else outer_radius
            points = self.annular_sector_points(
                high_center,
                inner_radius * scale,
                item_outer_radius * scale,
                start_angle,
                end_angle,
                steps=160,
            )
            fill = (
                (55, 191, 232)
                if selected
                else ((13, 36, 49) if index % 2 == 0 else (14, 39, 52))
            )
            self.pygame.draw.polygon(high, fill, points)
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_width, native_height),
        )
        surface = self.pygame.Surface(
            (self.width, self.height),
            self.pygame.SRCALPHA,
        )
        surface.fill((0, 0, 0, 0))
        surface.blit(smooth, (left, top))
        self.radial_menu_ring_cache[selected_index] = surface
        return surface

    def radial_menu_content_surface(self, selected_index: int | None) -> object:
        """Cache every expensive static 4x menu primitive per selection state."""
        cached = self.radial_menu_content_cache.get(selected_index)
        if cached is not None:
            return cached
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        center = self.radial_menu_center()
        inner_radius = min(self.width, self.height) * 0.175
        outer_radius = min(self.width, self.height) * 0.325
        surface.blit(self.radial_menu_ring_surface(selected_index), (0, 0))
        sector_width = math.tau / 6
        for index, item in enumerate(self.radial_menu):
            selected = index == selected_index
            middle_angle = -math.pi / 2 + index * sector_width
            start_angle = middle_angle - sector_width / 2
            end_angle = middle_angle + sector_width / 2
            item_outer_radius = outer_radius + 16 if selected else outer_radius
            if selected:
                text_color = (3, 30, 39)
            else:
                text_color = (205, 232, 241)

            label_radius = (inner_radius + item_outer_radius) / 2 + 5
            label_center = (
                round(center[0] + math.cos(middle_angle) * label_radius),
                round(center[1] + math.sin(middle_angle) * label_radius),
            )
            label = self.font_medium.render(
                str(item.get("label", "")),
                True,
                text_color,
            )
            surface.blit(label, label.get_rect(center=label_center))

            if selected:
                for offset, thickness in ((36, 7),):
                    self.draw_capsule_arc(
                        surface,
                        center,
                        outer_radius + offset,
                        outer_radius + offset + thickness,
                        start_angle + math.radians(2.5),
                        end_angle - math.radians(2.5),
                        (74, 211, 247, 235),
                        soft_edge=True,
                        steps=18,
                    )
                tangent = (-math.sin(middle_angle), math.cos(middle_angle))
                marker_center = (
                    center[0] + math.cos(middle_angle) * (inner_radius + 17),
                    center[1] + math.sin(middle_angle) * (inner_radius + 17),
                )
                marker = [
                    (
                        round(center[0] + math.cos(middle_angle) * (inner_radius - 2)),
                        round(center[1] + math.sin(middle_angle) * (inner_radius - 2)),
                    ),
                    (
                        round(marker_center[0] + tangent[0] * 11),
                        round(marker_center[1] + tangent[1] * 11),
                    ),
                    (
                        round(marker_center[0] - tangent[0] * 11),
                        round(marker_center[1] - tangent[1] * 11),
                    ),
                ]
                self.draw_aa_polygon(surface, (3, 25, 33), marker)

        self.draw_aa_circle(
            surface,
            (3, 12, 18),
            center,
            round(inner_radius - 3),
        )
        for width, alpha in ((13, 16), (8, 28), (3, 72)):
            self.draw_aa_ring(
                surface,
                (69, 181, 216, alpha),
                center,
                round(inner_radius),
                width,
            )
        self.radial_menu_content_cache[selected_index] = surface
        return surface

    def draw_radial_menu(self, now: float) -> None:
        if self.menu_background_surface is not None:
            self.screen.blit(self.menu_background_surface, (0, 0))
        layer = self.overlay_surface
        layer.fill((0, 0, 0, 105))
        self.screen.blit(layer, (0, 0))
        self.draw_status_bar(now)
        self.screen.blit(
            self.radial_menu_content_surface(self.menu_selected),
            (0, 0),
        )
        if self.token_popup_visible:
            self.draw_token_popup()

    def is_volume_status_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, _, _ = self.top_status_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx))
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and -131 <= angle <= -113
        )

    def is_token_status_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, _, _ = self.top_status_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx))
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and -149 <= angle <= -131
        )

    def is_restart_status_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, _, _ = self.top_status_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx))
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and -51 <= angle <= -33
        )

    def is_screensaver_status_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, _, _ = self.top_status_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx))
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and -69 <= angle <= -51
        )

    def screensaver_option_at(self, position: tuple[int, int]) -> int | None:
        center, inner_radius, outer_radius, start_angle, end_angle = (
            self.top_status_geometry()
        )
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        if not (
            inner_radius - 24 <= radius <= outer_radius + 18
            and start_angle <= angle <= end_angle
        ):
            return None
        ratio = (angle - start_angle) / (end_angle - start_angle)
        index = min(3, max(0, int(ratio * 4)))
        return (180, 300, 600, 0)[index]

    def is_volume_slider_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, start_angle, end_angle = (
            self.top_status_geometry()
        )
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and start_angle - math.radians(7) <= angle <= end_angle + math.radians(7)
        )

    def is_restart_slider_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, start_angle, end_angle = (
            self.restart_slider_geometry()
        )
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        radius = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        return (
            inner_radius - 24 <= radius <= outer_radius + 18
            and start_angle - math.radians(7) <= angle <= end_angle + math.radians(7)
        )

    def is_restart_slider_start_position(self, position: tuple[int, int]) -> bool:
        center, inner_radius, outer_radius, start_angle, _ = (
            self.restart_slider_geometry()
        )
        slider_radius = (inner_radius + outer_radius) / 2
        start = (
            center[0] + math.cos(start_angle) * slider_radius,
            center[1] + math.sin(start_angle) * slider_radius,
        )
        return math.dist(position, start) <= 38

    def restart_progress_from_position(self, position: tuple[int, int]) -> float:
        center, _, _, start_angle, end_angle = self.restart_slider_geometry()
        angle = math.atan2(position[1] - center[1], position[0] - center[0])
        ratio = (angle - start_angle) / (end_angle - start_angle)
        return max(0.0, min(ratio, 1.0))

    def volume_percent_from_position(self, position: tuple[int, int]) -> int:
        center, _, _, start_angle, end_angle = self.top_status_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        angle = math.atan2(dy, dx)
        if angle > 0:
            angle = start_angle if dx < 0 else end_angle
        ratio = (angle - start_angle) / (end_angle - start_angle)
        return max(0, min(round(ratio * 100), 100))

    def set_system_volume_from_position(
        self,
        position: tuple[int, int],
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self.volume_last_set_at < 0.055:
            return
        percent = self.volume_percent_from_position(position)
        if not force and percent == self.system_status.volume_percent:
            self.volume_last_interaction_at = now
            return
        environment = os.environ.copy()
        environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            result = subprocess.run(
                [
                    "/usr/bin/wpctl",
                    "set-volume",
                    "@DEFAULT_AUDIO_SINK@",
                    f"{percent / 100.0:.2f}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.6,
                check=False,
                env=environment,
            )
            if result.returncode == 0:
                self.system_status.volume_percent = percent
                self.volume_last_set_at = now
                self.volume_last_interaction_at = now
                self.needs_redraw = True
        except (OSError, subprocess.TimeoutExpired):
            log("volume slider warning: wpctl unavailable")

    def begin_screensaver_panel_mode(self, now: float) -> None:
        if self.menu_background_surface is None:
            self.prepare_menu_background()
        self.menu_active = True
        self.menu_selected = None
        self.volume_mode = False
        self.volume_dragging = False
        self.volume_return_started_at = 0.0
        self.restart_mode = False
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_return_started_at = 0.0
        self.screensaver_panel_mode = True
        self.screensaver_panel_started_at = now
        self.screensaver_panel_return_started_at = 0.0
        self.screensaver_panel_last_interaction_at = (
            now + self.volume_transition_seconds
        )
        self.menu_last_interaction_at = now
        self.status_visible_until = 0.0
        self.needs_redraw = True
        log("photo screensaver settings opened")

    def end_screensaver_panel_mode(self, now: float) -> None:
        self.screensaver_panel_mode = False
        self.screensaver_panel_return_started_at = now
        self.screensaver_panel_last_interaction_at = now
        self.menu_last_interaction_at = now
        self.needs_redraw = True
        log("photo screensaver settings returned")

    def begin_volume_mode(self, now: float) -> None:
        if self.menu_background_surface is None:
            self.prepare_menu_background()
        self.menu_active = True
        self.menu_selected = None
        self.restart_mode = False
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_return_started_at = 0.0
        self.screensaver_panel_mode = False
        self.screensaver_panel_return_started_at = 0.0
        self.volume_mode = True
        self.volume_mode_started_at = now
        self.volume_return_started_at = 0.0
        self.volume_last_interaction_at = now + self.volume_transition_seconds
        self.menu_last_interaction_at = now
        self.volume_dragging = False
        self.status_visible_until = 0.0
        self.needs_redraw = True
        log(f"volume slider opened volume={self.system_status.volume_percent}%")

    def end_volume_mode(self, now: float) -> None:
        self.volume_mode = False
        self.volume_dragging = False
        self.volume_return_started_at = now
        self.menu_last_interaction_at = now
        self.needs_redraw = True
        log(f"volume slider returned volume={self.system_status.volume_percent}%")

    def begin_restart_mode(self, now: float) -> None:
        if self.menu_background_surface is None:
            self.prepare_menu_background()
        self.menu_active = True
        self.menu_selected = None
        self.volume_mode = False
        self.volume_dragging = False
        self.volume_return_started_at = 0.0
        self.screensaver_panel_mode = False
        self.screensaver_panel_return_started_at = 0.0
        self.restart_mode = True
        self.restart_mode_started_at = now
        self.restart_return_started_at = 0.0
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_last_interaction_at = now + self.volume_transition_seconds
        self.menu_last_interaction_at = now
        self.restart_requested_at = 0.0
        self.restart_error_until = 0.0
        self.status_visible_until = 0.0
        self.needs_redraw = True
        log("restart slider opened")

    def end_restart_mode(self, now: float) -> None:
        if self.restart_requested_at:
            return
        self.restart_mode = False
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_return_started_at = now
        self.menu_last_interaction_at = now
        self.needs_redraw = True
        log("restart slider returned")

    def update_restart_request(self, now: float) -> None:
        if (
            self.restart_requested_at
            and self.restart_future is None
            and now - self.restart_requested_at >= 0.35
        ):
            self.restart_future = self.executor.submit(request_system_reboot)
            log("restart slider confirmed; reboot requested")
        if self.restart_future is None or not self.restart_future.done():
            return
        try:
            ok, detail = self.restart_future.result()
        except Exception as exc:
            ok, detail = False, str(exc)
        self.restart_future = None
        if ok:
            log(f"reboot request accepted via {detail}")
            return
        self.restart_requested_at = 0.0
        self.restart_progress = 0.0
        self.restart_completion_latched = False
        self.restart_last_interaction_at = now
        self.restart_error_until = now + 3.0
        self.needs_redraw = True
        log(f"restart slider warning: {detail}")

    def event_position(self, event: object) -> tuple[int, int]:
        if event.type in {
            getattr(self.pygame, "FINGERDOWN", -1),
            getattr(self.pygame, "FINGERMOTION", -1),
            getattr(self.pygame, "FINGERUP", -1),
        }:
            return (
                max(0, min(round(float(event.x) * self.width), self.width - 1)),
                max(0, min(round(float(event.y) * self.height), self.height - 1)),
            )
        x, y = getattr(event, "pos", self.pointer_position)
        return max(0, min(int(x), self.width - 1)), max(0, min(int(y), self.height - 1))

    def update_menu_selection(self, position: tuple[int, int]) -> None:
        self.pointer_position = position
        dx = position[0] - self.pointer_start[0]
        dy = position[1] - self.pointer_start[1]
        distance = math.hypot(dx, dy)
        selected = None
        if distance >= self.menu_deadzone:
            angle = math.atan2(dy, dx)
            candidates = [-math.pi / 2 + index * math.tau / 6 for index in range(6)]
            selected = min(
                range(6),
                key=lambda index: abs(
                    math.atan2(
                        math.sin(angle - candidates[index]),
                        math.cos(angle - candidates[index]),
                    )
                ),
            )
        if selected != self.menu_selected:
            self.menu_selected = selected
            self.needs_redraw = True

    def handle_pointer_down(self, position: tuple[int, int]) -> None:
        now = time.monotonic()
        if self.pointer_down or now - self.last_touch_at < self.touch_debounce:
            return
        self.last_touch_at = now
        if self.screensaver_active:
            self.note_screensaver_activity(now)
            self.pointer_down = False
            self.needs_redraw = True
            self.write_state()
            return
        self.note_screensaver_activity(now)
        self.pointer_down = True
        self.pointer_started_at = now
        self.pointer_start = position
        self.pointer_position = position
        self.pointer_moved = False
        self.menu_selected = None
        self.edge_exit_candidate = False
        self.edge_exit_ready = False
        self.edge_exit_progress = 0.0
        if self.settings_active:
            if self.settings_transition_active:
                self.pointer_down = False
                self.settings_pointer_target = None
                return
            self.settings_pointer_target = self.settings_target_at(position)
            self.settings_last_interaction_at = now
            self.needs_redraw = True
            return
        if self.menu_active:
            self.menu_last_interaction_at = now
            # While a control is visually returning to the status bar, keep that
            # control modal until its transition is over. This prevents a touch on
            # the fading restart rail from falling through to the volume target.
            restart_returning = (
                self.restart_return_started_at
                and now - self.restart_return_started_at
                < self.volume_transition_seconds
            )
            volume_returning = (
                self.volume_return_started_at
                and now - self.volume_return_started_at
                < self.volume_transition_seconds
            )
            screensaver_returning = (
                self.screensaver_panel_return_started_at
                and now - self.screensaver_panel_return_started_at
                < self.volume_transition_seconds
            )
            if restart_returning or volume_returning or screensaver_returning:
                self.pointer_down = False
                self.needs_redraw = True
                return
        status_interactive = (
            (self.menu_active or now < self.status_visible_until)
            and not self.volume_mode
            and not self.restart_mode
            and not self.screensaver_panel_mode
        )
        if self.token_popup_visible:
            if status_interactive and self.is_token_status_position(position):
                self.token_popup_last_interaction_at = now
                self.request_token_balance(now)
            else:
                self.dismiss_token_popup(now)
            # An outside tap dismisses only the popover. Swallow it so it cannot
            # accidentally trigger a control or radial-menu action underneath.
            self.pointer_down = False
            self.pointer_moved = False
            self.write_state()
            return
        if status_interactive and self.is_token_status_position(position):
            self.begin_token_popup(now)
            self.pointer_down = False
            self.pointer_moved = False
            self.write_state()
            return
        if self.camera_view_active and not self.menu_active:
            if self.gallery_active:
                self.camera_pointer_target = "gallery_ui"
                self.gallery_pointer_control = self.gallery_control_at(position)
                self.gallery_long_press_triggered = False
                self.needs_redraw = True
                return
            camera_control = self.camera_control_at(position)
            if camera_control is not None:
                self.camera_pointer_target = camera_control
                self.needs_redraw = True
                return
        if self.camera_view_active and not self.menu_active:
            center = (self.width / 2.0, self.height / 2.0)
            start_radius = math.hypot(
                position[0] - center[0],
                position[1] - center[1],
            )
            display_radius = min(self.width, self.height) / 2.0
            self.edge_exit_candidate = (
                start_radius >= display_radius * self.camera_exit_edge_ratio
            )
        if self.menu_active and self.screensaver_panel_mode:
            selected_timeout = self.screensaver_option_at(position)
            if selected_timeout is None:
                self.end_screensaver_panel_mode(now)
                self.pointer_down = False
                self.write_state()
            else:
                self.set_screensaver_timeout(selected_timeout)
                self.screensaver_panel_last_interaction_at = now
                self.menu_last_interaction_at = now
            return
        if self.menu_active and self.restart_mode:
            if self.restart_requested_at:
                self.pointer_down = False
                return
            if self.is_restart_slider_start_position(position):
                self.restart_dragging = True
                self.restart_progress = 0.0
                self.restart_completion_latched = False
                self.restart_last_interaction_at = now
                self.needs_redraw = True
            elif not self.is_restart_slider_position(position):
                self.end_restart_mode(now)
                self.pointer_down = False
                self.write_state()
            else:
                self.restart_last_interaction_at = now
            return
        if self.menu_active and self.volume_mode:
            if self.is_volume_slider_position(position):
                self.volume_dragging = True
                self.set_system_volume_from_position(position, force=True)
                self.needs_redraw = True
            else:
                self.end_volume_mode(now)
                self.pointer_down = False
                self.write_state()
            return
        if (
            self.menu_active or now < self.status_visible_until
        ) and self.is_screensaver_status_position(position):
            self.begin_screensaver_panel_mode(now)
            return
        if (
            self.menu_active or now < self.status_visible_until
        ) and self.is_restart_status_position(position):
            self.begin_restart_mode(now)
            return
        if (
            self.menu_active or now < self.status_visible_until
        ) and self.is_volume_status_position(position):
            self.begin_volume_mode(now)
            return
        if not self.menu_active:
            self.menu_background_surface = None
        self.needs_redraw = True

    def handle_pointer_motion(self, position: tuple[int, int]) -> None:
        if not self.pointer_down:
            return
        self.pointer_position = position
        if self.menu_active:
            self.menu_last_interaction_at = time.monotonic()
        if math.dist(position, self.pointer_start) > 24:
            self.pointer_moved = True
        if self.settings_active:
            self.settings_last_interaction_at = time.monotonic()
            if self.pointer_moved:
                self.settings_pointer_target = None
            self.needs_redraw = True
            return
        if self.camera_pointer_target is not None:
            self.needs_redraw = True
            return
        if self.edge_exit_candidate and self.camera_view_active and not self.menu_active:
            center = (self.width / 2.0, self.height / 2.0)
            start_radius = math.hypot(
                self.pointer_start[0] - center[0],
                self.pointer_start[1] - center[1],
            )
            current_radius = math.hypot(
                position[0] - center[0],
                position[1] - center[1],
            )
            inward_distance = max(0.0, start_radius - current_radius)
            travel_distance = max(1.0, math.dist(position, self.pointer_start))
            self.edge_exit_progress = min(
                1.0,
                inward_distance / self.camera_exit_swipe_px,
            )
            self.edge_exit_ready = (
                inward_distance >= self.camera_exit_swipe_px
                and inward_distance / travel_distance >= 0.65
            )
            self.needs_redraw = True
        if self.menu_active and self.screensaver_panel_mode:
            self.screensaver_panel_last_interaction_at = time.monotonic()
            self.needs_redraw = True
            return
        if self.menu_active and self.restart_mode:
            if self.restart_dragging:
                now = time.monotonic()
                if self.restart_completion_latched:
                    # Keep completion latched when the finger merely drifts
                    # away, but allow an intentional reverse drag along the
                    # track to cancel it. The small hysteresis avoids endpoint
                    # jitter toggling the confirmation state.
                    if self.is_restart_slider_position(position):
                        reverse_progress = self.restart_progress_from_position(
                            position
                        )
                        if reverse_progress < 0.93:
                            self.restart_completion_latched = False
                            self.restart_progress = reverse_progress
                        else:
                            self.restart_progress = 1.0
                    else:
                        self.restart_progress = 1.0
                elif self.is_restart_slider_position(position):
                    self.restart_progress = self.restart_progress_from_position(position)
                    if self.restart_progress >= 0.97:
                        self.restart_progress = 1.0
                        self.restart_completion_latched = True
                else:
                    self.restart_dragging = False
                    self.restart_progress = 0.0
                self.restart_last_interaction_at = now
                self.needs_redraw = True
            return
        if self.menu_active and self.volume_mode:
            if math.dist(position, self.pointer_start) > 4:
                self.volume_dragging = True
            if self.volume_dragging:
                self.set_system_volume_from_position(position)
            return
        if self.menu_active:
            # Status controls are tap targets, not hover targets. A directional
            # menu gesture may cross their arcs without changing control layers.
            self.update_menu_selection(position)

    def handle_pointer_up(self, position: tuple[int, int]) -> None:
        if not self.pointer_down:
            return
        self.pointer_position = position
        self.touch_count += 1
        if self.settings_active:
            target = self.settings_pointer_target
            if self.pointer_moved and self.settings_back_swipe_detected(position):
                self.handle_settings_target("back")
                log("settings returned by left-to-right swipe")
            elif not self.pointer_moved and target == self.settings_target_at(position):
                self.handle_settings_target(target)
            self.settings_pointer_target = None
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
        if self.camera_pointer_target is not None:
            target = self.camera_pointer_target
            if target == "gallery_ui":
                if self.gallery_long_press_triggered:
                    # The release only completes the long press. Deletion still
                    # requires a separate explicit confirmation tap.
                    pass
                elif self.pointer_moved:
                    self.handle_gallery_swipe(self.pointer_start, position)
                else:
                    self.handle_gallery_control(position)
            elif not self.pointer_moved:
                if target == "capture":
                    self.capture_photo()
                elif target == "gallery":
                    self.enter_gallery()
            self.camera_pointer_target = None
            self.gallery_pointer_control = None
            self.gallery_long_press_triggered = False
            self.pointer_down = False
            self.pointer_moved = False
            self.edge_exit_candidate = False
            self.edge_exit_ready = False
            self.edge_exit_progress = 0.0
            self.needs_redraw = True
            self.write_state()
            return
        if self.edge_exit_candidate and self.camera_view_active and not self.menu_active:
            self.handle_pointer_motion(position)
            if self.edge_exit_ready:
                self.set_camera_view(False)
                self.send_voice_command({"command": "visual_mode", "active": False})
                self.set_state(self.default_state, force=True)
                self.pointer_down = False
                self.pointer_moved = False
                self.edge_exit_candidate = False
                self.edge_exit_ready = False
                self.edge_exit_progress = 0.0
                self.needs_redraw = True
                self.write_state()
                log("camera visual voice mode exited by edge-inward swipe")
                return
        if self.menu_active:
            if self.screensaver_panel_mode:
                self.pointer_down = False
                self.pointer_moved = False
                self.screensaver_panel_last_interaction_at = time.monotonic()
                self.needs_redraw = True
                self.write_state()
                return
            if self.restart_mode:
                now = time.monotonic()
                completed = self.restart_completion_latched
                if self.restart_dragging and not completed:
                    if self.is_restart_slider_position(position):
                        self.restart_progress = self.restart_progress_from_position(position)
                    else:
                        self.restart_progress = 0.0
                    completed = self.restart_progress >= 0.97
                self.pointer_down = False
                self.pointer_moved = False
                self.restart_dragging = False
                if completed:
                    self.restart_progress = 1.0
                    self.restart_completion_latched = True
                    self.restart_requested_at = now
                    self.restart_last_interaction_at = now + 3600.0
                    log("restart slider completed")
                else:
                    self.restart_progress = 0.0
                    self.restart_completion_latched = False
                    transition_remaining = max(
                        0.0,
                        self.restart_mode_started_at
                        + self.volume_transition_seconds
                        - now,
                    )
                    self.restart_last_interaction_at = now + transition_remaining
                self.needs_redraw = True
                self.write_state()
                return
            if self.volume_mode:
                if self.volume_dragging:
                    self.set_system_volume_from_position(position, force=True)
                self.pointer_down = False
                self.pointer_moved = False
                self.volume_dragging = False
                now = time.monotonic()
                transition_remaining = max(
                    0.0,
                    self.volume_mode_started_at + self.volume_transition_seconds - now,
                )
                self.volume_last_interaction_at = now + transition_remaining
                self.needs_redraw = True
                self.write_state()
                return
            if not self.pointer_moved and self.is_screensaver_status_position(position):
                self.begin_screensaver_panel_mode(time.monotonic())
                self.pointer_down = False
                self.pointer_moved = False
                self.write_state()
                return
            if not self.pointer_moved and self.is_restart_status_position(position):
                self.begin_restart_mode(time.monotonic())
                self.pointer_down = False
                self.pointer_moved = False
                self.write_state()
                return
            if not self.pointer_moved and self.is_volume_status_position(position):
                self.begin_volume_mode(time.monotonic())
                self.pointer_down = False
                self.pointer_moved = False
                self.write_state()
                return
            self.update_menu_selection(position)
            selected = self.menu_selected
            if selected is not None:
                self.close_radial_menu()
                self.execute_menu_action(selected)
            else:
                # Releasing the long press only finishes opening the menu. Keep it
                # available for a second directional gesture instead of treating a
                # neutral release as a close command.
                self.menu_last_interaction_at = time.monotonic()
                self.menu_selected = None
        elif not self.pointer_moved and not self.camera_view_active:
            self.set_state(self.touch_state, ttl=self.touch_ttl, force=True)
            log(
                f"touch detected count={self.touch_count} state={self.touch_state} "
                f"ttl={self.touch_ttl}"
            )
        self.pointer_down = False
        self.edge_exit_candidate = False
        self.edge_exit_ready = False
        self.edge_exit_progress = 0.0
        self.camera_pointer_target = None
        self.needs_redraw = True
        self.write_state()

    def update_long_press(self, now: float) -> None:
        if self.pointer_down and self.camera_pointer_target == "gallery_ui":
            if (
                not self.pointer_moved
                and self.gallery_delete_candidate is None
                and self.gallery_pointer_control is not None
                and self.gallery_pointer_control[0] == "photo"
                and self.gallery_pointer_control[1] is not None
                and now - self.pointer_started_at >= self.gallery_long_press_seconds
            ):
                self.gallery_delete_candidate = self.gallery_pointer_control[1]
                self.gallery_long_press_triggered = True
                self.needs_redraw = True
                log(
                    "camera gallery delete prompt opened "
                    f"path={self.gallery_delete_candidate}"
                )
                self.write_state()
            return
        if (
            not self.pointer_down
            or self.menu_active
            or self.settings_active
            or self.pointer_moved
            or self.camera_pointer_target is not None
        ):
            return
        if now - self.pointer_started_at < self.long_press_seconds:
            return
        self.prepare_menu_background()
        self.menu_active = True
        self.menu_selected = None
        self.menu_opened_at = now
        self.menu_last_interaction_at = now
        self.status_visible_until = 0.0
        self.request_system_status_refresh(now)
        self.needs_redraw = True
        log(f"radial menu opened at={self.pointer_start}")
        self.write_state()

    def close_radial_menu(self) -> None:
        self.menu_active = False
        self.menu_selected = None
        self.token_popup_visible = False
        self.token_popup_last_interaction_at = 0.0
        self.menu_background_surface = None
        self.menu_opened_at = 0.0
        self.menu_last_interaction_at = 0.0
        self.volume_mode = False
        self.volume_return_started_at = 0.0
        self.screensaver_panel_mode = False
        self.screensaver_panel_return_started_at = 0.0
        self.restart_mode = False
        self.restart_return_started_at = 0.0
        self.restart_dragging = False
        self.restart_progress = 0.0
        self.needs_redraw = True

    @staticmethod
    def send_voice_command(payload: dict) -> bool:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), str(VOICE_SOCKET_PATH))
            client.close()
            return True
        except OSError as exc:
            log(f"voice menu action warning: {exc}")
            return False

    def execute_menu_action(self, selected: int) -> None:
        item = dict(self.radial_menu[selected])
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        action_type = str(action.get("type", "emit"))
        result = "emitted"
        if action_type != "camera_voice" and self.camera_view_active:
            self.set_camera_view(False)
            self.send_voice_command({"command": "visual_mode", "active": False})
        if action_type == "voice_trigger":
            result = "sent" if self.send_voice_command({"command": "trigger"}) else "failed"
        elif action_type == "voice_query":
            text = str(action.get("text", "")).strip()
            result = "sent" if text and self.send_voice_command({"command": "query", "text": text}) else "failed"
        elif action_type == "camera_voice":
            if self.camera_view_active:
                self.set_camera_view(False)
                sent = self.send_voice_command(
                    {"command": "visual_mode", "active": False}
                )
                result = "exited" if sent else "exit_signal_failed"
            else:
                self.set_camera_view(True)
                result = (
                    "entered"
                    if self.send_voice_command(
                        {"command": "visual_mode", "active": True}
                    )
                    else "enter_signal_failed"
                )
        elif action_type == "show_status":
            self.status_visible_until = time.monotonic() + 8.0
            self.request_system_status_refresh(time.monotonic())
            result = "shown"
        elif action_type == "open_settings":
            self.open_settings()
            result = "opened"
        elif action_type == "expression":
            response = self.set_state(
                str(action.get("state", self.default_state)),
                ttl=action.get("ttl"),
                force=True,
            )
            result = "shown" if response.get("ok") else "failed"
        self.last_menu_selection = {
            "id": item.get("id"),
            "label": item.get("label"),
            "action_type": action_type,
            "result": result,
            "selected_at": time.time(),
        }
        temporary = MENU_EVENT_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.last_menu_selection, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, MENU_EVENT_PATH)
        log(f"radial menu selected id={item.get('id')} action={action_type} result={result}")

    def process_pygame_events(self) -> None:
        for event in self.pygame.event.get():
            if event.type == self.pygame.QUIT:
                self.running = False
                continue
            if self.boot_active or not self.touch_enabled:
                continue
            if event.type == self.pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
                self.handle_pointer_down(self.event_position(event))
            elif event.type == self.pygame.MOUSEMOTION:
                self.handle_pointer_motion(self.event_position(event))
            elif event.type == self.pygame.MOUSEBUTTONUP and getattr(event, "button", 1) == 1:
                self.handle_pointer_up(self.event_position(event))
            elif event.type == getattr(self.pygame, "FINGERDOWN", -1):
                self.handle_pointer_down(self.event_position(event))
            elif event.type == getattr(self.pygame, "FINGERMOTION", -1):
                self.handle_pointer_motion(self.event_position(event))
            elif event.type == getattr(self.pygame, "FINGERUP", -1):
                self.handle_pointer_up(self.event_position(event))

    def write_state(self) -> None:
        payload = {
            "ok": True,
            "state": self.state,
            "output": self.output,
            "renderer": "pygame-persistent",
            "background_mode": self.background_mode,
            "display_scale": self.display_scale,
            "ui_theme": {
                "name": self.ui_theme.get("name", "RiverBank Orbital Blue"),
                "version": self.ui_theme.get("version", "fallback"),
                "path": str(UI_THEME_PATH),
                "edge_softness": self.ui_edge_softness,
            },
            "display_refresh_hz": round(self.display_refresh_hz, 3),
            "target_render_fps": round(self.target_render_fps, 3),
            "measured_render_fps": round(self.render_fps, 2),
            "render_interval_ms": round(self.render_interval * 1000.0, 3),
            "frame_rendering": {
                "average_ms": round(self.frame_render_ms, 3),
                "peak_ms": round(self.frame_render_peak_ms, 3),
            },
            "animation_interpolation": True,
            "max_animation_fps": round(self.target_render_fps, 3),
            "transition_rendering": {
                "strategy": "cached-composite-crossfade-scale",
                "duration_ms": round(self.volume_transition_seconds * 1000),
                "average_frame_ms": round(self.transition_frame_ms, 3),
                "peak_frame_ms": round(self.transition_frame_peak_ms, 3),
                "cache_entries": len(self.control_overlay_cache),
                "supersample": UI_AA_SCALE,
            },
            "boot_animation": {
                "active": self.boot_active,
                "phase": self.boot_phase,
                "boot_id": self.boot_id,
                "once_per_system_boot": True,
                "replay_suppressed": self.boot_replay_suppressed,
                "marker_path": str(BOOT_ANIMATION_MARKER_PATH),
                "health_current": self.boot_health_current,
                "health_healthy": self.boot_health_healthy,
                "health_check_count": self.boot_health_check_count,
                "failed_checks": self.boot_health_failed,
                "group_states": self.boot_group_states,
                "preload_ready": self.boot_preload_ready(),
                "warning": self.boot_warning,
            },
            "touch_enabled": self.touch_enabled,
            "touch_state": self.touch_state,
            "touch_ttl_seconds": self.touch_ttl,
            "touch_count": self.touch_count,
            "last_touch_monotonic": self.last_touch_at or None,
            "long_press_seconds": self.long_press_seconds,
            "menu_deadzone_px": self.menu_deadzone,
            "display_diameter_mm": self.display_diameter_mm,
            "radial_menu_offset_mm": self.radial_menu_offset_mm,
            "radial_menu_offset_px": self.radial_menu_offset_px,
            "pointer_down": self.pointer_down,
            "radial_menu_active": self.menu_active,
            "radial_menu_idle_seconds": self.menu_idle_seconds,
            "radial_menu_opened_monotonic": self.menu_opened_at or None,
            "radial_menu_last_interaction_monotonic": (
                self.menu_last_interaction_at or None
            ),
            "volume_slider_active": self.volume_mode,
            "volume_slider_idle_seconds": self.volume_idle_seconds,
            "token_popup": {
                "visible": self.token_popup_visible,
                "idle_seconds": self.token_popup_idle_seconds,
                "last_interaction_monotonic": (
                    self.token_popup_last_interaction_at or None
                ),
                "usage_percent": round(self.token_usage_percent(), 1),
                "capacity": self.token_meter_capacity,
                "balance_loading": self.token_balance_future is not None,
                "balance": self.token_balance_result,
                "balance_error": self.token_balance_error,
                "dismiss_policy": "outside-tap-or-2s-idle",
            },
            "speech_bubble": {
                "active": self.speech_bubble_active,
                "visible": self.speech_bubble_opacity(time.monotonic()) > 0.0,
                "final": self.speech_bubble_final,
                "text_length": len(self.speech_bubble_text),
                "stable_chars": self.speech_bubble_stable_chars,
                "max_lines": 3,
                "position": "lower-left-round-safe-area",
                "privacy": "text-kept-in-renderer-memory-only",
                "listening_label": False,
            },
            "screensaver_settings_active": self.screensaver_panel_mode,
            "settings": {
                "active": self.settings_active,
                "section": self.settings_section,
                "pointer_target": self.settings_pointer_target,
                "version": self.system_status.app_version,
                "read_only": False,
                "auto_refresh_seconds": 2.0,
                "transition": {
                    "active": self.settings_transition_active,
                    "duration_ms": round(self.settings_transition_seconds * 1000),
                    "direction": self.settings_transition_direction,
                    "from": self.settings_transition_from_section,
                    "to": self.settings_transition_to_section,
                    "exits_settings": self.settings_transition_exits_settings,
                    "style": "cached-carousel-slide",
                },
                "wifi_control": {
                    "enabled": self.system_status.wifi_enabled,
                    "pending": self.wifi_toggle_future is not None,
                    "target": self.wifi_toggle_target,
                    "error": self.wifi_toggle_error or None,
                },
            },
            "photo_screensaver": {
                "enabled": self.screensaver_idle_seconds > 0,
                "active": self.screensaver_active,
                "idle_seconds": self.screensaver_idle_seconds,
                "selected_photo": (
                    str(self.screensaver_photo) if self.screensaver_photo else None
                ),
                "displayed_photo": (
                    str(self.screensaver_current_photo)
                    if self.screensaver_current_photo
                    else None
                ),
                "config_path": str(self.screensaver_config_path),
                "fallback": "latest-gallery-photo",
                "wake_policy": "first-touch-only",
            },
            "restart_slider": {
                "active": self.restart_mode,
                "dragging": self.restart_dragging,
                "progress": round(self.restart_progress, 3),
                "requested": bool(self.restart_requested_at),
                "idle_seconds": self.volume_idle_seconds,
                "exit_policy": "idle-or-blank-tap",
            },
            "radial_menu_selected": (
                self.radial_menu[self.menu_selected].get("id")
                if self.menu_selected is not None
                else None
            ),
            "radial_menu_items": [
                {"id": item.get("id"), "label": item.get("label"), "glyph": item.get("glyph")}
                for item in self.radial_menu
            ],
            "camera_view": {
                "active": self.camera_view_active,
                "fps": self.camera_view_fps,
                "measured_fps": round(self.camera_measured_fps, 2),
                "last_frame_at": self.camera_last_success_at or None,
                "last_error": self.camera_last_error,
                "edge_exit_ratio": self.camera_exit_edge_ratio,
                "edge_exit_swipe_px": self.camera_exit_swipe_px,
                "edge_exit_candidate": self.edge_exit_candidate,
                "edge_exit_progress": round(self.edge_exit_progress, 3),
            },
            "camera_gallery": {
                "active": self.gallery_active,
                "selected_photo": (
                    str(self.gallery_selected) if self.gallery_selected else None
                ),
                "directory": str(self.camera_gallery_dir),
                "photo_count": len(self.gallery_entries),
                "page": self.gallery_page + 1,
                "page_count": self.gallery_page_count(),
                "page_transition": {
                    "active": self.gallery_page_transition_active,
                    "from": self.gallery_page_transition_from + 1,
                    "to": self.gallery_page_transition_to + 1,
                    "direction": self.gallery_page_transition_direction,
                    "duration_ms": round(
                        self.gallery_page_transition_seconds * 1000
                    ),
                    "style": "cached-clipped-slide-smoothstep",
                },
                "swipe_threshold_px": self.gallery_swipe_threshold_px,
                "long_press_seconds": self.gallery_long_press_seconds,
                "delete_candidate": (
                    str(self.gallery_delete_candidate)
                    if self.gallery_delete_candidate
                    else None
                ),
                "capture_pending": self.camera_capture_future is not None,
                "last_photo": (
                    str(self.camera_last_photo) if self.camera_last_photo else None
                ),
                "last_error": self.camera_capture_error,
                "controls_inward_mm": self.camera_controls_inward_mm,
                "controls_inward_px": self.camera_controls_inward_px,
            },
            "camera_indicator": {
                "active": self.camera_indicator_active(),
                "sources": list(self.camera_indicator_sources()),
                "ignores_background_camera_hub": True,
                "ignores_background_face_tracker": True,
                "lease_directory": str(ACTIVE_VISION_LEASE_DIR),
                "runtime_sources": sorted(self.runtime_vision_sources),
            },
            "last_menu_selection": self.last_menu_selection,
            "system_status": self.system_status.as_dict(),
            "renderer_pid": os.getpid(),
            "player_pid": os.getpid(),
            "window_id": self.window_id,
            "deadline_monotonic": self.deadline,
            "cache_mb": round(self.cache_bytes / 1024 / 1024, 1),
            "cached_states": list(self.cache),
            "pending_states": list(self.pending),
            "preloaded_first_frames": len(self.first_frames),
            "updated_at": time.time(),
            "states": sorted(self.expressions),
        }
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STATE_PATH)

    def tick(self) -> None:
        self.process_pygame_events()
        self.install_completed_decodes()
        now = time.monotonic()
        if self.boot_active:
            self.update_boot_state(now)
            if now - self.last_overlay_redraw >= self.render_interval:
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.update_speech_bubble(now):
            self.write_state()
        if self.screensaver_active:
            self.update_screensaver(now)
            if self.screensaver_active:
                if self.needs_redraw:
                    self.draw()
                self.refresh_always_on_top()
                return
        self.update_camera_view(now)
        self.update_camera_capture(now)
        self.update_gallery_page_transition(now)
        self.prune_vision_activity(now)
        self.update_restart_request(now)
        if self.update_token_balance(now):
            self.write_state()
        if self.update_system_status_async(now):
            self.needs_redraw = True
            self.write_state()
        if self.update_wifi_toggle(now):
            self.write_state()
        if self.wifi_toggle_notice_until and now >= self.wifi_toggle_notice_until:
            self.wifi_toggle_notice_until = 0.0
            self.wifi_toggle_error = ""
            self.needs_redraw = True
        if self.settings_active:
            if (
                self.settings_transition_active
                and now - self.last_overlay_redraw >= self.render_interval
            ):
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        self.update_long_press(now)
        if (
            self.volume_mode
            and not self.pointer_down
            and now - self.volume_last_interaction_at >= self.volume_idle_seconds
        ):
            self.end_volume_mode(now)
            self.write_state()
        if (
            self.screensaver_panel_mode
            and not self.pointer_down
            and now - self.screensaver_panel_last_interaction_at
            >= self.volume_idle_seconds
        ):
            self.end_screensaver_panel_mode(now)
            self.write_state()
        if (
            self.restart_mode
            and not self.pointer_down
            and not self.restart_requested_at
            and now - self.restart_last_interaction_at >= self.volume_idle_seconds
        ):
            self.end_restart_mode(now)
            self.write_state()
        if (
            self.token_popup_visible
            and not self.pointer_down
            and self.token_popup_last_interaction_at
            and now - self.token_popup_last_interaction_at
            >= self.token_popup_idle_seconds
        ):
            self.dismiss_token_popup(now)
            self.write_state()
        if (
            self.menu_active
            and not self.token_popup_visible
            and not self.volume_mode
            and not self.screensaver_panel_mode
            and not self.restart_mode
            and not self.pointer_down
            and self.menu_last_interaction_at
            and now - self.menu_last_interaction_at >= self.menu_idle_seconds
        ):
            self.close_radial_menu()
            log(f"radial menu closed after {self.menu_idle_seconds:.1f}s idle")
            self.write_state()
        if (
            self.status_visible_until
            and now >= self.status_visible_until
            and not self.token_popup_visible
        ):
            self.status_visible_until = 0.0
            self.needs_redraw = True
        if self.gallery_notice_until and now >= self.gallery_notice_until:
            self.gallery_notice_until = 0.0
            self.gallery_notice = None
            self.needs_redraw = True
        if self.deadline is not None and now >= self.deadline:
            self.set_state(self.default_state)
        self.update_screensaver(now)
        if self.screensaver_active:
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        self.advance_animation()
        refresh_driven_scene = (
            (not self.camera_view_active and len(self.current_animation().frames) > 1)
            or self.pointer_down
            or self.menu_active
            or now < self.status_visible_until
            or self.token_popup_visible
            or self.camera_indicator_active()
            or (self.camera_view_active and not self.gallery_active)
            or self.gallery_page_transition_active
            or now < self.camera_capture_flash_until
            or now < self.camera_capture_notice_until
            or now < self.camera_capture_error_until
            or now < self.gallery_notice_until
            or self.speech_bubble_opacity(now) > 0.0
        )
        if (
            refresh_driven_scene
            and now - self.last_overlay_redraw >= self.render_interval
        ):
            self.needs_redraw = True
        if self.needs_redraw:
            self.draw()
        self.refresh_always_on_top()

    def shutdown(self) -> None:
        self.running = False
        if self.camera_frame_future is not None:
            self.camera_frame_future.cancel()
        if self.camera_capture_future is not None:
            self.camera_capture_future.cancel()
        self.camera_executor.shutdown(wait=False, cancel_futures=True)
        self.status_executor.shutdown(wait=False, cancel_futures=True)
        self.control_executor.shutdown(wait=False, cancel_futures=True)
        self.balance_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.pygame.display.quit()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_VISION_LEASE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    display = PersistentExpressionDisplay(load_config())
    signal.signal(signal.SIGTERM, lambda *_: setattr(display, "running", False))
    signal.signal(signal.SIGINT, lambda *_: setattr(display, "running", False))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(SOCKET_PATH))
    server.setblocking(False)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        while display.running:
            time_until_refresh = max(
                0.0001,
                display.render_interval
                - max(0.0, time.monotonic() - display.last_overlay_redraw),
            )
            ready, _, _ = select.select(
                [server],
                [],
                [],
                min(0.01, time_until_refresh),
            )
            if ready:
                try:
                    request = json.loads(server.recv(65535).decode("utf-8"))
                    if request.get("command") == "status":
                        display.write_state()
                    elif request.get("command") == "metrics_reset":
                        metrics_now = time.monotonic()
                        display.render_fps = 0.0
                        display.render_frame_count = 0
                        display.render_fps_window_started = metrics_now
                        display.frame_render_ms = 0.0
                        display.frame_render_peak_ms = 0.0
                        display.transition_frame_ms = 0.0
                        display.transition_frame_peak_ms = 0.0
                        display.write_state()
                    elif request.get("command") == "menu_preview":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.prepare_menu_background()
                        else:
                            display.close_radial_menu()
                        display.menu_active = preview_active
                        preview_now = time.monotonic()
                        display.menu_opened_at = preview_now if preview_active else 0.0
                        display.menu_last_interaction_at = (
                            preview_now if preview_active else 0.0
                        )
                        display.pointer_down = bool(
                            preview_active and request.get("pointer_down", False)
                        )
                        display.volume_mode = bool(
                            preview_active and request.get("volume_mode", False)
                        )
                        display.restart_mode = bool(
                            preview_active and request.get("restart_mode", False)
                        )
                        display.screensaver_panel_mode = bool(
                            preview_active
                            and request.get("screensaver_mode", False)
                        )
                        if display.screensaver_panel_mode:
                            display.volume_mode = False
                            display.restart_mode = False
                        elif display.restart_mode:
                            display.volume_mode = False
                        display.volume_mode_started_at = (
                            time.monotonic() - display.volume_transition_seconds
                            if display.volume_mode
                            else 0.0
                        )
                        display.volume_return_started_at = 0.0
                        display.restart_mode_started_at = (
                            time.monotonic() - display.volume_transition_seconds
                            if display.restart_mode
                            else 0.0
                        )
                        display.restart_return_started_at = 0.0
                        display.screensaver_panel_started_at = (
                            time.monotonic() - display.volume_transition_seconds
                            if display.screensaver_panel_mode
                            else 0.0
                        )
                        display.screensaver_panel_return_started_at = 0.0
                        display.restart_dragging = False
                        display.restart_progress = max(
                            0.0,
                            min(float(request.get("restart_progress", 0.0)), 1.0),
                        )
                        display.restart_requested_at = 0.0
                        display.volume_last_interaction_at = (
                            time.monotonic() + 3600.0
                            if display.volume_mode
                            else time.monotonic()
                        )
                        display.restart_last_interaction_at = (
                            time.monotonic() + 3600.0
                            if display.restart_mode
                            else time.monotonic()
                        )
                        display.screensaver_panel_last_interaction_at = (
                            time.monotonic() + 3600.0
                            if display.screensaver_panel_mode
                            else time.monotonic()
                        )
                        display.pointer_start = display.radial_menu_center()
                        display.pointer_position = display.pointer_start
                        selected = request.get("selected")
                        display.menu_selected = (
                            int(selected)
                            if display.menu_active and isinstance(selected, int) and 0 <= selected < 6
                            else None
                        )
                        display.request_system_status_refresh(time.monotonic())
                        preview_token_popup = bool(
                            preview_active
                            and request.get("token_popup", False)
                            and not display.volume_mode
                            and not display.restart_mode
                            and not display.screensaver_panel_mode
                        )
                        display.token_popup_visible = False
                        if preview_token_popup:
                            display.begin_token_popup(preview_now)
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "transition_preview":
                        preview_mode = str(request.get("mode", "volume"))
                        preview_active = bool(request.get("active", True))
                        preview_now = time.monotonic()
                        transition_handlers = {
                            "volume": (
                                display.begin_volume_mode,
                                display.end_volume_mode,
                            ),
                            "restart": (
                                display.begin_restart_mode,
                                display.end_restart_mode,
                            ),
                            "screensaver": (
                                display.begin_screensaver_panel_mode,
                                display.end_screensaver_panel_mode,
                            ),
                        }
                        handlers = transition_handlers.get(preview_mode)
                        if handlers is None:
                            raise ValueError(
                                f"unsupported transition preview mode: {preview_mode}"
                            )
                        handlers[0 if preview_active else 1](preview_now)
                        display.write_state()
                    elif request.get("command") == "settings_preview":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.open_settings()
                            section = str(request.get("section", "")).strip()
                            if section in {"wifi", "bluetooth", "version", "system"}:
                                display.settings_section = section
                        else:
                            display.close_settings()
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "camera_view":
                        display.set_camera_view(bool(request.get("active", True)))
                    elif request.get("command") == "camera_capture":
                        display.capture_photo()
                    elif request.get("command") == "gallery_view":
                        if bool(request.get("active", True)):
                            display.enter_gallery()
                        else:
                            display.gallery_selected = None
                            display.gallery_active = False
                            display.cancel_gallery_page_transition()
                            display.gallery_pointer_control = None
                            display.gallery_long_press_triggered = False
                            display.gallery_delete_candidate = None
                            display.camera_next_fetch_at = 0.0
                            display.needs_redraw = True
                            display.write_state()
                    elif request.get("command") == "gallery_page":
                        display.start_gallery_page_transition(
                            int(request.get("direction", 1))
                        )
                    elif request.get("command") == "screenshot":
                        display.save_runtime_screenshot(
                            str(request.get("name", "display-preview"))
                        )
                    elif request.get("command") == "speech_bubble":
                        display.set_speech_bubble(
                            bool(request.get("active", True)),
                            str(request.get("text", "")),
                            int(request.get("stable_chars", 0)),
                            bool(request.get("final", False)),
                            float(request.get("ttl", 0.0)),
                        )
                        display.write_state()
                    elif request.get("command") == "vision_activity":
                        display.set_vision_activity(
                            str(request.get("source", "")),
                            bool(request.get("active", True)),
                            float(request.get("ttl_seconds", 180.0)),
                        )
                    else:
                        display.set_state(
                            str(request.get("state", "")),
                            ttl=request.get("ttl"),
                            force=bool(request.get("force", False)),
                        )
                except Exception as exc:
                    log(f"invalid expression command: {exc}")
            display.tick()
    finally:
        server.close()
        display.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
