from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_APP_DIR = TEST_ROOT / "apps" / "workshop"
APP_DIR = REPOSITORY_APP_DIR if REPOSITORY_APP_DIR.is_dir() else TEST_ROOT
sys.path.insert(0, str(APP_DIR))

from workshop_contract import (  # noqa: E402
    ContractError,
    authorize_request,
    validate_manifest,
)
from workshop_manager import (  # noqa: E402
    CHECKSUM_SCHEMA,
    WorkshopManager,
    canonical_json_bytes,
    inspect_package,
)


def manifest(*permissions: dict, runtime: str = "declarative-v1") -> dict:
    return {
        "apiVersion": "riverbank.workshop/v1",
        "kind": "RiverBankApp",
        "metadata": {
            "id": "tech.riverbank.testapp",
            "name": "Test App",
            "version": "1.2.3-beta.1",
            "description": "Workshop test application",
            "vendor": "RiverBank",
        },
        "spec": {
            "runtime": {
                "kind": runtime,
                "entrypoint": (
                    "app/main.json"
                    if runtime == "declarative-v1"
                    else "app/main.py"
                ),
                "protocol": "riverbank.app-host/v1",
            },
            "permissions": list(permissions),
            "resources": {
                "cpuPercent": 12,
                "memoryMB": 128,
                "storageMB": 64,
                "maxProcesses": 2,
            },
            "lifecycle": {"autostart": False},
            "ui": {"menuLabel": "测试", "glyph": "测"},
        },
    }


def permission(capability: str, **constraints: object) -> dict:
    return {
        "capability": capability,
        "reason": f"test {capability}",
        "constraints": constraints,
    }


def request(method: str, params: dict | None = None) -> dict:
    return {
        "protocol": "riverbank.app-host/v1",
        "type": "request",
        "id": "request-1",
        "method": method,
        "params": params or {},
    }


def build_unsigned_package(path: Path, app_manifest: dict) -> None:
    app_path = app_manifest["spec"]["runtime"]["entrypoint"]
    app_payload = canonical_json_bytes(
        {
            "schema": "riverbank.declarative-app/v1",
            "title": "Test App",
            "pipeline": [
                {"source": "app.lifecycle.foreground"},
                {"sink": "ui.present", "view": "status", "title": "Test App"},
            ],
            "safety": {
                "runOnlyInForeground": True,
                "stopOnUserExit": True,
                "retainImages": False,
            },
        }
    ) + b"\n"
    files = {
        "manifest.json": canonical_json_bytes(app_manifest) + b"\n",
        app_path: app_payload,
    }
    checksums = {
        "schema": CHECKSUM_SCHEMA,
        "algorithm": "sha256",
        "files": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in sorted(files.items())
        },
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("checksums.json", canonical_json_bytes(checksums) + b"\n")


