#!/usr/bin/env python3
"""Same-origin proxy from Paper Radar to the persistent RiverBank chat worker."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
QA_DIR = ROOT / "data" / "paper-qa"
CURRENT_KNOWLEDGE = QA_DIR / "current.json"
CONVERSATION_MAP = QA_DIR / "conversations.json"
PAPER_SOURCE = "paper-radar-internal"
PAPER_ID_RE = re.compile(r"^[a-f0-9]{20}$")


class PaperChatError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


class PaperChatProxy:
    def __init__(
        self,
        *,
        backend_url: str = "http://127.0.0.1:19734",
        token_file: Path = Path("/home/geo/.config/riverbank-video-call/token"),
        knowledge_path: Path = CURRENT_KNOWLEDGE,
        mapping_path: Path = CONVERSATION_MAP,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.token_file = token_file
        self.knowledge_path = knowledge_path
        self.mapping_path = mapping_path
        self.lock = threading.RLock()
        self.opener = build_opener(ProxyHandler({}))

    def validate_id(self, paper_id: str) -> str:
        value = str(paper_id or "").strip().lower()
        if not PAPER_ID_RE.fullmatch(value):
            raise PaperChatError("论文标识无效", status=400)
        return value

    def load_knowledge(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PaperChatError("当天论文知识库尚未生成", status=503) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperChatError("当天论文知识库暂时不可用", status=503) from exc
        return payload if isinstance(payload, dict) else {}

    def current_paper(self, paper_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        paper_id = self.validate_id(paper_id)
        knowledge = self.load_knowledge()
        papers = knowledge.get("papers", {})
        paper = papers.get(paper_id) if isinstance(papers, dict) else None
        if not isinstance(paper, dict):
            raise PaperChatError(
                "这篇文章不在当前日报知识库中；历史问答仍会保留",
                status=409,
            )
        return knowledge, paper

    def load_mapping(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            payload = {}
        except OSError as exc:
            raise PaperChatError("文章对话索引暂时不可用", status=503) from exc
        conversations = payload.get("conversations") if isinstance(payload, dict) else None
        return conversations if isinstance(conversations, dict) else {}

    def save_mapping(self, conversations: dict[str, Any]) -> None:
        atomic_write_json(
            self.mapping_path,
            {
                "schema": "riverbank.paper-conversation-map/v1",
                "updated_at": time.time(),
                "conversations": conversations,
            },
        )

    def read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PaperChatError("模型对话服务的配对令牌不可用", status=503) from exc
        if len(token) < 16:
            raise PaperChatError("模型对话服务的配对令牌无效", status=503)
        return token

    def backend_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        payload = None
        headers = {
            "Authorization": f"Bearer {self.read_token()}",
            "Accept": "application/json",
        }
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.backend_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(2_000_000)
        except HTTPError as exc:
            detail = exc.read(16_000).decode("utf-8", errors="replace").strip()
            raise PaperChatError(detail or f"模型对话服务返回 {exc.code}", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PaperChatError("模型对话服务暂时不可用", status=503) from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaperChatError("模型对话服务返回了无效数据", status=502) from exc
        if not isinstance(result, dict):
            raise PaperChatError("模型对话服务返回格式无效", status=502)
        return result

    def mapped_conversation(self, paper_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.load_mapping().get(self.validate_id(paper_id))
        return record if isinstance(record, dict) else None

    def ensure_conversation(self, paper_id: str, paper: dict[str, Any]) -> str:
        paper_id = self.validate_id(paper_id)
        with self.lock:
            conversations = self.load_mapping()
            existing = conversations.get(paper_id)
            if isinstance(existing, dict) and existing.get("conversation_id"):
                return str(existing["conversation_id"])
            response = self.backend_request(
                "/api/v1/chats",
                method="POST",
                body={
                    "title": str(paper.get("title", ""))[:120],
                    "source": PAPER_SOURCE,
                    "device_name": paper_id,
                },
            )
            conversation = response.get("conversation", {})
            conversation_id = str(conversation.get("id", ""))
            if not conversation_id:
                raise PaperChatError("无法建立文章对话", status=502)
            conversations[paper_id] = {
                "conversation_id": conversation_id,
                "title": str(paper.get("title", "")),
                "url": str(paper.get("url", "")),
                "created_at": time.time(),
            }
            self.save_mapping(conversations)
            return conversation_id

    def history(self, paper_id: str) -> dict[str, Any]:
        paper_id = self.validate_id(paper_id)
        try:
            knowledge, paper = self.current_paper(paper_id)
            available = True
            report_date = str(knowledge.get("date", ""))
        except PaperChatError as exc:
            if exc.status not in {409, 503}:
                raise
            paper = {}
            available = False
            report_date = ""
        mapping = self.mapped_conversation(paper_id)
        messages: list[dict[str, Any]] = []
        conversation_id = ""
        if mapping:
            conversation_id = str(mapping.get("conversation_id", ""))
            response = self.backend_request(
                f"/api/v1/chats/{quote(conversation_id, safe='')}/messages"
            )
            messages = response.get("messages", [])
            if not isinstance(messages, list):
                messages = []
            if not paper:
                paper = {
                    "title": str(mapping.get("title", "")),
                    "url": str(mapping.get("url", "")),
                }
        return {
            "ok": True,
            "paper_id": paper_id,
            "paper": {
                "title": str(paper.get("title", "")),
                "url": str(paper.get("url", "")),
                "reading_depth": str(paper.get("reading_depth", "")),
                "source_state": str(paper.get("source_state", "")),
            },
            "report_date": report_date,
            "knowledge_available": available,
            "conversation_id": conversation_id,
            "messages": messages,
        }

    def enqueue(self, paper_id: str, content: Any) -> dict[str, Any]:
        knowledge, paper = self.current_paper(paper_id)
        question = str(content or "").replace("\x00", "").strip()
        if not question:
            raise PaperChatError("问题不能为空", status=400)
        if len(question) > 4_000:
            raise PaperChatError("单次问题不能超过 4000 个字符", status=400)
        conversation_id = self.ensure_conversation(paper_id, paper)
        response = self.backend_request(
            f"/api/v1/chats/{quote(conversation_id, safe='')}/messages",
            method="POST",
            body={"content": question},
        )
        return {
            "ok": True,
            "paper_id": paper_id,
            "report_date": str(knowledge.get("date", "")),
            "conversation_id": conversation_id,
            "user_message": response.get("user_message"),
            "assistant_message": response.get("assistant_message"),
        }
