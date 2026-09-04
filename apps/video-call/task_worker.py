#!/usr/bin/env python3
"""Execute RiverBank background tasks independently from the voice assistant."""

from __future__ import annotations

import argparse
import base64
import html
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
from urllib.parse import urlparse

from image_generation import DashScopeImageGenerator, GeneratedImage
from report_library import ReportLibrary
from task_artifact_store import DEFAULT_TASK_ARTIFACT_ROOT, TaskArtifactStore
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


def build_image_prompt(task: dict[str, Any], report_content: str = "") -> str:
    context = report_content.strip()[:5_000]
    context_block = f"\n以下是已经完成的内容提要，配图需与其事实一致：\n{context}" if context else ""
    return (
        "请为 RiverBank 用户任务生成一张原创、高完成度、可直接交付的图片。"
        "画面干净、主体明确、构图完整、无水印；除非任务明确要求，否则避免在图中堆叠文字。\n"
        f"标题：{task['title']}\n"
        f"用户要求：{task['prompt']}"
        f"{context_block}"
    )


LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^\s)]+)\)")


def render_inline_markdown(value: str) -> str:
    parts: list[str] = []
    offset = 0
    for match in LINK_RE.finditer(value):
        parts.append(html.escape(value[offset : match.start()]))
        url = match.group(2)
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            parts.append(
                f'<a href="{html.escape(url, quote=True)}">'
                f"{html.escape(match.group(1))}</a>"
            )
        else:
            parts.append(html.escape(match.group(0)))
        offset = match.end()
    parts.append(html.escape(value[offset:]))
    rendered = "".join(parts)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    return rendered


