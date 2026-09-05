#!/usr/bin/env python3
"""Enrich one current-day paper buffer from its controlled primary source."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from arxiv_access import ArxivAccess
from paper_qa import (
    ARXIV_RE,
    CURRENT_PATH,
    MAX_CHUNKS,
    atomic_write_json,
    clean_text,
    fetch_arxiv_source,
    source_chunks,
    text_chunks,
)


PDF_SOURCE = "arxiv_pdf_on_demand"
PAPER_ID_RE = re.compile(r"^[a-f0-9]{20}$")
MAX_PDF_BYTES = 35 * 1024 * 1024
MAX_PDF_PAGES = 120
MAX_PDF_TEXT_CHARS = 450_000


def download_arxiv_pdf(arxiv_id: str) -> bytes:
    payload = ArxivAccess().fetch_bytes(
        f"https://arxiv.org/pdf/{arxiv_id}",
        timeout=90,
        max_bytes=MAX_PDF_BYTES,
        headers={"Accept": "application/pdf"},
    )
    if not payload.body.lstrip().startswith(b"%PDF-"):
        raise RuntimeError("原文地址没有返回有效 PDF")
    return payload.body


def extract_pdf_chunks(payload: bytes) -> list[dict[str, str]]:
    executable = os.environ.get("PAPER_RADAR_PDFTOTEXT", "/usr/bin/pdftotext")
    with tempfile.TemporaryDirectory(prefix="riverbank-paper-") as directory:
        source = Path(directory) / "paper.pdf"
        source.write_bytes(payload)
        try:
            result = subprocess.run(
                [
                    executable,
                    "-f",
                    "1",
                    "-l",
                    str(MAX_PDF_PAGES),
                    "-layout",
                    str(source),
                    "-",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("无法安全提取论文 PDF 正文") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail[:300] or "论文 PDF 正文提取失败")
    text = result.stdout.decode("utf-8", errors="replace")[:MAX_PDF_TEXT_CHARS]
    chunks: list[dict[str, str]] = []
    for page_number, page in enumerate(text.split("\f"), 1):
        cleaned = clean_text(page)
        if not cleaned:
            continue
        for index, part in enumerate(text_chunks(cleaned), 1):
            label = f"PDF 第 {page_number} 页"
            if index > 1:
                label += f"（续 {index}）"
            chunks.append({"label": label, "text": part, "source": PDF_SOURCE})
            if len(chunks) >= MAX_CHUNKS:
                return chunks
    if not chunks:
        raise RuntimeError("论文 PDF 未提取到可读正文")
    return chunks


def _full_source_count(knowledge: dict[str, Any]) -> int:
    papers = knowledge.get("papers", {})
    if not isinstance(papers, dict):
        return 0
    return sum(
        any(
            isinstance(chunk, dict)
            and str(chunk.get("source", "")).startswith("arxiv_")
            for chunk in paper.get("chunks", [])
        )
        for paper in papers.values()
        if isinstance(paper, dict)
    )


def enrich_current_paper(
    paper_id: str,
    *,
    knowledge_path: Path = CURRENT_PATH,
    pdf_loader: Callable[[str], bytes] | None = None,
    pdf_extractor: Callable[[bytes], list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    value = str(paper_id or "").strip().lower()
    if not PAPER_ID_RE.fullmatch(value):
        raise ValueError("论文标识无效")
    pdf_loader = pdf_loader or download_arxiv_pdf
    pdf_extractor = pdf_extractor or extract_pdf_chunks
    knowledge_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = knowledge_path.with_suffix(knowledge_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("当天论文知识库暂时不可用") from exc
        papers = knowledge.get("papers", {})
        paper = papers.get(value) if isinstance(papers, dict) else None
        if not isinstance(paper, dict):
            raise LookupError("这篇文章不在当前日报知识库中")
        existing = [item for item in paper.get("chunks", []) if isinstance(item, dict)]
        if any(item.get("source") == PDF_SOURCE for item in existing):
            return {"ok": True, "changed": False, "source_state": PDF_SOURCE}
        arxiv_id = str(paper.get("arxiv_id", "")).strip()
        if not ARXIV_RE.fullmatch(arxiv_id):
            raise LookupError("当前文章没有可受控读取的 arXiv 原文")

        timestamp = datetime.now().astimezone().isoformat()
        error = ""
        try:
            enriched = pdf_extractor(pdf_loader(arxiv_id))
            source_state = PDF_SOURCE
        except Exception as pdf_exc:
            error = clean_text(pdf_exc)[:500]
            existing_html = [item for item in existing if item.get("source") == "arxiv_html"]
            if existing_html:
                enriched = existing_html
                source_state = "arxiv_html"
            else:
                try:
                    source = fetch_arxiv_source(arxiv_id, requests.Session())
                    enriched = source_chunks(source)
                    if not enriched:
                        raise RuntimeError("arXiv HTML 未提取到可读正文")
                    source_state = "arxiv_html"
                except Exception as html_exc:
                    paper["source_error"] = clean_text(f"PDF: {pdf_exc}; HTML: {html_exc}")[:500]
                    paper["on_demand"] = {
                        "status": "failed",
                        "attempted_at": timestamp,
                        "target": "original_paper",
                    }
                    atomic_write_json(knowledge_path, knowledge)
                    return {
                        "ok": False,
                        "changed": False,
                        "source_state": str(paper.get("source_state", "daily_report")),
                        "error": paper["source_error"],
                    }

        daily = [item for item in existing if item.get("source") == "daily_report"]
        paper["chunks"] = (daily + enriched)[:MAX_CHUNKS]
        paper["source_state"] = source_state
        paper["source_error"] = error
        paper["on_demand"] = {
            "status": "partial" if error else "ready",
            "level": "full_pdf" if source_state == PDF_SOURCE else "full_html",
            "enriched_at": timestamp,
            "target": "original_paper",
            "retention": "replace_on_next_successful_daily_run",
        }
        if error:
            paper["on_demand"]["pdf_error"] = error
        knowledge["full_source_count"] = _full_source_count(knowledge)
        knowledge["last_enriched_at"] = timestamp
        atomic_write_json(knowledge_path, knowledge)
        return {
            "ok": True,
            "changed": True,
            "source_state": source_state,
            "chunk_count": len(paper["chunks"]),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_id")
    parser.add_argument("--knowledge", type=Path, default=CURRENT_PATH)
    args = parser.parse_args()
    try:
        result = enrich_current_paper(args.paper_id, knowledge_path=args.knowledge)
    except Exception as exc:
        result = {"ok": False, "changed": False, "error": clean_text(exc)[:500]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
