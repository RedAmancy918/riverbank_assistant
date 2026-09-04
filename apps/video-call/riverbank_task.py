#!/usr/bin/env python3
"""Submit and manage RiverBank background tasks from a terminal."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(
    os.environ.get(
        "RIVERBANK_TASK_CONFIG",
        Path.home() / ".config/riverbank-task/config.json",
    )
)
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class APIError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (OSError, ValueError) as exc:
        raise APIError(f"cannot read config {path}: {exc}") from exc
    return {
        "server": os.environ.get("RIVERBANK_SERVER_URL", "").strip()
        or str(payload.get("server") or "http://riverbank-tech:19734").strip(),
        "token": os.environ.get("RIVERBANK_PAIRING_TOKEN", "").strip()
        or str(payload.get("token") or "").strip(),
    }


def save_config(path: Path, server: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"server": server.rstrip("/"), "token": token},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class RiverBankClient:
    def __init__(self, server: str, token: str, timeout: float = 30.0) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        if not self.server.startswith(("http://", "https://")):
            raise APIError("server must begin with http:// or https://")
        if not self.token:
            raise APIError("pairing token is missing; run configure first")

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        body = None
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "riverbank-task-cli/0.20",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.server + path,
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
                if raw:
                    return data, response.headers
                return json.loads(data.decode("utf-8")) if data else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise APIError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise APIError(f"request failed: {exc}") from exc


def render_task(task: dict[str, Any], *, detail: bool = False) -> str:
    progress = int(float(task.get("progress") or 0) * 100)
    lines = [
        f"{task.get('id')}  [{task.get('status')}] {progress:3d}%  {task.get('title')}",
        f"  {task.get('status_message') or ''}",
    ]
    if detail:
        lines.extend(
            [
                f"  类型: {task.get('kind')}  来源: {task.get('source')}",
                f"  成果: {task.get('output_format') or 'text'}",
                f"  创建: {task.get('created_at')}  更新: {task.get('updated_at')}",
                f"  任务: {task.get('prompt')}",
            ]
        )
        if task.get("question"):
            lines.append(f"  需要补充: {task['question']}")
        if task.get("report_filename"):
            lines.append(f"  报告: {task['report_filename']}")
        if task.get("artifact_filename"):
            lines.append(f"  成果文件: {task['artifact_filename']}")
        if task.get("result_summary"):
            lines.append(f"  结果: {task['result_summary']}")
        if task.get("error"):
            lines.append(f"  错误: {task['error']}")
    return "\n".join(lines)


def wait_for_task(client: RiverBankClient, task_id: str, interval: float) -> dict[str, Any]:
    last_status = ""
    while True:
        task = client.request("GET", f"/api/v1/tasks/{task_id}")["task"]
        if task["status"] != last_status:
            print(render_task(task), flush=True)
            last_status = task["status"]
        if task["status"] in TERMINAL_STATUSES or task["status"] == "waiting_input":
            return task
        time.sleep(max(0.5, interval))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="save server and pairing token")
    configure.add_argument("--server", required=True)
    token_group = configure.add_mutually_exclusive_group(required=True)
    token_group.add_argument("--token")
    token_group.add_argument("--token-file", type=Path)

    submit = subparsers.add_parser("submit", help="submit an asynchronous task")
    submit.add_argument("prompt", nargs="?")
    submit.add_argument("--file", type=Path, help="read task prompt from UTF-8 file")
    submit.add_argument("--title", default="")
    submit.add_argument("--kind", choices=("research", "general", "file"), default="research")
    submit.add_argument(
        "--output-format",
        choices=("text", "image", "illustrated"),
        default="text",
        help="deliver Markdown text, PNG image, or a self-contained illustrated HTML file",
    )
    submit.add_argument("--device", default="terminal")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--poll", type=float, default=2.0)

    listing = subparsers.add_parser("list", help="list recent tasks")
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument(
        "--status",
        choices=("queued", "running", "waiting_input", "completed", "failed", "cancelled"),
        default="",
    )

    status = subparsers.add_parser("status", help="show one task")
    status.add_argument("task_id")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--poll", type=float, default=2.0)

    answer = subparsers.add_parser("answer", help="answer a task clarification")
    answer.add_argument("task_id")
    answer.add_argument("answer")

    cancel = subparsers.add_parser("cancel", help="cancel a task")
    cancel.add_argument("task_id")

    reports = subparsers.add_parser("reports", help="list generated reports")
    reports.add_argument("--limit", type=int, default=30)

    download = subparsers.add_parser("download", help="download a report or task result")
    download.add_argument("identifier", help="report ID, or task ID with --task")
    download.add_argument("--task", action="store_true")
    download.add_argument("--output", type=Path)
    return parser


def read_prompt(args: argparse.Namespace) -> str:
    if args.file:
        if args.prompt:
            raise APIError("use either positional prompt or --file, not both")
        try:
            return args.file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise APIError(f"cannot read prompt file: {exc}") from exc
    return str(args.prompt or "").strip()


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "configure":
        token = args.token or ""
        if args.token_file:
            try:
                token = args.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                print(f"错误：无法读取令牌文件：{exc}", file=sys.stderr)
                return 2
        save_config(args.config, args.server, token)
        print(f"配置已保存到 {args.config}")
        return 0
    try:
        config = load_config(args.config)
        client = RiverBankClient(config["server"], config["token"])
        if args.command == "submit":
            prompt = read_prompt(args)
            if not prompt:
                raise APIError("task prompt cannot be empty")
            payload = client.request(
                "POST",
                "/api/v1/tasks",
                {
                    "prompt": prompt,
                    "title": args.title,
                    "kind": args.kind,
                    "output_format": args.output_format,
                    "source": "terminal",
                    "device_name": args.device,
                },
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            task = payload["task"]
            if args.json:
                print_json(payload)
            else:
                print(render_task(task, detail=True))
            if args.wait:
                task = wait_for_task(client, task["id"], args.poll)
                if args.json:
                    print_json(task)
                return 0 if task["status"] in {"completed", "waiting_input"} else 2
            return 0
        if args.command == "list":
            query = urllib.parse.urlencode({"limit": args.limit, "status": args.status})
            payload = client.request("GET", f"/api/v1/tasks?{query}")
            if args.json:
                print_json(payload)
            else:
                for task in payload["tasks"]:
                    print(render_task(task))
            return 0
        if args.command == "status":
            if args.watch:
                task = wait_for_task(client, args.task_id, args.poll)
            else:
                task = client.request("GET", f"/api/v1/tasks/{args.task_id}")["task"]
            print_json(task) if args.json else print(render_task(task, detail=True))
            return 0
        if args.command == "answer":
            task = client.request(
                "POST",
                f"/api/v1/tasks/{args.task_id}/answer",
                {"answer": args.answer},
            )["task"]
            print_json(task) if args.json else print(render_task(task, detail=True))
            return 0
        if args.command == "cancel":
            task = client.request(
                "POST",
                f"/api/v1/tasks/{args.task_id}/cancel",
                {},
            )["task"]
            print_json(task) if args.json else print(render_task(task, detail=True))
            return 0
        if args.command == "reports":
            payload = client.request("GET", f"/api/v1/reports?limit={args.limit}")
            if args.json:
                print_json(payload)
            else:
                for report in payload["reports"]:
                    print(f"{report['id']}  {report['modified_at']}  {report['title']}")
            return 0
        if args.command == "download":
            report_id = args.identifier
            artifact_task_id = ""
            if args.task:
                task = client.request("GET", f"/api/v1/tasks/{report_id}")["task"]
                if task.get("artifact_filename"):
                    artifact_task_id = str(task["id"])
                report_id = str(task.get("report_id") or "")
                if not report_id and not artifact_task_id:
                    raise APIError("task has no report")
            if artifact_task_id:
                data, headers = client.request(
                    "GET",
                    f"/api/v1/tasks/{urllib.parse.quote(artifact_task_id)}/artifact",
                    raw=True,
                )
            else:
                data, headers = client.request(
                    "GET",
                    f"/api/v1/reports/{urllib.parse.quote(report_id)}/download",
                    raw=True,
                )
            output = args.output
            if output is None:
                disposition = headers.get("Content-Disposition", "")
                match = __import__("re").search(r"filename\*=UTF-8''([^;]+)", disposition)
                filename = urllib.parse.unquote(match.group(1)) if match else "report.md"
                output = Path(filename).name and Path(filename) or Path("report.md")
            output.write_bytes(data)
            print(f"已保存：{output.resolve()}")
            return 0
    except APIError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
