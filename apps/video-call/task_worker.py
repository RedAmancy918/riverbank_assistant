#!/usr/bin/env python3
"""Execute RiverBank background tasks independently from the voice assistant."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from report_library import ReportLibrary
from task_store import TaskStore


LOGGER = logging.getLogger("riverbank-task-worker")
NEEDS_INPUT_MARKER = "[[RIVERBANK_NEEDS_INPUT]]"
DEFAULT_DB_PATH = Path("/var/lib/riverbank-tasks/tasks.db")
DEFAULT_REPORTS_DIR = Path(
    "/home/geo/.hermes/profiles/daily/workspace/reports"
)
DEFAULT_WORKSPACE = Path("/home/geo/.hermes/profiles/daily/workspace")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def safe_report_stem(title: str, task_id: str) -> str:
    normalized = re.sub(r"[^\w\u3400-\u9fff-]+", "-", title, flags=re.UNICODE)
    normalized = normalized.strip("-_")[:48] or "task-report"
    return f"{datetime.now().astimezone():%Y-%m-%d_%H%M}_{normalized}_{task_id[:8]}"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def render_fallback_report(task: dict[str, Any], response: str) -> str:
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    answer_lines = []
    for item in task.get("answers") or []:
        answer_lines.extend(
            [
                f"- 问：{item.get('question', '')}",
                f"  答：{item.get('answer', '')}",
            ]
        )
    answers = "\n".join(answer_lines) if answer_lines else "- 无"
    return (
        f"# {task['title']}\n\n"
        f"- 生成时间：{generated}\n"
        f"- 任务 ID：`{task['id']}`\n"
        f"- 来源：{task.get('source') or 'unknown'}\n\n"
        "## 原始任务\n\n"
        f"{task['prompt']}\n\n"
        "## 补充信息\n\n"
        f"{answers}\n\n"
        "## 执行结果\n\n"
        f"{response.strip() or 'Hermes 未返回正文。'}\n"
    )


def build_prompt(task: dict[str, Any], report_path: Path) -> str:
    answers = task.get("answers") or []
    answer_text = "\n".join(
        f"- 问题：{item.get('question', '')}\n  用户补充：{item.get('answer', '')}"
        for item in answers
    ) or "- 暂无"
    return f"""
你是 RiverBank Assistant 的后台任务执行 Agent。该任务来自已鉴权设备，执行不依赖客户端保持在线。

[任务]
标题：{task['title']}
类型：{task['kind']}
原始要求：
{task['prompt']}

[已有补充]
{answer_text}

