#!/usr/bin/env python3
"""Cover the DSI desktop gap until the persistent expression window is ready."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import time


OUTPUT = os.environ.get("RIVERBANK_OUTPUT", "DSI-2")
DISPLAY_SIZE = tuple(
    int(value)
    for value in os.environ.get("RIVERBANK_DISPLAY_SIZE", "800x800").split("x", 1)
)
STATE_PATH = Path("/run/riverbank-expression/state.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
LOGO_PATH = Path(
    os.environ.get(
        "RIVERBANK_HANDOFF_LOGO",
        "/usr/share/plymouth/themes/riverbank/RiverBank-compact.png",
    )
)
DEFAULT_RUNTIME_DIR = Path(f"/run/user/{os.getuid()}")
HANDOFF_STATE_PATH = Path(
    os.environ.get(
        "RIVERBANK_HANDOFF_STATE",
        DEFAULT_RUNTIME_DIR / "riverbank-boot-handoff.json",
    )
)
STOP_REQUESTED = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(address)
        client.sendall(message.encode("utf-8"))


def write_handoff_state(payload: dict[str, object]) -> None:
    temporary = HANDOFF_STATE_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, HANDOFF_STATE_PATH)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def renderer_ready(boot_id: str, started_at: float) -> bool:
    try:
        if STATE_PATH.stat().st_mtime < started_at:
            return False
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        boot = payload.get("boot_animation") or {}
        return bool(
            payload.get("ok")
            and payload.get("renderer") == "pygame-persistent"
            and payload.get("output") == OUTPUT
            and boot.get("boot_id") == boot_id
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def wayland_socket_path() -> Path:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", DEFAULT_RUNTIME_DIR))
    wayland_name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    return runtime_dir / wayland_name


def create_swaybg_surface():
    """Map the logo with a tiny layer-shell client as soon as Wayland exists."""
    wayland_socket = wayland_socket_path()
    if not wayland_socket.exists():
        raise RuntimeError(f"Wayland socket is not ready: {wayland_socket}")

    swaybg = shutil.which("swaybg")
    if swaybg is None:
        raise RuntimeError("swaybg is not installed")

    process = subprocess.Popen(
        [
            swaybg,
            "-o",
            OUTPUT,
            "-i",
            str(LOGO_PATH),
            "-m",
            "center",
            "-c",
            "#000000",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    # Two or more DSI refresh intervals let layer-shell commit the first frame.
    for _ in range(6):
        time.sleep(1.0 / 60.0)
        if process.poll() is not None:
            error = process.stderr.read().strip() if process.stderr else ""
            raise RuntimeError(f"swaybg exited early: {error or process.returncode}")

    def update() -> None:
        if process.poll() is not None:
            error = process.stderr.read().strip() if process.stderr else ""
            raise RuntimeError(f"swaybg stopped: {error or process.returncode}")

    def destroy() -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stderr:
            process.stderr.close()

    return update, destroy


def create_gtk_surface():
    """Fallback surface for systems where swaybg is unavailable."""
    wayland_socket = wayland_socket_path()
    if not wayland_socket.exists():
        raise RuntimeError(f"Wayland socket is not ready: {wayland_socket}")

    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    Gdk.set_program_class("RiverBankBootHandoff")
    initialized, _arguments = Gtk.init_check([])
    if not initialized:
        raise RuntimeError("GTK could not connect to the Wayland display")

    display = Gdk.Display.get_default()
    screen = Gdk.Screen.get_default()
    if display is None or screen is None:
        raise RuntimeError("GTK Wayland display is not ready")

    monitor_index = None
    monitor_sizes: list[tuple[int, int]] = []
    for index in range(display.get_n_monitors()):
        geometry = display.get_monitor(index).get_geometry()
        size = (geometry.width, geometry.height)
        monitor_sizes.append(size)
        if size == DISPLAY_SIZE and monitor_index is None:
            monitor_index = index
    if monitor_index is None:
        raise RuntimeError(
            f"DSI display {DISPLAY_SIZE} is not ready: {monitor_sizes}"
        )

    css = Gtk.CssProvider()
    css.load_from_data(b"window, box { background-color: #000000; }")
    Gtk.StyleContext.add_provider_for_screen(
        screen,
        css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_title("RiverBank Boot Handoff")
    window.set_name("riverbank-boot-handoff")
    window.set_decorated(False)
    window.set_keep_above(True)
    window.set_skip_pager_hint(True)
    window.set_skip_taskbar_hint(True)
    window.set_default_size(*DISPLAY_SIZE)

    container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    container.set_size_request(*DISPLAY_SIZE)
    image = Gtk.Image.new_from_file(str(LOGO_PATH))
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    image.set_hexpand(True)
    image.set_vexpand(True)
    container.pack_start(image, True, True, 0)
    window.add(container)
    window.fullscreen_on_monitor(screen, monitor_index)
    window.show_all()

    def update() -> None:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        display.flush()

    def destroy() -> None:
        window.destroy()
        update()

    # Submit enough event iterations for the first black/logo frame to be
    # committed before systemd starts the heavier expression renderer.
    for _ in range(3):
        update()
        time.sleep(1.0 / 120.0)
    return update, destroy


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    started_at = time.time()
    boot_id = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    handoff_state: dict[str, object] = {
        "boot_id": boot_id,
        "pid": os.getpid(),
        "started_at": started_at,
        "surface_ready_at": None,
        "finished_at": None,
        "renderer_ready": False,
        "mode": "swaybg-wayland",
    }

    # When started against an already healthy renderer, satisfy systemd without
    # mapping a window. This keeps service maintenance from flashing the logo.
    if renderer_ready(boot_id, started_at - 2.0):
        handoff_state["finished_at"] = time.time()
        handoff_state["renderer_ready"] = True
        handoff_state["mode"] = "already-ready"
        write_handoff_state(handoff_state)
        notify_systemd("READY=1\nSTATUS=Expression renderer already ready\n")
        return 0

    update_surface = None
    destroy_surface = None
    attempt = 0
    last_error = ""
    while update_surface is None or destroy_surface is None:
        attempt += 1
        try:
            try:
                update_surface, destroy_surface = create_swaybg_surface()
                handoff_state["mode"] = "swaybg-wayland"
            except RuntimeError as swaybg_error:
                # A missing/not-yet-ready Wayland socket must remain cheap to
                # retry. Once Wayland exists, retain GTK as a robust fallback.
                if not wayland_socket_path().exists():
                    raise swaybg_error
                update_surface, destroy_surface = create_gtk_surface()
                handoff_state["mode"] = "gtk-wayland-fallback"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != last_error or attempt % 50 == 0:
                print(
                    f"handoff waiting attempt={attempt} error={error}",
                    flush=True,
                )
                last_error = error
            notify_systemd(f"STATUS=Waiting for {OUTPUT} Wayland surface\n")
            time.sleep(0.1)
    notify_systemd(
        f"READY=1\nSTATUS=Covering {OUTPUT} until expression renderer is ready\n"
    )
    handoff_state["surface_ready_at"] = time.time()
    write_handoff_state(handoff_state)

    while not STOP_REQUESTED and not renderer_ready(boot_id, started_at):
        update_surface()
        time.sleep(1.0 / 60.0)

    # Give the compositor one frame to commit the already-rendered expression
    # surface before removing the handoff layer.
    update_surface()
    time.sleep(0.05)
    destroy_surface()
    handoff_state["finished_at"] = time.time()
    handoff_state["renderer_ready"] = not STOP_REQUESTED
    write_handoff_state(handoff_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
