#!/usr/bin/env python3
"""Regression tests for the modular system status model."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import system_status as module
from system_status import SystemStatus, SystemStatusSnapshot


def sample_snapshot() -> SystemStatusSnapshot:
    return SystemStatusSnapshot(
        wifi_quality=73,
        wifi_enabled=True,
        tokens_today=1200,
        camera_active=True,
        camera_active_sources=("unit-test",),
        bluetooth_connected=True,
        volume_percent=42,
        wifi_ssid="RiverBank",
        wifi_ipv4="192.0.2.10",
        bluetooth_powered=True,
        bluetooth_devices=("Speaker",),
        hostname="riverbank-test",
        os_name="Test Linux",
        kernel_version="1.2.3",
        uptime_seconds=99,
        health_healthy_count=24,
        health_total_count=24,
        app_version="v0.6.0 beta",
        health_checks=(("test", "测试项目", True, ""),),
    )


def main() -> None:
    status = SystemStatus()
    snapshot = sample_snapshot()
    assert status.apply_snapshot(snapshot, 10.0)
    assert not status.apply_snapshot(snapshot, 11.0)
    assert status.as_dict()["camera_active_sources"] == ["unit-test"]
    assert status.as_dict()["health_checks"] == [
        {"id": "test", "name": "测试项目", "healthy": True, "detail": ""}
    ]
    assert status.snapshot_values()[0] == 73

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old_voice = module.VOICE_STATE_PATH
        old_leases = module.ACTIVE_VISION_LEASE_DIR
        old_version = module.VERSION_PATH
        old_legacy_version = module.LEGACY_VERSION_PATH
        old_health = module.HEALTH_STATUS_PATH
        try:
            module.VOICE_STATE_PATH = root / "voice.json"
            module.ACTIVE_VISION_LEASE_DIR = root / "leases"
            module.ACTIVE_VISION_LEASE_DIR.mkdir()
            module.VOICE_STATE_PATH.write_text(
                json.dumps({"visual_request_active": True}), encoding="utf-8"
            )
            active, sources = SystemStatus.read_camera_activity(100.0)
            assert active and sources == ("qwen_visual_request",)

            module.VOICE_STATE_PATH.write_text("{}", encoding="utf-8")
            (module.ACTIVE_VISION_LEASE_DIR / "face.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "source": "face-follow",
                        "expires_at": 105.0,
                    }
                ),
                encoding="utf-8",
            )
            active, sources = SystemStatus.read_camera_activity(100.0)
            assert active and sources == ("face-follow",)
            active, sources = SystemStatus.read_camera_activity(106.0)
            assert not active and not sources

            module.VERSION_PATH = root / "VERSION"
            module.LEGACY_VERSION_PATH = root / "legacy-VERSION"
            module.VERSION_PATH.write_text("v1.2.3 stable\n", encoding="utf-8")
            assert SystemStatus.read_app_version() == "v1.2.3 stable"

            module.HEALTH_STATUS_PATH = root / "health.json"
            module.HEALTH_STATUS_PATH.write_text(
                json.dumps(
                    {
                        "checks": [
                            {"id": "ok", "name": "正常项目", "healthy": True, "detail": "active"},
                            {"id": "bad", "name": "异常项目", "healthy": False, "detail": "size mismatch"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            assert SystemStatus.read_health_summary() == (1, 2)
            assert SystemStatus.read_health_checks() == (
                ("ok", "正常项目", True, ""),
                ("bad", "异常项目", False, "size mismatch"),
            )
        finally:
            module.VOICE_STATE_PATH = old_voice
            module.ACTIVE_VISION_LEASE_DIR = old_leases
            module.VERSION_PATH = old_version
            module.LEGACY_VERSION_PATH = old_legacy_version
            module.HEALTH_STATUS_PATH = old_health
    print("system status module: regression checks passed")


if __name__ == "__main__":
    main()