[执行规范]
1. 对会变化的事实主动使用 web 或 browser 核实，并保留可点击的来源链接与查询时间。
2. 清楚区分来源事实、分析和推断；不得虚构已经查询或已经完成的操作。
3. 可以使用 file 工具整理并生成文件，但只能为这项任务写入下面指定的 Markdown 报告；不要删除或覆盖其他文件，不要修改系统配置。
4. 若当前信息足以执行，完整结果必须保存到：{report_path}
5. Markdown 第一行是一级标题，并包含生成时间、摘要、结构化正文、来源。最终回复只概括完成情况和报告标题。
6. 只有缺少一项会实质改变结果且无法合理假设的信息时，才不要创建报告，并让最终回复严格以 {NEEDS_INPUT_MARKER} 开头，后面只写一个具体问题。
7. 尽量自主完成，不要为了偏好性细节反复追问。
""".strip()


class HermesProcessRunner:
    def __init__(
        self,
        *,
        hermes_bin: Path,
        workspace: Path,
        timeout_seconds: float,
        toolsets: str,
    ) -> None:
        self.hermes_bin = hermes_bin
        self.workspace = workspace
        self.timeout_seconds = max(60.0, timeout_seconds)
        self.toolsets = toolsets

    def run(self, task: dict[str, Any], report_path: Path, store: TaskStore) -> str:
        command = [
            str(self.hermes_bin),
            "chat",
            "--query",
            build_prompt(task, report_path),
            "--quiet",
            "--toolsets",
            self.toolsets,
            "--reasoning",
            "medium",
            "--max-turns",
            "32",
            "--source",
            "riverbank-task",
            "--yolo",
            "--in",
            str(self.workspace),
        ]
        environment = os.environ.copy()
        environment.setdefault("HERMES_ACCEPT_HOOKS", "1")
        environment.setdefault("HERMES_YOLO_MODE", "1")
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        started = time.monotonic()
        output = ""
        while True:
            if store.is_cancel_requested(task["id"]):
                self.terminate(process)
                raise InterruptedError("task cancelled")
            elapsed = time.monotonic() - started
            if elapsed >= self.timeout_seconds:
                self.terminate(process)
                raise TimeoutError(
                    f"Hermes task exceeded {self.timeout_seconds / 60:.0f} minutes"
                )
            try:
                output, _ = process.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if elapsed > 5:
                    store.update_progress(
                        task["id"],
                        min(0.82, 0.15 + elapsed / self.timeout_seconds * 0.67),
                        "Hermes 正在检索与整理",
                    )
        cleaned = ANSI_RE.sub("", output).strip()
        if process.returncode != 0:
            tail = cleaned[-4000:] if cleaned else f"exit code {process.returncode}"
            raise RuntimeError(f"Hermes failed: {tail}")
        if not cleaned and not report_path.is_file():
            raise RuntimeError("Hermes returned no response and no report")
        return cleaned

    @staticmethod
    def terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class TaskWorker:
    def __init__(
        self,
        *,
        store: TaskStore,
        runner: HermesProcessRunner,
        reports_dir: Path,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.runner = runner
        self.reports_dir = reports_dir
        self.report_library = ReportLibrary(reports_dir)
        self.poll_seconds = max(0.2, poll_seconds)
        self.running = True

    def stop(self, _signum: int | None = None, _frame: object = None) -> None:
        self.running = False

    def process(self, task: dict[str, Any]) -> None:
        report_path = self.reports_dir / f"{safe_report_stem(task['title'], task['id'])}.md"
        LOGGER.info("task started id=%s title=%s", task["id"], task["title"])
        try:
            self.store.update_progress(task["id"], 0.1, "正在启动 Hermes")
            response = self.runner.run(task, report_path, self.store)
            if self.store.is_cancel_requested(task["id"]):
                self.store.mark_cancelled(task["id"])
                return
            marker_index = response.find(NEEDS_INPUT_MARKER)
            if marker_index >= 0 and not report_path.is_file():
                question = response[marker_index + len(NEEDS_INPUT_MARKER) :].strip()
                self.store.wait_for_input(task["id"], question or "请补充任务所需信息。")
                LOGGER.info("task waiting input id=%s", task["id"])
                return
            if not report_path.is_file():
                atomic_write_text(report_path, render_fallback_report(task, response))
            self.store.update_progress(task["id"], 0.92, "正在归档报告")
            relative = report_path.resolve().relative_to(self.reports_dir.resolve()).as_posix()
            report_id = self.report_library.encode_id(relative)
            report_title, report_summary = self.report_library.extract_title_and_summary(
                report_path
            )
            summary = report_summary or response[-2_000:].strip()
            if report_title and summary and not summary.startswith(report_title):
                summary = f"{report_title}：{summary}"
            self.store.complete(
                task["id"],
                summary=summary,
                report_id=report_id,
                report_filename=report_path.name,
            )
            LOGGER.info("task completed id=%s report=%s", task["id"], report_path)
        except InterruptedError:
            self.store.mark_cancelled(task["id"])
            LOGGER.info("task cancelled id=%s", task["id"])
        except Exception as exc:
            LOGGER.exception("task failed id=%s", task["id"])
            self.store.fail(task["id"], str(exc))

    def serve(self, *, once: bool = False) -> int:
        recovered = self.store.recover_interrupted()
        if recovered:
            LOGGER.warning("recovered %s interrupted task(s)", recovered)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        while self.running:
            task = self.store.claim_next()
            if task is None:
                if once:
                    return 0
                time.sleep(self.poll_seconds)
                continue
            self.process(task)
            if once:
                return 0
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--hermes-bin",
        type=Path,
        default=Path("/home/geo/.local/bin/hermes"),
    )
    parser.add_argument(
        "--toolsets",
        default="browser,file,skills,web",
    )
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = TaskStore(args.db)
    if args.self_test:
        print(
            json.dumps(
                {
                    "ok": True,
                    "schema": "riverbank.task-worker/v1",
                    "db": str(args.db),
                    "reports_dir": str(args.reports_dir),
                    "workspace": str(args.workspace),
                    "hermes_bin": str(args.hermes_bin),
                    "counts": store.counts(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    worker = TaskWorker(
        store=store,
        runner=HermesProcessRunner(
            hermes_bin=args.hermes_bin,
            workspace=args.workspace,
            timeout_seconds=args.timeout,
            toolsets=args.toolsets,
        ),
        reports_dir=args.reports_dir,
        poll_seconds=args.poll,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    return worker.serve(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
