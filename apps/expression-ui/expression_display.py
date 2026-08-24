#!/usr/bin/env python3
"""Fullscreen expression renderer for the RiverBank DSI display."""

from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from PIL import Image


RIVERBANK_HOME = Path(os.environ.get("RIVERBANK_HOME", Path.home()))


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RIVERBANK_EXPRESSION_CONFIG", APP_DIR / "expressions.json"))
RUNTIME_DIR = Path(os.environ.get("RIVERBANK_EXPRESSION_RUNTIME", "/run/riverbank-expression"))
SOCKET_PATH = RUNTIME_DIR / "control.sock"
STATE_PATH = RUNTIME_DIR / "state.json"
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def log(message: str) -> None:
    print(message, flush=True)


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("expressions"), dict) or not data["expressions"]:
        raise ValueError("expressions.json must contain a non-empty expressions map")
    return data


def display_geometry(output_name: str) -> tuple[int, int, int, int] | None:
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
        mode_match = re.search(r"(\d+)x(\d+)\s+px.*\(.*current.*\)", raw_line)
        if mode_match:
            size = (int(mode_match.group(1)), int(mode_match.group(2)))
        if position and size:
            return position[0], position[1], size[0], size[1]
    return None


def extract_background_color(path: Path) -> str:
    """Return the dominant opaque color around the first frame's outer edge."""
    try:
        with Image.open(path) as source:
            frame = source.convert("RGBA")
        width, height = frame.size
        band = max(4, min(width, height) // 40)
        boxes = (
            (0, 0, width, band),
            (0, height - band, width, height),
            (0, band, band, height - band),
            (width - band, band, width, height - band),
        )
        colors: Counter[tuple[int, int, int]] = Counter()
        for box in boxes:
            for red, green, blue, alpha in frame.crop(box).getdata():
                if alpha < 128:
                    continue
                colors[(red, green, blue)] += 1
        if not colors:
            return "0x000000"
        red, green, blue = colors.most_common(1)[0][0]
        return f"0x{red:02x}{green:02x}{blue:02x}"
    except Exception as exc:
        log(f"background extraction failed for {path}: {exc}")
        return "0x000000"


class ExpressionDisplay:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.expressions: dict[str, str] = config["expressions"]
        self.default_state = str(config.get("default_state", "idle"))
        self.output = str(config.get("output", "DSI-2"))
        self.state = ""
        self.deadline: float | None = None
        self.player: subprocess.Popen | None = None
        self.running = True
        self.background_colors: dict[str, str] = {}

    def stop_player(self) -> None:
        player = self.player
        self.player = None
        if not player or player.poll() is not None:
            return
        player.terminate()
        try:
            player.wait(timeout=2)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait(timeout=2)

    def start_player(self, state: str) -> bool:
        path = Path(self.expressions[state])
        if not path.is_file():
            log(f"expression asset missing: {path}")
            return False
        geometry = display_geometry(self.output)
        if geometry is None:
            log(f"display output not ready: {self.output}")
            return False
        left, top, width, height = geometry
        background = self.background_colors.setdefault(state, extract_background_color(path))
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background}"
        )
        env = os.environ.copy()
        env.update(
            {
                "DISPLAY": env.get("DISPLAY", ":0"),
                "XAUTHORITY": env.get(
                    "XAUTHORITY", str(RIVERBANK_HOME / ".Xauthority")
                ),
                "SDL_VIDEODRIVER": "x11",
                "SDL_VIDEO_WINDOW_POS": f"{left},{top}",
            }
        )
        command = [
            "/usr/bin/ffplay",
            "-loglevel",
            "error",
            "-loop",
            "0",
            "-an",
            "-noborder",
            "-alwaysontop",
            "-window_title",
            "RiverBank Expression",
            "-left",
            str(left),
            "-top",
            str(top),
            "-x",
            str(width),
            "-y",
            str(height),
            "-vf",
            video_filter,
            str(path),
        ]
        self.player = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(0.25)
        if self.player.poll() is not None:
            error = self.player.stderr.read().decode("utf-8", errors="replace").strip()
            log(f"ffplay failed for {state}: {error or 'unknown error'}")
            self.player = None
            return False
        log(
            f"expression={state} output={self.output} geometry={left},{top} "
            f"{width}x{height} background={background}"
        )
        return True

    def set_state(self, state: str, ttl: float | None = None, force: bool = False) -> dict:
        if state not in self.expressions:
            return {"ok": False, "error": f"unknown state: {state}", "states": sorted(self.expressions)}
        ttl_value = None
        if ttl is not None:
            try:
                ttl_value = max(0.0, min(float(ttl), 300.0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "ttl must be numeric"}
        same_state = state == self.state and self.player is not None and self.player.poll() is None
        self.deadline = time.monotonic() + ttl_value if ttl_value else None
        if force or not same_state:
            self.stop_player()
            self.state = state
            self.start_player(state)
        self.write_state()
        return {"ok": True, "state": self.state, "ttl": ttl_value}

    def write_state(self) -> None:
        payload = {
            "ok": True,
            "state": self.state,
            "output": self.output,
            "player_pid": self.player.pid if self.player and self.player.poll() is None else None,
            "deadline_monotonic": self.deadline,
            "updated_at": time.time(),
            "states": sorted(self.expressions),
        }
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STATE_PATH)

    def tick(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.set_state(self.default_state)
            return
        if self.player is None or self.player.poll() is not None:
            self.start_player(self.state or self.default_state)
            self.write_state()

    def shutdown(self) -> None:
        self.running = False
        self.stop_player()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    display = ExpressionDisplay(load_config())
    signal.signal(signal.SIGTERM, lambda *_: setattr(display, "running", False))
    signal.signal(signal.SIGINT, lambda *_: setattr(display, "running", False))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o660)
    display.set_state(display.default_state, force=True)
    try:
        while display.running:
            ready, _, _ = select.select([server], [], [], 1.0)
            if not ready:
                display.tick()
                continue
            raw = server.recv(65535)
            try:
                request = json.loads(raw.decode("utf-8"))
                if request.get("command") == "status":
                    display.write_state()
                    continue
                display.set_state(
                    str(request.get("state", "")),
                    ttl=request.get("ttl"),
                    force=bool(request.get("force", False)),
                )
            except Exception as exc:
                log(f"invalid expression command: {exc}")
    finally:
        server.close()
        display.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
