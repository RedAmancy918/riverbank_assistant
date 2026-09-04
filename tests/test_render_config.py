#!/usr/bin/env python3
"""Regression checks for local and cross-device configuration rendering."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_config",
    ROOT / "scripts" / "render_config.py",
)
RENDER_CONFIG = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RENDER_CONFIG)


class RenderConfigTests(unittest.TestCase):
    def test_target_paths_are_not_resolved_through_development_host_symlinks(self) -> None:
        self.assertEqual(
            RENDER_CONFIG.absolute_path("/home/geo/riverbank-edge-os", "repo"),
            Path("/home/geo/riverbank-edge-os"),
        )

    def test_output_path_is_resolved_before_safe_cleanup(self) -> None:
        output = RENDER_CONFIG.absolute_path(
            str(ROOT / "build" / "generated"),
            "output",
            resolve_symlinks=True,
        )
        self.assertTrue(output.is_absolute())


if __name__ == "__main__":
    unittest.main()
