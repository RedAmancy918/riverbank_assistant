#!/usr/bin/env python3
"""Regression checks for the persistent RiverBank background-task queue."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temporary.name) / "tasks.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_task_lifecycle_and_idempotency(self) -> None:
        task, created = self.store.create_task(
            prompt="整理最近的机器人新闻并形成 Markdown 报告",
            title="机器人新闻",
            idempotency_key="client-request-1",
            source="ios",
        )
        self.assertTrue(created)
        duplicate, duplicate_created = self.store.create_task(
            prompt="这段内容不会创建第二项",
            idempotency_key="client-request-1",
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], task["id"])

        claimed = self.store.claim_next()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["status"], "running")
        self.store.wait_for_input(claimed["id"], "需要关注哪个行业？")
        waiting = self.store.get_task(claimed["id"])
        assert waiting is not None
        self.assertEqual(waiting["status"], "waiting_input")

        queued = self.store.answer(claimed["id"], "具身智能")
        assert queued is not None
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["answers"][0]["answer"], "具身智能")
        claimed_again = self.store.claim_next()
        assert claimed_again is not None
        self.store.complete(
            claimed_again["id"],
            summary="已经完成。",
            report_id="cmVwb3J0Lm1k",
            report_filename="report.md",
        )
        completed = self.store.get_task(claimed_again["id"])
        assert completed is not None
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 1)
        self.assertEqual(self.store.counts()["completed"], 1)

    def test_cancel_and_recover(self) -> None:
        queued, _ = self.store.create_task(prompt="等待取消的任务")
        cancelled = self.store.request_cancel(queued["id"])
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")

        running, _ = self.store.create_task(prompt="运行中的任务")
        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed["id"], running["id"])
        self.assertEqual(self.store.recover_interrupted(), 1)
        recovered = self.store.get_task(running["id"])
        assert recovered is not None
        self.assertEqual(recovered["status"], "queued")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_task(prompt="x")
        with self.assertRaises(ValueError):
            self.store.create_task(prompt="valid prompt", kind="shell")


if __name__ == "__main__":
    unittest.main()
