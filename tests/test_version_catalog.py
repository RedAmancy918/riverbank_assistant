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
        self.assertEqual(catalog["edge_system"]["artifact_class"], "device-system-software")
        self.assertFalse(
            catalog["edge_system"]["base_operating_system"]["included_in_edge_version"]
        )

    def test_release_tags_are_scoped_to_independent_artifacts(self):
        catalog = VERSIONCTL.load_catalog()
        self.assertEqual(
            VERSIONCTL.expected_tags(catalog),
            {
                "suite": "suite/v0.25.3-beta",
                "edge-system": "edge-system/v0.25.3-beta",
                "ios": "ios/v0.25.1-beta",
                "call": "call/v0.25.1-beta",
            },
        )
        self.assertEqual(VERSIONCTL.validate_tag(catalog, "edge-system/v0.25.3-beta"), [])
        self.assertTrue(VERSIONCTL.validate_tag(catalog, "v0.25.3"))


if __name__ == "__main__":
    unittest.main()
