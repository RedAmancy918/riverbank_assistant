import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scan_release",
    ROOT / "scripts/scan_release.py",
)
SCAN_RELEASE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SCAN_RELEASE)


class ReleaseScanWorkshopPolicyTests(unittest.TestCase):
    def test_rejects_workshop_user_packages_and_state(self):
        rejected = {
            "exports/my-clock.rbapp": "Workshop user package",
            "apps/workshop/drafts/proposal.rbapp": "Workshop user package",
            "apps/workshop/packages/com.example.clock/1.0.0/manifest.json": "Workshop user data",
            "apps/workshop/app-data/com.example.clock/state.json": "Workshop user data",
            "apps/workshop/registry.json": "Workshop user state",
            "apps/workshop/proposals.json": "Workshop user state",
            "apps/workshop/audit.jsonl": "Workshop user state",
            "data/workshop/app-data/example/private.json": "Workshop user data",
            "riverbank-user/workshop/registry.json": "Workshop user data",
        }
        for raw_path, expected in rejected.items():
            with self.subTest(path=raw_path):
                self.assertEqual(
                    SCAN_RELEASE.workshop_user_artifact_reason(Path(raw_path)),
                    expected,
                )

    def test_allows_platform_sources_schemas_tests_and_official_examples(self):
        allowed = (
            "apps/workshop/workshop_service.py",
            "apps/workshop/schemas/riverbank-app-manifest-v1.schema.json",
            "apps/workshop/examples/minimal-app/manifest.json",
            "apps/workshop/examples/sound-meter/app/main.json",
            "tests/test_workshop_pipeline.py",
            "docs/WORKSHOP_PROTOCOL.zh-CN.md",
        )
        for raw_path in allowed:
            with self.subTest(path=raw_path):
                self.assertIsNone(
                    SCAN_RELEASE.workshop_user_artifact_reason(Path(raw_path))
                )

    def test_gitignore_covers_device_local_workshop_outputs(self):
        patterns = set(
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        for expected in (
            "*.rbapp",
            "apps/workshop/drafts/",
            "apps/workshop/packages/",
            "apps/workshop/app-data/",
            "apps/workshop/registry.json",
            "apps/workshop/proposals.json",
            "apps/workshop/audit.jsonl",
            "data/workshop/",
            "riverbank-user/workshop/",
            "workshop-data/",
        ):
            with self.subTest(pattern=expected):
                self.assertIn(expected, patterns)


if __name__ == "__main__":
    unittest.main()
