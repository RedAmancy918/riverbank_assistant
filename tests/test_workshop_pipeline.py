from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TEST_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = TEST_ROOT / "apps" / "workshop"
if not APP_DIR.is_dir() and (TEST_ROOT / "workshop_declarative.py").is_file():
    APP_DIR = TEST_ROOT
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

from workshop_contract import ContractError, validate_manifest  # noqa: E402
from workshop_declarative import (  # noqa: E402
    permissions_for_app,
    validate_declarative_app,
)
from workshop_generator import (  # noqa: E402
    HermesPlanGenerator,
    build_candidate,
    fallback_plan,
    requirement_policy_error,
)
from workshop_host import (  # noqa: E402
    HailoObjectDetector,
    HailoResourceCoordinator,
    PipeWireMicrophoneCapture,
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


class FakeMicrophoneCapture:
    def __init__(self, source: str, sample_rate: int) -> None:
        self.source = source
        self.sample_rate = sample_rate
        self.started = False
        self.closed = False
        self.reads = 0

    def start(self) -> None:
        self.started = True

    def read_level(self, frame_milliseconds: int) -> dict:
        self.reads += 1
        return {
            "rms": 0.25,
            "peak": 0.5,
            "dbfs": -18.0,
            "sampleRate": self.sample_rate,
            "frameMilliseconds": frame_milliseconds,
            "samplesAnalyzed": round(self.sample_rate * frame_milliseconds / 1000),
        }

    def close(self) -> None:
        self.closed = True


class WorkshopPipelineTests(unittest.TestCase):
    def test_clock_plan_uses_host_clock_surface_and_local_time_fields(self) -> None:
        requirement = "帮我做一个桌面时钟"
        plan = fallback_plan(requirement)
        self.assertEqual(plan["pipeline"][0]["source"], "timer.interval")
        self.assertEqual(plan["pipeline"][0]["seconds"], 1)
        self.assertEqual(plan["pipeline"][1]["view"], "clock")
        presentation = plan["pipeline"][1]["presentation"]
        self.assertEqual(presentation["schema"], "riverbank.surface/v1")
        self.assertEqual(presentation["components"][0]["type"], "clock")
        manifest, app = build_candidate(requirement, plan)
        self.assertEqual(
            {item["capability"] for item in manifest["spec"]["permissions"]},
            {"ui.surface"},
        )
        self.assertEqual(app["pipeline"][1]["view"], "clock")
        context = DeclarativeRuntime._time_context(0)
        self.assertRegex(context["hourMinute"], r"^\d{2}:\d{2}$")
        self.assertRegex(context["time"], r"^\d{2}:\d{2}:\d{2}$")
        self.assertIn(context["weekday"], DeclarativeRuntime.WEEKDAYS_ZH)

    def test_unknown_surface_is_rejected_instead_of_silently_rendering_status(self) -> None:
        app = {
            "schema": "riverbank.declarative-app/v1",
            "title": "Unknown UI",
            "pipeline": [
                {"source": "app.lifecycle.foreground"},
                {"sink": "ui.present", "view": "invented-screen"},
            ],
            "safety": {"runOnlyInForeground": True},
        }
        with self.assertRaises(ContractError) as context:
            validate_declarative_app(app)
        self.assertEqual(context.exception.code, "unsupported_ui_view")

    def test_adaptive_surface_components_are_bounded_and_normalized(self) -> None:
        app = validate_declarative_app(
            {
                "schema": "riverbank.declarative-app/v1",
                "title": "Temperature",
                "pipeline": [
                    {"source": "timer.interval", "seconds": 5, "repeat": True},
                    {
                        "sink": "ui.present",
                        "view": "adaptive",
                        "presentation": {
                            "schema": "riverbank.surface/v1",
                            "layout": "hero",
                            "accent": "amber",
                            "components": [
                                {
                                    "id": "temperature",
                                    "type": "metric",
                                    "valueKey": "value",
                                    "label": "温度",
                                    "unit": "°C",
                                    "precision": 1,
                                }
                            ],
                        },
                    },
                ],
                "safety": {"runOnlyInForeground": True},
            }
        )
        surface = app["pipeline"][1]["presentation"]
        self.assertEqual(surface["layout"], "hero")
        self.assertEqual(surface["components"][0]["precision"], 1)

    def test_generator_repairs_a_semantic_plan_that_host_cannot_render(self) -> None:
        invalid = {
            **fallback_plan("帮我做一个桌面时钟"),
            "pipeline": [
                {"source": "timer.interval", "seconds": 1, "repeat": True},
                {"sink": "ui.present", "view": "beautiful-clock-that-does-not-exist"},
            ],
        }
        repaired = fallback_plan("帮我做一个桌面时钟")
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "hermes"
            binary.touch()
            generator = HermesPlanGenerator(hermes_bin=binary, workspace=Path(directory))
            with mock.patch.object(
                generator,
                "_run_prompt",
                side_effect=[json.dumps(invalid), json.dumps(repaired)],
            ) as invoke:
                plan = generator.generate("帮我做一个桌面时钟")
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(plan["generator"], "hermes-plan-repaired")
        self.assertEqual(plan["pipeline"][1]["view"], "clock")

    def test_microphone_pcm_is_reduced_to_metrics_without_payload(self) -> None:
        result = PipeWireMicrophoneCapture._level_metrics(
            b"\x00\x40" * 1600,
            16000,
            100,
        )
        self.assertEqual(result["rms"], 0.5)
        self.assertEqual(result["peak"], 0.5)
        self.assertAlmostEqual(result["dbfs"], -6.02, places=2)
        self.assertEqual(result["samplesAnalyzed"], 1600)
        self.assertNotIn("payload", result)
        self.assertNotIn("pcm", result)

    def test_sound_meter_example_manifest_matches_declarative_permissions(self) -> None:
        example = APP_DIR / "examples" / "sound-meter"
        manifest = validate_manifest(
            json.loads((example / "manifest.json").read_text(encoding="utf-8"))
        )
        app = validate_declarative_app(
            json.loads((example / "app" / "main.json").read_text(encoding="utf-8"))
        )
        declared = {
            item["capability"] for item in manifest["spec"]["permissions"]
        }
        required = {
            item["capability"] for item in permissions_for_app(app)
        }
        self.assertEqual(declared, required)

    def test_microphone_plan_runs_metrics_only_and_releases_lease(self) -> None:
        requirement = "做一个麦克风声音计数应用"
        plan = fallback_plan(requirement)
        self.assertEqual(plan["pipeline"][0]["source"], "microphone.stream")
        plan["pipeline"][0]["leaseSeconds"] = 2
        manifest, app = build_candidate(requirement, plan)
        capabilities = {
            item["capability"] for item in manifest["spec"]["permissions"]
        }
        self.assertEqual(
            capabilities,
            {"microphone.stream", "storage.app", "ui.surface"},
        )
        self.assertFalse(app["safety"]["retainAudio"])

        captures: list[FakeMicrophoneCapture] = []

        def microphone_factory(source: str, sample_rate: int) -> FakeMicrophoneCapture:
            capture = FakeMicrophoneCapture(source, sample_rate)
            captures.append(capture)
            return capture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = write_unsigned_package(root / "microphone.rbapp", manifest, app)
            manager = WorkshopManager(root / "data", root / "trust")
            store = WorkshopStore(root / "data")
            record = manager.install(package, allow_unsigned_local=True)
            app_id = str(record["id"])
            manager.approve(app_id, sorted(capabilities))
            broker = WorkshopHostBroker(
                manager,
                store,
                runtime_root=root / "run",
                task_token_file=root / "missing-token",
                microphone_factory=microphone_factory,
            )
            runtime = DeclarativeRuntime(
                manager,
                broker,
                store,
                state_path=root / "run" / "runtime.json",
            )
            broker.ui_callback = runtime.ui_update
            with mock.patch("workshop_host.send_expression") as indicator:
                runtime.launch(app_id)
                deadline = time.monotonic() + 2.0
                snapshot = runtime.snapshot()
                while time.monotonic() < deadline:
                    snapshot = runtime.snapshot()
                    data = snapshot.get("surface", {}).get("data", {})
                    if captures and captures[0].reads and "dbfs" in data:
                        break
                    time.sleep(0.02)
                self.assertTrue(captures)
                self.assertTrue(captures[0].started)
                self.assertGreater(captures[0].reads, 0)
                data = snapshot["surface"]["data"]
                self.assertEqual(data["dbfs"], -18.0)
                self.assertTrue(data["soundActive"])
                self.assertNotIn("pcm", data)
                self.assertNotIn("audio", data)
                runtime.stop(reason="test")
                self.assertTrue(captures[0].closed)
                self.assertFalse(broker.microphone_leases)
                commands = [call.args[0] for call in indicator.call_args_list]
                self.assertTrue(
                    any(
                        item.get("command") == "audio_activity" and item.get("active")
                        for item in commands
                    )
                )
                self.assertTrue(
                    any(
                        item.get("command") == "audio_activity" and not item.get("active")
                        for item in commands
                    )
                )

    def test_microphone_activity_indicator_has_distinct_and_combined_modes(self) -> None:
        try:
            from expression_display_persistent import PersistentExpressionDisplay
        except ModuleNotFoundError as exc:
            self.skipTest(f"expression renderer dependency unavailable: {exc}")
        display = PersistentExpressionDisplay.__new__(PersistentExpressionDisplay)
        display.runtime_audio_sources = {"workshop:test": time.monotonic() + 2}
        display.runtime_vision_sources = {}
        display.system_status = SimpleNamespace(camera_active_sources=())
        display.camera_view_active = False
        display.gallery_active = False
        display.video_call_active = False
        display.video_call_status = {}
        display.pomodoro = SimpleNamespace(status="idle")
        display.pomodoro_active = False
        self.assertEqual(display.activity_indicator_mode(), "microphone")
        display.runtime_vision_sources = {"camera:test": time.monotonic() + 2}
        self.assertEqual(display.activity_indicator_mode(), "camera_microphone")

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
