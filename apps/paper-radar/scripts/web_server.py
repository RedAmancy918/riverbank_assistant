#!/usr/bin/env python3
"""Serve Paper Radar and accept one-shot special-focus requests."""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from arxiv_access import ArxivAccess
from paper_chat_proxy import PaperChatError, PaperChatProxy
from special_focus import (
    cancel_request,
    clear_unconsumed_requests,
    queue_status,
    submit_request,
    update_request,
)


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
CONFIG_PATH = ROOT / "config" / "topics.json"
MAX_REQUEST_BYTES = 8192
PAPER_CHAT_PATH_RE = re.compile(r"^/api/paper-chat/([a-f0-9]{20})$")


def load_timezone() -> str:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(config.get("timezone", "Asia/Shanghai"))
    except (OSError, json.JSONDecodeError):
        return "Asia/Shanghai"


class PaperRadarHandler(SimpleHTTPRequestHandler):
    server_version = "PaperRadar/1.0"
    protocol_version = "HTTP/1.1"

    def api_path(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    def paper_chat_id(self) -> str:
        match = PAPER_CHAT_PATH_RE.fullmatch(self.api_path())
        return match.group(1) if match else ""

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json(status, {"ok": False, "error": message})

    def same_origin_request(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        return fetch_site in {"", "none", "same-origin", "same-site"}

    def read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise ValueError("请求必须使用 application/json")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON 内容无效") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON 顶层必须是对象")
        return payload

    def do_GET(self) -> None:
        if self.api_path() == "/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True, "service": "paper-radar"})
            return
        if self.api_path() == "/api/source-status":
            access = ArxivAccess()
            source = access.read_source_status()
            state = access.status()
            cooldown_active = bool(state.get("cooldown_active"))
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "state": str(source.get("state") or "unknown"),
                    "message": str(source.get("message") or ""),
                    "attempted_report_date": str(source.get("attempted_report_date") or ""),
                    "latest_report_date": str(source.get("latest_report_date") or ""),
                    "last_successful_report_date": str(source.get("last_successful_report_date") or ""),
                    "paper_source_date": str(source.get("paper_source_date") or ""),
                    "report_ready": bool(source.get("report_ready")),
                    "retry_at": (
                        str(state.get("cooldown_until") or source.get("retry_at") or "")
                        if cooldown_active
                        else ""
                    ),
                    "automatic_attempts": int(source.get("automatic_attempts") or 0),
                },
            )
            return
        paper_id = self.paper_chat_id()
        if paper_id:
            try:
                payload = self.server.paper_chat.history(paper_id)  # type: ignore[attr-defined]
            except PaperChatError as exc:
                self.send_error_json(HTTPStatus(exc.status), str(exc))
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        if self.api_path() == "/api/special-focus":
            try:
                state = queue_status(self.server.timezone_name)  # type: ignore[attr-defined]
            except Exception as exc:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_json(HTTPStatus.OK, {"ok": True, **state})
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self.api_path() == "/healthz":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.api_path() in {"/api/special-focus", "/api/source-status"}:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.paper_chat_id():
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        paper_id = self.paper_chat_id()
        is_special_focus = self.api_path() == "/api/special-focus"
        if not paper_id and not is_special_focus:
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if not self.same_origin_request():
            self.send_error_json(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
            return
        if paper_id:
            try:
                payload = self.read_json_body()
                result = self.server.paper_chat.enqueue(  # type: ignore[attr-defined]
                    paper_id,
                    payload.get("content"),
                )
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except PaperChatError as exc:
                self.send_error_json(HTTPStatus(exc.status), str(exc))
                return
            self.send_json(HTTPStatus.ACCEPTED, result)
            return
        try:
            payload = self.read_json_body()
            request = submit_request(
                str(payload.get("description", "")),
                self.server.timezone_name,  # type: ignore[attr-defined]
            )
            state = queue_status(self.server.timezone_name)  # type: ignore[attr-defined]
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        self.send_json(HTTPStatus.CREATED, {"ok": True, "request": request, **state})

    def do_DELETE(self) -> None:
        if self.api_path() != "/api/special-focus":
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if not self.same_origin_request():
            self.send_error_json(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
            return
        try:
            payload = self.read_json_body()
            clear_all = payload.get("clear_all") is True
            if clear_all:
                cleared_count = clear_unconsumed_requests(
                    self.server.timezone_name,  # type: ignore[attr-defined]
                )
                changed = True
            else:
                request_id = str(payload.get("id", "")).strip()
                if not request_id:
                    raise ValueError("缺少待取消请求的 id")
                changed = cancel_request(
                    request_id,
                    self.server.timezone_name,  # type: ignore[attr-defined]
                )
                cleared_count = 1 if changed else 0
            state = queue_status(self.server.timezone_name)  # type: ignore[attr-defined]
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if not changed:
            self.send_error_json(HTTPStatus.CONFLICT, "该请求已不存在或已被处理")
            return
        self.send_json(
            HTTPStatus.OK,
            {"ok": True, "cleared_count": cleared_count, **state},
        )

    def do_PATCH(self) -> None:
        if self.api_path() != "/api/special-focus":
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if not self.same_origin_request():
            self.send_error_json(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
            return
        try:
            payload = self.read_json_body()
            request_id = str(payload.get("id", "")).strip()
            if not request_id:
                raise ValueError("缺少待编辑请求的 id")
            request = update_request(
                request_id,
                str(payload.get("description", "")),
                self.server.timezone_name,  # type: ignore[attr-defined]
            )
            if request is None:
                self.send_error_json(HTTPStatus.CONFLICT, "该请求已不存在或已被处理")
                return
            state = queue_status(self.server.timezone_name)  # type: ignore[attr-defined]
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        self.send_json(HTTPStatus.OK, {"ok": True, "request": request, **state})

    def do_OPTIONS(self) -> None:
        self.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "不支持跨站预检请求")

    def list_directory(self, path: str) -> None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def end_headers(self) -> None:
        if not self.api_path().startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def log_message(self, format_string: str, *args: Any) -> None:
        if self.api_path() == "/healthz":
            return
        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format_string % args)
        )
        sys.stdout.flush()


class PaperRadarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: Any,
        timezone_name: str,
        paper_chat: PaperChatProxy,
    ):
        self.timezone_name = timezone_name
        self.paper_chat = paper_chat
        super().__init__(address, handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19732)
    parser.add_argument("--chat-backend", default="http://127.0.0.1:19734")
    parser.add_argument(
        "--chat-token-file",
        type=Path,
        default=Path("/home/geo/.config/riverbank-video-call/token"),
    )
    args = parser.parse_args()
    handler = partial(PaperRadarHandler, directory=str(PUBLIC))
    paper_chat = PaperChatProxy(
        backend_url=args.chat_backend,
        token_file=args.chat_token_file,
    )
    server = PaperRadarServer(
        (args.host, args.port),
        handler,
        load_timezone(),
        paper_chat,
    )
    print(
        f"Paper Radar listening on http://{args.host}:{args.port} "
        f"({server.timezone_name})",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
