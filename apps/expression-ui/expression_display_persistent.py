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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from animation_assets import (
    Animation,
    RawAnimation,
    apply_background_mode,
    decode_animation,
    dominant_edge_color,
    fitted_size,
    render_viewport,
)
from app_menu import (
    ApplicationMenuModel,
    pin_gesture_progress,
    pin_gesture_ready,
    pin_target_hit,
)
from system_status import ACTIVE_VISION_LEASE_DIR, HEALTH_STATUS_PATH, SystemStatus
from pomodoro import PomodoroTimer, completion_flash_frame
from performance_monitor import PerformanceMonitor, PerformanceSnapshot, format_bytes
from recovery_client import request_recovery
from music_player import (
    LyricLine,
    LyricsFetchResult,
    MusicPlayer,
    MusicTrack,
    PLAYBACK_MODE_LIST_LOOP,
    PLAYBACK_MODE_SHUFFLE,
    PLAYBACK_MODE_SINGLE_REPEAT,
    fetch_lrclib_lyrics,
    lyric_index_at,
    parse_lrc,
    scan_music_library,
    sidecar_lyrics_path,
)
from video_call_client import (
    activate_call,
    fetch_remote_frame,
    fetch_status as fetch_video_call_status,
    hangup_call,
)
from workshop_client import approve_proposal, reject_proposal, request as workshop_request, stop_app as stop_workshop_app


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
RECOVERY_STATUS_PATH = Path(
    os.environ.get(
        "RIVERBANK_RECOVERY_STATUS",
        "/var/lib/riverbank-recovery/status.json",
    )
)
PROVISIONING_STATUS_PATH = Path(
    os.environ.get(
        "RIVERBANK_PROVISIONING_STATUS",
        "/run/riverbank-provisioning/status.json",
    )
)
WORKSHOP_REGISTRY_PATH = Path(
    os.environ.get(
        "RIVERBANK_WORKSHOP_REGISTRY",
        "/mnt/nvme64/riverbank-user/workshop/registry.json",
    )
)
WORKSHOP_STATUS_PATH = Path(
    os.environ.get(
        "RIVERBANK_WORKSHOP_STATUS",
        "/run/riverbank-workshop/status.json",
    )
)
WORKSHOP_RUNTIME_PATH = Path(
    os.environ.get(
        "RIVERBANK_WORKSHOP_RUNTIME",
        "/run/riverbank-workshop/runtime.json",
    )
)
MENU_EVENT_PATH = RUNTIME_DIR / "menu-selection.json"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
# The service RuntimeDirectory is removed whenever this one service restarts.
# /dev/shm survives service restarts but is cleared by an actual OS reboot.
BOOT_ANIMATION_MARKER_PATH = Path(
    "/dev/shm/riverbank-expression-boot-animation-boot-id"
)
DAILY_ENV_PATH = Path(os.environ.get("RIVERBANK_DAILY_ENV", DAILY_HOME / ".env"))
POMODORO_STATE_PATH = Path(
    os.environ.get(
        "RIVERBANK_POMODORO_STATE",
        RIVERBANK_DATA / "pomodoro/state.json",
    )
)
APP_MENU_STATE_PATH = Path(
    os.environ.get(
        "RIVERBANK_APP_MENU_STATE",
        RIVERBANK_DATA / "ui/app-pin.json",
    )
)
MUSIC_PREFERENCES_PATH = Path(
    os.environ.get(
        "RIVERBANK_MUSIC_PREFERENCES",
        RIVERBANK_DATA / "ui/music-preferences.json",
    )
)
MUSIC_ARTWORK_CACHE_DIR = Path(
    os.environ.get(
        "RIVERBANK_MUSIC_ARTWORK_CACHE",
        RIVERBANK_DATA / "ui/music-artwork",
    )
)
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
        "id": "app_pin",
        "label": "空位",
        "glyph": "+",
        "action": {"type": "open_app_menu"},
    },
    {
        "id": "applications",
        "label": "应用",
        "glyph": "应",
        "action": {"type": "open_app_menu"},
    },
)
DEFAULT_APPLICATION_MENU = (
    {
        "id": "pomodoro",
        "label": "番茄",
        "glyph": "茄",
        "action": {"type": "launch_app", "app_id": "pomodoro"},
    },
    {
        "id": "performance",
        "label": "性能",
        "glyph": "能",
        "action": {"type": "launch_app", "app_id": "performance"},
    },
    {
        "id": "music",
        "label": "音乐",
        "glyph": "乐",
        "action": {"type": "launch_app", "app_id": "music"},
    },
    {
        "id": "video_call",
        "label": "通话",
        "glyph": "话",
        "action": {"type": "launch_app", "app_id": "video_call"},
    },
    {
        "id": "workshop",
        "label": "工坊",
        "glyph": "坊",
        "action": {"type": "launch_app", "app_id": "workshop"},
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
    raw = str(value)
    raw = raw.replace("${RIVERBANK_HOME}", str(RIVERBANK_HOME))
    raw = raw.replace("${RIVERBANK_DATA}", str(RIVERBANK_DATA))
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def uses_cjk_font(character: str) -> bool:
    """Route CJK glyphs to the compact device font and other glyphs to Latin."""
    codepoint = ord(character)
    return any(
        lower <= codepoint <= upper
        for lower, upper in (
            (0x1100, 0x11FF),
            (0x2E80, 0x31FF),
            (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF),
            (0xAC00, 0xD7AF),
            (0xF900, 0xFAFF),
            (0xFF00, 0xFFEF),
            (0x20000, 0x2FA1F),
        )
    )


def split_font_runs(text: str) -> list[tuple[bool, str]]:
    """Split text into CJK and non-CJK runs without changing its contents."""
    if not text:
        return []
    runs: list[tuple[bool, str]] = []
    current_kind = uses_cjk_font(text[0])
    current: list[str] = []
    for character in text:
        kind = uses_cjk_font(character)
        if current and kind != current_kind:
            runs.append((current_kind, "".join(current)))
            current = []
        current_kind = kind
        current.append(character)
    runs.append((current_kind, "".join(current)))
    return runs


class MixedGlyphFont:
    """Pygame font facade with deterministic CJK/Latin glyph fallback."""

    def __init__(
        self,
        pygame_module: object,
        cjk_path: str,
        latin_path: str,
        size: int,
        latin_scale: float = 1.0,
    ) -> None:
        self.pygame = pygame_module
        self.cjk = pygame_module.font.Font(cjk_path, size)
        self.latin = pygame_module.font.Font(
            latin_path,
            max(1, round(size * latin_scale)),
        )

    def font_runs(self, text: object) -> list[tuple[object, str]]:
        return [
            (self.cjk if is_cjk else self.latin, value)
            for is_cjk, value in split_font_runs(str(text))
        ]

    def size(self, text: object) -> tuple[int, int]:
        runs = self.font_runs(text)
        if not runs:
            return self.cjk.size("")
        if len(runs) == 1:
            return runs[0][0].size(runs[0][1])
        ascent = max(font.get_ascent() for font, _value in runs)
        width = 0
        height = 0
        for font, value in runs:
            run_width, run_height = font.size(value)
            width += run_width
            height = max(height, ascent - font.get_ascent() + run_height)
        return width, height

    def render(
        self,
        text: object,
        antialias: bool,
        color: object,
        background: object | None = None,
    ) -> object:
        runs = self.font_runs(text)
        if not runs:
            return self.cjk.render("", antialias, color, background)
        if len(runs) == 1:
            return runs[0][0].render(runs[0][1], antialias, color, background)
        rendered = [
            (font, font.render(value, antialias, color, background))
            for font, value in runs
        ]
        ascent = max(font.get_ascent() for font, _surface in rendered)
        width = sum(surface.get_width() for _font, surface in rendered)
        height = max(
            ascent - font.get_ascent() + surface.get_height()
            for font, surface in rendered
        )
        flags = self.pygame.SRCALPHA if background is None else 0
        surface = self.pygame.Surface((max(1, width), max(1, height)), flags)
        surface.fill((0, 0, 0, 0) if background is None else background)
        x = 0
        for font, run_surface in rendered:
            surface.blit(run_surface, (x, ascent - font.get_ascent()))
            x += run_surface.get_width()
        return surface

    def __getattr__(self, name: str) -> object:
        return getattr(self.cjk, name)


















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
        configured_applications = config.get(
            "application_menu",
            DEFAULT_APPLICATION_MENU,
        )
        if not isinstance(configured_applications, list):
            configured_applications = list(DEFAULT_APPLICATION_MENU)
        self.application_menu = ApplicationMenuModel(
            APP_MENU_STATE_PATH,
            configured_menu,
            configured_applications,
        )
        self.menu_level = "main"
        self.menu_pin_mode = False
        self.application_return_pending = False
        self.radial_menu = self.application_menu.main_items()
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
        self.menu_level_transition_active = False
        self.menu_level_transition_started_at = 0.0
        self.menu_level_transition_seconds = 0.22
        self.menu_level_transition_source: object | None = None
        self.menu_level_transition_target: object | None = None
        self.app_pin_candidate_app_id: str | None = None
        self.app_pin_drag_progress = 0.0
        self.app_pin_drag_ready = False
        self.app_pin_target_hit = False
        self.app_pin_full_reached_at = 0.0
        self.app_pin_hold_seconds = max(
            0.2,
            min(float(config.get("application_pin_hold_seconds", 0.35)), 0.8),
        )
        self.app_pin_drag_start_distance = self.menu_deadzone + 64.0
        self.app_pin_drag_confirm_distance = self.menu_deadzone + 152.0
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
        self.radial_menu_content_cache: dict[tuple, object] = {}
        self.radial_menu_time_cache_text = ""
        self.app_pin_affordance_cache: dict[
            tuple[int, int, bool, bool], tuple[object, tuple[int, int]]
        ] = {}
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
        self.pomodoro = PomodoroTimer(
            POMODORO_STATE_PATH,
            focus_seconds=max(
                60,
                int(float(config.get("pomodoro_focus_minutes", 25)) * 60),
            ),
            short_break_seconds=max(
                60,
                int(float(config.get("pomodoro_short_break_minutes", 5)) * 60),
            ),
            long_break_seconds=max(
                60,
                int(float(config.get("pomodoro_long_break_minutes", 15)) * 60),
            ),
            long_break_every=max(
                1,
                int(config.get("pomodoro_long_break_every", 4)),
            ),
        )
        self.pomodoro_active = False
        self.pomodoro_statistics_active = False
        self.pomodoro_statistics_page = 0
        self.pomodoro_pointer_target: str | None = None
        self.pomodoro_transition_active = False
        self.pomodoro_transition_opening = True
        self.pomodoro_transition_started_at = 0.0
        self.pomodoro_transition_seconds = max(
            0.16,
            min(float(config.get("pomodoro_transition_seconds", 0.28)), 0.5),
        )
        self.pomodoro_transition_source: object | None = None
        self.pomodoro_transition_target: object | None = None
        self.pomodoro_transition_exits_page = False
        self.pomodoro_phase_changed_at = 0.0
        self.pomodoro_last_transition_serial = self.pomodoro.transition_serial
        self.pomodoro_last_display_second = -1
        self.pomodoro_completion_alert_active = False
        self.pomodoro_completion_alert_started_at = 0.0
        self.pomodoro_completion_alert_phase: str | None = None
        self.pomodoro_completion_alert_cycles = 5
        self.pomodoro_completion_alert_cycle_seconds = 0.80
        self.pomodoro_completion_alert_rise_seconds = 0.18
        self.pomodoro_completion_alert_hold_seconds = 0.18
        self.pomodoro_completion_alert_pulse = 0
        self.pomodoro_completion_alert_intensity = 0.0
        self.pomodoro_completion_alert_lit = False
        self.pomodoro_completion_alert_on_surface: object | None = None
        self.pomodoro_completion_alert_off_surface: object | None = None
        self.pomodoro_button_cache: OrderedDict[tuple, object] = OrderedDict()
        self.pomodoro_button_cache_limit = 16
        self.pomodoro_mark_cache: dict[int, object] = {}
        self.pomodoro_duration_adjust_active = False
        self.pomodoro_duration_dial_moved = False
        self.pomodoro_duration_adjust_started_at = 0.0
        self.pomodoro_duration_hold_seconds = max(
            0.3,
            min(float(config.get("pomodoro_duration_hold_seconds", 0.45)), 1.2),
        )
        self.pomodoro_duration_adjust_seconds = max(
            0.16,
            min(float(config.get("pomodoro_duration_adjust_seconds", 0.24)), 0.5),
        )
        self.pomodoro_duration_min_minutes = 1
        self.pomodoro_duration_max_minutes = 180
        self.pomodoro_duration_step_minutes = 1
        # Once the long press has opened the dial, retain only enough motion
        # filtering to reject sensor jitter.  The former 18 px threshold was
        # wider than two one-minute arcs and made the values around 25
        # impossible to select from the crown.
        self.pomodoro_duration_drag_deadzone_pixels = 3.0
        self.pomodoro_duration_selected_minutes = 25
        initial_selectable_count = (
            (self.pomodoro_duration_max_minutes - self.pomodoro_duration_min_minutes)
            // self.pomodoro_duration_step_minutes
            + 1
        )
        self.pomodoro_duration_pointer_clockwise = (
            math.tau
            * (
                (self.pomodoro_duration_selected_minutes - self.pomodoro_duration_min_minutes)
                // self.pomodoro_duration_step_minutes
            )
            / initial_selectable_count
        )
        self.pomodoro_duration_original_seconds = self.pomodoro.duration_for()
        self.pomodoro_duration_dial_surface: object | None = None
        self.pomodoro_duration_adjust_base_surfaces: dict[str, object] = {}
        self.pomodoro_duration_wave_cache: OrderedDict[tuple[str, int], object] = OrderedDict()
        self.pomodoro_duration_wave_cache_limit = 30
        self.pomodoro_duration_time_cache: OrderedDict[int, object] = OrderedDict()
        self.pomodoro_duration_time_cache_limit = 36
        self.performance_monitor = PerformanceMonitor()
        self.performance_snapshot = PerformanceSnapshot()
        self.performance_active = False
        self.performance_section: str | None = None
        self.performance_health_scroll_offset = 0.0
        self.performance_health_scroll_start_offset = 0.0
        self.performance_pointer_target: str | None = None
        self.recovery_future: Future[dict] | None = None
        self.recovery_status: dict = {
            "state": "idle",
            "message": "恢复全部服务",
        }
        self.recovery_status_next_read_at = 0.0
        self.recovery_button_cache: OrderedDict[tuple, object] = OrderedDict()
        self.performance_future: Future[PerformanceSnapshot] | None = None
        self.performance_next_update_at = 0.0
        self.performance_refresh_seconds = max(
            0.5,
            min(float(config.get("performance_refresh_seconds", 1.0)), 5.0),
        )
        self.performance_transition_active = False
        self.performance_transition_opening = True
        self.performance_transition_started_at = 0.0
        self.performance_transition_seconds = max(
            0.16,
            min(float(config.get("performance_transition_seconds", 0.28)), 0.5),
        )
        self.performance_transition_source: object | None = None
        self.performance_transition_target: object | None = None
        self.performance_transition_exits_page = False
        self.performance_static_surface: object | None = None
        self.performance_health_card_cache: dict[tuple[tuple[int, int], bool], object] = {}
        self.performance_health_content_cache_key: tuple | None = None
        self.performance_health_content_cache_surface: object | None = None
        self.workshop_active = False
        self.workshop_section: str | None = None
        self.workshop_pointer_target: str | None = None
        self.workshop_notice = ""
        self.workshop_notice_until = 0.0
        self.workshop_status: dict = {}
        self.workshop_runtime_status: dict = {}
        self.workshop_status_next_read_at = 0.0
        self.workshop_create_request_blocked_until = 0.0
        self.workshop_emblem_surface: object | None = None
        self.workshop_button_cache: OrderedDict[tuple, object] = OrderedDict()
        self.workshop_button_cache_limit = 32
        self.workshop_action_future: Future[dict] | None = None
        self.workshop_action_kind = ""
        self.workshop_transition_active = False
        self.workshop_transition_opening = True
        self.workshop_transition_started_at = 0.0
        self.workshop_transition_seconds = max(
            0.16,
            min(float(config.get("workshop_transition_seconds", 0.28)), 0.5),
        )
        self.workshop_transition_source: object | None = None
        self.workshop_transition_target: object | None = None
        self.workshop_transition_exits_page = False
        self.music_player = MusicPlayer(
            app_path(config.get("music_library_dir", "/mnt/nvme64/Music")),
            player_command=str(config.get("music_player_command", "/usr/bin/cvlc")),
        )
        self.music_active = False
        self.music_pointer_target: str | None = None
        self.music_volume_dragging = False
        self.music_volume_interaction_started_at = 0.0
        self.music_volume_last_interaction_at = 0.0
        self.music_volume_expand_from = 0.0
        self.music_volume_idle_seconds = max(
            0.4,
            min(float(config.get("music_volume_idle_seconds", 0.8)), 2.0),
        )
        self.music_volume_transition_seconds = max(
            0.12,
            min(float(config.get("music_volume_transition_seconds", 0.22)), 0.5),
        )
        self.music_volume_idle_surface_cache_key: int | None = None
        self.music_volume_idle_surface_cache: object | None = None
        self.music_volume_expanded_surface_cache_key: tuple[int, bool] | None = None
        self.music_volume_expanded_surface_cache: object | None = None
        self.music_scan_future: Future[list[MusicTrack]] | None = None
        self.music_scan_completed = False
        self.music_scan_error = ""
        self.music_autoplay_pending = False
        self.music_ffprobe_command = str(
            config.get("music_ffprobe_command", "/usr/bin/ffprobe")
        )
        self.music_ffmpeg_command = str(
            config.get("music_ffmpeg_command", "/usr/bin/ffmpeg")
        )
        self.music_artwork_future: Future[tuple[str, bytes, tuple[int, int]]] | None = None
        self.music_artwork_future_path: str | None = None
        self.music_artwork_requested_path: str | None = None
        self.music_artwork_surface: object | None = None
        self.music_artwork_cache: OrderedDict[str, object] = OrderedDict()
        self.music_artwork_cache_limit = 12
        self.music_artwork_failed_paths: set[str] = set()
        self.music_center_mode = "artwork"
        self.music_center_previous_mode = "artwork"
        self.music_center_transition_active = False
        self.music_center_transition_started_at = 0.0
        self.music_center_transition_seconds = max(
            0.18,
            min(float(config.get("music_center_transition_seconds", 0.30)), 0.6),
        )
        self.music_transition_active = False
        self.music_transition_opening = True
        self.music_transition_started_at = 0.0
        self.music_transition_seconds = max(
            0.16,
            min(float(config.get("music_transition_seconds", 0.28)), 0.5),
        )
        self.music_transition_source: object | None = None
        self.music_transition_target: object | None = None
        self.music_transition_exits_page = False
        self.music_static_surface: object | None = None
        self.music_lyrics_enabled = True
        self.music_mode_hold_seconds = max(
            0.5,
            min(float(config.get("music_mode_hold_seconds", 0.75)), 1.5),
        )
        self.music_lyrics: list[LyricLine] = []
        self.music_lyrics_path: Path | None = None
        self.music_lyrics_track_path: str | None = None
        self.music_lyric_index = -1
        self.music_lyric_previous_index = -1
        self.music_lyric_changed_at = 0.0
        self.music_lyric_direction = 1
        self.music_lyric_scroll_seconds = max(
            0.18,
            min(float(config.get("music_lyric_scroll_seconds", 0.32)), 0.6),
        )
        self.music_lyrics_notice = ""
        self.music_lyrics_notice_until = 0.0
        self.music_lyrics_fetch_future: Future[LyricsFetchResult] | None = None
        self.music_lyrics_fetch_track_path: str | None = None
        self.music_lyrics_search_attempted: set[str] = set()
        self.music_lyrics_search_status = "idle"
        self.music_lyrics_search_error = ""
        self.music_lyrics_search_trigger = "idle"
        self.music_marquee_surface_cache: OrderedDict[str, tuple[object, object]] = OrderedDict()
        self.music_marquee_surface_cache_limit = 64
        self.music_player_lyric_surface_cache: OrderedDict[tuple, object] = OrderedDict()
        self.music_player_lyric_surface_cache_limit = 96
        self.music_horizontal_fade_cache: dict[tuple[int, int, int], object] = {}
        self.music_lyric_marquee_speed = max(
            20.0,
            min(float(config.get("music_lyric_marquee_speed", 70.0)), 120.0),
        )
        self.music_lyric_marquee_hold_seconds = max(
            0.2,
            min(float(config.get("music_lyric_marquee_hold_seconds", 0.65)), 2.0),
        )
        self.load_music_preferences()
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
        self.runtime_audio_sources: dict[str, float] = {}
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
        self.video_call_active = False
        self.video_call_pointer_target: str | None = None
        self.video_call_frame_surface: object | None = None
        self.video_call_frame_future: Future[bytes] | None = None
        self.video_call_status_future: Future[dict] | None = None
        self.video_call_status: dict = {
            "active": False,
            "waiting": False,
            "connection_state": "idle",
            "device_name": None,
        }
        self.video_call_base_url = str(
            config.get("video_call_base_url", "http://127.0.0.1:19734")
        )
        self.video_call_view_fps = max(
            5.0,
            min(float(config.get("video_call_view_fps", 20.0)), 30.0),
        )
        self.video_call_frame_interval = 1.0 / self.video_call_view_fps
        self.video_call_next_frame_at = 0.0
        self.video_call_next_status_at = 0.0
        self.video_call_last_frame_at = 0.0
        self.video_call_last_error: str | None = None
        self.video_call_error_logged_at = 0.0
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
        self.provisioning_status: dict = {"active": False, "phase": "idle"}
        self.provisioning_active = False
        self.provisioning_qr_surface: object | None = None
        self.provisioning_qr_path = ""
        self.provisioning_qr_mtime_ns = 0
        self.provisioning_next_read_at = 0.0
        self.cache_limit = int(float(config.get("cache_limit_mb", 896)) * 1024 * 1024)
        self.expression_animation_fps = max(
            12.0,
            min(
                float(config.get("expression_animation_fps", 30.0)),
                self.target_render_fps,
            ),
        )
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
        self.video_call_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="video-call-view",
        )
        self.performance_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-performance",
        )
        self.music_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-music",
        )
        self.lyrics_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="display-lyrics",
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
        latin_font_path = app_path(
            config.get(
                "latin_font_path",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
        )
        if not latin_font_path.is_file():
            log(f"latin font missing, using CJK font only: {latin_font_path}")
            latin_font_path = Path(font_path)
        try:
            latin_font_scale = float(config.get("latin_font_scale", 1.06))
        except (TypeError, ValueError):
            latin_font_scale = 1.06
        latin_font_scale = max(1.0, min(latin_font_scale, 1.12))

        def mixed_font(primary_path: str, size: int) -> MixedGlyphFont:
            return MixedGlyphFont(
                pygame,
                primary_path,
                str(latin_font_path),
                size,
                latin_font_scale,
            )

        self.font_small = mixed_font(font_path, 21)
        self.font_medium = mixed_font(font_path, 26)
        self.font_large = mixed_font(font_path, 34)
        self.font_camera_label = mixed_font(font_path, 21)
        self.font_speech_bubble_high = mixed_font(
            font_path,
            25 * UI_AA_SCALE,
        )
        status_font_path = str(app_path(
            config.get(
                "status_font_path",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
        ))
        self.font_status = mixed_font(status_font_path, 20)
        self.font_radial_time_high = mixed_font(
            status_font_path,
            52 * UI_AA_SCALE,
        )
        self.font_workshop_clock_high = mixed_font(
            status_font_path,
            96 * UI_AA_SCALE,
        )
        self.font_workshop_clock_seconds_high = mixed_font(
            status_font_path,
            34 * UI_AA_SCALE,
        )
        self.font_pomodoro_time = mixed_font(status_font_path, 82)
        self.font_pomodoro_label = mixed_font(font_path, 28)
        pomodoro_title_font_path = app_path(
            config.get(
                "pomodoro_title_font_path",
                "assets/fonts/MaShanZheng-Regular.ttf",
            )
        )
        self.pomodoro_title_font_path = (
            pomodoro_title_font_path
            if pomodoro_title_font_path.is_file()
            else Path(font_path)
        )
        self.font_pomodoro_title = pygame.font.Font(
            str(self.pomodoro_title_font_path),
            46,
        )
        self.font_pomodoro_stat_value = mixed_font(status_font_path, 42)
        performance_mono_path = Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        )
        self.font_performance_value = pygame.font.Font(
            str(performance_mono_path if performance_mono_path.is_file() else status_font_path),
            34,
        )
        self.font_music_track = mixed_font(font_path, 30)
        self.font_music_artist = mixed_font(font_path, 22)
        self.font_music_lyric_current_high = mixed_font(
            font_path,
            50 * UI_AA_SCALE,
        )
        self.font_music_lyric_muted_high = mixed_font(
            font_path,
            20 * UI_AA_SCALE,
        )
        self.font_music_lyric_toggle_high = mixed_font(
            font_path,
            25 * UI_AA_SCALE,
        )
        self.font_music_player_lyric_current_high = mixed_font(
            font_path,
            32 * UI_AA_SCALE,
        )
        self.font_music_player_lyric_muted_high = mixed_font(
            font_path,
            24 * UI_AA_SCALE,
        )
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
            self.expression_animation_fps,
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

    @staticmethod
    def release_unused_memory() -> None:
        """Return freed supersampling/decode arenas to Linux when glibc supports it."""
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            trim = getattr(libc, "malloc_trim", None)
            if trim is not None:
                trim(0)
        except (AttributeError, OSError):
            pass

    def current_animation(self) -> Animation:
        animation = self.cache.get(self.state)
        if animation is not None:
            self.cache.move_to_end(self.state)
            return animation
        return self.first_frames[self.state]

    def prepare_menu_background(self, source_surface: object | None = None) -> None:
        """Freeze and blur one complete frame for the menu background."""
        if source_surface is not None:
            base = source_surface.copy()
            if base.get_size() != self.target_size:
                base = self.pygame.transform.smoothscale(base, self.target_size)
        else:
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
            or self.music_active
            or self.performance_active
            or self.pomodoro_active
            or self.pomodoro.status == "running"
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

    def set_video_call_view(
        self,
        active: bool,
        *,
        request_service: bool = False,
        hangup: bool = False,
    ) -> dict:
        """Show the remote call surface without owning either camera device."""

        active = bool(active)
        was_active = self.video_call_active
        self.note_screensaver_activity()
        if active:
            self.prepare_voice_application_switch("video_call")
            if self.camera_view_active:
                self.set_camera_view(False)
            self.settings_active = False
            self.gallery_active = False
            self.video_call_active = True
            if not was_active:
                self.video_call_frame_surface = None
            self.video_call_pointer_target = None
            self.video_call_next_frame_at = 0.0
            self.video_call_next_status_at = 0.0
            self.video_call_last_error = None
            if request_service:
                self.control_executor.submit(
                    activate_call,
                    base_url=self.video_call_base_url,
                )
        else:
            self.video_call_active = False
            self.video_call_pointer_target = None
            if self.video_call_frame_future is not None:
                self.video_call_frame_future.cancel()
            self.video_call_frame_future = None
            if self.video_call_status_future is not None:
                self.video_call_status_future.cancel()
            self.video_call_status_future = None
            if hangup:
                self.control_executor.submit(
                    hangup_call,
                    base_url=self.video_call_base_url,
                )
        self.needs_redraw = True
        self.write_state()
        log(
            "video call view "
            f"active={self.video_call_active} request_service={request_service} "
            f"hangup={hangup}"
        )
        return {"ok": True, "video_call_active": self.video_call_active}

    def close_video_call_to_application_menu(self, *, hangup: bool) -> None:
        """Freeze the call frame before exposing the second-level app menu."""
        now = time.monotonic()
        frozen_call_frame = self.screen.copy()
        self.set_video_call_view(False, hangup=hangup)
        self.prepare_application_menu_return(
            now,
            background_surface=frozen_call_frame,
        )
        self.complete_application_menu_return(now)

    def update_video_call(self, now: float) -> None:
        if not self.video_call_active:
            return
        if self.video_call_status_future is not None and self.video_call_status_future.done():
            try:
                self.video_call_status = self.video_call_status_future.result()
                self.video_call_last_error = None
            except Exception as exc:
                self.video_call_last_error = str(exc)
            finally:
                self.video_call_status_future = None
                self.needs_redraw = True
        if (
            self.video_call_status_future is None
            and now >= self.video_call_next_status_at
        ):
            self.video_call_next_status_at = now + 0.5
            self.video_call_status_future = self.video_call_executor.submit(
                fetch_video_call_status,
                base_url=self.video_call_base_url,
            )
        if self.video_call_frame_future is not None and self.video_call_frame_future.done():
            try:
                self.video_call_frame_surface = self.decode_camera_frame(
                    self.video_call_frame_future.result()
                )
                self.video_call_last_frame_at = time.time()
                self.video_call_last_error = None
                self.needs_redraw = True
            except Exception as exc:
                message = str(exc)
                if "waiting for remote video" not in message:
                    self.video_call_last_error = message
                    if now - self.video_call_error_logged_at >= 5.0:
                        log(f"video call frame warning: {exc}")
                        self.video_call_error_logged_at = now
            finally:
                self.video_call_frame_future = None
        if self.video_call_frame_future is None and now >= self.video_call_next_frame_at:
            self.video_call_next_frame_at = now + self.video_call_frame_interval
            self.video_call_frame_future = self.video_call_executor.submit(
                fetch_remote_frame,
                self.target_size,
                base_url=self.video_call_base_url,
            )

    def video_call_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.gallery_back_button_center()) <= 48:
            return "back"
        if math.dist(position, (self.width // 2, round(self.height * 0.84))) <= 58:
            return "hangup"
        return None

    def draw_video_call_page(self, now: float) -> None:
        self.screen.fill((0, 0, 0))
        if self.video_call_frame_surface is not None:
            self.screen.blit(self.video_call_frame_surface, (0, 0))
            shade = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
            shade.fill((0, 0, 0, 0))
            self.pygame.draw.rect(shade, (0, 0, 0, 105), (0, 0, self.width, 150))
            self.pygame.draw.rect(
                shade,
                (0, 0, 0, 125),
                (0, self.height - 170, self.width, 170),
            )
            self.screen.blit(shade, (0, 0))
        else:
            for radius, alpha in ((220, 18), (150, 26), (86, 40)):
                self.draw_aa_circle(
                    self.screen,
                    (20, 129, 164, alpha),
                    (self.width // 2, self.height // 2 - 20),
                    radius,
                )
            self.draw_centered_text(
                "等待 Windows 设备连接",
                self.font_medium,
                (208, 239, 247),
                (self.width // 2, self.height // 2 - 8),
            )
            hint = (
                "通话服务不可用"
                if self.video_call_last_error
                else "局域网或 Tailscale"
            )
            self.draw_centered_text(
                hint,
                self.font_small,
                (103, 163, 180),
                (self.width // 2, self.height // 2 + 40),
            )

        self.draw_gallery_back_button()
        state = str(self.video_call_status.get("connection_state", "idle"))
        state_label = {
            "connected": "通话中",
            "connecting": "连接中",
            "new": "正在协商",
            "idle": "等待连接",
            "failed": "连接失败",
            "closed": "通话结束",
        }.get(state, state)
        device_name = str(self.video_call_status.get("device_name") or "视频通话")
        self.draw_centered_text(
            device_name,
            self.font_medium,
            (226, 246, 251),
            (self.width // 2, 94),
        )
        self.draw_centered_text(
            state_label,
            self.font_small,
            (93, 211, 242) if state == "connected" else (133, 176, 188),
            (self.width // 2, 130),
        )

        hangup_center = (self.width // 2, round(self.height * 0.84))
        pressed = self.pointer_down and self.video_call_pointer_target == "hangup"
        self.draw_aa_circle(
            self.screen,
            (211, 56, 67) if not pressed else (159, 40, 49),
            hangup_center,
            50,
        )
        phone = self.font_medium.render("挂断", True, (255, 245, 246))
        self.screen.blit(phone, phone.get_rect(center=hangup_center))

    def draw_video_call(self, now: float) -> None:
        self.draw_video_call_page(now)

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
        if changed:
            self.evict_cache()
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

    def pomodoro_back_center(self) -> tuple[int, int]:
        return self.gallery_back_button_center()

    def pomodoro_statistics_center(self) -> tuple[int, int]:
        return round(self.width * 0.78), round(self.height * 0.175)

    def pomodoro_dial_center(self) -> tuple[int, int]:
        return 400, 382

    def pomodoro_duration_crown_center(self) -> tuple[int, int]:
        center = self.pomodoro_dial_center()
        # With a 1–180 minute dial, 25 minutes sits 48 degrees clockwise
        # from twelve o'clock.  Aligning the crown to that angle prevents
        # the value from jumping as soon as a circular drag begins.
        angle = math.radians(-42.0)
        radius = 240
        return (
            round(center[0] + math.cos(angle) * radius),
            round(center[1] + math.sin(angle) * radius),
        )

    def pomodoro_duration_crown_enabled(self, snapshot: dict) -> bool:
        return (
            snapshot["phase"] in {"focus", "short_break", "long_break"}
            and snapshot["status"] != "running"
            and not self.pomodoro_statistics_active
        )

    def draw_pomodoro_duration_crown(
        self,
        snapshot: dict,
        *,
        completion_alert: bool = False,
        completion_presentation: bool = False,
    ) -> None:
        center = self.pomodoro_dial_center()
        angle = math.radians(-42.0)
        unit = (math.cos(angle), math.sin(angle))

        def point(radius: float) -> tuple[float, float]:
            return (
                center[0] + unit[0] * radius,
                center[1] + unit[1] * radius,
            )

        enabled = self.pomodoro_duration_crown_enabled(snapshot)
        pressed = (
            self.pointer_down
            and self.pomodoro_pointer_target == "duration_crown"
        )
        shift = -4.0 if pressed else 0.0
        if completion_alert:
            connector_color = (0, 0, 0, 255)
            body_color = (0, 0, 0, 255)
            highlight_color = (24, 24, 24, 255)
        elif completion_presentation:
            focus_theme = snapshot["phase"] == "focus"
            connector_color = (
                (139, 64, 63, 230)
                if focus_theme
                else (34, 113, 75, 230)
            )
            body_color = (
                (205, 78, 72, 255)
                if focus_theme
                else (55, 166, 102, 255)
            )
            highlight_color = (
                (249, 151, 137, 220)
                if focus_theme
                else (126, 231, 166, 220)
            )
        else:
            focus_theme = snapshot["phase"] == "focus"
            connector_color = (
                ((139, 64, 63, 230) if focus_theme else (34, 113, 75, 230))
                if enabled
                else (53, 72, 77, 190)
            )
            body_color = (
                ((205, 78, 72, 255) if focus_theme else (55, 166, 102, 255))
                if enabled
                else (70, 87, 92, 230)
            )
            highlight_color = (
                ((249, 151, 137, 220) if focus_theme else (126, 231, 166, 220))
                if enabled
                else (113, 130, 134, 170)
            )
        self.draw_aa_round_line(
            self.screen,
            connector_color,
            point(207),
            point(230 + shift),
            6,
        )
        self.draw_aa_round_line(
            self.screen,
            (5, 18, 22, 250),
            point(229 + shift),
            point(253 + shift),
            22,
        )
        self.draw_aa_round_line(
            self.screen,
            body_color,
            point(231 + shift),
            point(251 + shift),
            16,
        )
        self.draw_aa_round_line(
            self.screen,
            highlight_color,
            point(236 + shift),
            point(248 + shift),
            3,
        )

    def pomodoro_duration_minutes_from_position(
        self,
        position: tuple[int, int],
    ) -> int:
        clockwise = self.pomodoro_duration_clockwise_from_position(position)
        count = (
            (self.pomodoro_duration_max_minutes - self.pomodoro_duration_min_minutes)
            // self.pomodoro_duration_step_minutes
            + 1
        )
        index = round(clockwise / math.tau * count) % count
        return (
            self.pomodoro_duration_min_minutes
            + index * self.pomodoro_duration_step_minutes
        )

    def pomodoro_duration_clockwise_from_position(
        self,
        position: tuple[int, int],
    ) -> float:
        center = self.pomodoro_dial_center()
        angle = math.atan2(position[1] - center[1], position[0] - center[0])
        return (angle + math.pi / 2) % math.tau

    def begin_pomodoro_duration_adjustment(self, now: float) -> None:
        if self.pomodoro_duration_adjust_active:
            return
        snapshot = self.pomodoro.snapshot()
        if not self.pomodoro_duration_crown_enabled(snapshot):
            return
        duration_minutes = max(1, round(self.pomodoro.duration_for() / 60.0))
        step = self.pomodoro_duration_step_minutes
        selected = round(duration_minutes / step) * step
        self.pomodoro_duration_selected_minutes = max(
            self.pomodoro_duration_min_minutes,
            min(selected, self.pomodoro_duration_max_minutes),
        )
        selected_index = (
            self.pomodoro_duration_selected_minutes
            - self.pomodoro_duration_min_minutes
        ) // self.pomodoro_duration_step_minutes
        selectable_count = (
            (self.pomodoro_duration_max_minutes - self.pomodoro_duration_min_minutes)
            // self.pomodoro_duration_step_minutes
            + 1
        )
        self.pomodoro_duration_pointer_clockwise = (
            selected_index * math.tau / selectable_count
        )
        self.pomodoro_duration_original_seconds = self.pomodoro.duration_for()
        self.pomodoro_duration_adjust_active = True
        self.pomodoro_duration_dial_moved = False
        self.pomodoro_duration_adjust_started_at = now
        self.pomodoro_pointer_target = "duration_dial"
        self.needs_redraw = True
        log(
            "pomodoro duration dial opened "
            f"minutes={self.pomodoro_duration_selected_minutes}"
        )
        self.write_state()

    def update_pomodoro_duration_adjustment(
        self,
        position: tuple[int, int],
    ) -> None:
        if not self.pomodoro_duration_adjust_active:
            return
        if (
            math.dist(position, self.pointer_start)
            <= self.pomodoro_duration_drag_deadzone_pixels
        ):
            return
        self.pomodoro_duration_dial_moved = True
        self.pomodoro_duration_pointer_clockwise = (
            self.pomodoro_duration_clockwise_from_position(position)
        )
        selected = self.pomodoro_duration_minutes_from_position(position)
        if selected != self.pomodoro_duration_selected_minutes:
            self.pomodoro_duration_selected_minutes = selected
            self.needs_redraw = True

    def commit_pomodoro_duration_adjustment(self) -> None:
        if not self.pomodoro_duration_adjust_active:
            return
        selected = self.pomodoro_duration_selected_minutes
        phase = self.pomodoro.phase
        self.pomodoro.set_phase_duration(selected * 60)
        self.pomodoro_duration_adjust_active = False
        self.pomodoro_duration_dial_moved = False
        self.pomodoro_duration_adjust_started_at = 0.0
        self.pomodoro_pointer_target = None
        self.pomodoro_last_display_second = -1
        self.needs_redraw = True
        log(
            "pomodoro duration dial committed "
            f"phase={phase} minutes={selected}"
        )
        self.write_state()

    def pomodoro_duration_dial_ticks(self) -> object:
        if self.pomodoro_duration_dial_surface is not None:
            return self.pomodoro_duration_dial_surface
        native_size = 500
        center = native_size // 2
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_size * scale, native_size * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        tick_count = 72
        for index in range(tick_count):
            major = index % 12 == 0
            angle = -math.pi / 2 + index * math.tau / tick_count
            outer = 216
            inner = outer - (13 if major else 7)
            start = (
                round((center + math.cos(angle) * inner) * scale),
                round((center + math.sin(angle) * inner) * scale),
            )
            end = (
                round((center + math.cos(angle) * outer) * scale),
                round((center + math.sin(angle) * outer) * scale),
            )
            width = (3 if major else 1) * scale
            color = (92, 137, 148, 230) if major else (43, 78, 88, 205)
            self.pygame.draw.line(high, color, start, end, width)
            cap = max(1, width // 2)
            self.pygame.draw.circle(high, color, start, cap)
            self.pygame.draw.circle(high, color, end, cap)
        self.pomodoro_duration_dial_surface = self.pygame.transform.smoothscale(
            high,
            (native_size, native_size),
        )
        return self.pomodoro_duration_dial_surface

    def pomodoro_duration_wave_surface(
        self,
        pointer_step: int,
        phase: str,
    ) -> object:
        tick_count = 72
        selected_phase = (
            phase if phase in {"focus", "short_break", "long_break"} else "focus"
        )
        theme_key = "focus" if selected_phase == "focus" else "break"
        key = (theme_key, int(pointer_step) % tick_count)
        cached = self.pomodoro_duration_wave_cache.get(key)
        if cached is not None:
            self.pomodoro_duration_wave_cache.move_to_end(key)
            return cached
        native_size = 500
        center = native_size // 2
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_size * scale, native_size * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        influence_radius = 10.0
        outer_radius = 216.0
        for index in range(tick_count):
            circular_distance = abs(
                (index - key[1] + tick_count / 2) % tick_count
                - tick_count / 2
            )
            if circular_distance > influence_radius:
                continue
            weight = 0.5 * (
                1.0
                + math.cos(math.pi * circular_distance / influence_radius)
            )
            if weight <= 0.01:
                continue
            tick_length = 7.0 + 38.0 * weight
            angle = -math.pi / 2 + index * math.tau / tick_count
            start = (
                round(
                    (center + math.cos(angle) * (outer_radius - tick_length))
                    * scale
                ),
                round(
                    (center + math.sin(angle) * (outer_radius - tick_length))
                    * scale
                ),
            )
            end = (
                round((center + math.cos(angle) * outer_radius) * scale),
                round((center + math.sin(angle) * outer_radius) * scale),
            )
            width = max(2, round(2 + 4 * weight)) * scale
            if theme_key == "focus":
                color = (
                    round(74 + 164 * weight),
                    round(126 - 45 * weight),
                    round(138 - 66 * weight),
                    round(255 * (0.68 + 0.32 * weight)),
                )
            else:
                color = (
                    round(65 - 10 * weight),
                    round(130 + 101 * weight),
                    round(116 + 50 * weight),
                    round(255 * (0.68 + 0.32 * weight)),
                )
            self.pygame.draw.line(high, color, start, end, width)
            cap = max(1, width // 2)
            self.pygame.draw.circle(high, color, start, cap)
            self.pygame.draw.circle(high, color, end, cap)
        cached = self.pygame.transform.smoothscale(
            high,
            (native_size, native_size),
        )
        self.pomodoro_duration_wave_cache[key] = cached
        self.pomodoro_duration_wave_cache.move_to_end(key)
        while len(self.pomodoro_duration_wave_cache) > self.pomodoro_duration_wave_cache_limit:
            self.pomodoro_duration_wave_cache.popitem(last=False)
        return cached

    def pomodoro_duration_time_surface(self, minutes: int) -> object:
        selected = int(minutes)
        cached = self.pomodoro_duration_time_cache.get(selected)
        if cached is None:
            cached = self.font_pomodoro_time.render(
                f"{selected:02d}:00",
                True,
                (242, 248, 249),
            )
            self.pomodoro_duration_time_cache[selected] = cached
        self.pomodoro_duration_time_cache.move_to_end(selected)
        while len(self.pomodoro_duration_time_cache) > self.pomodoro_duration_time_cache_limit:
            self.pomodoro_duration_time_cache.popitem(last=False)
        return cached

    def pomodoro_duration_adjust_base(self, phase: str) -> object:
        selected_phase = (
            phase if phase in {"focus", "short_break", "long_break"} else "focus"
        )
        theme_key = "focus" if selected_phase == "focus" else "break"
        cached = self.pomodoro_duration_adjust_base_surfaces.get(theme_key)
        if cached is not None:
            return cached
        focus_theme = selected_phase == "focus"
        glow_color = (129, 39, 42, 76) if focus_theme else (31, 104, 68, 76)
        accent_color = (222, 72, 66, 255) if focus_theme else (66, 181, 112, 255)
        surface = self.pygame.Surface(self.target_size)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        try:
            self.screen = surface
            self.pointer_down = False
            center = self.pomodoro_dial_center()
            self.screen.fill((0, 0, 0))
            self.draw_aa_ring(
                self.screen,
                (42, 120, 148, 30),
                (self.width // 2, self.height // 2),
                min(self.width, self.height) // 2 - 10,
                width=3,
            )
            self.draw_gallery_back_button()
            self.draw_pomodoro_mark((318, 128), 20, selected_phase)
            self.draw_centered_text(
                "番茄钟",
                self.font_pomodoro_title,
                (222, 241, 246),
                (425, 126),
            )
            ticks = self.pomodoro_duration_dial_ticks()
            self.screen.blit(ticks, ticks.get_rect(center=center))
            self.draw_aa_ring(
                self.screen,
                glow_color,
                center,
                150,
                width=18,
            )
            self.draw_aa_ring(
                self.screen,
                (24, 53, 62, 238),
                center,
                142,
                width=15,
            )
            self.draw_aa_ring(
                self.screen,
                accent_color,
                center,
                142,
                width=16,
            )
            self.draw_centered_text(
                "松手设定 · 每次 1 分钟",
                self.font_small,
                (105, 157, 168),
                (400, 675),
            )
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
        self.pomodoro_duration_adjust_base_surfaces[theme_key] = surface
        return surface

    def draw_pomodoro_statistics_button(self) -> None:
        center = self.pomodoro_statistics_center()
        pressed = (
            self.pointer_down
            and self.pomodoro_pointer_target == "statistics"
        )
        self.draw_aa_circle(
            self.screen,
            (10, 42, 50) if pressed else (5, 27, 37),
            center,
            37,
        )
        self.draw_aa_ring(
            self.screen,
            (96, 214, 193) if pressed else (70, 178, 213),
            center,
            37,
            2,
        )
        scale = UI_AA_SCALE
        high = self.pygame.Surface((42 * scale, 42 * scale), self.pygame.SRCALPHA)
        high.fill((0, 0, 0, 0))
        bar_color = (208, 242, 235)
        for x, height in ((8, 12), (17, 21), (26, 29)):
            rect = self.pygame.Rect(
                x * scale,
                (34 - height) * scale,
                7 * scale,
                height * scale,
            )
            self.pygame.draw.rect(
                high,
                bar_color,
                rect,
                border_radius=round(3.5 * scale),
            )
        icon = self.pygame.transform.smoothscale(high, (42, 42))
        self.screen.blit(icon, icon.get_rect(center=center))

    def pomodoro_control_rects(self) -> dict[str, object]:
        reset = self.pygame.Rect(0, 0, 126, 66)
        primary = self.pygame.Rect(0, 0, 188, 74)
        skip = self.pygame.Rect(0, 0, 126, 66)
        reset.center = (226, 650)
        primary.center = (400, 650)
        skip.center = (574, 650)
        return {"reset": reset, "primary": primary, "skip": skip}

    def pomodoro_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.pomodoro_back_center()) <= 50:
            return "back"
        if self.pomodoro_duration_adjust_active:
            return None
        if self.pomodoro_statistics_active:
            return None
        if math.dist(position, self.pomodoro_statistics_center()) <= 50:
            return "statistics"
        snapshot = self.pomodoro.snapshot()
        if (
            self.pomodoro_duration_crown_enabled(snapshot)
            and math.dist(position, self.pomodoro_duration_crown_center()) <= 34
        ):
            return "duration_crown"
        for name, rect in self.pomodoro_control_rects().items():
            if rect.inflate(16, 16).collidepoint(position):
                return name
        return None

    def open_pomodoro(self) -> None:
        if self.pomodoro_active:
            return
        self.pomodoro_transition_source = self.screen.copy()
        self.pomodoro_active = True
        self.pomodoro_statistics_active = False
        self.pomodoro_statistics_page = 0
        self.pomodoro_duration_adjust_active = False
        self.pomodoro_duration_dial_moved = False
        self.pomodoro_pointer_target = None
        self.pomodoro_transition_active = True
        self.pomodoro_transition_opening = True
        self.pomodoro_transition_started_at = time.monotonic()
        self.pomodoro_transition_exits_page = False
        self.pomodoro_transition_target = self.render_pomodoro_surface(
            self.pomodoro_transition_started_at,
            statistics=False,
        )
        self.status_visible_until = 0.0
        self.token_popup_visible = False
        self.note_screensaver_activity(self.pomodoro_transition_started_at)
        self.needs_redraw = True
        self.write_state()
        log("pomodoro page opened")

    def close_pomodoro(self, animated: bool = True) -> None:
        if not self.pomodoro_active:
            return
        now = time.monotonic()
        return_target = self.prepare_application_menu_return(now)
        if animated and not self.pomodoro_transition_active:
            self.pomodoro_transition_source = self.screen.copy()
            self.pomodoro_transition_target = return_target
            self.pomodoro_transition_active = True
            self.pomodoro_transition_opening = False
            self.pomodoro_transition_started_at = now
            self.pomodoro_transition_exits_page = True
            self.pomodoro_statistics_active = False
            self.pomodoro_statistics_page = 0
            self.pomodoro_duration_adjust_active = False
            self.pomodoro_duration_dial_moved = False
            self.pomodoro_pointer_target = None
            self.needs_redraw = True
            self.write_state()
            log("pomodoro page exit transition started")
            return
        self.pomodoro_active = False
        self.pomodoro_statistics_active = False
        self.pomodoro_statistics_page = 0
        self.pomodoro_duration_adjust_active = False
        self.pomodoro_duration_dial_moved = False
        self.pomodoro_transition_active = False
        self.pomodoro_transition_source = None
        self.pomodoro_transition_target = None
        self.pomodoro_transition_exits_page = False
        self.pomodoro_pointer_target = None
        self.complete_application_menu_return(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("pomodoro page closed")

    def open_pomodoro_statistics(self) -> None:
        if not self.pomodoro_active or self.pomodoro_statistics_active:
            return
        self.pomodoro_transition_source = self.screen.copy()
        self.pomodoro_statistics_active = True
        self.pomodoro_statistics_page = 0
        self.pomodoro_pointer_target = None
        self.pomodoro_transition_active = True
        self.pomodoro_transition_opening = True
        self.pomodoro_transition_exits_page = False
        self.pomodoro_transition_started_at = time.monotonic()
        self.pomodoro_transition_target = self.render_pomodoro_surface(
            self.pomodoro_transition_started_at,
            statistics=True,
            statistics_page=0,
        )
        self.needs_redraw = True
        self.write_state()
        log("pomodoro statistics opened")

    def close_pomodoro_statistics(self) -> None:
        if not self.pomodoro_active or not self.pomodoro_statistics_active:
            return
        self.pomodoro_transition_source = self.screen.copy()
        self.pomodoro_statistics_active = False
        self.pomodoro_statistics_page = 0
        self.pomodoro_pointer_target = None
        self.pomodoro_transition_active = True
        self.pomodoro_transition_opening = False
        self.pomodoro_transition_exits_page = False
        self.pomodoro_transition_started_at = time.monotonic()
        self.pomodoro_transition_target = self.render_pomodoro_surface(
            self.pomodoro_transition_started_at,
            statistics=False,
        )
        self.needs_redraw = True
        self.write_state()
        log("pomodoro statistics returned")

    def navigate_pomodoro_statistics(self, page: int) -> None:
        requested_page = max(0, min(int(page), 1))
        if (
            not self.pomodoro_active
            or not self.pomodoro_statistics_active
            or requested_page == self.pomodoro_statistics_page
            or self.pomodoro_transition_active
        ):
            return
        previous_page = self.pomodoro_statistics_page
        self.pomodoro_transition_source = self.screen.copy()
        self.pomodoro_statistics_page = requested_page
        self.pomodoro_pointer_target = None
        self.pomodoro_transition_active = True
        self.pomodoro_transition_opening = requested_page > previous_page
        self.pomodoro_transition_exits_page = False
        self.pomodoro_transition_started_at = time.monotonic()
        self.pomodoro_transition_target = self.render_pomodoro_surface(
            self.pomodoro_transition_started_at,
            statistics=True,
            statistics_page=requested_page,
        )
        self.needs_redraw = True
        self.write_state()
        log(
            "pomodoro statistics page changed "
            f"from={previous_page} to={requested_page}"
        )

    def pomodoro_back_swipe_detected(
        self,
        position: tuple[int, int],
    ) -> bool:
        dx = position[0] - self.pointer_start[0]
        dy = position[1] - self.pointer_start[1]
        minimum_distance = max(80, round(self.width * 0.12))
        return dx >= minimum_distance and abs(dy) <= max(48, dx * 0.55)

    def pomodoro_horizontal_swipe_direction(
        self,
        position: tuple[int, int],
    ) -> int:
        dx = position[0] - self.pointer_start[0]
        dy = position[1] - self.pointer_start[1]
        minimum_distance = max(80, round(self.width * 0.12))
        if abs(dx) < minimum_distance or abs(dy) > max(48, abs(dx) * 0.55):
            return 0
        return 1 if dx > 0 else -1

    def handle_pomodoro_target(self, target: str | None) -> None:
        if target is None:
            return
        if target == "back":
            if self.pomodoro_statistics_active:
                self.close_pomodoro_statistics()
            else:
                self.close_pomodoro()
        elif target == "statistics":
            self.open_pomodoro_statistics()
        elif target == "primary":
            self.pomodoro.toggle()
            log(f"pomodoro toggled status={self.pomodoro.status}")
        elif target == "reset":
            self.pomodoro.reset_phase()
            log(f"pomodoro phase reset phase={self.pomodoro.phase}")
        elif target == "skip":
            self.pomodoro.skip()
            self.pomodoro_phase_changed_at = time.monotonic()
            log(f"pomodoro phase skipped next={self.pomodoro.phase}")
        self.pomodoro_pointer_target = None
        self.note_screensaver_activity(time.monotonic())
        self.needs_redraw = True
        self.write_state()

    def pomodoro_button_surface(
        self,
        size: tuple[int, int],
        role: str,
        pressed: bool,
        phase: str,
        completion_alert: bool = False,
    ) -> object:
        key = (size, role, pressed, phase, completion_alert)
        cached = self.pomodoro_button_cache.get(key)
        if cached is not None:
            self.pomodoro_button_cache.move_to_end(key)
            return cached
        scale = UI_AA_SCALE
        width, height = size
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
        focus = phase == "focus"
        if role == "primary" and completion_alert:
            base = (0, 0, 0, 255)
            border = (28, 28, 28, 255)
        elif role == "primary":
            base = (187, 58, 55, 246) if focus else (47, 139, 91, 246)
            if pressed:
                base = (151, 44, 43, 248) if focus else (35, 108, 69, 248)
            border = (246, 128, 116, 154) if focus else (117, 224, 159, 154)
        else:
            base = (13, 37, 48, 246) if not pressed else (23, 57, 68, 250)
            border = (89, 154, 170, 92) if not pressed else (108, 204, 218, 150)
        self.pygame.draw.rect(
            high,
            (0, 0, 0, 110),
            rect.move(0, 3 * scale),
            border_radius=(height // 2) * scale,
        )
        self.pygame.draw.rect(
            high,
            base,
            rect,
            border_radius=(height // 2) * scale,
        )
        self.pygame.draw.rect(
            high,
            border,
            rect,
            width=scale,
            border_radius=(height // 2) * scale,
        )
        surface = self.pygame.transform.smoothscale(high, size)
        self.pomodoro_button_cache[key] = surface
        self.pomodoro_button_cache.move_to_end(key)
        while len(self.pomodoro_button_cache) > self.pomodoro_button_cache_limit:
            self.pomodoro_button_cache.popitem(last=False)
        return surface

    def draw_pomodoro_mark(
        self,
        center: tuple[int, int],
        radius: int,
        _phase: str,
    ) -> None:
        radius = max(8, int(radius))
        surface = self.pomodoro_mark_cache.get(radius)
        if surface is None:
            scale = UI_AA_SCALE
            padding = 10
            native_size = (radius + padding) * 2
            high = self.pygame.Surface(
                (native_size * scale, native_size * scale),
                self.pygame.SRCALPHA,
            )
            high.fill((0, 0, 0, 0))
            cx = native_size * scale // 2
            cy = round((native_size / 2 + radius * 0.12) * scale)
            outline = (2, 10, 13, 255)
            red = (226, 49, 45, 255)
            red_shadow = (178, 31, 34, 255)
            leaf = (69, 177, 103, 255)
            leaf_light = (101, 202, 130, 255)

            body_rect = self.pygame.Rect(
                round((native_size / 2 - radius) * scale),
                round((native_size / 2 - radius * 0.73) * scale),
                radius * 2 * scale,
                round(radius * 1.86 * scale),
            )
            self.pygame.draw.ellipse(high, outline, body_rect)
            inner_body = body_rect.inflate(-5 * scale, -5 * scale)
            self.pygame.draw.ellipse(high, red, inner_body)
            shadow_rect = self.pygame.Rect(
                inner_body.left + round(radius * 1.02 * scale),
                inner_body.top + round(radius * 0.23 * scale),
                round(radius * 0.55 * scale),
                round(radius * 1.12 * scale),
            )
            self.pygame.draw.arc(
                high,
                red_shadow,
                shadow_rect,
                -math.pi / 2,
                math.pi / 2,
                max(scale, 3 * scale),
            )

            leaf_center_y = cy - round(radius * 0.48 * scale)
            outer_leaf = [
                (cx - round(radius * 0.92 * scale), leaf_center_y),
                (cx - round(radius * 0.46 * scale), leaf_center_y - round(radius * 0.30 * scale)),
                (cx - round(radius * 0.26 * scale), leaf_center_y - round(radius * 0.02 * scale)),
                (cx - round(radius * 0.12 * scale), leaf_center_y - round(radius * 0.62 * scale)),
                (cx + round(radius * 0.08 * scale), leaf_center_y - round(radius * 0.22 * scale)),
                (cx + round(radius * 0.40 * scale), leaf_center_y - round(radius * 0.48 * scale)),
                (cx + round(radius * 0.34 * scale), leaf_center_y - round(radius * 0.05 * scale)),
                (cx + round(radius * 0.92 * scale), leaf_center_y),
                (cx + round(radius * 0.42 * scale), leaf_center_y + round(radius * 0.23 * scale)),
                (cx + round(radius * 0.48 * scale), leaf_center_y + round(radius * 0.62 * scale)),
                (cx, leaf_center_y + round(radius * 0.32 * scale)),
                (cx - round(radius * 0.48 * scale), leaf_center_y + round(radius * 0.62 * scale)),
                (cx - round(radius * 0.42 * scale), leaf_center_y + round(radius * 0.22 * scale)),
            ]
            self.pygame.draw.polygon(high, outline, outer_leaf)
            inner_leaf = [
                (
                    round(cx + (x - cx) * 0.82),
                    round(leaf_center_y + (y - leaf_center_y) * 0.78),
                )
                for x, y in outer_leaf
            ]
            self.pygame.draw.polygon(high, leaf, inner_leaf)
            self.pygame.draw.polygon(
                high,
                leaf_light,
                [inner_leaf[0], inner_leaf[1], inner_leaf[2], inner_leaf[12]],
            )

            stem_outer = [
                (cx - 3 * scale, leaf_center_y - round(radius * 0.42 * scale)),
                (cx + round(radius * 0.16 * scale), leaf_center_y - round(radius * 0.98 * scale)),
                (cx + round(radius * 0.34 * scale), leaf_center_y - round(radius * 1.08 * scale)),
                (cx + 5 * scale, leaf_center_y - round(radius * 0.32 * scale)),
            ]
            self.pygame.draw.polygon(high, outline, stem_outer)
            stem_inner = [
                (cx, leaf_center_y - round(radius * 0.42 * scale)),
                (cx + round(radius * 0.18 * scale), leaf_center_y - round(radius * 0.88 * scale)),
                (cx + round(radius * 0.26 * scale), leaf_center_y - round(radius * 0.94 * scale)),
                (cx + 3 * scale, leaf_center_y - round(radius * 0.35 * scale)),
            ]
            self.pygame.draw.polygon(high, leaf, stem_inner)

            highlight_rect = self.pygame.Rect(
                body_rect.left + round(radius * 0.18 * scale),
                body_rect.top + round(radius * 0.34 * scale),
                round(radius * 0.48 * scale),
                round(radius * 0.90 * scale),
            )
            self.pygame.draw.arc(
                high,
                (255, 114, 103, 225),
                highlight_rect,
                math.pi / 2,
                math.pi * 1.48,
                max(scale, 3 * scale),
            )
            for angle, length in ((0.36, 0.22), (0.70, 0.18)):
                mark_center = (
                    round(cx + math.cos(angle) * radius * 0.70 * scale),
                    round(cy + math.sin(angle) * radius * 0.70 * scale),
                )
                tangent = (-math.sin(angle), math.cos(angle))
                half = radius * length * scale / 2
                start = (
                    round(mark_center[0] - tangent[0] * half),
                    round(mark_center[1] - tangent[1] * half),
                )
                end = (
                    round(mark_center[0] + tangent[0] * half),
                    round(mark_center[1] + tangent[1] * half),
                )
                width = max(scale, 2 * scale)
                self.pygame.draw.line(high, outline, start, end, width)
                self.pygame.draw.circle(high, outline, start, width // 2)
                self.pygame.draw.circle(high, outline, end, width // 2)

            surface = self.pygame.transform.smoothscale(
                high,
                (native_size, native_size),
            )
            self.pomodoro_mark_cache[radius] = surface
        self.screen.blit(surface, surface.get_rect(center=center))

    def draw_pomodoro_duration_adjustment(
        self,
        now: float,
        snapshot: dict,
    ) -> None:
        phase = str(snapshot.get("phase", "focus"))
        focus_theme = phase == "focus"
        glow_rgb = (129, 39, 42) if focus_theme else (31, 104, 68)
        accent_color = (
            (222, 72, 66, 255)
            if focus_theme
            else (66, 181, 112, 255)
        )
        elapsed = max(0.0, now - self.pomodoro_duration_adjust_started_at)
        raw = min(1.0, elapsed / self.pomodoro_duration_adjust_seconds)
        progress = 1.0 - (1.0 - raw) ** 3
        center = self.pomodoro_dial_center()
        if progress >= 0.999:
            self.screen.blit(self.pomodoro_duration_adjust_base(phase), (0, 0))
        else:
            self.screen.fill((0, 0, 0))
            self.draw_aa_ring(
                self.screen,
                (42, 120, 148, 30),
                (self.width // 2, self.height // 2),
                min(self.width, self.height) // 2 - 10,
                width=3,
            )
            self.draw_gallery_back_button()
            self.draw_pomodoro_mark((318, 128), 20, phase)
            self.draw_centered_text(
                "番茄钟",
                self.font_pomodoro_title,
                (222, 241, 246),
                (425, 126),
            )
            ticks = self.pomodoro_duration_dial_ticks().copy()
            ticks.set_alpha(round(255 * progress))
            self.screen.blit(ticks, ticks.get_rect(center=center))
            radius = round(204 - 62 * progress)
            self.draw_aa_ring(
                self.screen,
                (*glow_rgb, round(48 + 28 * progress)),
                center,
                radius + 8,
                width=18,
            )
            self.draw_aa_ring(
                self.screen,
                (24, 53, 62, 238),
                center,
                radius,
                width=15,
            )
            self.draw_aa_ring(
                self.screen,
                accent_color,
                center,
                radius,
                width=16,
            )
            self.draw_centered_text(
            "松手设定 · 每次 1 分钟",
                self.font_small,
                (105, 157, 168),
                (400, 675),
            )

        pointer_step = round(
            self.pomodoro_duration_pointer_clockwise / math.tau * 72
        ) % 72
        wave = self.pomodoro_duration_wave_surface(pointer_step, phase)
        if progress < 0.999:
            wave = wave.copy()
            wave.set_alpha(round(255 * progress))
        self.screen.blit(wave, wave.get_rect(center=center))
        time_surface = self.pomodoro_duration_time_surface(
            self.pomodoro_duration_selected_minutes
        )
        self.screen.blit(time_surface, time_surface.get_rect(center=center))

    def draw_pomodoro_page(
        self,
        now: float,
        *,
        snapshot_override: dict | None = None,
        completion_alert: bool = False,
    ) -> None:
        snapshot = snapshot_override or self.pomodoro.snapshot()
        if self.pomodoro_duration_adjust_active and snapshot_override is None:
            self.draw_pomodoro_duration_adjustment(now, snapshot)
            return
        phase = snapshot["phase"]
        status = snapshot["status"]
        focus = phase == "focus"
        theme_accent = (222, 72, 66, 255) if focus else (66, 181, 112, 255)
        ring_accent = (0, 0, 0, 255) if completion_alert else theme_accent
        self.screen.fill(theme_accent[:3] if completion_alert else (0, 0, 0))
        self.draw_aa_ring(
            self.screen,
            (42, 120, 148, 30),
            (self.width // 2, self.height // 2),
            min(self.width, self.height) // 2 - 10,
            width=3,
        )
        self.draw_gallery_back_button()
        self.draw_pomodoro_statistics_button()
        self.draw_pomodoro_mark((318, 128), 20, phase)
        self.draw_centered_text(
            "番茄钟",
            self.font_pomodoro_title,
            (222, 241, 246),
            (425, 126),
        )

        center = self.pomodoro_dial_center()
        radius = 204
        progress_width = 16
        progress_inner_radius = radius - progress_width
        progress_outer_radius = radius
        # Keep the halo stable across ready/running. Status changes should alter
        # only the arc length, never its apparent diameter or brightness layer.
        glow_alpha = 24
        self.draw_aa_ring(
            self.screen,
            (*ring_accent[:3], glow_alpha),
            center,
            radius + 8,
            width=18,
        )
        self.draw_aa_ring(
            self.screen,
            (24, 53, 62, 238),
            center,
            radius,
            width=15,
        )
        fraction = float(snapshot["remaining_fraction"])
        if fraction >= 0.999:
            self.draw_aa_ring(
                self.screen,
                ring_accent,
                center,
                radius,
                width=progress_width,
            )
        elif fraction > 0.001:
            self.draw_aa_gradient_arc(
                self.screen,
                center,
                progress_inner_radius,
                progress_outer_radius,
                -math.pi / 2,
                -math.pi / 2 + math.tau * fraction,
                ring_accent,
                # A fixed accent prevents the gradient from being re-stretched
                # across the remaining span when running changes to paused.
                ring_accent,
            )
        self.draw_pomodoro_duration_crown(
            snapshot,
            completion_alert=completion_alert,
            completion_presentation=(
                snapshot_override is not None and status == "completed"
            ),
        )

        remaining = max(0, math.ceil(float(snapshot["remaining_seconds"])))
        minutes, seconds = divmod(remaining, 60)
        time_text = f"{minutes:02d}:{seconds:02d}"
        time_surface = self.font_pomodoro_time.render(
            time_text,
            True,
            (235, 246, 249),
        )
        self.screen.blit(time_surface, time_surface.get_rect(center=(400, 365)))
        phase_labels = {
            "focus": "专注",
            "short_break": "短休",
            "long_break": "长休",
        }
        status_labels = {
            "ready": "准备开始",
            "running": "进行中",
            "paused": "已暂停",
            "completed": "已完成",
        }
        self.draw_centered_text(
            f"{phase_labels[phase]} · {status_labels[status]}",
            self.font_pomodoro_label,
            theme_accent[:3],
            (400, 443),
        )

        dot_y = 505
        completed_in_cycle = int(snapshot["cycle_position"])
        dot_gap = 34
        dot_start = 400 - dot_gap * 1.5
        for index in range(int(snapshot["long_break_every"])):
            dot_center = (round(dot_start + index * dot_gap), dot_y)
            filled = index < completed_in_cycle
            self.draw_aa_circle(
                self.screen,
                theme_accent[:3] if filled else (40, 72, 80),
                dot_center,
                7 if filled else 6,
            )
        self.draw_centered_text(
            f"本轮 {completed_in_cycle}/4",
            self.font_small,
            (113, 151, 160),
            (400, 538),
        )

        controls = self.pomodoro_control_rects()
        for name, rect in controls.items():
            pressed = self.pointer_down and self.pomodoro_pointer_target == name
            role = "primary" if name == "primary" else "secondary"
            self.screen.blit(
                self.pomodoro_button_surface(
                    rect.size,
                    role,
                    pressed,
                    phase,
                    completion_alert=completion_alert and name == "primary",
                ),
                rect.topleft,
            )
        labels = {
            "reset": "重置",
            "primary": "暂停" if status == "running" else "开始",
            "skip": "跳过",
        }
        for name, rect in controls.items():
            self.draw_centered_text(
                labels[name],
                self.font_medium,
                (245, 249, 250) if name == "primary" else (189, 222, 230),
                rect.center,
            )
        self.draw_centered_text(
            (
                f"{round(self.pomodoro.duration_for('focus') / 60)} 分钟专注"
                " · 5 分钟休息"
            ),
            self.font_small,
            (84, 126, 137),
            (400, 716),
        )

    def pomodoro_completion_snapshot(self, completed_phase: str) -> dict:
        snapshot = self.pomodoro.snapshot()
        snapshot["phase"] = (
            completed_phase
            if completed_phase in {"focus", "short_break", "long_break"}
            else "focus"
        )
        snapshot["status"] = "completed"
        snapshot["remaining_seconds"] = 0.0
        # The completion signal keeps the full theme ring visible so its
        # colour-to-black inversion remains unambiguous during each pulse.
        snapshot["remaining_fraction"] = 1.0
        return snapshot

    def render_pomodoro_completion_surface(
        self,
        now: float,
        completed_phase: str,
        *,
        lit: bool,
    ) -> object:
        surface = self.pygame.Surface(self.target_size)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        original_target = self.pomodoro_pointer_target
        try:
            self.screen = surface
            self.pointer_down = False
            self.pomodoro_pointer_target = None
            self.draw_pomodoro_page(
                now,
                snapshot_override=self.pomodoro_completion_snapshot(completed_phase),
                completion_alert=lit,
            )
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
            self.pomodoro_pointer_target = original_target
        return surface

    def start_pomodoro_completion_alert(
        self,
        completed_phase: str,
        now: float,
    ) -> None:
        phase = (
            completed_phase
            if completed_phase in {"focus", "short_break", "long_break"}
            else "focus"
        )
        self.pomodoro_completion_alert_phase = phase
        # Rendering the two completion surfaces can take a fraction of a
        # second on the Pi. Start the pulse clock only after both caches are
        # ready so the visible animation never skips its black first frame.
        self.pomodoro_completion_alert_started_at = 0.0
        self.pomodoro_completion_alert_pulse = 1
        self.pomodoro_completion_alert_intensity = 0.0
        self.pomodoro_completion_alert_lit = False
        self.pomodoro_completion_alert_off_surface = (
            self.render_pomodoro_completion_surface(now, phase, lit=False)
        )
        self.pomodoro_completion_alert_on_surface = (
            self.render_pomodoro_completion_surface(now, phase, lit=True)
        )
        self.pomodoro_completion_alert_started_at = time.monotonic()
        self.pomodoro_completion_alert_active = True
        self.pointer_down = False
        self.pointer_moved = False
        self.pomodoro_pointer_target = None
        self.performance_pointer_target = None
        self.music_pointer_target = None
        self.camera_pointer_target = None
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        log(
            "pomodoro completion alert started "
            f"phase={phase} cycles={self.pomodoro_completion_alert_cycles}"
        )

    def update_pomodoro_completion_alert(self, now: float) -> bool:
        if not self.pomodoro_completion_alert_active:
            return False
        active, intensity, pulse = completion_flash_frame(
            now - self.pomodoro_completion_alert_started_at,
            cycles=self.pomodoro_completion_alert_cycles,
            cycle_seconds=self.pomodoro_completion_alert_cycle_seconds,
            rise_seconds=self.pomodoro_completion_alert_rise_seconds,
            hold_seconds=self.pomodoro_completion_alert_hold_seconds,
        )
        if not active:
            completed_phase = self.pomodoro_completion_alert_phase
            self.pomodoro_completion_alert_active = False
            self.pomodoro_completion_alert_started_at = 0.0
            self.pomodoro_completion_alert_phase = None
            self.pomodoro_completion_alert_pulse = self.pomodoro_completion_alert_cycles
            self.pomodoro_completion_alert_intensity = 0.0
            self.pomodoro_completion_alert_lit = False
            self.pomodoro_completion_alert_on_surface = None
            self.pomodoro_completion_alert_off_surface = None
            self.note_screensaver_activity(now)
            self.needs_redraw = True
            log(f"pomodoro completion alert finished phase={completed_phase}")
            return True
        lit = intensity >= 0.999
        changed = lit != self.pomodoro_completion_alert_lit or (
            pulse != self.pomodoro_completion_alert_pulse
        )
        self.pomodoro_completion_alert_intensity = intensity
        self.pomodoro_completion_alert_lit = lit
        self.pomodoro_completion_alert_pulse = pulse
        if changed:
            self.needs_redraw = True
        return changed

    def draw_pomodoro_completion_alert(self) -> None:
        off_surface = self.pomodoro_completion_alert_off_surface
        on_surface = self.pomodoro_completion_alert_on_surface
        if off_surface is None:
            self.screen.fill((0, 0, 0))
            return
        self.screen.blit(off_surface, (0, 0))
        intensity = max(
            0.0,
            min(float(self.pomodoro_completion_alert_intensity), 1.0),
        )
        if on_surface is None or intensity <= 0.0:
            return
        on_surface.set_alpha(round(255 * intensity))
        self.screen.blit(on_surface, (0, 0))
        on_surface.set_alpha(None)

    def update_pomodoro_timer(self, now: float) -> bool:
        completed_phase = self.pomodoro.phase
        if not self.pomodoro.update():
            return False
        self.pomodoro.save()
        self.pomodoro_phase_changed_at = now
        self.pomodoro_last_transition_serial = self.pomodoro.transition_serial
        self.start_pomodoro_completion_alert(completed_phase, now)
        self.needs_redraw = True
        log(
            "pomodoro phase completed "
            f"completed={completed_phase} next={self.pomodoro.phase}"
        )
        return True

    def draw_pomodoro_statistics_chrome(
        self,
        title: str,
        page: int,
    ) -> None:
        self.screen.fill((0, 0, 0))
        self.draw_aa_ring(
            self.screen,
            (42, 120, 148, 30),
            (self.width // 2, self.height // 2),
            min(self.width, self.height) // 2 - 10,
            width=3,
        )
        self.draw_gallery_back_button()
        self.draw_centered_text(
            title,
            self.font_large,
            (222, 241, 246),
            (400, 128),
        )
        for index, x in enumerate((389, 411)):
            active = index == page
            self.draw_aa_circle(
                self.screen,
                (100, 213, 231) if active else (39, 81, 91),
                (x, 716),
                5 if active else 4,
            )

    def draw_pomodoro_today_statistics(self, statistics: dict) -> None:
        today = statistics["today"]
        today_focus_seconds = float(today["focus_seconds"])
        today_break_seconds = float(today["break_seconds"])
        total_seconds = today_focus_seconds + today_break_seconds
        focus_ratio = (
            today_focus_seconds / total_seconds if total_seconds > 0.0 else 0.0
        )
        today_minutes = round(today_focus_seconds / 60.0)
        break_minutes = round(today_break_seconds / 60.0)

        chart_center = (400, 352)
        chart_radius = 136
        chart_width = 34
        self.draw_aa_ring(
            self.screen,
            (25, 56, 65, 245),
            chart_center,
            chart_radius,
            chart_width,
        )
        start = -math.pi / 2
        if total_seconds > 0.0:
            if today_break_seconds <= 0.0:
                self.draw_aa_ring(
                    self.screen,
                    (229, 75, 67),
                    chart_center,
                    chart_radius,
                    chart_width,
                )
            elif today_focus_seconds <= 0.0:
                self.draw_aa_ring(
                    self.screen,
                    (72, 184, 113),
                    chart_center,
                    chart_radius,
                    chart_width,
                )
            else:
                split = start + math.tau * focus_ratio
                focus_span = math.tau * focus_ratio
                break_span = math.tau - focus_span
                gap = min(
                    math.radians(2.5),
                    focus_span * 0.18,
                    break_span * 0.18,
                )
                self.draw_aa_ring(
                    self.screen,
                    (229, 75, 67),
                    chart_center,
                    chart_radius,
                    chart_width,
                    start + gap,
                    split - gap,
                )
                self.draw_aa_ring(
                    self.screen,
                    (72, 184, 113),
                    chart_center,
                    chart_radius,
                    chart_width,
                    split + gap,
                    start + math.tau - gap,
                )
        self.draw_centered_text(
            str(today_minutes),
            self.font_pomodoro_stat_value,
            (240, 247, 249),
            (400, 334),
        )
        self.draw_centered_text(
            "今日专注 · 分钟",
            self.font_small,
            (113, 166, 178),
            (400, 383),
        )
        self.draw_aa_circle(self.screen, (229, 75, 67), (335, 516), 5)
        self.draw_centered_text(
            "专注",
            self.font_small,
            (167, 199, 207),
            (370, 516),
        )
        self.draw_aa_circle(self.screen, (72, 184, 113), (434, 516), 5)
        self.draw_centered_text(
            "休息",
            self.font_small,
            (167, 199, 207),
            (474, 516),
        )
        metrics = (
            ("今日番茄", str(int(today["completed_focus_sessions"]))),
            ("专注占比", f"{round(focus_ratio * 100)}%"),
            ("今日休息", f"{break_minutes}分"),
        )
        for x, (label, value) in zip((225, 400, 575), metrics):
            self.draw_centered_text(
                value,
                self.font_large,
                (231, 242, 245),
                (x, 585),
            )
            self.draw_centered_text(
                label,
                self.font_small,
                (92, 142, 154),
                (x, 624),
            )
        self.draw_centered_text(
            "向左滑查看近 7 天",
            self.font_small,
            (67, 108, 119),
            (400, 674),
        )

    def draw_pomodoro_week_statistics(self, statistics: dict) -> None:
        seven_days = statistics["seven_days"]
        focus_minutes = round(
            float(statistics["seven_day_focus_seconds"]) / 60.0
        )
        break_minutes = round(
            float(statistics["seven_day_break_seconds"]) / 60.0
        )
        completed = int(statistics["seven_day_completed_focus_sessions"])
        daily_average = round(focus_minutes / 7.0)
        metrics = (
            ("专注累计", f"{focus_minutes}分"),
            ("完成番茄", str(completed)),
            ("日均专注", f"{daily_average}分"),
        )
        for x, (label, value) in zip((225, 400, 575), metrics):
            self.draw_centered_text(
                value,
                self.font_large,
                (231, 242, 245),
                (x, 225),
            )
            self.draw_centered_text(
                label,
                self.font_small,
                (92, 142, 154),
                (x, 264),
            )
        self.draw_centered_text(
            f"休息累计 {break_minutes} 分钟",
            self.font_small,
            (97, 154, 136),
            (400, 304),
        )
        self.draw_centered_text(
            "每日专注分钟",
            self.font_small,
            (142, 184, 194),
            (400, 344),
        )

        chart_width_native = 490
        chart_height_native = 230
        chart_left = (self.width - chart_width_native) // 2
        chart_top = 356
        scale = UI_AA_SCALE
        chart = self.pygame.Surface(
            (chart_width_native * scale, chart_height_native * scale),
            self.pygame.SRCALPHA,
        )
        chart.fill((0, 0, 0, 0))
        recorded_maximum = max(
            float(day["focus_seconds"]) for day in seven_days
        )
        maximum = max(1.0, recorded_maximum)
        bar_positions: list[int] = []
        bar_heights: list[int] = []
        baseline = 196
        for index, day in enumerate(seven_days):
            x = 35 + index * 70
            bar_positions.append(x)
            seconds = float(day["focus_seconds"])
            height = 7 if seconds <= 0.0 else max(
                12,
                round(158 * seconds / maximum),
            )
            bar_heights.append(height)
            rect = self.pygame.Rect(
                (x - 14) * scale,
                (baseline - height) * scale,
                28 * scale,
                height * scale,
            )
            color = (
                (241, 105, 91, 245)
                if index == len(seven_days) - 1
                else (151, 57, 57, 230)
            )
            if seconds <= 0.0:
                color = (34, 72, 82, 220)
            self.pygame.draw.rect(
                chart,
                color,
                rect,
                border_radius=14 * scale,
            )
        chart = self.pygame.transform.smoothscale(
            chart,
            (chart_width_native, chart_height_native),
        )
        self.screen.blit(chart, (chart_left, chart_top))
        if recorded_maximum <= 0.0:
            self.draw_pomodoro_mark((400, 438), 30, "focus")
            self.draw_centered_text(
                "开始一次专注后，这里会出现趋势",
                self.font_small,
                (82, 133, 144),
                (400, 492),
            )
        for x, height, day in zip(bar_positions, bar_heights, seven_days):
            minutes = round(float(day["focus_seconds"]) / 60.0)
            if minutes > 0:
                self.draw_centered_text(
                    str(minutes),
                    self.font_small,
                    (150, 190, 199),
                    (chart_left + x, chart_top + baseline - height - 18),
                )
            self.draw_centered_text(
                str(day["weekday"]),
                self.font_small,
                (100, 145, 156),
                (chart_left + x, chart_top + 220),
            )
        self.draw_centered_text(
            "向右滑返回今日",
            self.font_small,
            (67, 108, 119),
            (400, 674),
        )

    def draw_pomodoro_statistics_page(
        self,
        now: float,
        page: int | None = None,
    ) -> None:
        del now
        selected_page = (
            self.pomodoro_statistics_page if page is None else int(page)
        )
        statistics = self.pomodoro.statistics_snapshot()
        if selected_page == 0:
            self.draw_pomodoro_statistics_chrome("专注统计", 0)
            self.draw_pomodoro_today_statistics(statistics)
        else:
            self.draw_pomodoro_statistics_chrome("近 7 天", 1)
            self.draw_pomodoro_week_statistics(statistics)

    def render_pomodoro_surface(
        self,
        now: float | None = None,
        *,
        statistics: bool | None = None,
        statistics_page: int | None = None,
    ) -> object:
        surface = self.pygame.Surface(self.target_size)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        original_target = self.pomodoro_pointer_target
        try:
            self.screen = surface
            self.pointer_down = False
            self.pomodoro_pointer_target = None
            render_now = time.monotonic() if now is None else now
            show_statistics = (
                self.pomodoro_statistics_active
                if statistics is None
                else bool(statistics)
            )
            if show_statistics:
                self.draw_pomodoro_statistics_page(
                    render_now,
                    page=statistics_page,
                )
            else:
                self.draw_pomodoro_page(render_now)
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
            self.pomodoro_pointer_target = original_target
        return surface

    def draw_pomodoro(self, now: float) -> None:
        if not self.pomodoro_transition_active:
            if self.pomodoro_statistics_active:
                self.draw_pomodoro_statistics_page(now)
            else:
                self.draw_pomodoro_page(now)
            return
        source = self.pomodoro_transition_source
        target = self.pomodoro_transition_target
        if source is None or target is None:
            self.pomodoro_transition_active = False
            if self.pomodoro_statistics_active:
                self.draw_pomodoro_statistics_page(now)
            else:
                self.draw_pomodoro_page(now)
            return
        raw = min(
            1.0,
            max(
                0.0,
                (now - self.pomodoro_transition_started_at)
                / self.pomodoro_transition_seconds,
            ),
        )
        if raw >= 1.0:
            self.screen.blit(target, (0, 0))
            self.pomodoro_transition_active = False
            self.pomodoro_transition_source = None
            self.pomodoro_transition_target = None
            if self.pomodoro_transition_exits_page:
                self.pomodoro_active = False
                self.pomodoro_statistics_active = False
                self.pomodoro_statistics_page = 0
                self.pomodoro_pointer_target = None
                self.complete_application_menu_return(now)
                self.note_screensaver_activity(now)
                log("pomodoro page exit transition completed")
            self.pomodoro_transition_exits_page = False
            self.write_state()
            return
        eased = 1.0 - (1.0 - raw) ** 3
        direction = 1 if self.pomodoro_transition_opening else -1
        travel = self.width
        source_x = -round(direction * travel * eased)
        target_x = round(direction * travel * (1.0 - eased))
        self.screen.fill((0, 0, 0))
        self.screen.blit(source, (source_x, 0))
        self.screen.blit(target, (target_x, 0))

    def request_performance_refresh(self, now: float | None = None) -> bool:
        requested_at = time.monotonic() if now is None else now
        if self.performance_future is not None:
            return False
        self.performance_future = self.performance_executor.submit(
            self.performance_monitor.collect
        )
        self.performance_next_update_at = (
            requested_at + self.performance_refresh_seconds
        )
        return True

    def update_performance_async(self, now: float) -> bool:
        changed = False
        if self.performance_future is not None and self.performance_future.done():
            try:
                snapshot = self.performance_future.result()
                changed = snapshot != self.performance_snapshot
                self.performance_snapshot = snapshot
                if (
                    changed
                    and self.performance_transition_active
                    and self.performance_transition_opening
                ):
                    self.performance_transition_target = (
                        self.render_performance_surface(now)
                    )
            except Exception as exc:
                log(f"performance snapshot warning: {exc}")
            finally:
                self.performance_future = None
                self.performance_next_update_at = (
                    now + self.performance_refresh_seconds
                )
        if (
            self.performance_active
            and self.performance_future is None
            and now >= self.performance_next_update_at
        ):
            self.request_performance_refresh(now)
        return changed

    def performance_background_surface(self) -> object:
        if self.performance_static_surface is not None:
            return self.performance_static_surface
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (self.width * scale, self.height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 255))

        def scaled_rect(rect: tuple[int, int, int, int]) -> object:
            return self.pygame.Rect(*(round(value * scale) for value in rect))

        for rect in (
            (110, 184, 580, 128),
            (110, 328, 580, 110),
            (110, 454, 580, 110),
            (118, 585, 174, 88),
            (313, 585, 174, 88),
            (508, 585, 174, 88),
        ):
            target = scaled_rect(rect)
            self.pygame.draw.rect(
                high,
                (5, 19, 27, 246),
                target,
                border_radius=round(24 * scale),
            )
            self.pygame.draw.rect(
                high,
                (25, 79, 94, 176),
                target,
                width=max(1, round(1.25 * scale)),
                border_radius=round(24 * scale),
            )
        self.pygame.draw.circle(
            high,
            (22, 81, 98, 88),
            (self.width * scale // 2, self.height * scale // 2),
            round((min(self.width, self.height) // 2 - 10) * scale),
            width=max(1, round(2 * scale)),
        )
        self.performance_static_surface = self.pygame.transform.smoothscale(
            high,
            self.target_size,
        )
        return self.performance_static_surface

    @staticmethod
    def performance_accent(percent: float) -> tuple[int, int, int]:
        if percent >= 90.0:
            return 255, 91, 91
        if percent >= 75.0:
            return 255, 177, 79
        return 63, 211, 239

    def draw_performance_bar(
        self,
        start: tuple[int, int],
        end_x: int,
        percent: float,
        color: tuple[int, int, int],
    ) -> None:
        self.draw_aa_round_line(
            self.screen,
            (21, 51, 61, 255),
            start,
            (end_x, start[1]),
            10,
        )
        fraction = max(0.0, min(float(percent) / 100.0, 1.0))
        if fraction > 0.001:
            self.draw_aa_round_line(
                self.screen,
                (*color, 255),
                start,
                (start[0] + (end_x - start[0]) * fraction, start[1]),
                10,
            )

    def draw_performance_page(self, now: float) -> None:
        if self.performance_section == "system":
            self.draw_performance_health_page(now)
            return
        del now
        snapshot = self.performance_snapshot
        self.screen.blit(self.performance_background_surface(), (0, 0))
        self.draw_gallery_back_button()
        self.draw_centered_text(
            "性能",
            self.font_large,
            (226, 245, 249),
            (400, 105),
        )
        live_color = (92, 237, 164) if snapshot.sampled_at else (94, 139, 151)
        self.draw_centered_text(
            "● LIVE  1s",
            self.font_status,
            live_color,
            (400, 143),
        )

        cpu_color = self.performance_accent(snapshot.cpu_percent)
        cpu_label = self.font_status.render("CPU", True, (111, 181, 199))
        self.screen.blit(cpu_label, (145, 203))
        cpu_value = self.font_performance_value.render(
            f"{snapshot.cpu_percent:4.1f}%",
            True,
            cpu_color,
        )
        self.screen.blit(cpu_value, cpu_value.get_rect(topright=(655, 194)))
        detail = self.font_status.render(
            f"{snapshot.cpu_temp_c:.0f}°C   {snapshot.cpu_freq_mhz / 1000.0:.2f} GHz   LOAD {snapshot.load_1:.2f}",
            True,
            (176, 210, 219),
        )
        self.screen.blit(detail, (145, 245))
        self.draw_performance_bar((145, 288), 655, snapshot.cpu_percent, cpu_color)

        memory_color = self.performance_accent(snapshot.memory_percent)
        memory_label = self.font_status.render("MEMORY", True, (111, 181, 199))
        self.screen.blit(memory_label, (145, 349))
        memory_value = self.font_performance_value.render(
            f"{snapshot.memory_percent:4.1f}%",
            True,
            memory_color,
        )
        self.screen.blit(memory_value, memory_value.get_rect(topright=(655, 340)))
        memory_detail = self.font_status.render(
            f"{format_bytes(snapshot.memory_used_bytes)} / {format_bytes(snapshot.memory_total_bytes)}",
            True,
            (151, 193, 205),
        )
        self.screen.blit(memory_detail, (145, 382))
        self.draw_performance_bar((145, 417), 655, snapshot.memory_percent, memory_color)

        disk_color = self.performance_accent(snapshot.disk_percent)
        disk_label = self.font_status.render("NVME", True, (111, 181, 199))
        self.screen.blit(disk_label, (145, 475))
        disk_value = self.font_performance_value.render(
            f"{snapshot.disk_percent:4.1f}%",
            True,
            disk_color,
        )
        self.screen.blit(disk_value, disk_value.get_rect(topright=(655, 466)))
        disk_detail = self.font_status.render(
            f"{format_bytes(snapshot.disk_used_bytes)} / {format_bytes(snapshot.disk_total_bytes)}",
            True,
            (151, 193, 205),
        )
        self.screen.blit(disk_detail, (145, 508))
        self.draw_performance_bar((145, 543), 655, snapshot.disk_percent, disk_color)

        hailo_state = (
            "ACTIVE" if snapshot.hailo_active else "READY" if snapshot.hailo_ready else "OFFLINE"
        )
        hailo_color = (
            (113, 245, 158)
            if snapshot.hailo_ready
            else (255, 111, 111)
        )
        self.draw_centered_text("HAILO-8", self.font_status, (103, 164, 181), (205, 611))
        self.draw_centered_text(hailo_state, self.font_status, hailo_color, (205, 647))

        self.draw_centered_text("NETWORK", self.font_status, (103, 164, 181), (400, 611))
        network_text = (
            f"↓{format_bytes(snapshot.network_rx_bps, per_second=True).replace(' ', '')} "
            f"↑{format_bytes(snapshot.network_tx_bps, per_second=True).replace(' ', '')}"
        )
        network_surface = self.font_status.render(network_text, True, (189, 224, 232))
        if network_surface.get_width() > 154:
            network_surface = self.pygame.transform.smoothscale(
                network_surface,
                (154, network_surface.get_height()),
            )
        self.screen.blit(network_surface, network_surface.get_rect(center=(400, 647)))

        health_ok = (
            snapshot.health_total_count > 0
            and snapshot.health_healthy_count == snapshot.health_total_count
        )
        self.draw_centered_text("SYSTEM", self.font_status, (103, 164, 181), (595, 611))
        self.draw_centered_text(
            f"{snapshot.health_healthy_count}/{snapshot.health_total_count}  ›",
            self.font_status,
            (113, 245, 158) if health_ok else (255, 177, 79),
            (595, 647),
        )

        footer = (
            f"DSI {self.display_refresh_hz:.1f} Hz  •  RSS {format_bytes(snapshot.process_rss_bytes)}"
            f"  •  UP {self.format_uptime(snapshot.uptime_seconds)}"
        )
        self.draw_centered_text(
            footer,
            self.font_status,
            (81, 139, 155),
            (400, 708),
        )

    def performance_system_rect(self) -> object:
        return self.pygame.Rect(508, 585, 174, 88)

    def performance_health_checks(
        self,
    ) -> tuple[tuple[str, str, bool, str], ...]:
        indexed = tuple(enumerate(self.system_status.health_checks))
        return tuple(
            record
            for _index, record in sorted(
                indexed,
                key=lambda item: (item[1][2], item[0]),
            )
        )

    def performance_health_viewport_rect(self) -> object:
        return self.pygame.Rect(105, 258, 590, 368)

    def performance_recovery_rect(self) -> object:
        return self.pygame.Rect(220, 164, 360, 66)

    def performance_recovery_surface(self, pressed: bool = False) -> object:
        state = str(self.recovery_status.get("state") or "idle")
        key = (state, pressed)
        cached = self.recovery_button_cache.get(key)
        if cached is not None:
            self.recovery_button_cache.move_to_end(key)
            return cached
        size = self.performance_recovery_rect().size
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (size[0] * scale, size[1] * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        if state == "running":
            fill, border, text_color = (17, 67, 82, 250), (74, 211, 247, 235), (205, 244, 251)
        elif state == "failed":
            fill, border, text_color = (52, 31, 18, 250), (236, 148, 73, 235), (255, 209, 158)
        elif state == "complete":
            fill, border, text_color = (15, 55, 43, 250), (92, 237, 164, 225), (194, 248, 220)
        else:
            fill, border, text_color = (5, 28, 39, 250), (50, 154, 185, 210), (214, 240, 247)
        if pressed:
            fill = tuple(min(255, channel + 14) for channel in fill[:3]) + (fill[3],)
        rect = high.get_rect()
        self.pygame.draw.rect(high, fill, rect, border_radius=28 * scale)
        self.pygame.draw.rect(
            high,
            border,
            rect,
            width=max(1, scale),
            border_radius=28 * scale,
        )
        label = {
            "running": "正在恢复…",
            "failed": "再次尝试恢复",
            "complete": "再次检查并恢复",
        }.get(state, "恢复全部服务")
        surface = self.pygame.transform.smoothscale(high, size)
        rendered = self.font_medium.render(label, True, text_color)
        surface.blit(
            rendered,
            rendered.get_rect(center=surface.get_rect().center),
        )
        self.recovery_button_cache[key] = surface
        while len(self.recovery_button_cache) > 8:
            self.recovery_button_cache.popitem(last=False)
        return surface

    def read_recovery_status(self, now: float | None = None, force: bool = False) -> bool:
        current = time.monotonic() if now is None else now
        if not force and current < self.recovery_status_next_read_at:
            return False
        self.recovery_status_next_read_at = current + (
            0.5 if self.recovery_status.get("state") == "running" else 2.0
        )
        try:
            payload = json.loads(RECOVERY_STATUS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("recovery status must be an object")
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"state": "idle", "message": "恢复全部服务"}
        changed = payload != self.recovery_status
        self.recovery_status = payload
        return changed

    def request_full_recovery(self) -> bool:
        if self.recovery_future is not None:
            return False
        self.recovery_status = {"state": "running", "message": "正在恢复服务"}
        self.recovery_status_next_read_at = time.monotonic() + 0.5
        self.recovery_future = self.control_executor.submit(request_recovery)
        self.needs_redraw = True
        self.write_state()
        log("full service recovery requested")
        return True

    def update_full_recovery(self, now: float) -> bool:
        changed = False
        if self.recovery_future is not None and self.recovery_future.done():
            try:
                response = self.recovery_future.result()
                if not response.get("ok") and response.get("state") != "running":
                    self.recovery_status = {
                        "state": "failed",
                        "message": str(response.get("error") or "恢复请求被拒绝"),
                    }
            except Exception as exc:
                self.recovery_status = {
                    "state": "failed",
                    "message": f"恢复控制器不可用：{exc}",
                }
            self.recovery_future = None
            self.recovery_status_next_read_at = 0.0
            changed = True
        if self.performance_active and self.performance_section == "system":
            changed = self.read_recovery_status(now) or changed
        if changed:
            self.recovery_button_cache.clear()
            self.needs_redraw = True
        return changed

    def performance_health_content_height(self) -> int:
        count = len(self.performance_health_checks())
        return max(1, count * 68 + max(0, count - 1) * 8)

    def performance_health_max_scroll(self) -> float:
        viewport = self.performance_health_viewport_rect()
        return float(max(0, self.performance_health_content_height() - viewport.height))

    def clamp_performance_health_scroll(self, value: float) -> float:
        return max(0.0, min(float(value), self.performance_health_max_scroll()))

    @staticmethod
    def performance_health_detail_summary(detail: str) -> str:
        value = str(detail or "").strip()
        if not value:
            return "检查未通过"
        drift_match = re.search(r'"drift_count"\s*:\s*(\d+)', value)
        if drift_match and int(drift_match.group(1)):
            return f"{int(drift_match.group(1))} 项文件与正式基线不一致"
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            drift_count = int(payload.get("drift_count") or 0)
            if drift_count:
                return f"{drift_count} 项文件与正式基线不一致"
            error = str(payload.get("error") or "").strip()
            if error:
                return error
        replacements = {
            "size_mismatch": "文件尺寸与正式基线不一致",
            "sha256_mismatch": "文件内容校验不一致",
            "missing": "必要文件缺失",
        }
        for marker, summary in replacements.items():
            if marker in value:
                return summary
        return value

    def performance_health_card_surface(
        self,
        size: tuple[int, int],
        healthy: bool,
    ) -> object:
        key = (size, healthy)
        cached = self.performance_health_card_cache.get(key)
        if cached is not None:
            return cached
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (size[0] * scale, size[1] * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        rect = high.get_rect()
        fill = (5, 19, 27, 246) if healthy else (38, 23, 13, 250)
        border = (25, 79, 94, 176) if healthy else (183, 104, 42, 220)
        self.pygame.draw.rect(
            high,
            fill,
            rect,
            border_radius=20 * scale,
        )
        self.pygame.draw.rect(
            high,
            border,
            rect,
            width=max(1, round(1.25 * scale)),
            border_radius=20 * scale,
        )
        surface = self.pygame.transform.smoothscale(high, size)
        self.performance_health_card_cache[key] = surface
        return surface

    def performance_health_content_surface(self) -> object:
        records = self.performance_health_checks()
        cache_key = records
        if (
            self.performance_health_content_cache_surface is not None
            and self.performance_health_content_cache_key == cache_key
        ):
            return self.performance_health_content_cache_surface
        if (
            self.performance_health_content_cache_key is not None
            and self.performance_health_content_cache_key != cache_key
        ):
            # A newly failed or recovered item should be visible immediately.
            self.performance_health_scroll_offset = 0.0
            self.performance_health_scroll_start_offset = 0.0
        viewport = self.performance_health_viewport_rect()
        surface = self.pygame.Surface(
            (viewport.width, self.performance_health_content_height()),
            self.pygame.SRCALPHA,
        )
        surface.fill((0, 0, 0, 0))
        height = 68
        gap = 8
        for index, (_check_id, name, healthy, detail) in enumerate(records):
            rect = self.pygame.Rect(0, index * (height + gap), viewport.width, height)
            surface.blit(
                self.performance_health_card_surface(rect.size, healthy),
                rect.topleft,
            )
            color = (92, 237, 164) if healthy else (255, 177, 79)
            self.draw_aa_circle(
                surface,
                (*color, 255),
                (rect.left + 27, rect.centery),
                6,
            )
            if healthy:
                name_surface = self.font_small.render(
                    self.ellipsize_text(name, self.font_small, 410),
                    True,
                    (212, 235, 241),
                )
                surface.blit(
                    name_surface,
                    name_surface.get_rect(midleft=(rect.left + 49, rect.centery)),
                )
            else:
                name_surface = self.font_small.render(
                    self.ellipsize_text(name, self.font_small, 410),
                    True,
                    (236, 239, 238),
                )
                detail_surface = self.font_status.render(
                    self.ellipsize_text(
                        self.performance_health_detail_summary(detail),
                        self.font_status,
                        410,
                    ),
                    True,
                    (205, 157, 112),
                )
                surface.blit(name_surface, (rect.left + 49, rect.top + 8))
                surface.blit(detail_surface, (rect.left + 49, rect.top + 38))
            state_surface = self.font_status.render(
                "正常" if healthy else "异常",
                True,
                color,
            )
            surface.blit(
                state_surface,
                state_surface.get_rect(midright=(rect.right - 25, rect.centery)),
            )
        self.performance_health_content_cache_key = cache_key
        self.performance_health_content_cache_surface = surface
        return surface

    def draw_performance_health_page(self, _now: float) -> None:
        self.screen.blit(self.settings_background_surface(), (0, 0))
        self.draw_gallery_back_button()
        records = self.performance_health_checks()
        healthy_count = sum(1 for _id, _name, healthy, _detail in records if healthy)
        failed_count = len(records) - healthy_count
        self.draw_centered_text(
            "SYSTEM",
            self.font_large,
            (226, 245, 249),
            (400, 92),
        )
        if records:
            summary = (
                f"{healthy_count}/{len(records)} 正常"
                if not failed_count
                else f"{healthy_count}/{len(records)} 正常  ·  {failed_count} 项异常"
            )
            summary_color = (113, 245, 158) if not failed_count else (255, 177, 79)
        else:
            summary = "正在读取检查结果"
            summary_color = (111, 177, 195)
        self.draw_centered_text(summary, self.font_status, summary_color, (400, 139))

        recovery_rect = self.performance_recovery_rect()
        recovery_pressed = (
            self.pointer_down and self.performance_pointer_target == "recover"
        )
        self.screen.blit(
            self.performance_recovery_surface(recovery_pressed),
            recovery_rect.topleft,
        )

        if not records:
            self.draw_centered_text(
                "健康监控正在刷新…",
                self.font_medium,
                (151, 193, 205),
                (400, 400),
            )
            return

        viewport = self.performance_health_viewport_rect()
        self.performance_health_scroll_offset = self.clamp_performance_health_scroll(
            self.performance_health_scroll_offset
        )
        content = self.performance_health_content_surface()
        source_area = self.pygame.Rect(
            0,
            round(self.performance_health_scroll_offset),
            viewport.width,
            min(viewport.height, content.get_height()),
        )
        self.screen.blit(content, viewport.topleft, source_area)

        maximum_scroll = self.performance_health_max_scroll()
        if maximum_scroll > 0:
            track_top = viewport.top + 10
            track_bottom = viewport.bottom - 10
            track_height = track_bottom - track_top
            thumb_height = max(
                42,
                round(track_height * viewport.height / content.get_height()),
            )
            thumb_travel = max(1, track_height - thumb_height)
            thumb_top = track_top + round(
                thumb_travel * self.performance_health_scroll_offset / maximum_scroll
            )
            self.draw_aa_round_line(
                self.screen,
                (30, 75, 88, 170),
                (714, track_top),
                (714, track_bottom),
                4,
            )
            self.draw_aa_round_line(
                self.screen,
                (77, 199, 226, 235),
                (714, thumb_top),
                (714, thumb_top + thumb_height),
                5,
            )
        self.draw_centered_text(
            f"上下滑动查看 {len(records)} 项检查  ·  右滑返回",
            self.font_status,
            (81, 139, 155),
            (400, 708),
        )

    def render_performance_surface(self, now: float | None = None) -> object:
        surface = self.pygame.Surface(self.target_size)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        original_target = self.performance_pointer_target
        try:
            self.screen = surface
            self.pointer_down = False
            self.performance_pointer_target = None
            self.draw_performance_page(time.monotonic() if now is None else now)
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
            self.performance_pointer_target = original_target
        return surface

    def open_performance(self) -> None:
        if self.performance_active:
            return
        now = time.monotonic()
        self.performance_transition_source = self.screen.copy()
        self.performance_active = True
        self.performance_section = None
        self.performance_health_scroll_offset = 0.0
        self.performance_health_scroll_start_offset = 0.0
        self.performance_pointer_target = None
        self.performance_transition_active = True
        self.performance_transition_opening = True
        self.performance_transition_started_at = now
        self.performance_transition_exits_page = False
        self.request_performance_refresh(now)
        self.performance_transition_target = self.render_performance_surface(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("performance page opened")

    def close_performance(self, animated: bool = True) -> None:
        if not self.performance_active:
            return
        now = time.monotonic()
        return_target = self.prepare_application_menu_return(now)
        self.performance_pointer_target = None
        if animated:
            self.performance_transition_source = self.screen.copy()
            self.performance_transition_target = return_target
            self.performance_transition_active = True
            self.performance_transition_opening = False
            self.performance_transition_started_at = now
            self.performance_transition_exits_page = True
        else:
            self.performance_active = False
            self.performance_section = None
            self.performance_health_scroll_offset = 0.0
            self.performance_health_scroll_start_offset = 0.0
            self.performance_transition_active = False
            self.performance_transition_source = None
            self.performance_transition_target = None
            self.performance_transition_exits_page = False
            self.complete_application_menu_return(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("performance page close requested")

    def performance_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.pomodoro_back_center()) <= 50:
            return "back"
        if (
            self.performance_section == "system"
            and self.performance_recovery_rect().collidepoint(position)
        ):
            return "recover"
        if (
            self.performance_section is None
            and self.performance_system_rect().collidepoint(position)
        ):
            return "system"
        return None

    def start_performance_section_transition(
        self,
        section: str | None,
        direction: int,
    ) -> bool:
        if self.performance_transition_active:
            return False
        self.performance_transition_source = self.screen.copy()
        self.performance_section = section
        if section is None:
            self.performance_health_scroll_offset = 0.0
            self.performance_health_scroll_start_offset = 0.0
        self.performance_transition_target = self.render_performance_surface()
        self.performance_transition_active = True
        self.performance_transition_opening = direction >= 0
        self.performance_transition_started_at = time.monotonic()
        self.performance_transition_exits_page = False
        self.performance_pointer_target = None
        self.needs_redraw = True
        self.write_state()
        return True

    def draw_performance(self, now: float) -> None:
        if not self.performance_transition_active:
            self.draw_performance_page(now)
            return
        source = self.performance_transition_source
        target = self.performance_transition_target
        if source is None or target is None:
            self.performance_transition_active = False
            self.draw_performance_page(now)
            return
        raw = min(
            1.0,
            max(
                0.0,
                (now - self.performance_transition_started_at)
                / self.performance_transition_seconds,
            ),
        )
        if raw >= 1.0:
            self.screen.blit(target, (0, 0))
            self.performance_transition_active = False
            self.performance_transition_source = None
            self.performance_transition_target = None
            if self.performance_transition_exits_page:
                self.performance_active = False
                self.performance_section = None
                self.performance_health_scroll_offset = 0.0
                self.performance_health_scroll_start_offset = 0.0
                self.performance_pointer_target = None
                self.complete_application_menu_return(now)
                log("performance page exit transition completed")
            self.performance_transition_exits_page = False
            self.write_state()
            return
        eased = 1.0 - (1.0 - raw) ** 3
        direction = 1 if self.performance_transition_opening else -1
        travel = self.width
        source_x = -round(direction * travel * eased)
        target_x = round(direction * travel * (1.0 - eased))
        self.screen.fill((0, 0, 0))
        self.screen.blit(source, (source_x, 0))
        self.screen.blit(target, (target_x, 0))

    @staticmethod
    def read_workshop_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def refresh_workshop_status(self, now: float, *, force: bool = False) -> bool:
        if not force and now < self.workshop_status_next_read_at:
            return False
        status = self.read_workshop_json(WORKSHOP_STATUS_PATH)
        runtime_status = self.read_workshop_json(WORKSHOP_RUNTIME_PATH)
        changed = (
            status != self.workshop_status
            or runtime_status != self.workshop_runtime_status
        )
        self.workshop_status = status
        self.workshop_runtime_status = runtime_status
        self.workshop_status_next_read_at = now + (
            0.5 if self.workshop_section == "runtime" else 1.0
        )
        return changed

    def workshop_pending_review(self) -> dict | None:
        proposal = self.workshop_status.get("pendingReview")
        return dict(proposal) if isinstance(proposal, dict) else None

    def workshop_installed_apps(self) -> list[dict]:
        apps = self.workshop_status.get("installedApps")
        if not isinstance(apps, list):
            return []
        return [dict(item) for item in apps if isinstance(item, dict)]

    def workshop_control_rects(self) -> dict[str, object]:
        if self.workshop_section == "review":
            approve = self.pygame.Rect(0, 0, 270, 70)
            reject = self.pygame.Rect(0, 0, 180, 70)
            approve.center = (312, 650)
            reject.center = (555, 650)
            return {"approve": approve, "reject": reject}
        if self.workshop_section == "installed":
            result: dict[str, object] = {}
            for index, app in enumerate(self.workshop_installed_apps()[:4]):
                rect = self.pygame.Rect(0, 0, 420, 72)
                rect.center = (400, 286 + index * 86)
                result[f"app:{app.get('id', '')}"] = rect
            return result
        if self.workshop_section == "runtime":
            stop = self.pygame.Rect(0, 0, 250, 70)
            stop.center = (400, 650)
            return {"stop_runtime": stop}
        create = self.pygame.Rect(0, 0, 360, 78)
        installed = self.pygame.Rect(0, 0, 210, 70)
        import_app = self.pygame.Rect(0, 0, 210, 70)
        create.center = (400, 452)
        installed.center = (282, 555)
        import_app.center = (518, 555)
        return {
            "create": create,
            "installed": installed,
            "import": import_app,
        }

    @staticmethod
    def workshop_installed_app_count() -> int:
        try:
            payload = json.loads(WORKSHOP_REGISTRY_PATH.read_text(encoding="utf-8"))
            apps = payload.get("apps") if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return 0
        return len(apps) if isinstance(apps, dict) else 0

    def workshop_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.gallery_back_button_center()) <= 50:
            return "back"
        for name, rect in self.workshop_control_rects().items():
            if rect.inflate(14, 14).collidepoint(position):
                return name
        return None

    def draw_workshop_emblem(self) -> None:
        center = (400, 310)
        if self.workshop_emblem_surface is None:
            size = 192
            local_center = (size // 2, size // 2)
            surface = self.pygame.Surface((size, size), self.pygame.SRCALPHA)
            surface.fill((0, 0, 0, 0))
            self.draw_aa_circle(surface, (4, 24, 33), local_center, 92)
            self.draw_aa_ring(surface, (41, 137, 168), local_center, 92, 2)
            colors = (
                (112, 224, 246),
                (71, 194, 230),
                (35, 137, 182),
            )
            for row in range(3):
                for column in range(3):
                    x = local_center[0] - 36 + column * 36
                    y = local_center[1] - 36 + row * 36
                    color = colors[min(2, max(row, column))]
                    self.draw_aa_round_line(
                        surface,
                        color,
                        (x - 8, y),
                        (x + 8, y),
                        18,
                    )
            self.workshop_emblem_surface = surface
        self.screen.blit(
            self.workshop_emblem_surface,
            self.workshop_emblem_surface.get_rect(center=center),
        )

    def draw_workshop_button(
        self,
        rect: object,
        label: str,
        *,
        primary: bool = False,
        selected: bool = False,
    ) -> None:
        cache_key = (
            int(rect.width),
            int(rect.height),
            str(label),
            bool(primary),
            bool(selected),
        )
        cached = self.workshop_button_cache.get(cache_key)
        if cached is not None:
            self.workshop_button_cache.move_to_end(cache_key)
            self.screen.blit(cached, rect.topleft)
            return
        if primary:
            color = (83, 207, 238) if not selected else (126, 229, 249)
            text_color = (3, 27, 35)
        else:
            color = (19, 52, 67) if not selected else (28, 78, 98)
            text_color = (205, 236, 244)
        surface = self.pygame.Surface(rect.size, self.pygame.SRCALPHA)
        surface.fill((0, 0, 0, 0))
        self.draw_aa_round_line(
            surface,
            color,
            (rect.height / 2, rect.height / 2),
            (rect.width - rect.height / 2, rect.height / 2),
            rect.height,
        )
        rendered = self.font_medium.render(label, True, text_color)
        surface.blit(rendered, rendered.get_rect(center=surface.get_rect().center))
        self.workshop_button_cache[cache_key] = surface
        self.workshop_button_cache.move_to_end(cache_key)
        while len(self.workshop_button_cache) > self.workshop_button_cache_limit:
            self.workshop_button_cache.popitem(last=False)
        self.screen.blit(surface, rect.topleft)

    @staticmethod
    def workshop_capability_label(capability: str) -> str:
        return {
            "ui.surface": "圆屏界面",
            "storage.app": "应用私有存储",
            "events.subscribe": "设备事件",
            "notifications.local": "本地提醒",
            "camera.snapshot": "相机拍照",
            "camera.stream": "相机实时画面",
            "microphone.stream": "麦克风",
            "speaker.playback": "扬声器",
            "vision.inference": "Hailo 视觉推理",
            "motor.pan_tilt": "云台控制",
            "assistant.query": "Daily 助手",
            "tasks.submit": "后台任务",
            "reports.read": "报告库",
            "network.outbound": "受限联网",
            "lifecycle.autostart": "开机运行",
        }.get(capability, capability)

    def poll_workshop_action(self, now: float) -> None:
        future = self.workshop_action_future
        if future is None or not future.done():
            return
        kind = self.workshop_action_kind
        self.workshop_action_future = None
        self.workshop_action_kind = ""
        try:
            result = future.result()
        except Exception as exc:
            self.workshop_notice = str(exc)[:28] or "操作失败"
            self.workshop_notice_until = now + 4.0
            return
        if not result.get("ok"):
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            self.workshop_notice = str(error.get("message") or "操作失败")[:28]
            self.workshop_notice_until = now + 4.0
            return
        if kind == "approve":
            self.workshop_section = "installed"
            self.workshop_notice = "应用已验证并安装"
        elif kind == "reject":
            self.workshop_section = None
            self.workshop_notice = "已拒绝这个应用"
        elif kind == "launch":
            self.workshop_section = "runtime"
            self.workshop_notice = "应用已启动"
        elif kind == "stop":
            self.workshop_section = "installed"
            self.workshop_notice = "应用已退出"
        self.workshop_notice_until = now + 3.0
        self.refresh_workshop_status(now, force=True)

    def draw_workshop_review(self, now: float, proposal: dict) -> None:
        self.screen.fill((0, 0, 0))
        self.draw_gallery_back_button()
        manifest = proposal.get("manifest") if isinstance(proposal.get("manifest"), dict) else {}
        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        risk = proposal.get("risk") if isinstance(proposal.get("risk"), dict) else {}
        permissions = risk.get("permissions") if isinstance(risk.get("permissions"), list) else []
        self.draw_centered_text("权限审核", self.font_large, (220, 245, 252), (400, 126))
        self.draw_centered_text(
            str(metadata.get("name") or "待审核应用")[:20],
            self.font_medium,
            (114, 214, 239),
            (400, 178),
        )
        description = str(metadata.get("description") or proposal.get("requirement") or "")
        for index in range(0, min(len(description), 54), 27):
            self.draw_centered_text(
                description[index : index + 27],
                self.font_small,
                (119, 158, 171),
                (400, 224 + index // 27 * 30),
            )
        overall = str(risk.get("overall") or "low")
        risk_label = {"low": "低风险", "medium": "中风险", "high": "含高风险权限"}.get(overall, overall)
        risk_color = (121, 226, 173) if overall == "low" else (245, 195, 91) if overall == "medium" else (255, 130, 112)
        self.draw_centered_text(risk_label, self.font_small, risk_color, (400, 292))
        for index, permission in enumerate(permissions[:7]):
            if not isinstance(permission, dict):
                continue
            capability = str(permission.get("capability") or "")
            level = str(permission.get("risk") or "low")
            dot = (86, 211, 231) if level == "low" else (246, 187, 77) if level == "medium" else (250, 111, 95)
            y = 340 + index * 38
            self.draw_aa_circle(self.screen, dot, (222, y), 5)
            label = self.workshop_capability_label(capability)
            rendered = self.font_small.render(label, True, (202, 231, 238))
            self.screen.blit(rendered, (244, y - rendered.get_height() // 2))
        if len(permissions) > 7:
            self.draw_centered_text(
                f"另有 {len(permissions) - 7} 项，已写入授权摘要",
                self.font_small,
                (118, 157, 169),
                (400, 606),
            )
        rects = self.workshop_control_rects()
        busy = self.workshop_action_future is not None
        self.draw_workshop_button(
            rects["approve"],
            "正在处理" if busy else "批准并安装",
            primary=True,
            selected=self.pointer_down and self.workshop_pointer_target == "approve",
        )
        self.draw_workshop_button(
            rects["reject"],
            "拒绝",
            selected=self.pointer_down and self.workshop_pointer_target == "reject",
        )

    def draw_workshop_installed(self, now: float) -> None:
        self.screen.fill((0, 0, 0))
        self.draw_gallery_back_button()
        self.draw_centered_text("我的应用", self.font_large, (220, 245, 252), (400, 132))
        apps = self.workshop_installed_apps()
        if not apps:
            self.draw_centered_text("还没有安装用户应用", self.font_medium, (115, 159, 174), (400, 360))
        rects = self.workshop_control_rects()
        for app in apps[:4]:
            app_id = str(app.get("id") or "")
            rect = rects.get(f"app:{app_id}")
            if rect is None:
                continue
            status = "可运行" if app.get("status") == "enabled" else "已停用"
            label = f"{str(app.get('name') or '应用')[:12]}  ·  {status}"
            self.draw_workshop_button(
                rect,
                label,
                selected=self.pointer_down and self.workshop_pointer_target == f"app:{app_id}",
            )
        self.draw_centered_text("点击应用启动；左滑返回", self.font_small, (74, 118, 132), (400, 655))

    def draw_workshop_high_text(
        self,
        text: str,
        font: object,
        color: tuple[int, int, int],
        center: tuple[int, int],
    ) -> object:
        high = font.render(text, True, color)
        smooth = self.pygame.transform.smoothscale(
            high,
            (
                max(1, round(high.get_width() / UI_AA_SCALE)),
                max(1, round(high.get_height() / UI_AA_SCALE)),
            ),
        )
        rect = smooth.get_rect(center=center)
        self.screen.blit(smooth, rect)
        return rect

    @staticmethod
    def workshop_surface_accent(name: str) -> tuple[int, int, int]:
        return {
            "cyan": (75, 210, 241),
            "green": (92, 216, 153),
            "amber": (241, 183, 81),
            "red": (235, 86, 91),
            "neutral": (185, 211, 219),
        }.get(str(name).lower(), (75, 210, 241))

    @staticmethod
    def workshop_surface_value(data: dict, key: str, fallback: object = "—") -> object:
        value: object = data
        for part in str(key).split("."):
            if not isinstance(value, dict) or part not in value:
                return fallback
            value = value[part]
        return fallback if value is None else value

    def draw_workshop_clock(
        self,
        runtime: dict,
        data: dict,
        presentation: dict | None = None,
    ) -> None:
        presentation = presentation if isinstance(presentation, dict) else {}
        components = presentation.get("components") if isinstance(presentation.get("components"), list) else []
        clock_component = next(
            (item for item in components if isinstance(item, dict) and item.get("type") == "clock"),
            {},
        )
        clock_format = str(clock_component.get("format") or "24h").lower()
        show_seconds = bool(clock_component.get("showSeconds", True))
        show_date = bool(clock_component.get("showDate", True))
        show_weekday = bool(clock_component.get("showWeekday", True))
        accent = self.workshop_surface_accent(str(presentation.get("accent") or "cyan"))
        timestamp = data.get("timestamp")
        try:
            local = time.localtime(float(timestamp))
        except (TypeError, ValueError, OverflowError):
            local = time.localtime()
        hour_minute = (
            time.strftime("%I:%M", local).lstrip("0") or "12:00"
            if clock_format == "12h"
            else str(data.get("hourMinute") or time.strftime("%H:%M", local))
        )
        seconds = str(data.get("seconds") or time.strftime("%S", local)).zfill(2)[-2:]
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        weekday = str(data.get("weekday") or weekdays[local.tm_wday])
        second_value = max(0, min(int(seconds) if seconds.isdigit() else 0, 59))

        title = str(runtime.get("title") or "桌面时钟")[:20]
        self.draw_centered_text(title, self.font_medium, (185, 222, 232), (400, 110))
        self.draw_centered_text("本机时间", self.font_small, (65, 123, 140), (400, 151))

        center = (400, 367)
        for index in range(60):
            angle = -math.pi / 2 + index * math.tau / 60
            major = index % 5 == 0
            outer = 188
            inner = outer - (13 if major else 7)
            color = (
                accent
                if index <= second_value
                else ((41, 99, 116) if major else (24, 58, 69))
            )
            start = (
                round(center[0] + math.cos(angle) * inner),
                round(center[1] + math.sin(angle) * inner),
            )
            end = (
                round(center[0] + math.cos(angle) * outer),
                round(center[1] + math.sin(angle) * outer),
            )
            self.draw_aa_round_line(self.screen, color, start, end, 3 if major else 2)

        self.draw_aa_circle(self.screen, (2, 13, 19), center, 156)
        self.draw_aa_ring(self.screen, (19, 62, 76), center, 156, 2)
        clock_center_x = 400 if not show_seconds else 382
        clock_rect = self.draw_workshop_high_text(
            hour_minute,
            self.font_workshop_clock_high,
            (226, 244, 248),
            (clock_center_x, 355),
        )
        if show_seconds:
            seconds_center = (min(clock_rect.right + 28, 555), 390)
            self.draw_workshop_high_text(
                seconds,
                self.font_workshop_clock_seconds_high,
                accent,
                seconds_center,
            )
        date_ascii = str(data.get("date") or time.strftime("%Y-%m-%d", local)).replace("-", ".")
        if show_date and show_weekday:
            date_surface = self.font_status.render(date_ascii, True, (126, 173, 186))
            weekday_surface = self.font_small.render(weekday, True, (126, 173, 186))
            gap = 34
            total_width = date_surface.get_width() + weekday_surface.get_width() + gap
            left = 400 - total_width // 2
            self.screen.blit(date_surface, date_surface.get_rect(midleft=(left, 452)))
            self.draw_aa_circle(
                self.screen,
                accent,
                (left + date_surface.get_width() + gap // 2, 452),
                3,
            )
            self.screen.blit(
                weekday_surface,
                weekday_surface.get_rect(midleft=(left + date_surface.get_width() + gap, 452)),
            )
        elif show_date:
            self.draw_centered_text(date_ascii, self.font_status, (126, 173, 186), (400, 452))
        elif show_weekday:
            self.draw_centered_text(weekday, self.font_small, (126, 173, 186), (400, 452))

    def draw_workshop_adaptive_surface(
        self,
        runtime: dict,
        data: dict,
        presentation: dict,
    ) -> None:
        components = presentation.get("components") if isinstance(presentation.get("components"), list) else []
        accent = self.workshop_surface_accent(str(presentation.get("accent") or "cyan"))
        title = str(runtime.get("title") or "用户应用")[:20]
        self.draw_centered_text(title, self.font_medium, (185, 222, 232), (400, 128))
        first = components[0] if components and isinstance(components[0], dict) else {}
        kind = str(first.get("type") or "status")
        if kind == "clock":
            self.draw_workshop_clock(runtime, data, presentation)
            return

        center = (400, 350)
        self.draw_aa_circle(self.screen, (2, 17, 24), center, 145)
        self.draw_aa_ring(self.screen, (22, 74, 89), center, 145, 2)
        raw_value = self.workshop_surface_value(data, str(first.get("valueKey") or "value"))
        label = str(first.get("label") or "当前值")[:18]
        if kind == "metric":
            precision = int(first.get("precision") or 0)
            try:
                value_text = f"{float(raw_value):.{precision}f}"
            except (TypeError, ValueError):
                value_text = str(raw_value)[:12]
            value_text += str(first.get("unit") or "")[:8]
        elif kind == "progress":
            minimum = float(first.get("minimum") or 0)
            maximum = float(first.get("maximum") or 100)
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                numeric = minimum
            progress = max(0.0, min((numeric - minimum) / max(maximum - minimum, 1e-6), 1.0))
            value_text = f"{progress * 100:.0f}%"
            for index in range(40):
                angle = -math.pi / 2 + index * math.tau / 40
                color = accent if index / 39 <= progress else (25, 61, 72)
                start = (round(center[0] + math.cos(angle) * 160), round(center[1] + math.sin(angle) * 160))
                end = (round(center[0] + math.cos(angle) * 170), round(center[1] + math.sin(angle) * 170))
                self.draw_aa_round_line(self.screen, color, start, end, 3)
        else:
            value_text = str(raw_value)[:16]
        self.draw_centered_text(value_text, self.font_large, accent, (400, 338))
        self.draw_centered_text(label, self.font_small, (117, 162, 175), (400, 398))

        detail_y = 524
        for component in components[1:4]:
            if not isinstance(component, dict):
                continue
            component_kind = str(component.get("type") or "text")
            if component_kind == "text":
                detail = str(
                    component.get("text")
                    or self.workshop_surface_value(data, str(component.get("valueKey") or ""), "")
                )[:34]
            else:
                detail_value = self.workshop_surface_value(data, str(component.get("valueKey") or "value"))
                detail = f"{str(component.get('label') or '状态')[:12]}  {str(detail_value)[:16]}"
            self.draw_centered_text(detail, self.font_small, (145, 185, 196), (400, detail_y))
            detail_y += 37

    def draw_workshop_runtime(self, now: float) -> None:
        self.screen.fill((0, 0, 0))
        self.draw_gallery_back_button()
        runtime = self.workshop_runtime_status
        surface = runtime.get("surface") if isinstance(runtime.get("surface"), dict) else {}
        data = surface.get("data") if isinstance(surface.get("data"), dict) else {}
        view = str(surface.get("view") or "status")
        presentation = surface.get("presentation") if isinstance(surface.get("presentation"), dict) else {}
        if view == "clock" or presentation:
            if view == "clock":
                self.draw_workshop_clock(runtime, data, presentation)
            else:
                self.draw_workshop_adaptive_surface(runtime, data, presentation)
            error = str(runtime.get("error") or "")
            if error:
                self.draw_centered_text(error[:28], self.font_small, (255, 151, 136), (400, 566))
            rect = self.workshop_control_rects()["stop_runtime"]
            self.draw_workshop_button(
                rect,
                "退出时钟",
                selected=self.pointer_down and self.workshop_pointer_target == "stop_runtime",
            )
            return
        title = str(runtime.get("title") or "用户应用")[:20]
        status = str(runtime.get("status") or "starting")
        self.draw_centered_text(title, self.font_large, (220, 245, 252), (400, 134))
        status_label = {
            "starting": "正在启动",
            "running": "运行中",
            "completed": "已完成",
            "lease_expired": "本次授权已到期",
            "error": "运行失败",
        }.get(status, status)
        status_color = (119, 224, 174) if status in {"running", "completed"} else (255, 131, 110) if status == "error" else (112, 198, 225)
        self.draw_centered_text(status_label, self.font_medium, status_color, (400, 198))
        self.draw_aa_circle(self.screen, (4, 25, 34), (400, 390), 150)
        self.draw_aa_ring(self.screen, (26, 100, 126), (400, 390), 150, 2)
        detection_count = int(data.get("detectionCount") or 0)
        counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
        if "dbfs" in data:
            value = f"{float(data.get('dbfs') or -96.0):.0f}"
            value_label = "环境音量 dBFS"
        else:
            value = next(iter(counters.values()), detection_count) if counters else detection_count
            value_label = "当前计数" if "counter" in view or counters else "应用状态"
        self.draw_centered_text(str(value), self.font_large, (104, 224, 245), (400, 374))
        self.draw_centered_text(
            value_label,
            self.font_small,
            (109, 158, 174),
            (400, 438),
        )
        error = str(runtime.get("error") or "")
        if error:
            self.draw_centered_text(error[:28], self.font_small, (255, 151, 136), (400, 566))
        rect = self.workshop_control_rects()["stop_runtime"]
        self.draw_workshop_button(
            rect,
            "退出应用",
            selected=self.pointer_down and self.workshop_pointer_target == "stop_runtime",
        )

    def draw_workshop_page(self, now: float) -> None:
        self.poll_workshop_action(now)
        self.refresh_workshop_status(now)
        if self.workshop_section == "review":
            proposal = self.workshop_pending_review()
            if proposal is not None:
                self.draw_workshop_review(now, proposal)
                return
            self.workshop_section = None
        elif self.workshop_section == "installed":
            self.draw_workshop_installed(now)
            return
        elif self.workshop_section == "runtime":
            self.draw_workshop_runtime(now)
            return
        self.screen.fill((0, 0, 0))
        self.draw_gallery_back_button()
        self.draw_centered_text(
            "工坊",
            self.font_large,
            (220, 245, 252),
            (400, 136),
        )
        self.draw_centered_text(
            "把一句想法变成 RiverBank 应用",
            self.font_small,
            (105, 165, 183),
            (400, 184),
        )
        self.draw_workshop_emblem()
        rects = self.workshop_control_rects()
        self.draw_workshop_button(
            rects["create"],
            "创建应用",
            primary=True,
            selected=self.pointer_down and self.workshop_pointer_target == "create",
        )
        self.draw_workshop_button(
            rects["installed"],
            "审核权限" if self.workshop_pending_review() is not None else "我的应用",
            selected=self.pointer_down and self.workshop_pointer_target == "installed",
        )
        self.draw_workshop_button(
            rects["import"],
            "导入应用",
            selected=self.pointer_down and self.workshop_pointer_target == "import",
        )
        if self.workshop_notice and now < self.workshop_notice_until:
            notice = self.workshop_notice
            color = (153, 207, 220)
        else:
            counts = self.workshop_status.get("counts") if isinstance(self.workshop_status.get("counts"), dict) else {}
            active = int(counts.get("active") or 0)
            notice = "正在生成安全方案" if active else "签名校验 · 权限审核 · 受控运行"
            color = (69, 111, 123)
        self.draw_centered_text(notice, self.font_small, color, (400, 660))

    def render_workshop_surface(self, now: float | None = None) -> object:
        surface = self.pygame.Surface(self.target_size)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        original_target = self.workshop_pointer_target
        try:
            self.screen = surface
            self.pointer_down = False
            self.workshop_pointer_target = None
            self.draw_workshop_page(time.monotonic() if now is None else now)
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
            self.workshop_pointer_target = original_target
        return surface

    def open_workshop(self) -> None:
        if self.workshop_active:
            return
        now = time.monotonic()
        self.workshop_section = None
        self.refresh_workshop_status(now, force=True)
        self.workshop_transition_source = self.screen.copy()
        self.workshop_active = True
        self.workshop_pointer_target = None
        self.workshop_notice = ""
        self.workshop_notice_until = 0.0
        self.workshop_transition_active = True
        self.workshop_transition_opening = True
        self.workshop_transition_started_at = now
        self.workshop_transition_exits_page = False
        self.workshop_transition_target = self.render_workshop_surface(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("workshop page opened")

    def close_workshop(self, animated: bool = True) -> None:
        if not self.workshop_active:
            return
        now = time.monotonic()
        return_target = self.prepare_application_menu_return(now)
        self.workshop_pointer_target = None
        if animated:
            self.workshop_transition_source = self.screen.copy()
            self.workshop_transition_target = return_target
            self.workshop_transition_active = True
            self.workshop_transition_opening = False
            self.workshop_transition_started_at = now
            self.workshop_transition_exits_page = True
        else:
            self.workshop_active = False
            self.workshop_transition_active = False
            self.workshop_transition_source = None
            self.workshop_transition_target = None
            self.workshop_transition_exits_page = False
            self.complete_application_menu_return(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("workshop page close requested")

    def handle_workshop_target(self, target: str | None) -> None:
        if target == "back":
            if self.workshop_section == "runtime":
                if self.workshop_action_future is None:
                    self.workshop_action_kind = "stop"
                    self.workshop_action_future = self.control_executor.submit(stop_workshop_app)
                return
            if self.workshop_section is not None:
                self.workshop_section = None
                self.workshop_pointer_target = None
                self.needs_redraw = True
                return
            self.close_workshop(animated=True)
            return
        now = time.monotonic()
        if target == "create":
            if now < self.workshop_create_request_blocked_until:
                self.workshop_notice = "正在聆听，请直接说出需求"
                self.workshop_notice_until = max(
                    self.workshop_notice_until,
                    self.workshop_create_request_blocked_until,
                )
                self.needs_redraw = True
                return
            sent = self.send_voice_command(
                {
                    "command": "workshop_create",
                    "source": "workshop",
                    "prompt_user": False,
                    "requested_at": time.time(),
                    "request_id": f"workshop-{time.time_ns()}",
                }
            )
            if sent:
                self.workshop_pointer_target = None
                self.workshop_create_request_blocked_until = now + 3.0
                self.workshop_notice = "提示音后，请直接说出应用需求"
                self.workshop_notice_until = now + 12.0
                self.needs_redraw = True
                log("workshop requirement conversation requested")
            else:
                self.workshop_notice = "语音助手暂时不可用"
                self.workshop_notice_until = now + 3.0
        elif target == "installed":
            self.refresh_workshop_status(now, force=True)
            self.workshop_section = (
                "review" if self.workshop_pending_review() is not None else "installed"
            )
        elif target == "import":
            self.workshop_notice = "请通过客户端导入已签名 .rbapp"
            self.workshop_notice_until = now + 3.0
        elif target == "approve" and self.workshop_action_future is None:
            proposal = self.workshop_pending_review()
            manifest = proposal.get("manifest") if isinstance(proposal, dict) else None
            spec = manifest.get("spec") if isinstance(manifest, dict) else None
            permissions = spec.get("permissions") if isinstance(spec, dict) else None
            capabilities = [
                str(item.get("capability"))
                for item in permissions or []
                if isinstance(item, dict) and item.get("capability")
            ]
            if proposal and capabilities:
                self.workshop_action_kind = "approve"
                self.workshop_action_future = self.control_executor.submit(
                    approve_proposal,
                    str(proposal["id"]),
                    capabilities,
                )
        elif target == "reject" and self.workshop_action_future is None:
            proposal = self.workshop_pending_review()
            if proposal:
                self.workshop_action_kind = "reject"
                self.workshop_action_future = self.control_executor.submit(
                    reject_proposal,
                    str(proposal["id"]),
                )
        elif target and target.startswith("app:") and self.workshop_action_future is None:
            app_id = target.partition(":")[2]
            if app_id:
                self.workshop_action_kind = "launch"
                self.workshop_action_future = self.control_executor.submit(
                    workshop_request,
                    {"command": "launch", "appId": app_id},
                    timeout=5.0,
                )
        elif target == "stop_runtime" and self.workshop_action_future is None:
            self.workshop_action_kind = "stop"
            self.workshop_action_future = self.control_executor.submit(stop_workshop_app)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()

    def draw_workshop(self, now: float) -> None:
        if not self.workshop_transition_active:
            self.draw_workshop_page(now)
            return
        source = self.workshop_transition_source
        target = self.workshop_transition_target
        if source is None or target is None:
            self.workshop_transition_active = False
            self.draw_workshop_page(now)
            return
        raw = min(
            1.0,
            max(
                0.0,
                (now - self.workshop_transition_started_at)
                / self.workshop_transition_seconds,
            ),
        )
        if raw >= 1.0:
            self.screen.blit(target, (0, 0))
            self.workshop_transition_active = False
            self.workshop_transition_source = None
            self.workshop_transition_target = None
            if self.workshop_transition_exits_page:
                self.workshop_active = False
                self.workshop_pointer_target = None
                self.complete_application_menu_return(now)
                log("workshop page exit transition completed")
            self.workshop_transition_exits_page = False
            self.write_state()
            return
        eased = 1.0 - (1.0 - raw) ** 3
        direction = 1 if self.workshop_transition_opening else -1
        travel = self.width
        source_x = -round(direction * travel * eased)
        target_x = round(direction * travel * (1.0 - eased))
        self.screen.fill((0, 0, 0))
        self.screen.blit(source, (source_x, 0))
        self.screen.blit(target, (target_x, 0))

    @staticmethod
    def decode_music_artwork(path: str) -> tuple[str, bytes, tuple[int, int]]:
        resampling = getattr(Image, "Resampling", Image)
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(
                image,
                (520, 520),
                method=resampling.LANCZOS,
                centering=(0.5, 0.5),
            ).convert("RGBA")
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).ellipse((2, 2, 517, 517), fill=255)
        image.putalpha(mask)
        return path, image.tobytes(), image.size

    def request_music_artwork(self) -> bool:
        track = self.music_player.current_track
        artwork_path = track.artwork_path if track is not None else ""
        if not artwork_path:
            changed = self.music_artwork_surface is not None
            self.music_artwork_surface = None
            self.music_artwork_requested_path = None
            if changed:
                self.needs_redraw = True
            return changed
        cached = self.music_artwork_cache.get(artwork_path)
        if cached is not None:
            changed = self.music_artwork_surface is not cached
            self.music_artwork_cache.move_to_end(artwork_path)
            self.music_artwork_surface = cached
            self.music_artwork_requested_path = artwork_path
            if changed:
                self.needs_redraw = True
            return changed
        desired_changed = self.music_artwork_requested_path != artwork_path
        if desired_changed:
            self.music_artwork_surface = None
            self.music_artwork_requested_path = artwork_path
        if artwork_path in self.music_artwork_failed_paths:
            return desired_changed
        if self.music_artwork_future is not None:
            return desired_changed
        self.music_artwork_future = self.music_executor.submit(
            self.decode_music_artwork,
            artwork_path,
        )
        self.music_artwork_future_path = artwork_path
        return desired_changed

    def update_music_artwork(self) -> bool:
        changed = False
        if self.music_artwork_future is not None and self.music_artwork_future.done():
            try:
                path, pixels, size = self.music_artwork_future.result()
                high = self.pygame.image.fromstring(pixels, size, "RGBA")
                surface = self.pygame.transform.smoothscale(high, (260, 260))
                self.music_artwork_cache[path] = surface
                self.music_artwork_cache.move_to_end(path)
                while len(self.music_artwork_cache) > self.music_artwork_cache_limit:
                    self.music_artwork_cache.popitem(last=False)
            except Exception as exc:
                if self.music_artwork_future_path:
                    self.music_artwork_failed_paths.add(self.music_artwork_future_path)
                log(f"music artwork decode warning: {exc}")
            finally:
                self.music_artwork_future = None
                self.music_artwork_future_path = None
            changed = True
        if self.request_music_artwork():
            changed = True
        if changed:
            self.needs_redraw = True
        return changed

    def load_music_preferences(self) -> None:
        try:
            payload = json.loads(MUSIC_PREFERENCES_PATH.read_text(encoding="utf-8"))
            self.music_lyrics_enabled = bool(payload.get("lyrics_enabled", True))
            self.music_player.set_playback_mode(
                str(payload.get("playback_mode", PLAYBACK_MODE_LIST_LOOP))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.music_lyrics_enabled = True
            self.music_player.set_playback_mode(PLAYBACK_MODE_LIST_LOOP)

    def save_music_preferences(self) -> None:
        try:
            MUSIC_PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = MUSIC_PREFERENCES_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "lyrics_enabled": self.music_lyrics_enabled,
                        "playback_mode": self.music_player.playback_mode,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, MUSIC_PREFERENCES_PATH)
        except OSError as exc:
            log(f"music preference save warning: {exc}")

    def set_music_playback_mode(self, mode: str) -> bool:
        if not self.music_player.set_playback_mode(mode):
            return False
        self.save_music_preferences()
        self.music_lyrics_notice = self.music_playback_mode_label(
            self.music_player.playback_mode
        )
        self.music_lyrics_notice_until = time.monotonic() + 1.6
        self.needs_redraw = True
        return True

    def set_music_lyrics_enabled(self, enabled: bool) -> bool:
        self.music_lyrics_enabled = bool(enabled)
        self.save_music_preferences()
        if self.music_lyrics_enabled and not self.music_lyrics:
            self.music_lyrics_notice = "主页歌词已开启 · 当前曲目暂无歌词"
        else:
            self.music_lyrics_notice = (
                "主页歌词已开启"
                if self.music_lyrics_enabled
                else "主页歌词已关闭"
            )
        self.music_lyrics_notice_until = time.monotonic() + 1.8
        self.needs_redraw = True
        return True

    def refresh_music_lyrics(self, now: float, *, force: bool = False) -> bool:
        track = self.music_player.current_track
        track_path = track.path if track is not None else None
        if not force and track_path == self.music_lyrics_track_path:
            return False
        previous_path = self.music_lyrics_track_path
        self.music_lyrics_track_path = track_path
        self.music_lyrics_path = sidecar_lyrics_path(track) if track is not None else None
        self.music_lyrics = (
            parse_lrc(self.music_lyrics_path) if self.music_lyrics_path is not None else []
        )
        self.music_lyric_index = lyric_index_at(
            self.music_lyrics,
            self.music_player.elapsed(now),
        )
        self.music_lyric_previous_index = self.music_lyric_index
        self.music_lyric_changed_at = now
        self.music_lyric_direction = 1
        if track_path != previous_path:
            self.music_center_mode = "artwork"
            self.music_center_previous_mode = "artwork"
            self.music_center_transition_active = False
            log(
                "music lyrics changed "
                f"available={bool(self.music_lyrics)} lines={len(self.music_lyrics)} "
                f"track={track.title if track is not None else 'none'}"
            )
        self.needs_redraw = True
        return True

    def update_music_lyrics(self, now: float) -> bool:
        changed = self.update_automatic_lyrics_search(now)
        if self.refresh_music_lyrics(now):
            changed = True
        if self.music_lyrics_notice_until and now >= self.music_lyrics_notice_until:
            self.music_lyrics_notice_until = 0.0
            self.music_lyrics_notice = ""
            changed = True
        if not self.music_lyrics:
            return changed
        index = lyric_index_at(self.music_lyrics, self.music_player.elapsed(now))
        if index != self.music_lyric_index:
            self.music_lyric_previous_index = self.music_lyric_index
            self.music_lyric_direction = 1 if index >= self.music_lyric_index else -1
            self.music_lyric_index = index
            self.music_lyric_changed_at = now
            changed = True
        if changed:
            self.needs_redraw = True
        return changed

    def request_automatic_lyrics_search(
        self,
        *,
        trigger: str = "playback-monitor",
    ) -> bool:
        track = self.music_player.current_track
        if (
            track is None
            or self.music_player.status != "playing"
            or self.music_lyrics
            or self.music_lyrics_fetch_future is not None
            or track.path in self.music_lyrics_search_attempted
        ):
            return False
        local_path = sidecar_lyrics_path(track)
        if local_path is not None:
            self.refresh_music_lyrics(time.monotonic(), force=True)
            self.music_lyrics_search_status = "local"
            self.music_lyrics_search_trigger = trigger
            log(
                "automatic lyrics loaded from local file "
                f"trigger={trigger} track={track.title}"
            )
            return True
        self.music_lyrics_search_attempted.add(track.path)
        self.music_lyrics_fetch_track_path = track.path
        self.music_lyrics_search_status = "searching"
        self.music_lyrics_search_error = ""
        self.music_lyrics_search_trigger = trigger
        self.music_lyrics_fetch_future = self.lyrics_executor.submit(
            fetch_lrclib_lyrics,
            track,
        )
        self.needs_redraw = True
        log(
            "automatic lyrics search started "
            f"trigger={trigger} track={track.title}"
        )
        return True

    def start_lyrics_search_for_playback(self, now: float, *, trigger: str) -> bool:
        """Synchronize the track and enqueue lyric lookup as playback starts."""
        changed = self.refresh_music_lyrics(now)
        if self.music_player.status != "playing":
            return changed
        return self.request_automatic_lyrics_search(trigger=trigger) or changed

    def update_automatic_lyrics_search(self, now: float) -> bool:
        changed = False
        future = self.music_lyrics_fetch_future
        if future is not None and future.done():
            try:
                result = future.result()
            except Exception as exc:
                result = LyricsFetchResult(
                    self.music_lyrics_fetch_track_path or "",
                    "error",
                    error=str(exc),
                )
            self.music_lyrics_fetch_future = None
            self.music_lyrics_fetch_track_path = None
            self.music_lyrics_search_status = result.status
            self.music_lyrics_search_error = result.error
            current = self.music_player.current_track
            if result.found and current is not None and result.track_path == current.path:
                self.refresh_music_lyrics(now, force=True)
                self.music_lyrics_notice = "已自动匹配同步歌词"
                self.music_lyrics_notice_until = now + 1.8
            elif result.status == "instrumental":
                self.music_lyrics_notice = "纯音乐，无歌词"
                self.music_lyrics_notice_until = now + 1.8
            elif result.status == "not_found":
                self.music_lyrics_notice = "未找到同步歌词"
                self.music_lyrics_notice_until = now + 1.8
            elif result.status == "rate_limited":
                self.music_lyrics_notice = "歌词源暂时繁忙"
                self.music_lyrics_notice_until = now + 1.8
            if result.error:
                log(
                    "automatic lyrics search warning "
                    f"status={result.status} error={result.error}"
                )
            else:
                log(
                    "automatic lyrics search completed "
                    f"status={result.status} path={result.lyrics_path or 'none'}"
                )
            changed = True
        if self.request_automatic_lyrics_search(trigger="playback-monitor"):
            changed = True
        if changed:
            self.needs_redraw = True
        return changed

    def music_lyrics_overlay_active(self, now: float) -> bool:
        return bool(
            self.music_lyrics_enabled
            and self.music_lyrics
            and self.music_lyric_index >= 0
            and self.music_player.status in {"playing", "paused"}
            and not self.music_active
            and not self.performance_active
            and not self.pomodoro_active
            and not self.settings_active
            and not self.camera_view_active
            and not self.gallery_active
            and not self.menu_active
            and not self.token_popup_visible
            and now >= self.status_visible_until
            and self.speech_bubble_opacity(now) <= 0.0
        )

    def music_lyric_band_geometry(self) -> tuple[int, int, int, int]:
        center_x = self.width / 2.0
        center_y = round(self.height * 0.8)
        half_height = 36
        circle_radius = min(self.width, self.height) / 2.0 - 3.0
        furthest_y = max(
            abs(center_y - half_height - self.height / 2.0),
            abs(center_y + half_height - self.height / 2.0),
        )
        half_chord = math.sqrt(max(0.0, circle_radius ** 2 - furthest_y ** 2))
        left = math.ceil(center_x - half_chord) + 8
        right = math.floor(center_x + half_chord) - 8
        return left, right, center_y, half_height * 2

    def music_marquee_surfaces(self, text: str) -> tuple[object, object]:
        cleaned = " ".join(str(text).split())
        cached = self.music_marquee_surface_cache.get(cleaned)
        if cached is not None:
            self.music_marquee_surface_cache.move_to_end(cleaned)
            return cached
        bright_high = self.font_music_lyric_current_high.render(
            cleaned,
            True,
            (229, 248, 251),
        )
        shadow_high = self.font_music_lyric_current_high.render(
            cleaned,
            True,
            (0, 0, 0),
        )
        target_size = (
            max(1, round(bright_high.get_width() / UI_AA_SCALE)),
            max(1, round(bright_high.get_height() / UI_AA_SCALE)),
        )
        bright = self.pygame.transform.smoothscale(bright_high, target_size)
        shadow = self.pygame.transform.smoothscale(shadow_high, target_size)
        shadow.set_alpha(210)
        result = (bright, shadow)
        self.music_marquee_surface_cache[cleaned] = result
        self.music_marquee_surface_cache.move_to_end(cleaned)
        while len(self.music_marquee_surface_cache) > self.music_marquee_surface_cache_limit:
            self.music_marquee_surface_cache.popitem(last=False)
        return result

    def music_horizontal_fade_mask(
        self,
        width: int,
        height: int,
        fade_width: int,
    ) -> object:
        key = (width, height, fade_width)
        cached = self.music_horizontal_fade_cache.get(key)
        if cached is not None:
            return cached
        mask = self.pygame.Surface((width, height), self.pygame.SRCALPHA)
        for x in range(width):
            edge_distance = min(x, width - 1 - x)
            ratio = max(0.0, min(edge_distance / max(1, fade_width), 1.0))
            smooth = ratio * ratio * (3.0 - 2.0 * ratio)
            alpha = round(255 * smooth)
            self.pygame.draw.line(mask, (255, 255, 255, alpha), (x, 0), (x, height))
        self.music_horizontal_fade_cache[key] = mask
        return mask

    def music_marquee_x(
        self,
        text_width: int,
        left: int,
        right: int,
        now: float,
    ) -> float:
        available_width = max(1, right - left)
        if text_width <= available_width:
            return left + (available_width - text_width) / 2.0
        overflow = text_width - available_width
        hold = self.music_lyric_marquee_hold_seconds
        travel = overflow / self.music_lyric_marquee_speed
        cycle = hold + travel + hold + travel
        line = self.music_lyrics[self.music_lyric_index]
        line_elapsed = max(0.0, self.music_player.elapsed(now) - line.timestamp_seconds)
        phase = line_elapsed % cycle
        if phase < hold:
            offset = 0.0
        elif phase < hold + travel:
            offset = (phase - hold) * self.music_lyric_marquee_speed
        elif phase < hold + travel + hold:
            offset = float(overflow)
        else:
            offset = overflow - (
                phase - hold - travel - hold
            ) * self.music_lyric_marquee_speed
        return left - max(0.0, min(float(overflow), offset))

    def draw_music_lyrics_overlay(self, now: float) -> None:
        if not self.music_lyrics_overlay_active(now):
            return
        line = self.music_lyrics[self.music_lyric_index]
        bright, shadow = self.music_marquee_surfaces(line.text)
        left, right, center_y, band_height = self.music_lyric_band_geometry()
        x = round(self.music_marquee_x(bright.get_width(), left, right, now))
        y = center_y - bright.get_height() // 2
        band_width = right - left
        band = self.pygame.Surface((band_width, band_height), self.pygame.SRCALPHA)
        local_y = y - (center_y - band_height // 2)
        band.blit(shadow, (x - left + 2, local_y + 2))
        band.blit(bright, (x - left, local_y))
        band.blit(
            self.music_horizontal_fade_mask(band_width, band_height, 56),
            (0, 0),
            special_flags=self.pygame.BLEND_RGBA_MULT,
        )
        self.screen.blit(band, (left, center_y - band_height // 2))

    def request_music_scan(self) -> bool:
        if self.music_scan_future is not None:
            return False
        self.music_scan_error = ""
        self.music_scan_future = self.music_executor.submit(
            scan_music_library,
            self.music_player.library_dir,
            self.music_ffprobe_command,
            MUSIC_ARTWORK_CACHE_DIR,
            self.music_ffmpeg_command,
        )
        self.needs_redraw = True
        return True

    def update_music(self, now: float) -> bool:
        changed = False
        if self.music_scan_future is not None and self.music_scan_future.done():
            try:
                tracks = self.music_scan_future.result()
                self.music_player.set_library(tracks)
                self.music_scan_completed = True
                self.music_scan_error = ""
                log(
                    "music library scan completed "
                    f"tracks={len(tracks)} path={self.music_player.library_dir}"
                )
                if self.music_autoplay_pending:
                    self.music_autoplay_pending = False
                    if self.music_player.play():
                        log("music autoplay started after library scan")
            except Exception as exc:
                self.music_scan_error = str(exc)
                self.music_scan_completed = True
                self.music_autoplay_pending = False
                log(f"music library scan warning: {exc}")
            finally:
                self.music_scan_future = None
            self.refresh_music_lyrics(now, force=True)
            if self.start_lyrics_search_for_playback(now, trigger="library-scan"):
                changed = True
            changed = True
        if self.music_player.poll():
            changed = True
            self.refresh_music_lyrics(now, force=True)
            if self.start_lyrics_search_for_playback(now, trigger="automatic-advance"):
                changed = True
            current = self.music_player.current_track
            if self.music_player.status == "playing" and current is not None:
                log(f"music track advanced title={current.title}")
            else:
                log("music playback completed")
        if (
            changed
            and self.music_transition_active
            and self.music_transition_opening
        ):
            self.music_transition_target = self.render_music_surface(now)
        if changed:
            self.needs_redraw = True
        return changed

    def music_background_surface(self) -> object:
        if self.music_static_surface is not None:
            return self.music_static_surface
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (self.width * scale, self.height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 255))
        center = (self.width * scale // 2, self.height * scale // 2)
        self.pygame.draw.circle(
            high,
            (18, 74, 91, 96),
            center,
            round((min(self.width, self.height) // 2 - 10) * scale),
            width=max(1, round(2 * scale)),
        )
        self.music_static_surface = self.pygame.transform.smoothscale(
            high,
            self.target_size,
        )
        return self.music_static_surface

    def render_music_center_album_surface(self) -> object:
        surface = self.pygame.Surface((270, 270), self.pygame.SRCALPHA)
        self.draw_aa_circle(surface, (4, 20, 29, 255), (135, 135), 130)
        if self.music_artwork_surface is not None:
            surface.blit(self.music_artwork_surface, (5, 5))
            return surface
        note_color = (85, 216, 236, 255)
        scale = UI_AA_SCALE
        note = self.pygame.Surface(
            (surface.get_width() * scale, surface.get_height() * scale),
            self.pygame.SRCALPHA,
        )

        def scaled_points(points: tuple[tuple[int, int], ...]) -> list[tuple[int, int]]:
            return [(x * scale, y * scale) for x, y in points]

        # A broad, slightly bowed beam with substantial stems mirrors the
        # reference double-note silhouette while remaining crisp on the DSI.
        self.pygame.draw.polygon(
            note,
            note_color,
            scaled_points(
                (
                    (88, 82),
                    (109, 76),
                    (134, 72),
                    (160, 69),
                    (184, 68),
                    (184, 96),
                    (160, 97),
                    (134, 100),
                    (109, 105),
                    (88, 112),
                )
            ),
        )
        self.pygame.draw.rect(
            note,
            note_color,
            self.pygame.Rect(88 * scale, 92 * scale, 21 * scale, 80 * scale),
        )
        self.pygame.draw.rect(
            note,
            note_color,
            self.pygame.Rect(163 * scale, 83 * scale, 21 * scale, 76 * scale),
        )
        self.pygame.draw.ellipse(
            note,
            note_color,
            self.pygame.Rect(61 * scale, 157 * scale, 55 * scale, 39 * scale),
        )
        self.pygame.draw.ellipse(
            note,
            note_color,
            self.pygame.Rect(136 * scale, 144 * scale, 55 * scale, 39 * scale),
        )
        surface.blit(
            self.pygame.transform.smoothscale(note, surface.get_size()),
            (0, 0),
        )
        return surface

    def music_player_lyrics_geometry(self) -> tuple[int, int, int, int]:
        center_x = self.width / 2.0
        center_y = self.height // 2
        band_height = 180
        circle_radius = min(self.width, self.height) / 2.0 - 3.0
        furthest_y = band_height / 2.0
        half_chord = math.sqrt(max(0.0, circle_radius ** 2 - furthest_y ** 2))
        left = math.ceil(center_x - half_chord) + 12
        right = math.floor(center_x + half_chord) - 12
        return left, right, center_y, band_height

    def music_player_lyric_surface(
        self,
        text: str,
        *,
        current: bool,
        max_width: int,
    ) -> object:
        cleaned = " ".join(str(text).split())
        key = (cleaned, current, max_width)
        cached = self.music_player_lyric_surface_cache.get(key)
        if cached is not None:
            self.music_player_lyric_surface_cache.move_to_end(key)
            return cached
        font = (
            self.font_music_player_lyric_current_high
            if current
            else self.font_music_player_lyric_muted_high
        )
        color = (229, 248, 251) if current else (103, 158, 172)
        high = font.render(cleaned, True, color)
        target_size = (
            max(1, round(high.get_width() / UI_AA_SCALE)),
            max(1, round(high.get_height() / UI_AA_SCALE)),
        )
        if target_size[0] > max_width:
            ratio = max_width / target_size[0]
            target_size = (max_width, max(1, round(target_size[1] * ratio)))
        surface = self.pygame.transform.smoothscale(high, target_size)
        self.music_player_lyric_surface_cache[key] = surface
        self.music_player_lyric_surface_cache.move_to_end(key)
        while (
            len(self.music_player_lyric_surface_cache)
            > self.music_player_lyric_surface_cache_limit
        ):
            self.music_player_lyric_surface_cache.popitem(last=False)
        return surface

    def draw_music_player_lyric_group(
        self,
        band: object,
        index: int,
        offset_y: float,
        opacity: float,
    ) -> None:
        if opacity <= 0.0:
            return
        if not self.music_lyrics:
            rendered = self.music_player_lyric_surface(
                "暂无歌词",
                current=True,
                max_width=band.get_width() - 80,
            )
            rendered.set_alpha(round(255 * opacity))
            band.blit(
                rendered,
                rendered.get_rect(
                    center=(band.get_width() // 2, round(90 + offset_y))
                ),
            )
            rendered.set_alpha(None)
            return
        active_index = max(0, min(index, len(self.music_lyrics) - 1))
        for relative, center_y in ((-1, 40), (0, 90), (1, 140)):
            line_index = active_index + relative
            if not 0 <= line_index < len(self.music_lyrics):
                continue
            current = relative == 0
            rendered = self.music_player_lyric_surface(
                self.music_lyrics[line_index].text,
                current=current,
                max_width=band.get_width() - 72,
            )
            rendered.set_alpha(round(255 * opacity * (1.0 if current else 0.62)))
            band.blit(
                rendered,
                rendered.get_rect(
                    center=(band.get_width() // 2, round(center_y + offset_y))
                ),
            )
            rendered.set_alpha(None)

    def render_music_player_lyrics_band(self, now: float) -> tuple[object, tuple[int, int]]:
        left, right, center_y, band_height = self.music_player_lyrics_geometry()
        band_width = right - left
        band = self.pygame.Surface((band_width, band_height), self.pygame.SRCALPHA)
        active_index = max(0, self.music_lyric_index)
        elapsed = max(0.0, now - self.music_lyric_changed_at)
        transitioning = bool(
            self.music_lyrics
            and self.music_lyric_previous_index >= 0
            and self.music_lyric_previous_index != self.music_lyric_index
            and elapsed < self.music_lyric_scroll_seconds
        )
        if transitioning:
            raw = min(1.0, elapsed / self.music_lyric_scroll_seconds)
            eased = 1.0 - (1.0 - raw) ** 3
            travel = 50.0 * self.music_lyric_direction
            self.draw_music_player_lyric_group(
                band,
                self.music_lyric_previous_index,
                -travel * eased,
                1.0 - eased,
            )
            self.draw_music_player_lyric_group(
                band,
                active_index,
                travel * (1.0 - eased),
                eased,
            )
        else:
            self.draw_music_player_lyric_group(band, active_index, 0.0, 1.0)
        band.blit(
            self.music_horizontal_fade_mask(band_width, band_height, 72),
            (0, 0),
            special_flags=self.pygame.BLEND_RGBA_MULT,
        )
        return band, (left, center_y - band_height // 2)

    def draw_music_album_center(self, _now: float, opacity: float) -> None:
        if opacity <= 0.0:
            return
        layer = self.pygame.Surface((300, 300), self.pygame.SRCALPHA)
        layer.blit(self.render_music_center_album_surface(), (15, 15))
        self.draw_aa_ring(layer, (44, 190, 220, 215), (150, 150), 132, 3)
        layer.set_alpha(round(255 * opacity))
        self.screen.blit(layer, (250, 155))

    def draw_music_player_lyrics(self, now: float, opacity: float) -> None:
        if opacity <= 0.0:
            return
        band, position = self.render_music_player_lyrics_band(now)
        band.set_alpha(round(255 * opacity))
        self.screen.blit(band, position)

    def music_mode_opacity(self, mode: str, now: float) -> float:
        if not self.music_center_transition_active:
            return 1.0 if self.music_center_mode == mode else 0.0
        elapsed = max(0.0, now - self.music_center_transition_started_at)
        raw = min(1.0, elapsed / self.music_center_transition_seconds)
        eased = raw * raw * (3.0 - 2.0 * raw)
        if self.music_center_mode == mode:
            return eased
        if self.music_center_previous_mode == mode:
            return 1.0 - eased
        return 0.0

    def toggle_music_center_mode(self, now: float | None = None) -> None:
        changed_at = time.monotonic() if now is None else now
        self.music_center_previous_mode = self.music_center_mode
        self.music_center_mode = (
            "lyrics" if self.music_center_mode == "artwork" else "artwork"
        )
        self.music_center_transition_active = True
        self.music_center_transition_started_at = changed_at
        self.needs_redraw = True

    def draw_music_center(self, now: float) -> None:
        if not self.music_center_transition_active:
            if self.music_center_mode == "lyrics":
                self.draw_music_player_lyrics(now, 1.0)
            else:
                self.draw_music_album_center(now, 1.0)
            return
        elapsed = max(0.0, now - self.music_center_transition_started_at)
        raw = min(1.0, elapsed / self.music_center_transition_seconds)
        if raw >= 1.0:
            self.music_center_transition_active = False
            if self.music_center_mode == "lyrics":
                self.draw_music_player_lyrics(now, 1.0)
            else:
                self.draw_music_album_center(now, 1.0)
            self.write_state()
            return
        eased = raw * raw * (3.0 - 2.0 * raw)
        if self.music_center_previous_mode == "artwork":
            self.draw_music_album_center(now, 1.0 - eased)
        else:
            self.draw_music_player_lyrics(now, 1.0 - eased)
        if self.music_center_mode == "artwork":
            self.draw_music_album_center(now, eased)
        else:
            self.draw_music_player_lyrics(now, eased)

    @staticmethod
    def format_music_time(seconds: float) -> str:
        total = max(0, round(seconds))
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def ellipsize_music_text(text: str, font: object, max_width: int) -> str:
        cleaned = " ".join(str(text).split())
        if font.size(cleaned)[0] <= max_width:
            return cleaned
        suffix = "…"
        while cleaned and font.size(cleaned + suffix)[0] > max_width:
            cleaned = cleaned[:-1]
        return cleaned + suffix if cleaned else suffix

    def music_control_centers(self) -> dict[str, tuple[int, int]]:
        return {
            "previous": (270, 632),
            "toggle": (400, 632),
            "next": (530, 632),
            "lyrics": (545, 140),
            "mode": (624, 140),
        }

    @staticmethod
    def music_volume_geometry() -> tuple[tuple[int, int], int, float, float]:
        return (400, 400), 348, -0.42, 0.55

    def music_volume_percent_from_position(
        self,
        position: tuple[int, int],
    ) -> int:
        center, _, start_angle, end_angle = self.music_volume_geometry()
        angle = math.atan2(position[1] - center[1], position[0] - center[0])
        ratio = (end_angle - angle) / max(0.001, end_angle - start_angle)
        return max(0, min(round(ratio * 100), 100))

    def is_music_volume_position(self, position: tuple[int, int]) -> bool:
        center, radius, start_angle, end_angle = self.music_volume_geometry()
        dx = position[0] - center[0]
        dy = position[1] - center[1]
        pointer_radius = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        return (
            radius - 30 <= pointer_radius <= radius + 34
            and start_angle - 0.08 <= angle <= end_angle + 0.08
        )

    def music_target_at(self, position: tuple[int, int]) -> str | None:
        if math.dist(position, self.pomodoro_back_center()) <= 50:
            return "back"
        if self.is_music_volume_position(position):
            return "volume"
        for name, center in self.music_control_centers().items():
            radius = 51 if name == "toggle" else (35 if name == "lyrics" else 42)
            if math.dist(position, center) <= radius:
                return name
        if math.dist(position, (400, 305)) <= 132:
            return "center"
        if self.music_center_mode == "lyrics" or self.music_center_transition_active:
            left, right, center_y, band_height = self.music_player_lyrics_geometry()
            if left <= position[0] <= right and (
                center_y - band_height // 2
                <= position[1]
                <= center_y + band_height // 2
            ):
                return "center"
        return None

    def music_volume_expansion(self, now: float) -> float:
        if self.music_volume_dragging:
            raw = min(
                1.0,
                max(0.0, now - self.music_volume_interaction_started_at)
                / self.music_volume_transition_seconds,
            )
            eased = raw * raw * (3.0 - 2.0 * raw)
            return self.music_volume_expand_from + (
                1.0 - self.music_volume_expand_from
            ) * eased
        if self.music_volume_last_interaction_at <= 0.0:
            return 0.0
        elapsed = max(0.0, now - self.music_volume_last_interaction_at)
        if elapsed <= self.music_volume_idle_seconds:
            return 1.0
        raw = min(
            1.0,
            (elapsed - self.music_volume_idle_seconds)
            / self.music_volume_transition_seconds,
        )
        eased = raw * raw * (3.0 - 2.0 * raw)
        return 1.0 - eased

    def draw_music_volume_control(self, now: float) -> None:
        center, radius, start_angle, end_angle = self.music_volume_geometry()
        percent = max(0, min(int(self.system_status.volume_percent), 100))
        ratio = percent / 100.0
        pressed = self.pointer_down and self.music_pointer_target == "volume"
        active_angle = end_angle - (end_angle - start_angle) * ratio
        knob_center = (
            round(center[0] + math.cos(active_angle) * radius),
            round(center[1] + math.sin(active_angle) * radius),
        )

        if (
            self.music_volume_idle_surface_cache is None
            or self.music_volume_idle_surface_cache_key != percent
        ):
            idle_layer = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
            self.draw_capsule_arc(
                idle_layer,
                center,
                radius - 2,
                radius + 2,
                start_angle,
                end_angle,
                (42, 112, 128, 205),
                steps=44,
                soft_edge=True,
            )
            self.draw_aa_circle(
                idle_layer,
                (102, 226, 244, 255),
                knob_center,
                7,
            )
            self.music_volume_idle_surface_cache_key = percent
            self.music_volume_idle_surface_cache = idle_layer

        cache_key = (percent, pressed)
        if (
            self.music_volume_expanded_surface_cache is None
            or self.music_volume_expanded_surface_cache_key != cache_key
        ):
            expanded_layer = self.pygame.Surface(
                self.target_size,
                self.pygame.SRCALPHA,
            )
            self.draw_capsule_arc(
                expanded_layer,
                center,
                radius - 19,
                radius + 19,
                start_angle,
                end_angle,
                (5, 27, 36, 246),
                border=(18, 86, 103, 220),
                border_width=2,
                steps=48,
                soft_edge=True,
            )
            self.draw_capsule_arc(
                expanded_layer,
                center,
                radius - 4,
                radius + 4,
                start_angle,
                end_angle,
                (24, 72, 83, 255),
                steps=44,
                soft_edge=False,
            )
            if percent > 0:
                self.draw_capsule_arc(
                    expanded_layer,
                    center,
                    radius - 4,
                    radius + 4,
                    active_angle,
                    end_angle,
                    (56, 209, 235, 255),
                    steps=44,
                    soft_edge=True,
                )
            knob_color = (
                (116, 237, 250, 255)
                if pressed
                else (221, 248, 251, 255)
            )
            self.draw_aa_circle(expanded_layer, knob_color, knob_center, 11)
            self.draw_aa_circle(
                expanded_layer,
                (17, 82, 98, 255),
                knob_center,
                5,
            )
            self.music_volume_expanded_surface_cache_key = cache_key
            self.music_volume_expanded_surface_cache = expanded_layer

        expansion = max(0.0, min(self.music_volume_expansion(now), 1.0))
        if expansion < 1.0 and self.music_volume_idle_surface_cache is not None:
            self.music_volume_idle_surface_cache.set_alpha(
                round(255 * (1.0 - expansion))
            )
            self.screen.blit(self.music_volume_idle_surface_cache, (0, 0))
            self.music_volume_idle_surface_cache.set_alpha(None)
        if expansion > 0.0 and self.music_volume_expanded_surface_cache is not None:
            self.music_volume_expanded_surface_cache.set_alpha(round(255 * expansion))
            self.screen.blit(self.music_volume_expanded_surface_cache, (0, 0))
            self.music_volume_expanded_surface_cache.set_alpha(None)

    def draw_music_transport_button(
        self,
        name: str,
        center: tuple[int, int],
        enabled: bool,
    ) -> None:
        pressed = self.pointer_down and self.music_pointer_target == name
        outer = (47, 199, 226, 255) if enabled else (45, 72, 80, 220)
        inner = (9, 39, 51, 255) if enabled else (6, 23, 31, 255)
        if pressed and enabled:
            inner = (20, 91, 110, 255)
        radius = 47 if name == "toggle" else 38
        self.draw_aa_circle(self.screen, outer, center, radius)
        self.draw_aa_circle(self.screen, inner, center, radius - 3)
        icon_color = (223, 247, 250, 255) if enabled else (76, 101, 108, 255)
        x, y = center
        if name == "toggle":
            if self.music_player.status == "playing":
                self.draw_aa_round_line(
                    self.screen, icon_color, (x - 10, y - 14), (x - 10, y + 14), 7
                )
                self.draw_aa_round_line(
                    self.screen, icon_color, (x + 10, y - 14), (x + 10, y + 14), 7
                )
            else:
                self.draw_aa_polygon(
                    self.screen,
                    icon_color,
                    ((x - 10, y - 17), (x - 10, y + 17), (x + 19, y)),
                )
            return
        if name == "previous":
            points = ((x + 12, y - 14), (x + 12, y + 14), (x - 12, y))
            line_x = x - 16
        else:
            points = ((x - 12, y - 14), (x - 12, y + 14), (x + 12, y))
            line_x = x + 16
        self.draw_aa_polygon(self.screen, icon_color, points)
        self.draw_aa_round_line(
            self.screen,
            icon_color,
            (line_x, y - 14),
            (line_x, y + 14),
            5,
        )

    @staticmethod
    def music_playback_mode_label(mode: str) -> str:
        return {
            PLAYBACK_MODE_LIST_LOOP: "列表循环",
            PLAYBACK_MODE_SINGLE_REPEAT: "单曲循环",
            PLAYBACK_MODE_SHUFFLE: "乱序播放",
        }.get(mode, "列表循环")

    def draw_music_playback_mode_button(self) -> None:
        center = self.music_control_centers()["mode"]
        pressed = self.pointer_down and self.music_pointer_target == "mode"
        self.draw_aa_circle(self.screen, (38, 128, 151, 235), center, 37)
        self.draw_aa_circle(
            self.screen,
            (11, 42, 53, 255) if not pressed else (20, 83, 100, 255),
            center,
            34,
        )
        color = (191, 237, 245, 255)
        x, y = center
        mode = self.music_player.playback_mode
        if mode == PLAYBACK_MODE_SINGLE_REPEAT:
            # RiverBank defines single-track looping as one continuous loop
            # arrow.  Keep the arrowhead tangent to the stroke so it reads as
            # one glyph—there is deliberately no numeral or second arrow.
            radius = 17
            stroke = 3
            # Rotate the complete glyph 90° counter-clockwise so the gap no
            # longer hollows out the top and the arrowhead stays clear of the
            # camera privacy indicator at the button's right edge.
            start_angle = math.radians(225)
            end_angle = math.radians(488)
            self.draw_aa_ring(
                self.screen,
                color,
                center,
                radius,
                stroke,
                start_angle=start_angle,
                end_angle=end_angle,
            )
            start_point = (
                round(x + math.cos(start_angle) * radius),
                round(y - math.sin(start_angle) * radius),
            )
            self.draw_aa_circle(
                self.screen,
                color,
                start_point,
                math.ceil(stroke / 2),
            )
            end_point = (
                x + math.cos(end_angle) * radius,
                y - math.sin(end_angle) * radius,
            )
            tangent = (-math.sin(end_angle), -math.cos(end_angle))
            normal = (math.cos(end_angle), -math.sin(end_angle))
            tip = (
                round(end_point[0] + tangent[0] * 5),
                round(end_point[1] + tangent[1] * 5),
            )
            base = (
                end_point[0] - tangent[0] * 3,
                end_point[1] - tangent[1] * 3,
            )
            self.draw_aa_polygon(
                self.screen,
                color,
                (
                    tip,
                    (
                        round(base[0] + normal[0] * 5),
                        round(base[1] + normal[1] * 5),
                    ),
                    (
                        round(base[0] - normal[0] * 5),
                        round(base[1] - normal[1] * 5),
                    ),
                ),
            )
            return
        if mode == PLAYBACK_MODE_SHUFFLE:
            for start, joint, end in (
                ((x - 17, y - 11), (x - 7, y - 11), (x + 9, y + 10)),
                ((x - 17, y + 11), (x - 7, y + 11), (x + 9, y - 10)),
            ):
                self.draw_aa_round_line(self.screen, color, start, joint, 4)
                self.draw_aa_round_line(self.screen, color, joint, end, 4)
            self.draw_aa_polygon(
                self.screen,
                color,
                ((x + 8, y - 16), (x + 20, y - 10), (x + 8, y - 4)),
            )
            self.draw_aa_polygon(
                self.screen,
                color,
                ((x + 8, y + 4), (x + 20, y + 10), (x + 8, y + 16)),
            )
            return
        for offset in (-11, 0, 11):
            self.draw_aa_circle(self.screen, color, (x - 14, y + offset), 3)
            self.draw_aa_round_line(
                self.screen,
                color,
                (x - 5, y + offset),
                (x + 17, y + offset),
                4,
            )

    def draw_music_expression_lyrics_button(self) -> None:
        """Draw the persistent toggle for lyrics on the expression desktop."""

        center = self.music_control_centers()["lyrics"]
        pressed = self.pointer_down and self.music_pointer_target == "lyrics"
        selected = self.music_lyrics_enabled
        outer = (49, 203, 229, 245) if selected else (27, 74, 86, 220)
        inner = (16, 76, 91, 255) if selected else (5, 28, 35, 255)
        if pressed:
            inner = (24, 102, 120, 255)
        self.draw_aa_circle(self.screen, outer, center, 32)
        self.draw_aa_circle(self.screen, inner, center, 29)
        glyph_high = self.font_music_lyric_toggle_high.render(
            "词",
            True,
            (221, 246, 250) if selected else (83, 124, 134),
        )
        glyph = self.pygame.transform.smoothscale(
            glyph_high,
            (
                max(1, glyph_high.get_width() // UI_AA_SCALE),
                max(1, glyph_high.get_height() // UI_AA_SCALE),
            ),
        )
        self.screen.blit(glyph, glyph.get_rect(center=center))

    def draw_music_page(self, now: float) -> None:
        snapshot = self.music_player.snapshot(now)
        track = self.music_player.current_track
        self.screen.blit(self.music_background_surface(), (0, 0))
        self.draw_gallery_back_button()
        self.draw_centered_text(
            "音乐",
            self.font_large,
            (226, 245, 249),
            (400, 105),
        )
        self.draw_music_expression_lyrics_button()
        self.draw_music_playback_mode_button()

        # Album artwork and synchronized lyrics cross-fade in the center region.
        # The circular frame belongs to the album layer only; lyrics are borderless.
        self.draw_music_center(now)

        if track is None:
            heading = "正在扫描音乐库" if self.music_scan_future is not None else "音乐库为空"
            if self.music_scan_error:
                heading = "音乐库读取失败"
            self.draw_centered_text(
                heading,
                self.font_music_track,
                (214, 239, 244),
                (400, 470),
            )
            self.draw_centered_text(
                "将音频放入 SSD / Music，长按模式按钮刷新",
                self.font_small,
                (105, 166, 180),
                (400, 514),
            )
        else:
            title = self.ellipsize_music_text(track.title, self.font_music_track, 500)
            artist = self.ellipsize_music_text(
                track.artist or "未知艺术家",
                self.font_music_artist,
                450,
            )
            artwork_opacity = self.music_mode_opacity("artwork", now)
            if artwork_opacity > 0.0:
                title_surface = self.font_music_track.render(
                    title,
                    True,
                    (226, 245, 249),
                )
                artist_surface = self.font_music_artist.render(
                    artist,
                    True,
                    (111, 177, 191),
                )
                layer_alpha = round(255 * artwork_opacity)
                title_surface.set_alpha(layer_alpha)
                artist_surface.set_alpha(layer_alpha)
                self.screen.blit(
                    title_surface,
                    title_surface.get_rect(center=(400, 462)),
                )
                self.screen.blit(
                    artist_surface,
                    artist_surface.get_rect(center=(400, 497)),
                )
            self.draw_aa_round_line(
                self.screen,
                (23, 69, 80, 255),
                (155, 539),
                (645, 539),
                8,
            )
            progress = float(snapshot["progress"])
            if progress > 0.0:
                progress_x = 155 + 490 * progress
                self.draw_aa_round_line(
                    self.screen,
                    (55, 209, 235, 255),
                    (155, 539),
                    (progress_x, 539),
                    8,
                )
            elapsed_text = self.font_status.render(
                self.format_music_time(float(snapshot["elapsed_seconds"])),
                True,
                (101, 160, 173),
            )
            duration_text = self.font_status.render(
                self.format_music_time(float(snapshot["duration_seconds"])),
                True,
                (101, 160, 173),
            )
            self.screen.blit(elapsed_text, (155, 552))
            self.screen.blit(duration_text, (645 - duration_text.get_width(), 552))

        self.draw_music_volume_control(now)

        enabled = bool(self.music_player.tracks)
        for name in ("previous", "toggle", "next"):
            self.draw_music_transport_button(
                name,
                self.music_control_centers()[name],
                enabled,
            )
        status_label = {
            "playing": "正在播放",
            "paused": "已暂停",
            "stopped": "待播放",
            "empty": "等待音乐",
            "error": "播放失败",
        }.get(self.music_player.status, self.music_player.status)
        footer = f"{status_label}  ·  {len(self.music_player.tracks)} 首"
        footer_color = (89, 148, 162)
        if self.music_lyrics_notice and now < self.music_lyrics_notice_until:
            footer = self.music_lyrics_notice
            footer_color = (141, 201, 214)
        self.draw_centered_text(
            footer,
            self.font_small,
            footer_color,
            (400, 715),
        )

    def render_music_surface(self, now: float | None = None) -> object:
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        original_screen = self.screen
        original_pointer_down = self.pointer_down
        original_target = self.music_pointer_target
        try:
            self.screen = surface
            self.pointer_down = False
            self.music_pointer_target = None
            self.draw_music_page(time.monotonic() if now is None else now)
        finally:
            self.screen = original_screen
            self.pointer_down = original_pointer_down
            self.music_pointer_target = original_target
        return surface

    def open_music(self) -> None:
        if self.music_active:
            return
        now = time.monotonic()
        self.music_transition_source = self.screen.copy()
        self.music_active = True
        self.music_pointer_target = None
        self.music_volume_dragging = False
        self.music_volume_interaction_started_at = 0.0
        self.music_volume_last_interaction_at = 0.0
        self.music_volume_expand_from = 0.0
        self.music_transition_active = True
        self.music_transition_opening = True
        self.music_transition_started_at = now
        self.music_transition_exits_page = False
        self.request_music_scan()
        self.request_system_status_refresh(now)
        self.music_transition_target = self.render_music_surface(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("music page opened")

    def close_music(self, animated: bool = True) -> None:
        if not self.music_active:
            return
        now = time.monotonic()
        return_target = self.prepare_application_menu_return(now)
        self.music_pointer_target = None
        self.music_volume_dragging = False
        self.music_volume_interaction_started_at = 0.0
        self.music_volume_last_interaction_at = 0.0
        self.music_volume_expand_from = 0.0
        if animated:
            self.music_transition_source = self.screen.copy()
            self.music_transition_target = return_target
            self.music_transition_active = True
            self.music_transition_opening = False
            self.music_transition_started_at = now
            self.music_transition_exits_page = True
        else:
            self.music_active = False
            self.music_transition_active = False
            self.music_transition_source = None
            self.music_transition_target = None
            self.music_transition_exits_page = False
            self.complete_application_menu_return(now)
        self.note_screensaver_activity(now)
        self.needs_redraw = True
        self.write_state()
        log("music page close requested")

    def handle_music_target(self, target: str | None) -> None:
        if target == "back":
            self.close_music(animated=True)
        elif target == "refresh":
            if self.request_music_scan():
                self.music_lyrics_notice = "正在刷新音乐库"
                self.music_lyrics_notice_until = time.monotonic() + 2.0
        elif target == "mode":
            mode = self.music_player.cycle_playback_mode()
            self.set_music_playback_mode(mode)
        elif target == "lyrics":
            self.set_music_lyrics_enabled(not self.music_lyrics_enabled)
        elif target == "center":
            self.toggle_music_center_mode()
        elif target == "toggle":
            self.music_player.toggle()
        elif target == "previous":
            self.music_player.previous()
        elif target == "next":
            self.music_player.next()
        else:
            return
        if target in {"toggle", "previous", "next"}:
            self.start_lyrics_search_for_playback(
                time.monotonic(),
                trigger=f"control-{target}",
            )
        self.needs_redraw = True
        self.write_state()
        log(f"music control target={target} status={self.music_player.status}")

    def draw_music(self, now: float) -> None:
        if not self.music_transition_active:
            self.draw_music_page(now)
            return
        source = self.music_transition_source
        target = self.music_transition_target
        if source is None or target is None:
            self.music_transition_active = False
            self.draw_music_page(now)
            return
        raw = min(
            1.0,
            max(
                0.0,
                (now - self.music_transition_started_at)
                / self.music_transition_seconds,
            ),
        )
        if raw >= 1.0:
            self.screen.blit(target, (0, 0))
            self.music_transition_active = False
            self.music_transition_source = None
            self.music_transition_target = None
            if self.music_transition_exits_page:
                self.music_active = False
                self.music_pointer_target = None
                self.complete_application_menu_return(now)
                log("music page exit transition completed")
            self.music_transition_exits_page = False
            if self.music_lyrics_overlay_active(now):
                self.draw_music_lyrics_overlay(now)
            self.write_state()
            return
        eased = 1.0 - (1.0 - raw) ** 3
        direction = 1 if self.music_transition_opening else -1
        travel = self.width
        source_x = -round(direction * travel * eased)
        target_x = round(direction * travel * (1.0 - eased))
        self.screen.fill((0, 0, 0))
        self.screen.blit(source, (source_x, 0))
        self.screen.blit(target, (target_x, 0))

    def draw(self) -> None:
        render_started = time.perf_counter()
        now = time.monotonic()
        if self.boot_active:
            self.screen.fill((0, 0, 0))
            self.draw_boot_animation(now)
        elif self.provisioning_active:
            self.draw_provisioning()
        elif self.pomodoro_completion_alert_active:
            self.draw_pomodoro_completion_alert()
        elif self.screensaver_active:
            self.draw_screensaver()
        elif self.video_call_active:
            self.draw_video_call(now)
        elif self.music_active:
            self.draw_music(now)
        elif self.workshop_active:
            self.draw_workshop(now)
        elif self.performance_active:
            self.draw_performance(now)
        elif self.pomodoro_active:
            self.draw_pomodoro(now)
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
            not self.pomodoro_completion_alert_active
            and self.music_lyrics_overlay_active(now)
        ):
            self.draw_music_lyrics_overlay(now)
        indicator_mode = self.activity_indicator_mode()
        if (
            not self.boot_active
            and not self.screensaver_active
            and not self.pomodoro_completion_alert_active
            and indicator_mode is not None
            and not self.menu_active
            and not self.token_popup_visible
        ):
            application_page = self.application_page_active()
            # The expression desktop retains its calibrated upper-right point,
            # and its expanded status bar renders the camera item in place.
            # Every application page instead shares one unambiguous privacy
            # location at the top-centre of the physical circular display.
            if application_page or now >= self.status_visible_until:
                self.draw_activity_indicator(
                    now,
                    (
                        self.application_camera_indicator_position()
                        if application_page
                        else self.expression_camera_indicator_position()
                    ),
                    mode=indicator_mode,
                    compact=True,
                )
        if (
            not self.boot_active
            and not self.screensaver_active
            and not self.pomodoro_completion_alert_active
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

    def update_provisioning_status(self, now: float, force: bool = False) -> bool:
        if not force and now < self.provisioning_next_read_at:
            return False
        self.provisioning_next_read_at = now + 1.0
        try:
            payload = json.loads(PROVISIONING_STATUS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("provisioning status must be an object")
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"active": False, "phase": "idle"}
        active = bool(payload.get("active"))
        qr_path = str(payload.get("qr_path") or "")
        qr_mtime = 0
        if qr_path:
            try:
                qr_mtime = Path(qr_path).stat().st_mtime_ns
            except OSError:
                qr_mtime = 0
        changed = (
            payload != self.provisioning_status
            or active != self.provisioning_active
            or qr_path != self.provisioning_qr_path
            or qr_mtime != self.provisioning_qr_mtime_ns
        )
        self.provisioning_status = payload
        self.provisioning_active = active
        if qr_path != self.provisioning_qr_path or qr_mtime != self.provisioning_qr_mtime_ns:
            self.provisioning_qr_surface = None
            if qr_mtime:
                try:
                    loaded = self.pygame.image.load(qr_path).convert()
                    self.provisioning_qr_surface = self.pygame.transform.scale(
                        loaded,
                        (340, 340),
                    )
                except (OSError, self.pygame.error) as exc:
                    log(f"provisioning QR warning: {exc}")
            self.provisioning_qr_path = qr_path
            self.provisioning_qr_mtime_ns = qr_mtime
        if changed:
            self.needs_redraw = True
        return changed

    def draw_provisioning(self) -> None:
        self.screen.fill((0, 0, 0))
        self.draw_aa_ring(
            self.screen,
            (38, 127, 153, 76),
            (self.width // 2, self.height // 2),
            min(self.width, self.height) // 2 - 10,
            width=3,
        )
        self.draw_centered_text(
            "网络设置",
            self.font_large,
            (226, 246, 250),
            (self.width // 2, 90),
        )
        message = str(
            self.provisioning_status.get("message")
            or "手机扫码完成网络与设备设置"
        )
        self.draw_centered_text(
            self.ellipsize_text(message, self.font_small, 600),
            self.font_small,
            (130, 185, 199),
            (self.width // 2, 132),
        )
        qr_rect = self.pygame.Rect(230, 172, 340, 340)
        if self.provisioning_qr_surface is not None:
            self.screen.blit(self.provisioning_qr_surface, qr_rect.topleft)
        else:
            self.screen.blit(
                self.settings_card_surface(qr_rect.size, False, False),
                qr_rect.topleft,
            )
            self.draw_centered_text(
                "二维码准备中",
                self.font_medium,
                (171, 207, 216),
                qr_rect.center,
            )
        ssid = str(self.provisioning_status.get("ssid") or "")
        mode = str(self.provisioning_status.get("mode") or "lan")
        title = f"临时 Wi-Fi：{ssid}" if mode == "hotspot" and ssid else "打开手机相机扫码"
        self.draw_centered_text(
            self.ellipsize_text(title, self.font_medium, 590),
            self.font_medium,
            (214, 239, 245),
            (self.width // 2, 563),
        )
        self.draw_centered_text(
            "设置 Wi-Fi · 设备名 · 账号 · 模型密钥",
            self.font_small,
            (102, 165, 181),
            (self.width // 2, 609),
        )
        self.draw_centered_text(
            "密钥不会写入二维码、日志或状态文件",
            self.font_status,
            (79, 133, 147),
            (self.width // 2, 667),
        )

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
                f"Edge OS · {status.app_version}",
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
                ("Edge OS", status.app_version),
                ("基础系统", status.os_name),
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

    def draw_aa_round_line(
        self,
        target: object,
        color: tuple[int, ...],
        start: tuple[float, float],
        end: tuple[float, float],
        width: int,
    ) -> None:
        """Draw a supersampled line with fully rounded end caps."""

        line_width = max(1, round(width))
        radius = max(1, math.ceil(line_width / 2))
        padding = radius + 3
        left = math.floor(min(start[0], end[0]) - padding)
        top = math.floor(min(start[1], end[1]) - padding)
        right = math.ceil(max(start[0], end[0]) + padding)
        bottom = math.ceil(max(start[1], end[1]) + padding)
        native_width = max(1, right - left)
        native_height = max(1, bottom - top)
        scale = UI_AA_SCALE
        high = self.pygame.Surface(
            (native_width * scale, native_height * scale),
            self.pygame.SRCALPHA,
        )
        high.fill((0, 0, 0, 0))
        scaled_start = (
            round((start[0] - left) * scale),
            round((start[1] - top) * scale),
        )
        scaled_end = (
            round((end[0] - left) * scale),
            round((end[1] - top) * scale),
        )
        scaled_width = line_width * scale
        cap_radius = max(1, round(scaled_width / 2))
        self.pygame.draw.line(
            high,
            color,
            scaled_start,
            scaled_end,
            scaled_width,
        )
        self.pygame.draw.circle(high, color, scaled_start, cap_radius)
        self.pygame.draw.circle(high, color, scaled_end, cap_radius)
        smooth = self.pygame.transform.smoothscale(
            high,
            (native_width, native_height),
        )
        target.blit(smooth, (left, top))

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
        self.draw_activity_indicator(
            now,
            center,
            mode="camera",
            compact=compact,
            target=target,
            scale=scale,
        )

    def draw_activity_indicator(
        self,
        now: float,
        center: tuple[int, int],
        mode: str,
        compact: bool = False,
        target: object | None = None,
        scale: int = 1,
    ) -> None:
        """Draw smooth camera, microphone, Pomodoro, or shared activity pulses."""

        canvas = target or self.screen
        period = 3.2
        angle = 2.0 * math.pi * (now % period) / period
        wave = 0.5 + 0.5 * math.cos(angle)
        brightness = round(70 + 185 * wave)
        radius = (5 if compact else 7) * scale
        glow = round(22 + 35 * wave)
        if mode == "pomodoro":
            glow_color = (glow, 8, 8)
            core_color = (brightness, 44, 38)
        elif mode == "microphone":
            glow_color = (glow, round(glow * 0.55), 4)
            core_color = (brightness, round(brightness * 0.62), 26)
        elif mode in {
            "camera_microphone",
            "microphone_pomodoro",
            "camera_microphone_pomodoro",
        }:
            palettes = {
                "camera_microphone": ((35, 255, 100), (255, 159, 26)),
                "microphone_pomodoro": ((255, 159, 26), (232, 55, 48)),
                "camera_microphone_pomodoro": (
                    (35, 255, 100),
                    (255, 159, 26),
                    (232, 55, 48),
                ),
            }
            palette = palettes[mode]
            progress = (now % period) / period * len(palette)
            index = int(progress) % len(palette)
            fraction = progress - int(progress)
            blend = 0.5 - 0.5 * math.cos(math.pi * fraction)
            start = palette[index]
            end = palette[(index + 1) % len(palette)]
            shared_brightness = 0.58 + 0.42 * wave
            core_color = tuple(
                round((start[channel] * (1.0 - blend) + end[channel] * blend) * shared_brightness)
                for channel in range(3)
            )
            glow_color = tuple(max(3, round(value * 0.18)) for value in core_color)
        elif mode == "combined":
            green_ratio = 0.5 + 0.5 * math.cos(angle)
            red_ratio = 1.0 - green_ratio
            shared_brightness = 0.52 + 0.48 * (
                0.5 + 0.5 * math.cos(2.0 * angle)
            )
            core_color = (
                round((232 * red_ratio + 35 * green_ratio) * shared_brightness),
                round((55 * red_ratio + 255 * green_ratio) * shared_brightness),
                round((48 * red_ratio + 100 * green_ratio) * shared_brightness),
            )
            glow_color = (
                round((46 * red_ratio + 8 * green_ratio) * shared_brightness),
                round((8 * red_ratio + 57 * green_ratio) * shared_brightness),
                round((8 * red_ratio + 26 * green_ratio) * shared_brightness),
            )
        else:
            glow_color = (8, glow, 26)
            core_color = (35, brightness, 100)
        if scale > 1:
            self.pygame.draw.circle(
                canvas,
                glow_color,
                center,
                radius + 7 * scale,
            )
            self.pygame.draw.circle(canvas, core_color, center, radius)
        else:
            self.draw_aa_circle(canvas, glow_color, center, radius + 7)
            self.draw_aa_circle(canvas, core_color, center, radius)

    def camera_indicator_sources(self) -> tuple[str, ...]:
        sources = list(self.system_status.camera_active_sources)
        sources.extend(self.runtime_vision_sources)
        if self.camera_view_active and not self.gallery_active:
            sources.append("screen_visual_mode")
        if self.video_call_active or bool(self.video_call_status.get("active")):
            sources.append("video_call")
        return tuple(dict.fromkeys(sources))

    def camera_indicator_active(self) -> bool:
        return bool(self.camera_indicator_sources())

    def microphone_indicator_sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.runtime_audio_sources))

    def microphone_indicator_active(self) -> bool:
        return bool(self.microphone_indicator_sources())

    def pomodoro_background_indicator_active(self) -> bool:
        return self.pomodoro.status == "running" and not self.pomodoro_active

    def activity_indicator_mode(self) -> str | None:
        camera_active = self.camera_indicator_active()
        microphone_active = self.microphone_indicator_active()
        pomodoro_active = self.pomodoro_background_indicator_active()
        if camera_active and microphone_active and pomodoro_active:
            return "camera_microphone_pomodoro"
        if camera_active and microphone_active:
            return "camera_microphone"
        if microphone_active and pomodoro_active:
            return "microphone_pomodoro"
        if camera_active and pomodoro_active:
            return "combined"
        if pomodoro_active:
            return "pomodoro"
        if microphone_active:
            return "microphone"
        if camera_active:
            return "camera"
        return None

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

    def set_audio_activity(
        self,
        source: str,
        active: bool,
        ttl_seconds: float = 2.0,
    ) -> dict:
        source = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(source)).strip("_")
        if not source:
            return {"ok": False, "error": "audio activity source is required"}
        if active:
            ttl_seconds = max(0.5, min(float(ttl_seconds), 300.0))
            self.runtime_audio_sources[source] = time.monotonic() + ttl_seconds
        else:
            self.runtime_audio_sources.pop(source, None)
        self.needs_redraw = True
        self.write_state()
        log(f"audio activity source={source} active={active}")
        return {
            "ok": True,
            "source": source,
            "active": active,
            "microphone_indicator_active": self.microphone_indicator_active(),
        }

    def prune_audio_activity(self, now: float) -> None:
        expired = [
            source
            for source, deadline in self.runtime_audio_sources.items()
            if now >= deadline
        ]
        if not expired:
            return
        for source in expired:
            self.runtime_audio_sources.pop(source, None)
        self.needs_redraw = True
        self.write_state()
        log(f"audio activity expired sources={expired}")

    def expression_camera_indicator_position(self) -> tuple[int, int]:
        display_radius = min(self.width, self.height) / 2.0
        safe_radius = max(0.0, display_radius - self.camera_indicator_edge_inset_px)
        angle = math.radians(self.camera_indicator_angle_degrees)
        return (
            round(self.width / 2.0 + math.cos(angle) * safe_radius),
            round(self.height / 2.0 + math.sin(angle) * safe_radius),
        )

    def application_camera_indicator_position(self) -> tuple[int, int]:
        """Return the common privacy-light point for every full-page app."""

        return (self.width // 2, max(34, round(self.height * 0.05)))

    def application_page_active(self) -> bool:
        """Whether the renderer is showing a page rather than the desktop."""

        return bool(
            self.video_call_active
            or self.music_active
            or self.workshop_active
            or self.performance_active
            or self.pomodoro_active
            or self.settings_active
            or self.gallery_active
            or self.camera_view_active
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
        # Keep the power control visually subordinate to the status indicators.
        # Its geometry is intentionally lighter than a literal scaled-down copy:
        # a compact 24 px body, a 3 px stroke and a shorter centre stem give it
        # the same optical weight as the neighbouring Bluetooth/bell glyphs.
        radius = 12 * scale
        stroke = 3 * scale
        arc = self.pygame.Rect(
            x - radius,
            y - radius + 1 * scale,
            radius * 2,
            radius * 2,
        )
        start_angle = math.radians(140)
        end_angle = math.radians(400)
        self.pygame.draw.arc(
            canvas,
            color,
            arc,
            start_angle,
            end_angle,
            width=stroke,
        )
        arc_center = (x, y + 1 * scale)
        for angle in (start_angle, end_angle):
            endpoint = (
                round(arc_center[0] + math.cos(angle) * radius),
                round(arc_center[1] - math.sin(angle) * radius),
            )
            self.pygame.draw.circle(canvas, color, endpoint, stroke // 2)
        power_top = (x, y - 14 * scale)
        power_bottom = (x, y - 2 * scale)
        self.pygame.draw.line(
            canvas,
            color,
            power_top,
            power_bottom,
            width=stroke,
        )
        self.pygame.draw.circle(canvas, color, power_top, stroke // 2)
        self.pygame.draw.circle(canvas, color, power_bottom, stroke // 2)

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
            activity_mode = (
                self.activity_indicator_mode()
                if camera_active_override is None
                else ("camera" if camera_active_override else None)
            )
            if activity_mode is not None:
                self.draw_activity_indicator(
                    now,
                    (middle_x, middle_y),
                    mode=activity_mode,
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

    def radial_menu_context_items(
        self,
        level: str,
        pin_mode: bool = False,
    ) -> list[dict]:
        if level == "applications":
            return self.application_menu.application_items(pin_mode)
        return self.application_menu.main_items()

    def render_application_menu_return_surface(self, now: float) -> object:
        """Render the shared second-level destination for application exits."""
        surface = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        original_screen = self.screen
        try:
            self.screen = surface
            self.draw_radial_menu(now)
        finally:
            self.screen = original_screen
        return surface

    def prepare_application_menu_return(
        self,
        now: float,
        *,
        background_surface: object | None = None,
    ) -> object:
        """Prepare navigation state and the transition target for an app exit."""
        self.prepare_menu_background(background_surface)
        self.menu_active = True
        self.set_radial_menu_context("applications", False, animate=False)
        self.menu_selected = None
        self.reset_application_pin_gesture()
        self.menu_opened_at = now
        self.menu_last_interaction_at = now
        self.status_visible_until = 0.0
        self.token_popup_visible = False
        self.token_popup_last_interaction_at = 0.0
        self.application_return_pending = True
        self.request_system_status_refresh(now)
        return self.render_application_menu_return_surface(now)

    def complete_application_menu_return(self, now: float) -> None:
        """Start the menu idle window only after the exit animation completes."""
        self.menu_active = True
        self.set_radial_menu_context("applications", False, animate=False)
        self.menu_selected = None
        self.reset_application_pin_gesture()
        self.menu_opened_at = now
        self.menu_last_interaction_at = now
        self.application_return_pending = False
        self.needs_redraw = True
        log("application returned to second-level menu")

    def set_radial_menu_context(
        self,
        level: str,
        pin_mode: bool = False,
        *,
        animate: bool = True,
    ) -> None:
        level = "applications" if level == "applications" else "main"
        # Kept in the control protocol for backward compatibility. Application
        # pinning is now an outward continuation gesture, not a separate mode.
        pin_mode = False
        if self.menu_level == level and self.menu_pin_mode == pin_mode:
            self.radial_menu = self.radial_menu_context_items(level, pin_mode)
            return
        source = None
        if animate and self.menu_active:
            source = self.radial_menu_content_surface(self.menu_selected)
        self.menu_level = level
        self.menu_pin_mode = pin_mode
        self.radial_menu = self.radial_menu_context_items(level, pin_mode)
        self.menu_selected = None
        self.reset_application_pin_gesture()
        if source is not None:
            self.menu_level_transition_source = source
            self.menu_level_transition_target = self.radial_menu_content_surface(None)
            self.menu_level_transition_started_at = time.monotonic()
            self.menu_level_transition_active = True
        else:
            self.menu_level_transition_active = False
            self.menu_level_transition_source = None
            self.menu_level_transition_target = None
        self.menu_last_interaction_at = time.monotonic()
        self.needs_redraw = True

    def open_application_menu(self, pin_mode: bool = False) -> None:
        self.set_radial_menu_context("applications", False, animate=True)
        log("application menu opened")

    def reset_application_pin_gesture(self) -> None:
        self.app_pin_candidate_app_id = None
        self.app_pin_drag_progress = 0.0
        self.app_pin_drag_ready = False
        self.app_pin_target_hit = False
        self.app_pin_full_reached_at = 0.0

    def application_pin_target_contains(
        self,
        position: tuple[int, int],
        selected: int,
    ) -> bool:
        """Match touch hit-testing to the visible outer pin-option arc."""
        outer_radius = min(self.width, self.height) * 0.325
        return pin_target_hit(
            position,
            self.radial_menu_center(),
            selected,
            target_radius=outer_radius + 40.0,
            target_half_width=20.0,
            radial_padding=12.0,
            angular_padding_degrees=3.0,
        )

    def update_application_pin_hold(self, now: float) -> bool:
        """Advance dwell confirmation even while the finger is stationary."""
        ready = pin_gesture_ready(
            self.app_pin_drag_progress if self.app_pin_target_hit else 0.0,
            self.app_pin_full_reached_at,
            now,
            self.app_pin_hold_seconds,
        )
        if ready == self.app_pin_drag_ready:
            return False
        self.app_pin_drag_ready = ready
        self.needs_redraw = True
        return True

    def update_application_pin_gesture(
        self,
        selected: int | None,
        distance: float,
    ) -> None:
        app_id: str | None = None
        if self.menu_level == "applications" and selected is not None:
            item = self.radial_menu[selected]
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            if str(action.get("type", "")) == "launch_app":
                candidate = str(action.get("app_id", "")).strip()
                if self.application_menu.application(candidate) is not None:
                    app_id = candidate
        if app_id is None or distance <= self.app_pin_drag_start_distance:
            changed = bool(
                self.app_pin_candidate_app_id
                or self.app_pin_drag_progress
                or self.app_pin_drag_ready
            )
            self.reset_application_pin_gesture()
            if changed:
                self.needs_redraw = True
            return
        progress = pin_gesture_progress(
            distance,
            self.app_pin_drag_start_distance,
            self.app_pin_drag_confirm_distance,
        )
        now = time.monotonic()
        target_hit = progress >= 0.985 and self.application_pin_target_contains(
            self.pointer_position,
            selected,
        )
        if target_hit:
            if (
                app_id != self.app_pin_candidate_app_id
                or not self.app_pin_target_hit
                or self.app_pin_full_reached_at <= 0.0
            ):
                self.app_pin_full_reached_at = now
        else:
            self.app_pin_full_reached_at = 0.0
        ready = pin_gesture_ready(
            progress if target_hit else 0.0,
            self.app_pin_full_reached_at,
            now,
            self.app_pin_hold_seconds,
        )
        if (
            app_id != self.app_pin_candidate_app_id
            or abs(progress - self.app_pin_drag_progress) >= 0.01
            or target_hit != self.app_pin_target_hit
            or ready != self.app_pin_drag_ready
        ):
            self.app_pin_candidate_app_id = app_id
            self.app_pin_drag_progress = progress
            self.app_pin_target_hit = target_hit
            self.app_pin_drag_ready = ready
            self.needs_redraw = True

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
        original_level = self.menu_level
        original_pin_mode = self.menu_pin_mode
        original_menu = self.radial_menu
        for level, pin_mode in (
            ("main", False),
            ("applications", False),
        ):
            self.menu_level = level
            self.menu_pin_mode = pin_mode
            self.radial_menu = self.radial_menu_context_items(level, pin_mode)
            for selected_index in (None, *range(len(self.radial_menu))):
                self.radial_menu_content_surface(selected_index)
            if level == "applications":
                for selected_index, item in enumerate(self.radial_menu):
                    action = (
                        item.get("action")
                        if isinstance(item.get("action"), dict)
                        else {}
                    )
                    if str(action.get("type", "")) != "launch_app":
                        continue
                    for frame in range(1, 25):
                        progress = frame / 24.0
                        for unpin in (False, True):
                            self.application_pin_affordance_surface(
                                selected_index,
                                progress,
                                frame == 24,
                                unpin=unpin,
                            )
        self.menu_level = original_level
        self.menu_pin_mode = original_pin_mode
        self.radial_menu = original_menu
        for meter in ("wifi", "token", "volume"):
            for level in range(21):
                self.status_meter_surface(meter, level)
        self.status_bar_surface(now)
        for phase in ("focus", "short_break"):
            self.pomodoro_duration_adjust_base(phase)
            # Prewarm a sparse set and fill the remaining pointer positions lazily.
            # This preserves a smooth first gesture without retaining 144 large
            # supersampled dial frames in RAM from boot.
            for pointer_step in range(0, 72, 6):
                self.pomodoro_duration_wave_surface(pointer_step, phase)
        for minutes in (1, 5, 10, 15, 20, 25, 30, 45, 60, 90, 120, 180):
            self.pomodoro_duration_time_surface(minutes)
        self.release_unused_memory()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log(
            "control and radial-menu transition cache ready "
            f"entries={len(self.control_overlay_cache) + len(self.radial_menu_content_cache) + len(self.app_pin_affordance_cache) + 64 + len(self.pomodoro_duration_wave_cache) + len(self.pomodoro_duration_time_cache)} "
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
        clock_text = time.strftime("%H:%M") if self.menu_level == "main" else ""
        if clock_text and clock_text != self.radial_menu_time_cache_text:
            self.radial_menu_time_cache_text = clock_text
            stale_main_keys = [
                key
                for key in self.radial_menu_content_cache
                if key and key[0] == "main"
            ]
            for key in stale_main_keys:
                self.radial_menu_content_cache.pop(key, None)
        cache_key = (
            self.menu_level,
            self.menu_pin_mode,
            self.application_menu.pinned_app_id,
            selected_index,
            clock_text,
        )
        cached = self.radial_menu_content_cache.get(cache_key)
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
        if self.menu_level == "applications":
            title = self.font_medium.render(
                "应用",
                True,
                (151, 205, 222),
            )
            surface.blit(title, title.get_rect(center=center))
        elif clock_text:
            high = self.font_radial_time_high.render(
                clock_text,
                True,
                (211, 235, 242),
            )
            clock = self.pygame.transform.smoothscale(
                high,
                (
                    max(1, round(high.get_width() / UI_AA_SCALE)),
                    max(1, round(high.get_height() / UI_AA_SCALE)),
                ),
            )
            surface.blit(clock, clock.get_rect(center=(center[0], center[1] - 2)))
        self.radial_menu_content_cache[cache_key] = surface
        return surface

    def application_pin_affordance_surface(
        self,
        selected: int,
        progress: float,
        ready: bool,
        *,
        unpin: bool = False,
    ) -> tuple[object, tuple[int, int]]:
        """Return one pre-renderable frame of the pin or unpin affordance."""
        frame_count = 24
        frame = max(1, min(round(progress * frame_count), frame_count))
        cache_key = (selected, frame, bool(ready), bool(unpin))
        cached = self.app_pin_affordance_cache.get(cache_key)
        if cached is not None:
            return cached
        progress = frame / frame_count
        eased = progress * progress * (3.0 - 2.0 * progress)
        center = self.radial_menu_center()
        outer_radius = min(self.width, self.height) * 0.325
        middle_angle = -math.pi / 2 + selected * math.tau / 6
        half_sector = math.tau / 12
        start_angle = middle_angle - half_sector + math.radians(2.5)
        end_angle = middle_angle + half_sector - math.radians(2.5)
        middle_radius = outer_radius + 40.0
        thickness = 7.0 + 33.0 * eased
        inner = middle_radius - thickness / 2.0
        outer = middle_radius + thickness / 2.0
        layer = self.pygame.Surface(self.target_size, self.pygame.SRCALPHA)
        layer.fill((0, 0, 0, 0))
        glow_alpha = round((34 + 42 * eased) * progress)
        self.draw_capsule_arc(
            layer,
            center,
            inner - 6.0 * eased,
            outer + 6.0 * eased,
            start_angle,
            end_angle,
            (57, 205, 242, glow_alpha),
            soft_edge=True,
            steps=28,
        )
        if ready and unpin:
            fill = (247, 155, 135, 252)
        elif ready:
            fill = (105, 226, 248, 252)
        elif unpin:
            fill = (
                round(183 + 32 * eased),
                round(112 + 28 * eased),
                round(105 + 20 * eased),
                round(225 + 27 * eased),
            )
        else:
            fill = (
                round(70 + 22 * eased),
                round(205 + 18 * eased),
                round(239 + 8 * eased),
                round(225 + 27 * eased),
            )
        self.draw_capsule_arc(
            layer,
            center,
            inner,
            outer,
            start_angle,
            end_angle,
            fill,
            soft_edge=True,
            steps=36,
        )
        label_progress = max(0.0, min((progress - 0.24) / 0.36, 1.0))
        if label_progress > 0.0:
            self.draw_curved_arc_text(
                layer,
                "取消固定" if unpin else "固定到桌面",
                center,
                middle_radius,
                middle_angle,
                min(end_angle - start_angle - math.radians(12), math.radians(32)),
                (3, 27, 35),
                round(255 * label_progress),
            )
        bounds = layer.get_bounding_rect(min_alpha=1)
        cropped = self.pygame.Surface(bounds.size, self.pygame.SRCALPHA)
        cropped.blit(layer, (0, 0), bounds)
        cached = (cropped, (bounds.x, bounds.y))
        self.app_pin_affordance_cache[cache_key] = cached
        return cached

    def draw_curved_arc_text(
        self,
        target: object,
        text: str,
        center: tuple[int, int],
        radius: float,
        middle_angle: float,
        angular_span: float,
        color: tuple[int, int, int],
        alpha: int,
    ) -> None:
        """Lay out glyphs individually on an arc with a curved baseline."""
        glyphs = list(text)
        if not glyphs:
            return
        if len(glyphs) == 1:
            angles = [middle_angle]
        else:
            step = angular_span / (len(glyphs) - 1)
            angles = [
                middle_angle - angular_span / 2.0 + index * step
                for index in range(len(glyphs))
            ]
        middle_rotation = math.degrees(middle_angle) + 90.0
        flip = middle_rotation > 90.0 or middle_rotation < -90.0
        if flip:
            angles.reverse()
        for glyph_text, angle in zip(glyphs, angles):
            glyph = self.font_small.render(glyph_text, True, color)
            glyph.set_alpha(max(0, min(alpha, 255)))
            rotation = math.degrees(angle) + 90.0 + (180.0 if flip else 0.0)
            while rotation > 180.0:
                rotation -= 360.0
            while rotation <= -180.0:
                rotation += 360.0
            oriented = self.pygame.transform.rotozoom(glyph, -rotation, 1.0)
            anchor = (
                round(center[0] + math.cos(angle) * radius),
                round(center[1] + math.sin(angle) * radius),
            )
            target.blit(oriented, oriented.get_rect(center=anchor))

    def draw_application_pin_affordance(self) -> None:
        """Morph the selected app's outer line into a pin confirmation arc."""
        progress = max(0.0, min(self.app_pin_drag_progress, 1.0))
        selected = self.menu_selected
        if (
            self.menu_level != "applications"
            or selected is None
            or self.app_pin_candidate_app_id is None
            or progress <= 0.0
        ):
            return
        item = self.radial_menu[selected]
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        if str(action.get("app_id", "")) != self.app_pin_candidate_app_id:
            return
        surface, position = self.application_pin_affordance_surface(
            selected,
            progress,
            self.app_pin_drag_ready,
            unpin=(
                self.application_menu.pinned_app_id
                == self.app_pin_candidate_app_id
            ),
        )
        self.screen.blit(surface, position)

    def draw_radial_menu(self, now: float) -> None:
        if self.menu_background_surface is not None:
            self.screen.blit(self.menu_background_surface, (0, 0))
        layer = self.overlay_surface
        layer.fill((0, 0, 0, 105))
        self.screen.blit(layer, (0, 0))
        self.draw_status_bar(now)
        if self.menu_level_transition_active:
            elapsed = now - self.menu_level_transition_started_at
            raw = max(
                0.0,
                min(elapsed / self.menu_level_transition_seconds, 1.0),
            )
            eased = 1.0 - (1.0 - raw) ** 3
            source = self.menu_level_transition_source
            target = self.menu_level_transition_target
            if source is not None:
                faded = source.copy()
                faded.set_alpha(round(255 * (1.0 - eased)))
                self.screen.blit(faded, (0, 0))
            if target is not None:
                appearing = target.copy()
                appearing.set_alpha(round(255 * eased))
                self.screen.blit(appearing, (0, 0))
            if raw >= 1.0:
                self.menu_level_transition_active = False
                self.menu_level_transition_source = None
                self.menu_level_transition_target = None
        else:
            self.screen.blit(
                self.radial_menu_content_surface(self.menu_selected),
                (0, 0),
            )
            self.draw_application_pin_affordance()
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
        percent = self.volume_percent_from_position(position)
        self.set_system_volume_percent(percent, force=force)

    def set_music_volume_from_position(
        self,
        position: tuple[int, int],
        force: bool = False,
    ) -> None:
        percent = self.music_volume_percent_from_position(position)
        self.set_system_volume_percent(percent, force=force)

    def set_system_volume_percent(
        self,
        percent: int,
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self.volume_last_set_at < 0.055:
            return
        percent = max(0, min(int(percent), 100))
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
        self.update_application_pin_gesture(selected, distance)
        if selected != self.menu_selected:
            self.menu_selected = selected
            self.needs_redraw = True

    def handle_pointer_down(self, position: tuple[int, int]) -> None:
        now = time.monotonic()
        if self.provisioning_active:
            self.last_touch_at = now
            return
        if self.pomodoro_completion_alert_active:
            self.last_touch_at = now
            return
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
        self.reset_application_pin_gesture()
        self.edge_exit_candidate = False
        self.edge_exit_ready = False
        self.edge_exit_progress = 0.0
        if self.video_call_active:
            self.video_call_pointer_target = self.video_call_target_at(position)
            self.needs_redraw = True
            return
        if self.music_active:
            if self.music_transition_active:
                self.pointer_down = False
                self.music_pointer_target = None
                return
            self.music_pointer_target = self.music_target_at(position)
            if self.music_pointer_target == "volume":
                self.music_volume_expand_from = self.music_volume_expansion(now)
                self.music_volume_dragging = True
                self.music_volume_interaction_started_at = now
                self.music_volume_last_interaction_at = now
                self.set_music_volume_from_position(position, force=True)
            self.needs_redraw = True
            return
        if self.workshop_active:
            if self.workshop_transition_active:
                self.pointer_down = False
                self.workshop_pointer_target = None
                return
            self.workshop_pointer_target = self.workshop_target_at(position)
            self.needs_redraw = True
            return
        if self.performance_active:
            if self.performance_transition_active:
                self.pointer_down = False
                self.performance_pointer_target = None
                return
            if self.performance_section == "system":
                self.performance_health_scroll_start_offset = (
                    self.performance_health_scroll_offset
                )
            self.performance_pointer_target = self.performance_target_at(position)
            self.needs_redraw = True
            return
        if self.pomodoro_active:
            if self.pomodoro_transition_active:
                self.pointer_down = False
                self.pomodoro_pointer_target = None
                return
            self.pomodoro_pointer_target = self.pomodoro_target_at(position)
            self.needs_redraw = True
            return
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
        if self.video_call_active:
            if self.pointer_moved:
                self.video_call_pointer_target = None
            self.needs_redraw = True
            return
        if self.music_active:
            if self.music_volume_dragging:
                self.music_volume_last_interaction_at = time.monotonic()
                self.set_music_volume_from_position(position)
                self.needs_redraw = True
                return
            if self.pointer_moved:
                self.music_pointer_target = None
            self.needs_redraw = True
            return
        if self.workshop_active:
            if self.pointer_moved:
                self.workshop_pointer_target = None
            self.needs_redraw = True
            return
        if self.performance_active:
            if self.pointer_moved:
                self.performance_pointer_target = None
                if self.performance_section == "system":
                    dx = position[0] - self.pointer_start[0]
                    dy = position[1] - self.pointer_start[1]
                    if abs(dy) > abs(dx) * 0.72:
                        self.performance_health_scroll_offset = (
                            self.clamp_performance_health_scroll(
                                self.performance_health_scroll_start_offset - dy
                            )
                        )
            self.needs_redraw = True
            return
        if self.pomodoro_active:
            if self.pomodoro_duration_adjust_active:
                self.update_pomodoro_duration_adjustment(position)
            elif self.pomodoro_pointer_target == "duration_crown":
                if (
                    time.monotonic() - self.pointer_started_at
                    >= self.pomodoro_duration_hold_seconds
                ):
                    self.begin_pomodoro_duration_adjustment(time.monotonic())
                    self.update_pomodoro_duration_adjustment(position)
                elif math.dist(position, self.pointer_start) > 60:
                    self.pomodoro_pointer_target = None
            elif self.pointer_moved:
                self.pomodoro_pointer_target = None
            self.needs_redraw = True
            return
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
            if self.menu_level_transition_active:
                self.menu_selected = None
                self.needs_redraw = True
                return
            # Status controls are tap targets, not hover targets. A directional
            # menu gesture may cross their arcs without changing control layers.
            self.update_menu_selection(position)

    def handle_pointer_up(self, position: tuple[int, int]) -> None:
        if not self.pointer_down:
            return
        self.pointer_position = position
        self.touch_count += 1
        if self.video_call_active:
            target = self.video_call_pointer_target
            swipe_direction = (
                self.pomodoro_horizontal_swipe_direction(position)
                if self.pointer_moved
                else 0
            )
            if swipe_direction > 0:
                self.close_video_call_to_application_menu(hangup=True)
            elif (
                not self.pointer_moved
                and target in {"back", "hangup"}
                and target == self.video_call_target_at(position)
            ):
                self.close_video_call_to_application_menu(hangup=True)
            self.video_call_pointer_target = None
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
        if self.music_active:
            target = self.music_pointer_target
            held_seconds = max(0.0, time.monotonic() - self.pointer_started_at)
            if self.music_volume_dragging:
                self.set_music_volume_from_position(position, force=True)
                self.music_volume_dragging = False
                self.music_volume_last_interaction_at = time.monotonic()
                self.music_volume_expand_from = 1.0
                self.music_pointer_target = None
                self.pointer_down = False
                self.pointer_moved = False
                self.needs_redraw = True
                self.write_state()
                return
            swipe_direction = (
                self.pomodoro_horizontal_swipe_direction(position)
                if self.pointer_moved
                else 0
            )
            if swipe_direction > 0:
                self.close_music(animated=True)
                log("music returned by left-to-right swipe")
            elif (
                not self.pointer_moved
                and target is not None
                and target == self.music_target_at(position)
            ):
                self.handle_music_target(
                    "refresh"
                    if target == "mode"
                    and held_seconds >= self.music_mode_hold_seconds
                    else target
                )
            self.music_pointer_target = None
            self.music_volume_dragging = False
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
        if self.workshop_active:
            target = self.workshop_pointer_target
            swipe_direction = (
                self.pomodoro_horizontal_swipe_direction(position)
                if self.pointer_moved
                else 0
            )
            if swipe_direction > 0:
                if self.workshop_section == "runtime":
                    self.handle_workshop_target("back")
                elif self.workshop_section is not None:
                    self.workshop_section = None
                else:
                    self.close_workshop(animated=True)
                log("workshop returned by left-to-right swipe")
            elif (
                not self.pointer_moved
                and target is not None
                and target == self.workshop_target_at(position)
            ):
                self.handle_workshop_target(target)
            self.workshop_pointer_target = None
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
        if self.performance_active:
            target = self.performance_pointer_target
            swipe_direction = (
                self.pomodoro_horizontal_swipe_direction(position)
                if self.pointer_moved
                else 0
            )
            if self.performance_section == "system":
                if swipe_direction > 0:
                    self.start_performance_section_transition(None, -1)
                    log("performance health returned by left-to-right swipe")
                elif (
                    not self.pointer_moved
                    and target == "back"
                    and target == self.performance_target_at(position)
                ):
                    self.start_performance_section_transition(None, -1)
                elif (
                    not self.pointer_moved
                    and target == "recover"
                    and target == self.performance_target_at(position)
                ):
                    self.request_full_recovery()
            elif swipe_direction > 0:
                self.close_performance(animated=True)
                log("performance returned by left-to-right swipe")
            elif (
                not self.pointer_moved
                and target == "back"
                and target == self.performance_target_at(position)
            ):
                self.close_performance(animated=True)
            elif (
                not self.pointer_moved
                and target == "system"
                and target == self.performance_target_at(position)
            ):
                self.performance_health_scroll_offset = 0.0
                self.performance_health_scroll_start_offset = 0.0
                self.request_system_status_refresh(time.monotonic())
                self.start_performance_section_transition("system", 1)
                log("performance health detail opened")
            self.performance_pointer_target = None
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
        if self.pomodoro_active:
            if self.pomodoro_duration_adjust_active:
                self.update_pomodoro_duration_adjustment(position)
                self.commit_pomodoro_duration_adjustment()
                self.pointer_down = False
                self.pointer_moved = False
                self.needs_redraw = True
                return
            target = self.pomodoro_pointer_target
            swipe_direction = (
                self.pomodoro_horizontal_swipe_direction(position)
                if self.pointer_moved
                else 0
            )
            if self.pomodoro_statistics_active and swipe_direction < 0:
                self.navigate_pomodoro_statistics(1)
            elif (
                self.pomodoro_statistics_active
                and swipe_direction > 0
                and self.pomodoro_statistics_page > 0
            ):
                self.navigate_pomodoro_statistics(0)
            elif swipe_direction > 0:
                self.handle_pomodoro_target("back")
                log("pomodoro returned by left-to-right swipe")
            elif not self.pointer_moved and target == self.pomodoro_target_at(position):
                self.handle_pomodoro_target(target)
            self.pomodoro_pointer_target = None
            self.pointer_down = False
            self.pointer_moved = False
            self.needs_redraw = True
            self.write_state()
            return
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
            if self.menu_level_transition_active:
                self.pointer_down = False
                self.pointer_moved = False
                self.menu_selected = None
                self.menu_last_interaction_at = time.monotonic()
                self.needs_redraw = True
                self.write_state()
                return
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
            if (
                self.menu_level == "applications"
                and self.app_pin_drag_ready
                and self.app_pin_candidate_app_id
                and self.menu_selected is not None
            ):
                selected_item = dict(self.radial_menu[self.menu_selected])
                selected_action = (
                    selected_item.get("action")
                    if isinstance(selected_item.get("action"), dict)
                    else {}
                )
                if str(selected_action.get("type", "")) == "launch_app":
                    self.execute_menu_item(
                        {
                            "id": selected_item.get("id"),
                            "label": selected_item.get("label"),
                            "glyph": selected_item.get("glyph"),
                            "action": {
                                "type": (
                                    "unpin_app"
                                    if self.application_menu.pinned_app_id
                                    == self.app_pin_candidate_app_id
                                    else "pin_app"
                                ),
                                "app_id": self.app_pin_candidate_app_id,
                            },
                        }
                    )
                    self.pointer_down = False
                    self.pointer_moved = False
                    self.menu_selected = None
                    self.write_state()
                    return
            self.update_menu_selection(position)
            selected = self.menu_selected
            if selected is not None:
                item = dict(self.radial_menu[selected])
                if self.menu_action_keeps_open(item):
                    self.execute_menu_item(item)
                else:
                    self.close_radial_menu()
                    self.execute_menu_item(item)
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
        if self.pomodoro_active:
            if (
                self.pointer_down
                and self.pomodoro_pointer_target == "duration_crown"
                and now - self.pointer_started_at
                >= self.pomodoro_duration_hold_seconds
            ):
                self.begin_pomodoro_duration_adjustment(now)
            return
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
            or self.music_active
            or self.workshop_active
            or self.performance_active
            or self.pomodoro_active
            or self.settings_active
            or self.pointer_moved
            or self.camera_pointer_target is not None
        ):
            return
        if now - self.pointer_started_at < self.long_press_seconds:
            return
        self.prepare_menu_background()
        self.set_radial_menu_context("main", False, animate=False)
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
        self.set_radial_menu_context("main", False, animate=False)
        self.menu_level_transition_active = False
        self.menu_level_transition_source = None
        self.menu_level_transition_target = None
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

    @staticmethod
    def menu_item_action_type(item: dict) -> str:
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        return str(action.get("type", "emit"))

    def menu_action_keeps_open(self, item: dict) -> bool:
        action_type = self.menu_item_action_type(item)
        return action_type in {
            "open_app_menu",
            "open_app_pin_menu",
            "menu_back",
            "pin_app",
            "unpin_app",
            "noop",
        }

    def launch_application(self, app_id: str) -> bool:
        if app_id == "pomodoro":
            self.open_pomodoro()
            return True
        if app_id == "performance":
            self.open_performance()
            return True
        if app_id == "music":
            self.open_music()
            return True
        if app_id == "workshop":
            self.open_workshop()
            return True
        if app_id == "video_call":
            self.set_video_call_view(True, request_service=True)
            return True
        return False

    def prepare_voice_application_switch(self, target: str) -> None:
        """Close other full-page apps without stopping their background work."""

        self.close_radial_menu()
        if target != "video_call":
            self.video_call_active = False
            self.video_call_pointer_target = None
            if self.video_call_frame_future is not None:
                self.video_call_frame_future.cancel()
            self.video_call_frame_future = None
        if target != "pomodoro":
            self.pomodoro_active = False
            self.pomodoro_statistics_active = False
            self.pomodoro_duration_adjust_active = False
            self.pomodoro_transition_active = False
            self.pomodoro_transition_source = None
            self.pomodoro_transition_target = None
            self.pomodoro_pointer_target = None
        if target != "performance":
            self.performance_active = False
            self.performance_section = None
            self.performance_health_scroll_offset = 0.0
            self.performance_health_scroll_start_offset = 0.0
            self.performance_transition_active = False
            self.performance_transition_source = None
            self.performance_transition_target = None
            self.performance_pointer_target = None
        if target != "music":
            self.music_active = False
            self.music_transition_active = False
            self.music_transition_source = None
            self.music_transition_target = None
            self.music_pointer_target = None
            self.music_volume_dragging = False
        if target != "workshop":
            if self.workshop_section == "runtime" and self.workshop_action_future is None:
                self.workshop_action_kind = "stop"
                self.workshop_action_future = self.control_executor.submit(stop_workshop_app)
            self.workshop_active = False
            self.workshop_section = None
            self.workshop_transition_active = False
            self.workshop_transition_source = None
            self.workshop_transition_target = None
            self.workshop_pointer_target = None
        self.needs_redraw = True

    def execute_menu_item(self, item: dict) -> None:
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        action_type = str(action.get("type", "emit"))
        result = "emitted"
        navigation_actions = {
            "open_app_menu",
            "open_app_pin_menu",
            "menu_back",
            "pin_app",
            "unpin_app",
            "noop",
        }
        if (
            action_type != "camera_voice"
            and action_type not in navigation_actions
            and self.camera_view_active
        ):
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
        elif action_type == "open_pomodoro":
            self.open_pomodoro()
            result = "opened"
        elif action_type == "open_app_menu":
            self.open_application_menu(False)
            result = "opened"
        elif action_type == "open_app_pin_menu":
            self.open_application_menu(False)
            result = "opened"
        elif action_type == "menu_back":
            self.set_radial_menu_context("main", False, animate=True)
            result = "returned"
        elif action_type == "pin_app":
            app_id = str(action.get("app_id", "")).strip()
            if self.application_menu.pin(app_id):
                self.radial_menu_content_cache.clear()
                self.set_radial_menu_context("main", False, animate=True)
                result = "pinned"
            else:
                result = "pin_failed"
        elif action_type == "unpin_app":
            app_id = str(action.get("app_id", "")).strip()
            if self.application_menu.unpin(app_id):
                self.radial_menu_content_cache.clear()
                self.set_radial_menu_context("main", False, animate=True)
                result = "unpinned"
            else:
                result = "unpin_failed"
        elif action_type == "launch_app":
            app_id = str(action.get("app_id", "")).strip()
            result = "opened" if self.launch_application(app_id) else "failed"
        elif action_type == "noop":
            self.menu_selected = None
            self.menu_last_interaction_at = time.monotonic()
            result = "ignored"
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

    def execute_menu_action(self, selected: int) -> None:
        self.execute_menu_item(dict(self.radial_menu[selected]))

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
            "expression_animation_fps": round(self.expression_animation_fps, 3),
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
            "provisioning": {
                "active": self.provisioning_active,
                "phase": self.provisioning_status.get("phase", "idle"),
                "mode": self.provisioning_status.get("mode"),
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
            "radial_menu_level": self.menu_level,
            "application_return_navigation": {
                "target": "applications",
                "pending": self.application_return_pending,
                "applies_to": ["pomodoro", "performance", "music", "future-apps"],
                "idle_window_starts_after_transition": True,
            },
            "radial_menu_center_display": {
                "main": "current-time",
                "format": "24-hour-HH:MM",
                "interactive": False,
            },
            "radial_menu_pin_mode": self.menu_pin_mode,
            "radial_menu_pinned_app": self.application_menu.pinned_app_id,
            "radial_menu_pin_gesture": {
                "candidate_app": self.app_pin_candidate_app_id,
                "progress": round(self.app_pin_drag_progress, 3),
                "ready": self.app_pin_drag_ready,
                "target_hit": self.app_pin_target_hit,
                "hold_seconds": self.app_pin_hold_seconds,
                "full_reached_monotonic": self.app_pin_full_reached_at or None,
                "start_distance_px": round(self.app_pin_drag_start_distance),
                "confirm_distance_px": round(self.app_pin_drag_confirm_distance),
                "release_action": "pin-to-home-menu-after-target-hit-and-hold",
            },
            "radial_menu_level_transition": {
                "active": self.menu_level_transition_active,
                "duration_ms": round(self.menu_level_transition_seconds * 1000),
                "style": "cached-crossfade-ease-out",
            },
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
            "pomodoro": {
                **self.pomodoro.snapshot(),
                "active": self.pomodoro_active,
                "statistics_active": self.pomodoro_statistics_active,
                "statistics_page": self.pomodoro_statistics_page,
                "statistics": self.pomodoro.statistics_snapshot(),
                "pointer_target": self.pomodoro_pointer_target,
                "duration_dial": {
                    "active": self.pomodoro_duration_adjust_active,
                    "moved": self.pomodoro_duration_dial_moved,
                    "selected_minutes": self.pomodoro_duration_selected_minutes,
                    "pointer_degrees_clockwise": round(
                        math.degrees(self.pomodoro_duration_pointer_clockwise),
                        2,
                    ),
                    "minimum_minutes": self.pomodoro_duration_min_minutes,
                    "maximum_minutes": self.pomodoro_duration_max_minutes,
                    "step_minutes": self.pomodoro_duration_step_minutes,
                    "visual_tick_count": 72,
                    "hold_seconds": self.pomodoro_duration_hold_seconds,
                    "editable_phases": [
                        "focus",
                        "short_break",
                        "long_break",
                    ],
                    "allowed_statuses": ["ready", "paused"],
                    "scope": "current-phase-one-off",
                    "gesture": "hold-crown-then-circular-drag-release-to-confirm",
                },
                "completion_alert": {
                    "active": self.pomodoro_completion_alert_active,
                    "completed_phase": self.pomodoro_completion_alert_phase,
                    "cycles": self.pomodoro_completion_alert_cycles,
                    "cycle_seconds": self.pomodoro_completion_alert_cycle_seconds,
                    "rise_seconds": self.pomodoro_completion_alert_rise_seconds,
                    "hold_seconds": self.pomodoro_completion_alert_hold_seconds,
                    "fall_seconds": round(
                        self.pomodoro_completion_alert_cycle_seconds
                        - self.pomodoro_completion_alert_rise_seconds
                        - self.pomodoro_completion_alert_hold_seconds,
                        3,
                    ),
                    "total_seconds": round(
                        self.pomodoro_completion_alert_cycles
                        * self.pomodoro_completion_alert_cycle_seconds,
                        3,
                    ),
                    "pulse": self.pomodoro_completion_alert_pulse,
                    "intensity": round(
                        self.pomodoro_completion_alert_intensity,
                        4,
                    ),
                    "lit": self.pomodoro_completion_alert_lit,
                    "transition": "smoothstep-alpha-crossfade",
                    "focus_background": "tomato-red",
                    "break_background": "leaf-green",
                    "theme_controls_when_lit": "black",
                    "input_blocked_during_alert": True,
                },
                "transition": {
                    "active": self.pomodoro_transition_active,
                    "opening": self.pomodoro_transition_opening,
                    "exits_page": self.pomodoro_transition_exits_page,
                    "duration_ms": round(
                        self.pomodoro_transition_seconds * 1000
                    ),
                    "style": "cached-carousel-slide-ease-out",
                    "pre_rendered": True,
                },
                "colors": {
                    "focus": "tomato-red",
                    "break": "leaf-green",
                },
                "continues_when_page_closed": True,
                "restores_after_service_restart": True,
            },
            "performance": {
                **self.performance_snapshot.as_dict(),
                "active": self.performance_active,
                "section": self.performance_section,
                "health_scroll_offset": round(
                    self.performance_health_scroll_offset,
                    2,
                ),
                "health_scroll_max": round(
                    self.performance_health_max_scroll(),
                    2,
                ),
                "pointer_target": self.performance_pointer_target,
                "refresh_seconds": self.performance_refresh_seconds,
                "refresh_pending": self.performance_future is not None,
                "transition": {
                    "active": self.performance_transition_active,
                    "opening": self.performance_transition_opening,
                    "exits_page": self.performance_transition_exits_page,
                    "duration_ms": round(
                        self.performance_transition_seconds * 1000
                    ),
                    "style": "cached-carousel-slide-ease-out",
                },
            },
            "music": {
                **self.music_player.snapshot(),
                "active": self.music_active,
                "pointer_target": self.music_pointer_target,
                "scan_pending": self.music_scan_future is not None,
                "scan_completed": self.music_scan_completed,
                "scan_error": self.music_scan_error,
                "autoplay_pending": self.music_autoplay_pending,
                "voice_controls": [
                    "open",
                    "close",
                    "play",
                    "pause",
                    "previous",
                    "next",
                    "set-mode",
                    "set-lyrics",
                ],
                "playback_mode_control": {
                    "tap_action": "cycle-list-single-shuffle",
                    "hold_action": "refresh-library",
                    "hold_seconds": self.music_mode_hold_seconds,
                    "label": self.music_playback_mode_label(
                        self.music_player.playback_mode
                    ),
                },
                "volume_control": {
                    "percent": self.system_status.volume_percent,
                    "dragging": self.music_volume_dragging,
                    "geometry": list(self.music_volume_geometry()),
                    "scope": "system-default-audio-sink",
                    "direction": "bottom-zero-top-full",
                    "interaction": "touch-or-drag-arc",
                    "idle_style": "thin-arc-and-position-dot",
                    "adjusting_style": "expanded-capsule-track",
                    "idle_seconds": self.music_volume_idle_seconds,
                },
                "supported_extensions": sorted(
                    ["mp3", "flac", "wav", "m4a", "aac", "ogg", "opus"]
                ),
                "lyrics": {
                    "enabled": True,
                    "expression_overlay_enabled": self.music_lyrics_enabled,
                    "expression_overlay_toggle": "lyrics-button",
                    "available": bool(self.music_lyrics),
                    "automatic_search": True,
                    "search_status": self.music_lyrics_search_status,
                    "search_trigger": self.music_lyrics_search_trigger,
                    "search_pending": self.music_lyrics_fetch_future is not None,
                    "search_error": self.music_lyrics_search_error or None,
                    "provider": "lrclib",
                    "manual_control": False,
                    "expression_overlay_manual_control": True,
                    "path": str(self.music_lyrics_path) if self.music_lyrics_path else None,
                    "line_count": len(self.music_lyrics),
                    "current_index": self.music_lyric_index,
                    "current_line": (
                        self.music_lyrics[self.music_lyric_index].text
                        if 0 <= self.music_lyric_index < len(self.music_lyrics)
                        else None
                    ),
                    "overlay_active": self.music_lyrics_overlay_active(time.monotonic()),
                    "format": "same-basename-lrc",
                    "display_style": "single-line-borderless-horizontal-marquee",
                    "band_geometry": list(self.music_lyric_band_geometry()),
                    "horizontal_edge_fade_px": 56,
                    "marquee_speed_px_per_second": self.music_lyric_marquee_speed,
                    "short_lines_centered": True,
                },
                "artwork": {
                    "available": bool(
                        self.music_player.current_track
                        and self.music_player.current_track.artwork_path
                    ),
                    "path": (
                        self.music_player.current_track.artwork_path
                        if self.music_player.current_track
                        and self.music_player.current_track.artwork_path
                        else None
                    ),
                    "decoded": self.music_artwork_surface is not None,
                    "loading": self.music_artwork_future is not None,
                },
                "center": {
                    "mode": self.music_center_mode,
                    "tap_action": "toggle-artwork-lyrics",
                    "lyrics_style": "borderless-horizontal-axis-vertical-scroll",
                    "lyrics_geometry": list(self.music_player_lyrics_geometry()),
                    "horizontal_edge_fade_px": 72,
                    "transition_active": self.music_center_transition_active,
                    "transition_ms": round(
                        self.music_center_transition_seconds * 1000
                    ),
                },
                "continues_when_page_closed": True,
                "transition": {
                    "active": self.music_transition_active,
                    "opening": self.music_transition_opening,
                    "exits_page": self.music_transition_exits_page,
                    "duration_ms": round(self.music_transition_seconds * 1000),
                    "style": "cached-carousel-slide-ease-out",
                },
            },
            "workshop": {
                "active": self.workshop_active,
                "pointer_target": self.workshop_pointer_target,
                "notice": (
                    self.workshop_notice
                    if time.monotonic() < self.workshop_notice_until
                    else ""
                ),
                "capabilities": [
                    "requirements-conversation",
                    "model-to-declarative-plan",
                    "signed-package-validation",
                    "capability-authorization",
                    "user-permission-review",
                    "transactional-registration",
                    "declarative-host-runtime",
                ],
                "section": self.workshop_section,
                "manifest_protocol": "riverbank.workshop/v1",
                "host_protocol": "riverbank.app-host/v1",
                "installed_app_count": self.workshop_installed_app_count(),
                "pending_review": self.workshop_pending_review(),
                "service_connected": bool(self.workshop_status.get("ok")),
                "action_pending": self.workshop_action_future is not None,
                "execution_policy": "validated-declarative-only",
                "python_sandbox_enabled": False,
                "transition": {
                    "active": self.workshop_transition_active,
                    "opening": self.workshop_transition_opening,
                    "exits_page": self.workshop_transition_exits_page,
                    "duration_ms": round(
                        self.workshop_transition_seconds * 1000
                    ),
                    "style": "cached-carousel-slide-ease-out",
                },
            },
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
            "video_call": {
                **self.video_call_status,
                "view_active": self.video_call_active,
                "pointer_target": self.video_call_pointer_target,
                "last_frame_at": self.video_call_last_frame_at or None,
                "last_error": self.video_call_last_error,
                "frame_pending": self.video_call_frame_future is not None,
                "status_pending": self.video_call_status_future is not None,
                "view_fps": self.video_call_view_fps,
                "base_url": self.video_call_base_url,
                "transport_scope": "LAN-or-tailnet",
                "local_camera_source": "camera-hub",
                "controls": ["open", "hangup"],
            },
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
            "activity_indicator": {
                "mode": self.activity_indicator_mode(),
                "pomodoro_background_active": (
                    self.pomodoro_background_indicator_active()
                ),
                "camera_active": self.camera_indicator_active(),
                "microphone_active": self.microphone_indicator_active(),
                "microphone_sources": list(self.microphone_indicator_sources()),
                "position": list(
                    self.application_camera_indicator_position()
                    if self.application_page_active()
                    else self.expression_camera_indicator_position()
                ),
                "desktop_position": list(
                    self.expression_camera_indicator_position()
                ),
                "application_position": list(
                    self.application_camera_indicator_position()
                ),
                "placement": (
                    "application-top-centre"
                    if self.application_page_active()
                    else "desktop-upper-right"
                ),
            },
            "last_menu_selection": self.last_menu_selection,
            "system_status": self.system_status.as_dict(),
            "renderer_pid": os.getpid(),
            "player_pid": os.getpid(),
            "window_id": self.window_id,
            "deadline_monotonic": self.deadline,
            "cache_mb": round(self.cache_bytes / 1024 / 1024, 1),
            "cache_limit_mb": round(self.cache_limit / 1024 / 1024, 1),
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
        self.update_provisioning_status(now)
        if self.provisioning_active:
            if now - self.last_overlay_redraw >= self.render_interval:
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.update_pomodoro_timer(now):
            self.write_state()
        if self.update_pomodoro_completion_alert(now):
            self.write_state()
        if (
            self.pomodoro_completion_alert_active
            and now - self.last_overlay_redraw >= self.render_interval
        ):
            self.needs_redraw = True
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
        self.update_video_call(now)
        self.update_camera_capture(now)
        self.update_gallery_page_transition(now)
        self.prune_vision_activity(now)
        self.prune_audio_activity(now)
        self.update_restart_request(now)
        if self.update_token_balance(now):
            self.write_state()
        if self.update_system_status_async(now):
            self.needs_redraw = True
            self.write_state()
        if self.update_performance_async(now):
            self.needs_redraw = True
            self.write_state()
        if self.update_full_recovery(now):
            self.write_state()
        if self.update_music(now):
            self.write_state()
        if self.update_music_lyrics(now):
            self.write_state()
        if self.update_music_artwork():
            self.write_state()
        if self.update_wifi_toggle(now):
            self.write_state()
        if self.wifi_toggle_notice_until and now >= self.wifi_toggle_notice_until:
            self.wifi_toggle_notice_until = 0.0
            self.wifi_toggle_error = ""
            self.needs_redraw = True
        if self.video_call_active:
            if now - self.last_overlay_redraw >= self.render_interval:
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.music_active:
            if (
                self.music_transition_active
                or self.music_center_transition_active
                or self.music_volume_expansion(now) > 0.0
                or self.music_player.status == "playing"
            ) and now - self.last_overlay_redraw >= self.render_interval:
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.workshop_active:
            if now >= self.workshop_status_next_read_at:
                if self.refresh_workshop_status(now, force=True):
                    self.needs_redraw = True
            if self.workshop_action_future is not None and self.workshop_action_future.done():
                self.needs_redraw = True
            if self.workshop_notice_until and now >= self.workshop_notice_until:
                self.workshop_notice = ""
                self.workshop_notice_until = 0.0
                self.needs_redraw = True
            if (
                self.workshop_transition_active
                and now - self.last_overlay_redraw >= self.render_interval
            ):
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.performance_active:
            if (
                self.performance_transition_active
                and now - self.last_overlay_redraw >= self.render_interval
            ):
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
        if self.pomodoro_active:
            self.update_long_press(now)
            display_second = max(
                0,
                math.ceil(self.pomodoro.remaining()),
            )
            if (
                self.pomodoro_transition_active
                and now - self.last_overlay_redraw >= self.render_interval
            ):
                self.needs_redraw = True
            elif (
                self.pomodoro_duration_adjust_active
                and now - self.pomodoro_duration_adjust_started_at
                < self.pomodoro_duration_adjust_seconds
                and now - self.last_overlay_redraw >= self.render_interval
            ):
                self.needs_redraw = True
            elif display_second != self.pomodoro_last_display_second:
                self.pomodoro_last_display_second = display_second
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
            self.refresh_always_on_top()
            return
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
            self.menu_active
            and self.pointer_down
            and self.app_pin_candidate_app_id is not None
            and self.update_application_pin_hold(now)
        ):
            self.write_state()
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
            or self.activity_indicator_mode() is not None
            or (self.camera_view_active and not self.gallery_active)
            or self.gallery_page_transition_active
            or now < self.camera_capture_flash_until
            or now < self.camera_capture_notice_until
            or now < self.camera_capture_error_until
            or now < self.gallery_notice_until
            or self.speech_bubble_opacity(now) > 0.0
            or (
                self.music_lyrics_overlay_active(now)
                and self.music_player.status == "playing"
            )
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
        if self.video_call_frame_future is not None:
            self.video_call_frame_future.cancel()
        if self.video_call_status_future is not None:
            self.video_call_status_future.cancel()
        if self.performance_future is not None:
            self.performance_future.cancel()
        if self.music_scan_future is not None:
            self.music_scan_future.cancel()
        if self.music_artwork_future is not None:
            self.music_artwork_future.cancel()
        if self.music_lyrics_fetch_future is not None:
            self.music_lyrics_fetch_future.cancel()
        self.music_player.shutdown()
        self.camera_executor.shutdown(wait=False, cancel_futures=True)
        self.video_call_executor.shutdown(wait=False, cancel_futures=True)
        self.performance_executor.shutdown(wait=False, cancel_futures=True)
        self.music_executor.shutdown(wait=False, cancel_futures=True)
        self.lyrics_executor.shutdown(wait=False, cancel_futures=True)
        self.status_executor.shutdown(wait=False, cancel_futures=True)
        self.control_executor.shutdown(wait=False, cancel_futures=True)
        self.balance_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.pygame.display.quit()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


def run_self_test() -> dict:
    """Validate modular imports and static assets without opening the display."""
    config = load_config()
    missing_assets = [
        str(app_path(value))
        for value in config["expressions"].values()
        if not app_path(value).is_file()
    ]
    if missing_assets:
        raise FileNotFoundError(
            "missing expression assets: " + ", ".join(missing_assets[:3])
        )
    width, height = (int(value) for value in config.get("target_size", [800, 800]))
    if width <= 0 or height <= 0:
        raise ValueError("target_size must contain positive dimensions")
    status = SystemStatus()
    performance = PerformanceMonitor()
    music = MusicPlayer(Path("/tmp/riverbank-music-self-test"))
    pomodoro = PomodoroTimer(
        Path(os.environ.get("TMPDIR", "/tmp"))
        / f"riverbank-pomodoro-self-test-{os.getpid()}.json"
    )
    return {
        "ok": True,
        "renderer": "modular-v1",
        "target_size": [width, height],
        "expression_count": len(config["expressions"]),
        "animation_module": Animation.__module__,
        "status_module": SystemStatus.__module__,
        "performance_module": performance.__class__.__module__,
        "music_module": music.__class__.__module__,
        "pomodoro_module": PomodoroTimer.__module__,
        "pomodoro_defaults": {
            "focus_seconds": pomodoro.focus_seconds,
            "short_break_seconds": pomodoro.short_break_seconds,
            "long_break_seconds": pomodoro.long_break_seconds,
        },
        "version": status.read_app_version(),
    }


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        print(json.dumps(run_self_test(), ensure_ascii=False, separators=(",", ":")))
        return 0
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
                        display.set_radial_menu_context(
                            str(request.get("level", "main")),
                            bool(request.get("pin_mode", False)),
                            animate=False,
                        )
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
                        display.reset_application_pin_gesture()
                        preview_pin_progress = max(
                            0.0,
                            min(float(request.get("pin_drag_progress", 0.0)), 1.0),
                        )
                        if (
                            display.menu_level == "applications"
                            and display.menu_selected is not None
                            and preview_pin_progress > 0.0
                        ):
                            preview_item = display.radial_menu[display.menu_selected]
                            preview_action = (
                                preview_item.get("action")
                                if isinstance(preview_item.get("action"), dict)
                                else {}
                            )
                            if str(preview_action.get("type", "")) == "launch_app":
                                display.app_pin_candidate_app_id = str(
                                    preview_action.get("app_id", "")
                                ).strip() or None
                                display.app_pin_drag_progress = preview_pin_progress
                                display.app_pin_drag_ready = preview_pin_progress >= 0.985
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
                    elif request.get("command") == "performance_view":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.prepare_voice_application_switch("performance")
                            display.open_performance()
                            section = str(request.get("section", "")).strip()
                            if section == "system":
                                display.performance_section = "system"
                                display.performance_health_scroll_offset = (
                                    display.clamp_performance_health_scroll(
                                        float(request.get("scroll_offset", 0.0) or 0.0)
                                    )
                                )
                                display.performance_health_scroll_start_offset = (
                                    display.performance_health_scroll_offset
                                )
                                display.performance_transition_target = (
                                    display.render_performance_surface()
                                )
                        else:
                            display.close_performance(
                                animated=bool(request.get("animated", True))
                            )
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "workshop_view":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.prepare_voice_application_switch("workshop")
                            display.open_workshop()
                        else:
                            display.close_workshop(
                                animated=bool(request.get("animated", True))
                            )
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "workshop_app_view":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.prepare_voice_application_switch("workshop")
                            display.open_workshop()
                            display.workshop_section = "runtime"
                            display.refresh_workshop_status(time.monotonic(), force=True)
                        elif display.workshop_active:
                            display.workshop_section = "installed"
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "music_view":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.prepare_voice_application_switch("music")
                            display.open_music()
                        else:
                            display.close_music(
                                animated=bool(request.get("animated", True))
                            )
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "music_control":
                        action = str(request.get("action", "")).strip().lower()
                        action_map = {
                            "play": "toggle",
                            "pause": "toggle",
                            "toggle": "toggle",
                            "previous": "previous",
                            "next": "next",
                            "refresh": "refresh",
                            "mode": "mode",
                            "lyrics": "lyrics",
                            "center": "center",
                        }
                        if action == "set_mode":
                            if not display.set_music_playback_mode(
                                str(request.get("mode", ""))
                            ):
                                raise ValueError(
                                    "unsupported music playback mode: "
                                    f"{request.get('mode')}"
                                )
                        elif action == "set_lyrics":
                            display.set_music_lyrics_enabled(
                                bool(request.get("enabled", True))
                            )
                        elif action not in action_map:
                            raise ValueError(f"unsupported music action: {action}")
                        elif action == "play" and not display.music_player.tracks:
                            display.music_autoplay_pending = True
                            display.request_music_scan()
                        elif action == "play" and display.music_player.status == "playing":
                            pass
                        elif action == "pause" and display.music_player.status != "playing":
                            display.music_autoplay_pending = False
                            pass
                        else:
                            display.handle_music_target(action_map[action])
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "music_volume_preview":
                        preview_active = bool(request.get("active", True))
                        preview_now = time.monotonic()
                        display.music_volume_dragging = False
                        display.music_volume_interaction_started_at = 0.0
                        display.music_volume_expand_from = (
                            1.0 if preview_active else 0.0
                        )
                        display.music_volume_last_interaction_at = (
                            preview_now if preview_active else 0.0
                        )
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "pomodoro_completion_preview":
                        preview_active = bool(request.get("active", True))
                        preview_now = time.monotonic()
                        if preview_active:
                            completed_phase = str(
                                request.get("phase", "focus")
                            ).strip()
                            if completed_phase not in {
                                "focus",
                                "short_break",
                                "long_break",
                            }:
                                raise ValueError(
                                    "unsupported completion phase: "
                                    f"{completed_phase}"
                                )
                            display.start_pomodoro_completion_alert(
                                completed_phase,
                                preview_now,
                            )
                            if str(request.get("seek", "start")) == "peak":
                                peak_now = time.monotonic()
                                display.pomodoro_completion_alert_started_at = (
                                    peak_now
                                    - display.pomodoro_completion_alert_rise_seconds
                                    - 0.01
                                )
                                display.update_pomodoro_completion_alert(peak_now)
                        else:
                            display.pomodoro_completion_alert_active = False
                            display.pomodoro_completion_alert_started_at = 0.0
                            display.pomodoro_completion_alert_phase = None
                            display.pomodoro_completion_alert_pulse = 0
                            display.pomodoro_completion_alert_intensity = 0.0
                            display.pomodoro_completion_alert_lit = False
                            display.pomodoro_completion_alert_on_surface = None
                            display.pomodoro_completion_alert_off_surface = None
                            display.note_screensaver_activity(preview_now)
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "pomodoro_view":
                        preview_active = bool(request.get("active", True))
                        page = str(request.get("page", "timer")).strip().lower()
                        statistics_page = max(
                            0,
                            min(int(request.get("statistics_page", 0)), 1),
                        )
                        if page not in {"timer", "statistics"}:
                            raise ValueError(
                                f"unsupported pomodoro page: {page}"
                            )
                        if preview_active:
                            display.prepare_voice_application_switch("pomodoro")
                            if not display.pomodoro_active:
                                display.open_pomodoro()
                            if bool(request.get("animated", True)):
                                if (
                                    page == "statistics"
                                    and not display.pomodoro_statistics_active
                                ):
                                    display.open_pomodoro_statistics()
                                elif (
                                    page == "timer"
                                    and display.pomodoro_statistics_active
                                ):
                                    display.close_pomodoro_statistics()
                            else:
                                display.pomodoro_active = True
                                display.pomodoro_statistics_active = (
                                    page == "statistics"
                                )
                                display.pomodoro_statistics_page = (
                                    statistics_page
                                    if page == "statistics"
                                    else 0
                                )
                                display.pomodoro_transition_active = False
                                display.pomodoro_transition_source = None
                                display.pomodoro_transition_target = None
                                display.pomodoro_transition_exits_page = False
                        else:
                            display.close_pomodoro(
                                animated=bool(request.get("animated", True))
                            )
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "pomodoro_action":
                        action = str(request.get("action", "toggle"))
                        requested_seconds = max(
                            60,
                            min(int(request.get("seconds", 25 * 60)), 180 * 60),
                        )
                        handlers = {
                            "toggle": lambda: display.pomodoro.toggle(),
                            "start": lambda: display.pomodoro.start(),
                            "pause": lambda: display.pomodoro.pause(),
                            "reset": lambda: display.pomodoro.reset_phase(),
                            "reset_cycle": lambda: display.pomodoro.reset_cycle(),
                            "skip": lambda: display.pomodoro.skip(),
                            "start_custom": lambda: display.pomodoro.start_focus_duration(
                                requested_seconds
                            ),
                            "set_custom": lambda: display.pomodoro.set_focus_duration(
                                requested_seconds
                            ),
                        }
                        handler = handlers.get(action)
                        if handler is None:
                            raise ValueError(
                                f"unsupported pomodoro action: {action}"
                            )
                        handler()
                        if action in {"skip", "reset_cycle"}:
                            display.pomodoro_phase_changed_at = time.monotonic()
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "pomodoro_duration_preview":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.close_radial_menu()
                            if not display.pomodoro_active:
                                display.open_pomodoro()
                            requested_minutes = max(
                                display.pomodoro_duration_min_minutes,
                                min(
                                    int(request.get("minutes", 25)),
                                    display.pomodoro_duration_max_minutes,
                                ),
                            )
                            step = display.pomodoro_duration_step_minutes
                            display.pomodoro_duration_selected_minutes = max(
                                display.pomodoro_duration_min_minutes,
                                min(
                                    round(requested_minutes / step) * step,
                                    display.pomodoro_duration_max_minutes,
                                ),
                            )
                            selected_index = (
                                display.pomodoro_duration_selected_minutes
                                - display.pomodoro_duration_min_minutes
                            ) // step
                            selectable_count = (
                                (
                                    display.pomodoro_duration_max_minutes
                                    - display.pomodoro_duration_min_minutes
                                )
                                // step
                                + 1
                            )
                            display.pomodoro_duration_pointer_clockwise = (
                                selected_index * math.tau / selectable_count
                            )
                            display.pomodoro_active = True
                            display.pomodoro_statistics_active = False
                            display.pomodoro_statistics_page = 0
                            display.pomodoro_transition_active = False
                            display.pomodoro_duration_adjust_active = True
                            display.pomodoro_duration_dial_moved = False
                            display.pomodoro_duration_adjust_started_at = (
                                time.monotonic()
                                - display.pomodoro_duration_adjust_seconds
                            )
                            display.pomodoro_pointer_target = None
                        else:
                            display.pomodoro_duration_adjust_active = False
                            display.pomodoro_duration_dial_moved = False
                            display.pomodoro_duration_adjust_started_at = 0.0
                            display.pomodoro_pointer_target = None
                        display.needs_redraw = True
                        display.write_state()
                    elif request.get("command") == "video_call_view":
                        preview_active = bool(request.get("active", True))
                        if preview_active:
                            display.set_video_call_view(
                                True,
                                request_service=bool(
                                    request.get("request_service", False)
                                ),
                            )
                        elif display.video_call_active:
                            display.close_video_call_to_application_menu(
                                hangup=bool(request.get("hangup", False))
                            )
                        else:
                            display.complete_application_menu_return(
                                time.monotonic()
                            )
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
                    elif request.get("command") == "audio_activity":
                        display.set_audio_activity(
                            str(request.get("source", "")),
                            bool(request.get("active", True)),
                            float(request.get("ttl_seconds", 2.0)),
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
