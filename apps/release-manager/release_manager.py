#!/usr/bin/env python3
"""Seal and verify RiverBank Assistant system releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path


SCHEMA = "riverbank.release/v1"
REPOSITORY_PATH = Path(
    os.environ.get(
        "RIVERBANK_REPO",
        Path(__file__).resolve().parents[2],
    )
).resolve()
MANIFEST_PATH = Path(
    os.environ.get("RIVERBANK_RELEASE_MANIFEST", "/etc/riverbank/release-manifest.json")
)
VERSION_PATH = Path(
    os.environ.get("RIVERBANK_VERSION_FILE", "/etc/riverbank/VERSION")
)
UI_VERSION_PATH = Path(
    os.environ.get(
        "RIVERBANK_UI_VERSION_FILE",
        REPOSITORY_PATH / "apps/expression-ui/VERSION",
    )
)
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
CHANNELS = ("beta", "stable")


def repository_file(relative: str) -> str:
    return str(REPOSITORY_PATH / relative)


COMPONENTS = (
    {
        "id": "assistant-core",
        "name": "Daily voice assistant and Hermes integration",
        "services": ("hermes-gateway.service", "hermes-voice.service"),
        "files": (
            repository_file("apps/expression-ui/daily_voice_assistant.py"),
            repository_file("apps/expression-ui/flight_search.py"),
            repository_file("apps/expression-ui/voice_latency.py"),
            repository_file("apps/expression-ui/pomodoro_voice.py"),
            repository_file("apps/expression-ui/hermes-voicectl.py"),
            "/home/geo/.hermes/profiles/daily/SOUL.md",
            "/home/geo/.hermes/profiles/daily/profile.yaml",
            "/etc/systemd/system/hermes-voice.service",
            "/etc/systemd/system/hermes-voice.service.d/daily-profile.conf",
            "/etc/systemd/system/hermes-voice.service.d/performance.conf",
        ),
    },
    {
        "id": "expression-ui",
        "name": "Round display, expressions and interaction bridge",
        "services": (
            "expression-display.service",
            "hermes-expression-bridge.service",
            "riverbank-boot-handoff.service",
            "riverbank-reboot-request.path",
        ),
        "files": (
            repository_file("apps/expression-ui/expression_display_persistent.py"),
            repository_file("apps/expression-ui/app_menu.py"),
            repository_file("apps/expression-ui/animation_assets.py"),
            repository_file("apps/expression-ui/system_status.py"),
            repository_file("apps/expression-ui/performance_monitor.py"),
            repository_file("apps/expression-ui/music_player.py"),
            repository_file("apps/expression-ui/expression_events.py"),
            repository_file("apps/expression-ui/response_emotions.py"),
            repository_file("apps/expression-ui/pomodoro.py"),
            repository_file("apps/expression-ui/video_call_client.py"),
            repository_file("apps/expression-ui/assets/fonts/MaShanZheng-Regular.ttf"),
            repository_file("apps/expression-ui/assets/fonts/OFL-MaShanZheng.txt"),
            repository_file("apps/expression-ui/hermes_expression_bridge.py"),
            repository_file("apps/expression-ui/expressionctl.py"),
            repository_file("apps/expression-ui/expressions.json"),
            repository_file("apps/expression-ui/boot_handoff.py"),
            repository_file("tests/test_animation_assets.py"),
            repository_file("tests/test_app_menu.py"),
            repository_file("tests/test_expression_events.py"),
            repository_file("tests/test_response_emotions.py"),
            repository_file("tests/test_pomodoro.py"),
            repository_file("tests/test_pomodoro_voice.py"),
            repository_file("tests/test_performance_monitor.py"),
            repository_file("tests/test_music_player.py"),
            repository_file("tests/test_video_call_client.py"),
            repository_file("tests/test_system_status.py"),
            "/etc/systemd/system/expression-display.service",
            "/etc/systemd/system/expression-display.service.d/boot.conf",
            "/etc/systemd/system/expression-display.service.d/handoff.conf",
            "/etc/systemd/system/expression-display.service.d/performance.conf",
            "/etc/systemd/system/hermes-expression-bridge.service",
            "/etc/systemd/system/riverbank-boot-handoff.service",
            "/etc/systemd/system/riverbank-reboot-request.path",
            "/etc/systemd/system/riverbank-reboot-request.service",
        ),
    },
    {
        "id": "workshop-platform",
        "name": "Workshop contracts, package validation and capability policy",
        "services": ("riverbank-workshop.service",),
        "files": (
            repository_file("apps/workshop/workshop_contract.py"),
            repository_file("apps/workshop/workshop_declarative.py"),
            repository_file("apps/workshop/workshop_generator.py"),
            repository_file("apps/workshop/workshop_host.py"),
            repository_file("apps/workshop/workshop_manager.py"),
            repository_file("apps/workshop/workshop_package.py"),
            repository_file("apps/workshop/workshop_runtime.py"),
            repository_file("apps/workshop/workshop_service.py"),
            repository_file("apps/workshop/workshop_store.py"),
            repository_file("apps/workshop/workshopctl.py"),
            repository_file("apps/workshop/host-api-methods.json"),
            repository_file("apps/workshop/schemas/riverbank-app-manifest-v1.schema.json"),
            repository_file("apps/workshop/schemas/riverbank-app-host-v1.schema.json"),
            repository_file("apps/workshop/examples/minimal-app/manifest.json"),
            repository_file("apps/workshop/examples/minimal-app/app/main.json"),
            repository_file("apps/workshop/examples/cat-watcher/manifest.json"),
            repository_file("apps/workshop/examples/cat-watcher/app/main.json"),
            repository_file("tests/test_workshop_contract.py"),
            repository_file("tests/test_workshop_pipeline.py"),
            repository_file("apps/workshop/README.md"),
            repository_file("docs/WORKSHOP_PROTOCOL.zh-CN.md"),
            repository_file("config/systemd/riverbank-workshop.service"),
            repository_file("apps/expression-ui/workshop_client.py"),
        ),
    },
    {
        "id": "video-call",
        "name": "WebRTC, report library and persistent background task platform",
        "services": (
            "riverbank-video-call.service",
            "riverbank-task-worker.service",
            "riverbank-chat-worker.service",
        ),
        "files": (
            repository_file("apps/video-call/video_call_server.py"),
            repository_file("apps/video-call/chat_store.py"),
            repository_file("apps/video-call/chat_worker.py"),
            repository_file("apps/video-call/report_library.py"),
            repository_file("apps/video-call/task_store.py"),
            repository_file("apps/video-call/task_worker.py"),
            repository_file("apps/video-call/riverbank_task.py"),
            repository_file("apps/video-call/video_callctl.py"),
            repository_file("apps/video-call/video_call_smoke.py"),
            repository_file("apps/video-call/requirements.txt"),
            repository_file("apps/video-call/windows-client/package.json"),
            repository_file("apps/video-call/windows-client/package-lock.json"),
            repository_file("apps/video-call/windows-client/main.js"),
            repository_file("apps/video-call/windows-client/preload.js"),
            repository_file("apps/video-call/windows-client/renderer.js"),
            repository_file("apps/video-call/windows-client/index.html"),
            repository_file("apps/video-call/windows-client/styles.css"),
            repository_file("tests/test_report_library.py"),
            repository_file("tests/test_chat_store.py"),
            repository_file("apps/video-call/windows-client/build.ps1"),
            repository_file("apps/video-call/windows-client/build-mac.sh"),
            repository_file("apps/video-call/windows-client/build-mac-icon.mjs"),
            repository_file("apps/video-call/windows-client/assets/riverbank-mark.svg"),
            repository_file("apps/video-call/windows-client/assets/riverbank-mark-1024.png"),
            repository_file("apps/video-call/windows-client/assets/riverbank-call.ico"),
            repository_file("apps/video-call/windows-client/assets/riverbank-call.icns"),
            "/etc/systemd/system/riverbank-video-call.service",
            "/etc/systemd/system/riverbank-task-worker.service",
            "/etc/systemd/system/riverbank-chat-worker.service",
        ),
    },
    {
        "id": "vision-runtime",
        "name": "Shared camera and lease-controlled Hailo inference",
        "services": ("camera-hub.service", "riverbank-face-tracker.service"),
        "files": (
            repository_file("apps/face-tracker/face_tracker.py"),
            repository_file("apps/face-tracker/face_tracker_supervisor.py"),
            repository_file("apps/face-tracker/vision_leases.py"),
            repository_file("apps/face-tracker/face_trackerctl.py"),
            repository_file("tests/test_vision_leases.py"),
            repository_file("apps/health-monitor/face_tracker_health.py"),
            "/etc/systemd/system/camera-hub.service",
            "/etc/systemd/system/riverbank-face-tracker.service",
            "/etc/systemd/system/riverbank-face-tracker.service.d/performance.conf",
        ),
    },
    {
        "id": "audio-runtime",
        "name": "Six-microphone array and speaker integration",
        "services": ("listengo-mic.service",),
        "files": (
            repository_file("apps/listengo-mic/listengo_daemon.py"),
            repository_file("apps/listengo-mic/listengo-micctl"),
            "/etc/systemd/system/listengo-mic.service",
        ),
    },
    {
        "id": "paper-radar",
        "name": "Daily research intelligence collector and web archive",
        "services": ("paper-radar-web.service",),
        "files": (
            repository_file("apps/paper-radar/config/topics.json"),
            repository_file("apps/paper-radar/scripts/collect.py"),
            repository_file("apps/paper-radar/scripts/finalize_run.py"),
            repository_file("apps/paper-radar/scripts/render.py"),
            repository_file("apps/paper-radar/scripts/special_focus.py"),
            repository_file("apps/paper-radar/scripts/web_server.py"),
            repository_file("apps/paper-radar/scripts/health_check.py"),
            repository_file("apps/paper-radar/static/site.js"),
            "/etc/systemd/system/paper-radar-web.service",
        ),
    },
    {
        "id": "health-platform",
        "name": "Boot handoff and extensible service health monitoring",
        "services": ("riverbank-health-monitor.service",),
        "files": (
            repository_file("apps/health-monitor/health_monitor.py"),
            repository_file("apps/health-monitor/expression_event_health.py"),
            repository_file("apps/health-monitor/proxy_egress_check.py"),
            repository_file("apps/health-monitor/voice_caption_health.py"),
            repository_file("apps/release-manager/release_manager.py"),
            "/etc/riverbank/health-monitor.json",
            "/etc/systemd/system/riverbank-health-monitor.service",
        ),
    },
    {
        "id": "reliability-provisioning",
        "name": "Resource budgets, one-touch recovery and offline phone provisioning",
        "services": (
            "riverbank-recovery.service",
            "riverbank-provisioning.service",
        ),
        "files": (
            repository_file("apps/recovery/recovery_manager.py"),
            repository_file("apps/recovery/config.example.json"),
            repository_file("apps/provisioning/provisioning_service.py"),
            repository_file("apps/provisioning/config.example.json"),
            repository_file("apps/expression-ui/recovery_client.py"),
            repository_file("tests/test_recovery_manager.py"),
            repository_file("tests/test_provisioning.py"),
            repository_file("tests/test_paper_radar_network.py"),
            repository_file("config/systemd/riverbank-recovery.service"),
            repository_file("config/systemd/riverbank-provisioning.service"),
            repository_file("config/systemd/riverbank-apps.slice"),
            "/etc/riverbank/recovery.json",
            "/etc/riverbank/provisioning.json",
        ),
    },
)


def normalize_version(value: str) -> str:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("version must use semantic form vMAJOR.MINOR.PATCH")
    return ".".join(match.groups())


def display_version(version: str, channel: str) -> str:
    return f"v{version} {channel}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(raw_path: str) -> dict:
    path = Path(raw_path)
    if not path.is_file():
        raise FileNotFoundError(f"release input is missing: {path}")
    stat_result = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat_result.st_size,
        "sha256": sha256(path),
    }


def read_text(path: Path, default: str = "unknown") -> str:
    try:
        return path.read_text(encoding="utf-8").replace("\x00", "").strip()
    except OSError:
        return default


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def build_manifest(version: str, channel: str, notes: str) -> dict:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    components = []
    seen: set[str] = set()
    for component in COMPONENTS:
        records = []
        for raw_path in component["files"]:
            if raw_path in seen:
                raise ValueError(f"duplicate release input: {raw_path}")
            seen.add(raw_path)
            records.append(file_record(raw_path))
        components.append(
            {
                "id": component["id"],
                "name": component["name"],
                "version": version,
                "services": list(component["services"]),
                "files": records,
            }
        )
    return {
        "schema": SCHEMA,
        "product": "RiverBank Assistant",
        "version": version,
        "display_version": display_version(version, channel),
        "channel": channel,
        "release_notes": notes.strip(),
        "sealed_at": generated_at,
        "sealed_epoch": int(time.time()),
        "host": {
            "hostname_at_seal": socket.gethostname(),
            "hardware_model": read_text(Path("/proc/device-tree/model")),
            "architecture": platform.machine(),
            "kernel": platform.release(),
        },
        "compatibility": {
            "release_manifest": 1,
            "expression_events": 1,
            "vision_leases": 1,
            "paper_special_focus": 1,
            "pomodoro_state": 1,
            "background_tasks": 1,
            "recovery_control": 1,
            "offline_provisioning": 1,
            "workshop_declarative": 1,
            "workshop_host": 1,
        },
        "runtime_contracts": {
            "paper_web_port": 19732,
            "camera_hub_port": 19733,
            "camera_hub_binding": "127.0.0.1",
            "video_call_port": 19734,
            "video_call_scope": "LAN-or-tailnet",
            "task_database": "/var/lib/riverbank-tasks/tasks.db",
            "task_https_path": "/assistant",
            "face_tracker_control": "/run/riverbank-face-tracker/control.sock",
            "expression_control": "/run/riverbank-expression/control.sock",
            "health_status": "/var/lib/riverbank-health-monitor/status.json",
            "recovery_control": "/run/riverbank-recovery/control.sock",
            "provisioning_port": 19735,
            "provisioning_status": "/run/riverbank-provisioning/status.json",
            "workload_slice": "riverbank-apps.slice",
            "workshop_control": "/run/riverbank-workshop/control.sock",
            "workshop_runtime": "/run/riverbank-workshop/runtime.json",
            "workshop_execution_policy": "validated-declarative-only",
        },
        "components": components,
    }


def seal(version_value: str, channel: str, notes: str) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("sealing a release requires root privileges")
    version = normalize_version(version_value)
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of: {', '.join(CHANNELS)}")
    displayed = display_version(version, channel)
    # Resolve and hash every required input before changing either visible
    # version file. A missing release input therefore leaves the installed
    # release untouched instead of producing a half-sealed version.
    manifest = build_manifest(version, channel, notes)
    atomic_write(VERSION_PATH, displayed + "\n")
    atomic_write(UI_VERSION_PATH, displayed + "\n")
    atomic_write(
        MANIFEST_PATH,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest


def load_manifest() -> dict:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be a JSON object")
    return payload


def verify() -> dict:
    errors: list[dict] = []
    try:
        manifest = load_manifest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "drift": [], "checked_files": 0}
    if manifest.get("schema") != SCHEMA:
        errors.append(
            {
                "path": str(MANIFEST_PATH),
                "reason": "schema_mismatch",
                "expected": SCHEMA,
                "actual": manifest.get("schema"),
            }
        )
    expected_display = manifest.get("display_version")
    for version_path in (VERSION_PATH, UI_VERSION_PATH):
        actual = read_text(version_path, default="")
        if actual != expected_display:
            errors.append(
                {
                    "path": str(version_path),
                    "reason": "version_mismatch",
                    "expected": expected_display,
                    "actual": actual,
                }
            )
    checked = 0
    for component in manifest.get("components", []):
        for record in component.get("files", []):
            checked += 1
            path = Path(record.get("path", ""))
            if not path.is_file():
                errors.append({"path": str(path), "reason": "missing"})
                continue
            actual_size = path.stat().st_size
            if actual_size != record.get("size_bytes"):
                errors.append(
                    {
                        "path": str(path),
                        "reason": "size_mismatch",
                        "expected": record.get("size_bytes"),
                        "actual": actual_size,
                    }
                )
                continue
            actual_hash = sha256(path)
            if actual_hash != record.get("sha256"):
                errors.append(
                    {
                        "path": str(path),
                        "reason": "sha256_mismatch",
                        "expected": record.get("sha256"),
                        "actual": actual_hash,
                    }
                )
    return {
        "ok": not errors,
        "product": manifest.get("product"),
        "version": manifest.get("display_version"),
        "channel": manifest.get("channel"),
        "sealed_at": manifest.get("sealed_at"),
        "components": len(manifest.get("components", [])),
        "checked_files": checked,
        "drift_count": len(errors),
        "drift": errors,
    }


def print_result(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if "version" in payload:
        print(payload["version"])
    print(
        f"integrity={'verified' if payload.get('ok') else 'drifted'} "
        f"components={payload.get('components', 0)} "
        f"files={payload.get('checked_files', 0)}"
    )
    for item in payload.get("drift", []):
        print(f"- {item.get('path')}: {item.get('reason')}")


def parser_for(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=program)
    subparsers = parser.add_subparsers(dest="command")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--json", action="store_true")
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--version", required=True)
    seal_parser.add_argument("--channel", choices=CHANNELS, required=True)
    seal_parser.add_argument("--notes", default="")
    seal_parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    program = Path(sys.argv[0]).name
    args = parser_for(program).parse_args()
    command = args.command or "status"
    try:
        if command == "seal":
            manifest = seal(args.version, args.channel, args.notes)
            result = verify()
            result["release_notes"] = manifest.get("release_notes")
            print_result(result, args.json)
            return 0 if result["ok"] else 1
        result = verify()
        print_result(result, getattr(args, "json", False))
        return 0 if result["ok"] else 1
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