class WorkshopContractTests(unittest.TestCase):
    def test_valid_manifest_is_normalized(self) -> None:
        normalized = validate_manifest(
            manifest(permission("ui.surface"), permission("storage.app"))
        )
        self.assertEqual(normalized["metadata"]["id"], "tech.riverbank.testapp")
        self.assertEqual(normalized["spec"]["resources"]["memoryMB"], 128)
        self.assertEqual(
            normalized["spec"]["runtime"]["protocol"],
            "riverbank.app-host/v1",
        )

    def test_forbidden_system_capability_is_rejected(self) -> None:
        with self.assertRaises(ContractError) as context:
            validate_manifest(manifest(permission("system.power")))
        self.assertEqual(context.exception.code, "forbidden_capability")

    def test_direct_process_command_is_rejected(self) -> None:
        value = manifest(permission("ui.surface"), runtime="python-sandbox-v1")
        value["spec"]["runtime"]["command"] = "/bin/sh"
        with self.assertRaises(ContractError) as context:
            validate_manifest(value)
        self.assertEqual(context.exception.code, "direct_process_forbidden")

    def test_stream_capability_requires_privacy_indicator(self) -> None:
        with self.assertRaises(ContractError) as context:
            validate_manifest(manifest(permission("camera.stream")))
        self.assertEqual(context.exception.code, "privacy_indicator_required")

    def test_network_requires_exact_domains_without_wildcards(self) -> None:
        with self.assertRaises(ContractError) as context:
            validate_manifest(
                manifest(
                    permission(
                        "network.outbound",
                        domains=["*.example.com"],
                        methods=["GET"],
                    )
                )
            )
        self.assertEqual(context.exception.code, "invalid_domain")

    def test_request_needs_declaration_grant_and_user_presence(self) -> None:
        app_manifest = manifest(
            permission("camera.snapshot"),
            permission(
                "network.outbound",
                domains=["api.example.com"],
                methods=["GET"],
            ),
        )
        session = {
            "app_id": "tech.riverbank.testapp",
            "foreground": True,
            "user_present": True,
        }
        allowed = authorize_request(
            app_manifest,
            {"network.outbound": {"granted": True}},
            request(
                "network.fetch",
                {"url": "https://api.example.com/v1/data", "httpMethod": "GET"},
            ),
            session=session,
        )
        self.assertTrue(allowed.allowed)
        denied = authorize_request(
            app_manifest,
            {"camera.snapshot": {"granted": True}},
            request("camera.snapshot"),
            session={**session, "user_present": False},
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, "user_presence_required")

    def test_network_domain_is_enforced_per_request(self) -> None:
        app_manifest = manifest(
            permission(
                "network.outbound",
                domains=["api.example.com"],
                methods=["GET"],
            )
        )
        decision = authorize_request(
            app_manifest,
            {"network.outbound": True},
            request("network.fetch", {"url": "https://evil.example/v1"}),
            session={
                "app_id": "tech.riverbank.testapp",
                "foreground": True,
                "user_present": True,
            },
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "network_domain_denied")

    def test_declared_capability_still_needs_device_grant(self) -> None:
        decision = authorize_request(
            manifest(permission("camera.snapshot")),
            {},
            request("camera.snapshot"),
            session={
                "app_id": "tech.riverbank.testapp",
                "foreground": True,
                "user_present": True,
            },
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "capability_not_granted")

    def test_request_cannot_supply_an_app_identity(self) -> None:
        decision = authorize_request(
            manifest(permission("camera.snapshot")),
            {"camera.snapshot": True},
            request("camera.snapshot", {"app_id": "tech.attacker.fake"}),
            session={
                "app_id": "tech.riverbank.testapp",
                "foreground": True,
                "user_present": True,
            },
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "identity_spoofing")

    def test_motor_target_and_speed_stay_inside_manifest_limits(self) -> None:
        app_manifest = manifest(
            permission(
                "motor.pan_tilt",
                panDegrees=[-60, 60],
                tiltDegrees=[-25, 30],
                maxDegreesPerSecond=45,
                emergencyStop=True,
            )
        )
        decision = authorize_request(
            app_manifest,
            {"motor.pan_tilt": True},
            request(
                "motor.move",
                {"pan": 80, "tilt": 0, "degreesPerSecond": 20},
            ),
            session={
                "app_id": "tech.riverbank.testapp",
                "foreground": True,
                "user_present": True,
            },
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "motor_limit_exceeded")

    def test_unsigned_external_package_is_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "app.rbapp"
            build_unsigned_package(package, manifest(permission("ui.surface")))
            with self.assertRaises(ContractError) as context:
                inspect_package(package)
            self.assertEqual(context.exception.code, "signature_required")

    def test_package_install_is_atomic_and_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app.rbapp"
            build_unsigned_package(package, manifest(permission("ui.surface")))
            manager = WorkshopManager(root / "data", root / "trust")
            record = manager.install(package, allow_unsigned_local=True)
            self.assertEqual(record["status"], "installed_disabled")
            self.assertFalse(record["permissions"][0]["granted"])
            installed = (
                root
                / "data"
                / "packages"
                / "tech.riverbank.testapp"
                / "1.2.3-beta.1"
                / "app"
                / "main.json"
            )
            self.assertTrue(installed.is_file())
            self.assertEqual(manager.list_apps()[0]["id"], "tech.riverbank.testapp")

    def test_package_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "bad.rbapp"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../outside", b"blocked")
            with self.assertRaises(ContractError) as context:
                inspect_package(package, require_signature=False)
            self.assertEqual(context.exception.code, "unsafe_path")

    def test_package_checksum_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "changed.rbapp"
            app_manifest = manifest(permission("ui.surface"))
            files = {
                "manifest.json": canonical_json_bytes(app_manifest) + b"\n",
                "app/main.json": b'{"schema":"riverbank.declarative-app/v1"}\n',
            }
            checksums = {
                "schema": CHECKSUM_SCHEMA,
                "algorithm": "sha256",
                "files": {
                    "manifest.json": hashlib.sha256(files["manifest.json"]).hexdigest(),
                    "app/main.json": "0" * 64,
                },
            }
            with zipfile.ZipFile(package, "w") as archive:
                for name, payload in files.items():
                    archive.writestr(name, payload)
                archive.writestr(
                    "checksums.json", canonical_json_bytes(checksums) + b"\n"
                )
            with self.assertRaises(ContractError) as context:
                inspect_package(package, require_signature=False)
            self.assertEqual(context.exception.code, "checksum_mismatch")


if __name__ == "__main__":
    unittest.main()
