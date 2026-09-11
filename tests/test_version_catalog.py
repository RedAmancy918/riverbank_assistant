import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("versionctl", ROOT / "scripts/versionctl.py")
VERSIONCTL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(VERSIONCTL)


class VersionCatalogTests(unittest.TestCase):
    def test_catalog_and_repository_versions_are_consistent(self):
        catalog = VERSIONCTL.load_catalog()
        self.assertEqual(VERSIONCTL.validate_catalog(catalog), [])
        self.assertEqual(VERSIONCTL.validate_repository(catalog), [])

    def test_apps_are_separate_artifacts_from_edge_and_base_os(self):
        catalog = VERSIONCTL.load_catalog()
        self.assertEqual(catalog["clients"]["ios"]["artifact_class"], "companion-client")
        self.assertEqual(catalog["clients"]["desktop"]["artifact_class"], "companion-client")
        self.assertEqual(catalog["edge_system"]["artifact_class"], "device-operating-system")
        self.assertEqual(catalog["edge_system"]["name"], "RiverBank Edge OS")
        self.assertEqual(catalog["edge_system"]["artifact_id"], "riverbank-edge-os")
        self.assertEqual(
            catalog["edge_system"]["distribution_mode"], "managed-linux-system-layer"
        )
        self.assertFalse(
            catalog["edge_system"]["base_operating_system"]["included_in_edge_version"]
        )

    def test_release_tags_are_scoped_to_independent_artifacts(self):
        catalog = VERSIONCTL.load_catalog()
        self.assertEqual(
            VERSIONCTL.expected_tags(catalog),
            {
                "suite": "suite/v0.26.6-beta",
                "edge-os": "edge-os/v0.26.6-beta",
                "ios": "ios/v0.25.2-beta",
                "call": "call/v0.25.1-beta",
            },
        )
        self.assertEqual(VERSIONCTL.validate_tag(catalog, "edge-os/v0.26.6-beta"), [])
        self.assertTrue(VERSIONCTL.validate_tag(catalog, "v0.26.6"))

    def test_release_workflow_uses_edge_os_scope(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('"edge-os/v*-beta"', workflow)
        self.assertIn("RiverBank-Edge-OS-${version}", workflow)
        self.assertNotIn('"edge-system/v*-beta"', workflow)

    def test_workshop_user_apps_are_device_data_not_release_artifacts(self):
        policy = VERSIONCTL.load_catalog()["workshop_apps"]
        self.assertEqual(policy["ownership"], "device-user")
        self.assertEqual(policy["storage_root"], "RIVERBANK_DATA/workshop")
        self.assertFalse(policy["included_in_source_control"])
        self.assertFalse(policy["included_in_edge_os_release"])
        self.assertFalse(policy["included_in_ota"])


if __name__ == "__main__":
    unittest.main()
