from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "health-monitor"))

from health_monitor import mounted_filesystem_for, run_check  # noqa: E402


class HealthMonitorMountTests(unittest.TestCase):
    def test_nested_data_directory_uses_containing_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "riverbank-user"
            data.mkdir()
            with mock.patch(
                "health_monitor.os.path.ismount",
                side_effect=lambda candidate: Path(candidate) == root,
            ):
                self.assertEqual(mounted_filesystem_for(str(data)), root)
                ok, detail = run_check({"type": "mount", "target": str(data)})
        self.assertTrue(ok)
        self.assertEqual(detail, f"mounted via {root}")

    def test_root_filesystem_is_not_accepted_as_data_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with mock.patch(
                "health_monitor.os.path.ismount",
                side_effect=lambda candidate: Path(candidate) == Path("/"),
            ):
                ok, detail = run_check({"type": "mount", "target": str(data)})
        self.assertFalse(ok)
        self.assertIn("dedicated", detail)


if __name__ == "__main__":
    unittest.main()
