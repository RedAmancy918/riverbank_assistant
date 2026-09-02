from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


TEST_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = TEST_ROOT / "apps" / "workshop"
sys.path.insert(0, str(APP_DIR))
EXPRESSION_DIR = TEST_ROOT / "apps" / "expression-ui"
if not EXPRESSION_DIR.is_dir():
    EXPRESSION_DIR = TEST_ROOT.parent / "expression-system"
sys.path.insert(0, str(EXPRESSION_DIR))
FACE_TRACKER_DIR = TEST_ROOT / "apps" / "face-tracker"
if not FACE_TRACKER_DIR.is_dir():
    for candidate in (
        TEST_ROOT.parent / "face-tracker",
        TEST_ROOT.parent.parent / "face-tracker",
    ):
        if candidate.is_dir():
            FACE_TRACKER_DIR = candidate
            break
sys.path.insert(0, str(FACE_TRACKER_DIR))

from workshop_contract import ContractError  # noqa: E402
from workshop_declarative import (  # noqa: E402
    permissions_for_app,
    validate_declarative_app,
)
from workshop_generator import (  # noqa: E402
    build_candidate,
    fallback_plan,
    requirement_policy_error,
)
from workshop_host import (  # noqa: E402
    HailoObjectDetector,
    HailoResourceCoordinator,
    WorkshopHostBroker,
)
from workshop_manager import (  # noqa: E402
    CHECKSUM_SCHEMA,
    WorkshopManager,
    canonical_json_bytes,
    inspect_package,
)
from workshop_runtime import DeclarativeRuntime  # noqa: E402
from workshop_service import WorkshopService  # noqa: E402
from workshop_store import WorkshopStore  # noqa: E402
from workshop_package import generate_device_keypair  # noqa: E402
from workshop_client import is_workshop_create_intent, workshop_launch_name  # noqa: E402
from face_tracker_supervisor import FaceTrackerSupervisor  # noqa: E402