def markdown_to_safe_html(source: str) -> str:
    rendered: list[str] = []
    list_kind = ""
    in_code = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            rendered.append(f"</{list_kind}>")
            list_kind = ""

    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            close_list()
            if in_code:
                rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            rendered.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            continue
        unordered = re.match(r"^[-*+]\s+(.+)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if unordered or ordered:
            wanted = "ul" if unordered else "ol"
            if list_kind != wanted:
                close_list()
                list_kind = wanted
                rendered.append(f"<{wanted}>")
            item = (unordered or ordered).group(1)
            rendered.append(f"<li>{render_inline_markdown(item)}</li>")
            continue
        close_list()
        if stripped.startswith(">"):
            rendered.append(
                f"<blockquote>{render_inline_markdown(stripped[1:].strip())}</blockquote>"
            )
        elif stripped in {"---", "***"}:
            rendered.append("<hr>")
        else:
            rendered.append(f"<p>{render_inline_markdown(stripped)}</p>")
    close_list()
    if in_code:
        rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(rendered)


def render_illustrated_document(
    task: dict[str, Any],
    report_content: str,
    generated: GeneratedImage,
) -> bytes:
    image_data = base64.b64encode(generated.payload).decode("ascii")
    title = html.escape(str(task["title"]))
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light; --ink:#111712; --muted:#657068; --cyan:#28bfe8; --line:#d9dfda; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#f4f2eb; color:var(--ink); font:17px/1.75 -apple-system,BlinkMacSystemFont,"Noto Sans SC","PingFang SC",sans-serif; }}
main {{ width:min(920px,calc(100% - 32px)); margin:32px auto 80px; background:#fff; border:1px solid var(--line); box-shadow:0 18px 60px #18251a16; }}
header {{ padding:42px 7% 30px; border-bottom:1px solid var(--line); }}
.brand {{ color:#148bae; font-size:12px; font-weight:800; letter-spacing:.18em; }}
h1 {{ margin:.35em 0 .25em; font-size:clamp(30px,5vw,54px); line-height:1.12; }}
.meta {{ color:var(--muted); font-size:13px; }}
figure {{ margin:0; padding:0; background:#071014; }}
figure img {{ display:block; width:100%; max-height:720px; object-fit:contain; }}
figcaption {{ padding:10px 7%; color:#c5d5d8; background:#071014; font-size:12px; }}
article {{ padding:38px 7% 64px; }}
article h1 {{ font-size:34px; }} article h2 {{ margin-top:1.8em; font-size:25px; }} article h3 {{ margin-top:1.5em; font-size:20px; }}
p {{ margin:.8em 0; }} li {{ margin:.35em 0; }} a {{ color:#087d9d; }}
blockquote {{ margin:1.2em 0; padding:.5em 1em; border-left:4px solid var(--cyan); background:#effafd; }}
pre {{ overflow:auto; padding:18px; border-radius:12px; background:#081216; color:#d8edf2; }}
code {{ font-family:"SFMono-Regular",Consolas,monospace; }} hr {{ border:0; border-top:1px solid var(--line); margin:2em 0; }}
@media print {{ body {{ background:#fff; }} main {{ width:100%; margin:0; border:0; box-shadow:none; }} }}
</style>
</head>
<body><main>
<header><div class="brand">RIVERBANK</div><h1>{title}</h1><div class="meta">生成于 {html.escape(generated_at)}</div></header>
<figure><img src="data:{html.escape(generated.media_type, quote=True)};base64,{image_data}" alt="{title}"><figcaption>AI 生成配图 · {html.escape(generated.model)}</figcaption></figure>
<article>{markdown_to_safe_html(report_content)}</article>
</main></body></html>"""
    return document.encode("utf-8")


def build_prompt(task: dict[str, Any], report_path: Path) -> str:
    answers = task.get("answers") or []
    output_label = {
        "text": "文字报告",
        "image": "原创图片（同时保留内容依据）",
        "illustrated": "图文报告（同时生成原创配图）",
    }.get(str(task.get("output_format") or "text"), "文字报告")
    answer_text = "\n".join(
        f"- 问题：{item.get('question', '')}\n  用户补充：{item.get('answer', '')}"
        for item in answers
    ) or "- 暂无"
    return f"""
你是 RiverBank 旗下智能产品“小灰”的后台任务执行 Agent。该任务来自已鉴权设备，执行不依赖客户端保持在线。

[任务]
标题：{task['title']}
类型：{task['kind']}
最终成果：{output_label}
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
        artifacts_dir: Path | None = None,
        image_generator: DashScopeImageGenerator | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.runner = runner
        self.reports_dir = reports_dir
        self.report_library = ReportLibrary(reports_dir)
        self.artifact_store = TaskArtifactStore(
            artifacts_dir if artifacts_dir is not None else reports_dir / ".task-artifacts"
        )
        self.image_generator = image_generator or DashScopeImageGenerator()
        self.poll_seconds = max(0.2, poll_seconds)
        self.running = True

    def stop(self, _signum: int | None = None, _frame: object = None) -> None:
        self.running = False

    def process(self, task: dict[str, Any]) -> None:
        report_path = self.reports_dir / f"{safe_report_stem(task['title'], task['id'])}.md"
        output_format = str(task.get("output_format") or "text")
        artifact_filename = ""
        artifact_media_type = ""
        artifact_size_bytes = 0
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
            if output_format in {"image", "illustrated"}:
                status = "正在生成图片" if output_format == "image" else "正在生成配图与排版"
                self.store.update_progress(task["id"], 0.78, status)
                report_content = report_path.read_text(encoding="utf-8")
                generated = self.image_generator.generate(
                    build_image_prompt(task, report_content)
                )
                if output_format == "image":
                    artifact_filename = f"riverbank-image-{task['id'][:8]}.png"
                    artifact_payload = generated.payload
                    artifact_media_type = generated.media_type
                    generation_note = (
                        "\n\n## 生成成果\n\n"
                        "已生成可在任务详情中预览和下载的原创图片。\n\n"
                        f"- 图片模型：`{generated.model}`\n"
                        f"- 请求 ID：`{generated.request_id or '未提供'}`\n"
                    )
                    atomic_write_text(report_path, report_content.rstrip() + generation_note)
                else:
                    artifact_filename = f"riverbank-report-{task['id'][:8]}.html"
                    artifact_payload = render_illustrated_document(
                        task, report_content, generated
                    )
                    artifact_media_type = "text/html"
                artifact_path = self.artifact_store.save(
                    task["id"], artifact_filename, artifact_payload
                )
                artifact_size_bytes = artifact_path.stat().st_size
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
                artifact_filename=artifact_filename,
                artifact_media_type=artifact_media_type,
                artifact_size_bytes=artifact_size_bytes,
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
    parser.add_argument(
        "--artifacts-dir", type=Path, default=DEFAULT_TASK_ARTIFACT_ROOT
    )
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
                    "artifacts_dir": str(args.artifacts_dir),
                    "image_generation": DashScopeImageGenerator().available,
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
        artifacts_dir=args.artifacts_dir,
        poll_seconds=args.poll,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    return worker.serve(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
