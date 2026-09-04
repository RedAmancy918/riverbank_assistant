#!/usr/bin/env python3
"""Regression checks for task reports and clarification handoff."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from task_store import TaskStore
from task_worker import NEEDS_INPUT_MARKER, TaskWorker


class FakeRunner:
    def __init__(self, response: str, *, create_report: bool = False) -> None:
        self.response = response
        self.create_report = create_report

    def run(self, task, report_path, store):
        if self.create_report:
            report_path.write_text("# 测试报告\n\n完整内容。\n", encoding="utf-8")
        return self.response


class FakeImageGenerator:
    available = True

    def generate(self, prompt):
        return SimpleNamespace(
            payload=b"\x89PNG\r\n\x1a\nriverbank-test",
            filename="generated.png",
            media_type="image/png",
            model="test-image-model",
            request_id="request-1",
        )


class TaskWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = TaskStore(root / "tasks.db")
        self.reports = root / "reports"
        self.reports.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def worker(self, runner: FakeRunner, *, image_generator=None) -> TaskWorker:
        return TaskWorker(
            store=self.store,
            runner=runner,
            reports_dir=self.reports,
            image_generator=image_generator,
            poll_seconds=0.01,
        )

    def test_completed_task_always_has_report(self) -> None:
        task, _ = self.store.create_task(prompt="生成一份测试报告", title="测试")
        self.worker(FakeRunner("报告已经生成。", create_report=False)).serve(once=True)
        completed = self.store.get_task(task["id"])
        assert completed is not None
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["report_id"])
        self.assertTrue((self.reports / completed["report_filename"]).is_file())

    def test_clarification_pauses_without_report(self) -> None:
        task, _ = self.store.create_task(prompt="整理指定主题", title="待补充")
        self.worker(
            FakeRunner(f"{NEEDS_INPUT_MARKER} 你希望关注哪个主题？")
        ).serve(once=True)
        waiting = self.store.get_task(task["id"])
        assert waiting is not None
        self.assertEqual(waiting["status"], "waiting_input")
        self.assertIn("哪个主题", waiting["question"])
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_image_task_creates_private_png_and_companion_report(self) -> None:
        task, _ = self.store.create_task(
            prompt="生成一张简洁的机器人插画",
            title="机器人插画",
            output_format="image",
        )
        self.worker(
            FakeRunner("runner should not be used"),
            image_generator=FakeImageGenerator(),
        ).serve(once=True)
        completed = self.store.get_task(task["id"])
        assert completed is not None
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["artifact_media_type"], "image/png")
        artifact = (
            self.reports
            / ".task-artifacts"
            / task["id"]
            / completed["artifact_filename"]
        )
        self.assertEqual(artifact.read_bytes(), b"\x89PNG\r\n\x1a\nriverbank-test")

    def test_illustrated_task_creates_self_contained_html(self) -> None:
        task, _ = self.store.create_task(
            prompt="整理机器人新闻并配图",
            title="机器人新闻",
            output_format="illustrated",
        )
        self.worker(
            FakeRunner("报告已经生成。", create_report=True),
            image_generator=FakeImageGenerator(),
        ).serve(once=True)
        completed = self.store.get_task(task["id"])
        assert completed is not None
        self.assertEqual(completed["artifact_media_type"], "text/html")
        artifact = (
            self.reports
            / ".task-artifacts"
            / task["id"]
            / completed["artifact_filename"]
        )
        document = artifact.read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", document)
        self.assertIn("测试报告", document)


if __name__ == "__main__":
    unittest.main()