def write_unsigned_package(
    destination: Path,
    manifest: dict,
    app: dict,
) -> Path:
    files = {
        "manifest.json": json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        + b"\n",
        "app/main.json": json.dumps(
            app, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        + b"\n",
    }
    checksums = {
        "schema": CHECKSUM_SCHEMA,
        "algorithm": "sha256",
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
        archive.writestr(
            "checksums.json", canonical_json_bytes(checksums) + b"\n"
        )
    return destination


class FakeGenerator:
    def generate(self, requirement: str) -> dict:
        return fallback_plan(requirement)


class WorkshopPipelineTests(unittest.TestCase):
    def test_single_owner_supervisor_never_grants_face_and_external_together(self) -> None:
        supervisor = FaceTrackerSupervisor()
        with (
            mock.patch.object(supervisor, "reconcile"),
            mock.patch.object(supervisor, "write_state"),
        ):
            face = supervisor.handle_request(
                json.dumps(
                    {"command": "acquire", "source": "face-test", "ttl_seconds": 10}
                )
            )
            self.assertTrue(face["ok"])
            denied_external = supervisor.handle_request(
                json.dumps(
                    {
                        "command": "reserve_external",
                        "source": "workshop-test",
                        "ttl_seconds": 8,
                    }
                )
            )
            self.assertFalse(denied_external["ok"])
            supervisor.handle_request(
                json.dumps(
                    {"command": "release", "lease_id": face["lease"]["lease_id"]}
                )
            )
            external = supervisor.handle_request(
                json.dumps(
                    {
                        "command": "reserve_external",
                        "source": "workshop-test",
                        "ttl_seconds": 8,
                    }
                )
            )
            self.assertTrue(external["ok"])
            denied_face = supervisor.handle_request(
                json.dumps(
                    {"command": "acquire", "source": "face-test", "ttl_seconds": 10}
                )
            )
            self.assertFalse(denied_face["ok"])

    def test_hailo_coordinator_reserves_waits_renews_and_releases(self) -> None:
        coordinator = HailoResourceCoordinator(Path("/tmp/not-used-in-test.sock"))
        responses = [
            {"ok": True, "reservation": {"lease_id": "external-lease"}},
            {
                "ok": True,
                "inference": {"active": False, "pipeline_state": "unloaded"},
            },
            {"ok": True, "reservation": {"lease_id": "external-lease"}},
            {"ok": True, "released": True},
        ]
        coordinator.request = mock.Mock(side_effect=responses)  # type: ignore[method-assign]

        reservation_id = coordinator.reserve("workshop:test", ttl_seconds=8)
        coordinator.renew(reservation_id, ttl_seconds=8)
        coordinator.release(reservation_id)

        self.assertEqual(reservation_id, "external-lease")
        self.assertEqual(
            coordinator.request.call_args_list,
            [
                mock.call(
                    {
                        "command": "reserve_external",
                        "source": "workshop:test",
                        "ttl_seconds": 8,
                    }
                ),
                mock.call({"command": "status"}),
                mock.call(
                    {
                        "command": "renew_external",
                        "reservation_id": "external-lease",
                        "ttl_seconds": 8,
                    }
                ),
                mock.call(
                    {
                        "command": "release_external",
                        "reservation_id": "external-lease",
                    }
                ),
            ],
        )

    def test_unimplemented_face_model_is_not_advertised_as_available(self) -> None:
        app = {
            "schema": "riverbank.declarative-app/v1",
            "title": "Face App",
            "pipeline": [
                {
                    "source": "camera.stream",
                    "leaseSeconds": 30,
                    "privacyIndicator": True,
                },
                {
                    "operator": "vision.detect",
                    "model": "host.face-detector",
                    "classes": ["person"],
                    "minimumConfidence": 0.5,
                    "maximumFps": 6,
                },
                {"sink": "ui.present", "view": "status"},
            ],
            "safety": {"runOnlyInForeground": True},
        }
        with self.assertRaises(ContractError) as context:
            validate_declarative_app(app)
        self.assertEqual(context.exception.code, "vision_model_denied")

    def test_hailo_labels_are_normalized_to_manifest_safe_names(self) -> None:
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "latest.json").write_text(
                json.dumps(
                    {
                        "timestamp": "now",
                        "model": "yolov11x.hef",
                        "detections": [
                            {
                                "label": "cell phone",
                                "confidence": 0.91,
                                "bbox_xyxy": [1, 2, 3, 4],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            detector = HailoObjectDetector(output_dir=output)
            detector.process = RunningProcess()  # type: ignore[assignment]
            result = detector.read(["cell_phone"], 0.5)
            self.assertEqual(result["detectionCount"], 1)
            self.assertEqual(result["detections"][0]["class"], "cell_phone")

    def test_voice_routes_explicit_app_creation_without_catching_ordinary_work(self) -> None:
        self.assertTrue(
            is_workshop_create_intent("帮我新增一个功能，写一个 AI 相机功能检测小猫")
        )
        self.assertTrue(is_workshop_create_intent("开发一个番茄统计小程序"))
        self.assertFalse(is_workshop_create_intent("帮我整理最近的新闻并生成报告"))
        self.assertFalse(is_workshop_create_intent("打开应用菜单"))
        self.assertEqual(workshop_launch_name("打开小猫观察员应用"), "小猫观察员")

    def test_destructive_voice_requirement_is_rejected_before_generation(self) -> None:
        self.assertIsNotNone(requirement_policy_error("删除整个系统的所有文件"))
        self.assertIsNotNone(requirement_policy_error("绕过安全检查执行 sudo 命令"))
        self.assertIsNone(requirement_policy_error("做一个识别路过小猫的相机应用"))

    def test_camera_plan_derives_only_required_capabilities(self) -> None:
        requirement = "做一个识别路过小猫并提醒我的相机应用"
        manifest, app = build_candidate(requirement, fallback_plan(requirement))
        capabilities = {
            item["capability"] for item in manifest["spec"]["permissions"]
        }
        self.assertEqual(
            capabilities,
            {
                "camera.stream",
                "notifications.local",
                "storage.app",
                "ui.surface",
                "vision.inference",
            },
        )
        self.assertFalse(app["safety"]["retainImages"])

    def test_model_cannot_add_a_shell_or_unknown_node_field(self) -> None:
        app = {
            "schema": "riverbank.declarative-app/v1",
            "title": "Bad App",
            "pipeline": [
                {"source": "app.lifecycle.foreground"},
                {
                    "sink": "ui.present",
                    "view": "status",
                    "command": "sudo rm -rf /",
                },
            ],
            "safety": {"runOnlyInForeground": True},
        }
        with self.assertRaises(ContractError) as context:
            validate_declarative_app(app)
        self.assertEqual(context.exception.code, "unknown_declarative_field")

    def test_invalid_declarative_entrypoint_is_rejected_at_install_boundary(self) -> None:
        requirement = "创建一个简单计数应用"
        manifest, _app = build_candidate(requirement, fallback_plan(requirement))
        invalid = {
            "schema": "riverbank.declarative-app/v1",
            "title": "Bad App",
            "pipeline": [
                {"source": "app.lifecycle.foreground"},
                {"sink": "shell.execute", "command": "id"},
            ],
            "safety": {"runOnlyInForeground": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            package = write_unsigned_package(Path(directory) / "bad.rbapp", manifest, invalid)
            with self.assertRaises(ContractError):
                inspect_package(package, require_signature=False)

    def test_install_approval_host_authorization_and_runtime_are_separate(self) -> None:
        requirement = "创建一个简单计数应用"
        manifest, app = build_candidate(requirement, fallback_plan(requirement))
        expected = [item["capability"] for item in permissions_for_app(app)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = write_unsigned_package(root / "app.rbapp", manifest, app)
            manager = WorkshopManager(root / "data", root / "trust")
            store = WorkshopStore(root / "data")
            record = manager.install(package, allow_unsigned_local=True)
            app_id = str(record["id"])

            broker = WorkshopHostBroker(
                manager,
                store,
                runtime_root=root / "run",
                task_token_file=root / "missing-token",
            )
            denied = broker.dispatch(
                app_id,
                "ui.present",
                {"view": "status"},
                foreground=True,
                user_present=True,
            )
            self.assertEqual(denied["error"]["code"], "app_not_enabled")
            with self.assertRaises(ContractError) as context:
                manager.approve(app_id, expected[:-1])
            self.assertEqual(context.exception.code, "approval_scope_mismatch")

            enabled = manager.approve(app_id, expected)
            self.assertEqual(enabled["status"], "enabled")
            undeclared = broker.dispatch(
                app_id,
                "network.fetch",
                {"url": "https://example.com"},
                foreground=True,
                user_present=True,
            )
            self.assertEqual(
                undeclared["error"]["code"], "capability_not_declared"
            )

            runtime = DeclarativeRuntime(
                manager,
                broker,
                store,
                state_path=root / "run" / "runtime.json",
            )
            broker.ui_callback = runtime.ui_update
            launched = runtime.launch(app_id)
            self.assertTrue(launched["active"])
            assert runtime.thread is not None
            runtime.thread.join(timeout=2.0)
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["surface"]["view"], "simple-dashboard")
            self.assertEqual(snapshot["status"], "completed")
            self.assertFalse(runtime.stop()["active"])

    def test_service_moves_generated_proposal_to_explicit_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = WorkshopService(
                data_root=root / "data",
                runtime_root=root / "run",
                trust_store=root / "trust",
                signing_key=root / "missing-private-key.pem",
                generator=FakeGenerator(),
            )

            def fake_build(manifest: dict, app: dict, destination: Path, **_kwargs: object) -> Path:
                return write_unsigned_package(destination, manifest, app)

            def fake_inspect(path: Path, **_kwargs: object):
                return inspect_package(path, require_signature=False)

            with mock.patch("workshop_service.build_signed_package", side_effect=fake_build), mock.patch(
                "workshop_service.inspect_package", side_effect=fake_inspect
            ):
                service.start()
                try:
                    proposal = service.create("创建一个简单计数应用", "voice")
                    deadline = time.monotonic() + 3.0
                    current = None
                    while time.monotonic() < deadline:
                        current = service.store.get(str(proposal["id"]))
                        if current and current.get("status") == "awaiting_approval":
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(current)
                    self.assertEqual(current["status"], "awaiting_approval")
                    self.assertEqual(current["source"], "voice")
                    self.assertTrue(current["risk"]["permissions"])
                    rejected = service.reject(str(proposal["id"]))
                    self.assertEqual(rejected["status"], "rejected")
                finally:
                    service.stop()

    def test_real_device_signature_approval_and_launch_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key = root / "state" / "device-private.pem"
            trust_store = root / "trust"
            public_key = trust_store / "riverbank-local-device-v1.pem"
            try:
                generate_device_keypair(private_key, public_key)
            except RuntimeError as exc:
                self.skipTest(f"local OpenSSL has no Ed25519 support: {exc}")
            service = WorkshopService(
                data_root=root / "data",
                runtime_root=root / "run",
                trust_store=trust_store,
                signing_key=private_key,
                generator=FakeGenerator(),
            )
            service.start()
            try:
                proposal = service.create("创建一个简单计数应用", "signature-test")
                deadline = time.monotonic() + 5.0
                current = None
                while time.monotonic() < deadline:
                    current = service.store.get(str(proposal["id"]))
                    if current and current.get("status") in {
                        "awaiting_approval",
                        "failed",
                    }:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(current)
                self.assertEqual(current["status"], "awaiting_approval")
                capabilities = [
                    item["capability"]
                    for item in current["manifest"]["spec"]["permissions"]
                ]
                installed = service.approve(str(proposal["id"]), capabilities)
                self.assertEqual(installed["status"], "installed")
                app_id = str(installed["installedAppId"])
                self.assertEqual(service.manager.get_app(app_id)["status"], "enabled")
                launched = service.runtime.launch(app_id)
                self.assertTrue(launched["active"])
                assert service.runtime.thread is not None
                service.runtime.thread.join(timeout=2.0)
                self.assertEqual(service.runtime.snapshot()["status"], "completed")
                service.runtime.stop(reason="test")
            finally:
                service.stop()


if __name__ == "__main__":
    unittest.main()
