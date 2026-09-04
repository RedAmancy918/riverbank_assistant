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
        self.assertEqual(completed["output_format"], "text")
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
        with self.assertRaises(ValueError):
            self.store.create_task(prompt="valid prompt", output_format="video")

    def test_generated_artifact_metadata_is_persisted(self) -> None:
        task, _ = self.store.create_task(
            prompt="生成一张产品概念图",
            output_format="image",
        )
        claimed = self.store.claim_next()
        assert claimed is not None
        self.store.complete(
            task["id"],
            summary="图片已生成",
            artifact_filename="riverbank-image.png",
            artifact_media_type="image/png",
            artifact_size_bytes=1024,
        )
        completed = self.store.get_task(task["id"])
        assert completed is not None
        self.assertEqual(completed["output_format"], "image")
        self.assertEqual(completed["artifact_filename"], "riverbank-image.png")
        self.assertEqual(completed["artifact_size_bytes"], 1024)

    def test_tasks_are_isolated_and_cleanup_refuses_active_work(self) -> None:
        alice, _ = self.store.create_task(
            prompt="Alice private task",
            owner_user_id="alice-user-id",
            idempotency_key="same-key",
        )
        bob, _ = self.store.create_task(
            prompt="Bob private task",
            owner_user_id="bob-user-id",
            idempotency_key="same-key",
        )
        self.assertNotEqual(alice["id"], bob["id"])
        self.assertIsNone(
            self.store.get_task(alice["id"], owner_user_id="bob-user-id")
        )
        self.assertEqual(
            self.store.counts(owner_user_id="alice-user-id")["queued"], 1
        )
        with self.assertRaises(RuntimeError):
            self.store.delete_owner_tasks("alice-user-id")
        cancelled = self.store.request_cancel(
            alice["id"], owner_user_id="alice-user-id"
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            self.store.delete_owner_tasks("alice-user-id")["deleted"], 1
        )
        self.assertEqual(
            self.store.list_tasks(owner_user_id="alice-user-id"), []
        )

    def test_task_report_owner_index_preserves_account_isolation(self) -> None:
        task, _ = self.store.create_task(
            prompt="private report task",
            owner_user_id="alice-user-id",
        )
        claimed = self.store.claim_next()
        assert claimed is not None
        self.store.complete(
            task["id"],
            summary="done",
            report_id="private-report-id",
            report_filename="private.md",
        )
        self.assertEqual(
            self.store.report_owners(),
            {"private-report-id": {"alice-user-id"}},
        )

    def test_first_admin_can_claim_legacy_tasks(self) -> None:
        legacy, _ = self.store.create_task(prompt="Legacy device task")
        self.assertEqual(self.store.claim_unowned_tasks("first-admin-id"), 1)
        self.assertIsNotNone(
            self.store.get_task(
                legacy["id"], owner_user_id="first-admin-id"
            )
        )


if __name__ == "__main__":
    unittest.main()
