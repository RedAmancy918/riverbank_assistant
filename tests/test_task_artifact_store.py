#!/usr/bin/env python3
"""Security and persistence checks for generated task artifacts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from task_artifact_store import TaskArtifactError, TaskArtifactStore


class TaskArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "artifacts"
        self.store = TaskArtifactStore(self.root)
        self.task_id = "a" * 32

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_scoped_to_task_directory(self) -> None:
        saved = self.store.save(self.task_id, "result.png", b"png-data")
        self.assertEqual(saved.read_bytes(), b"png-data")
        self.assertEqual(
            self.store.resolve(self.task_id, "result.png").read_bytes(),
            b"png-data",
        )

    def test_rejects_path_traversal_and_invalid_task_ids(self) -> None:
        with self.assertRaises(TaskArtifactError):
            self.store.save(self.task_id, "../secret", b"payload")
        with self.assertRaises(TaskArtifactError):
            self.store.resolve("not-a-task", "result.png")

    def test_deletes_only_explicit_validated_task_directories(self) -> None:
        self.store.save(self.task_id, "result.png", b"png-data")
        other_id = "b" * 32
        self.store.save(other_id, "result.png", b"keep")
        self.assertEqual(self.store.delete_tasks([self.task_id]), 1)
        self.assertFalse((self.root / self.task_id).exists())
        self.assertTrue((self.root / other_id / "result.png").is_file())


if __name__ == "__main__":
    unittest.main()
