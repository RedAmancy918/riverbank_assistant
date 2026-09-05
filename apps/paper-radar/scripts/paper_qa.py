#!/usr/bin/env python3
"""Build the replace-on-each-run knowledge buffer used by paper Q&A."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from arxiv_access import ArxivAccess


ROOT = Path(__file__).resolve().parents[1]
QA_DIR = ROOT / "data" / "paper-qa"
CURRENT_PATH = QA_DIR / "current.json"
TEMP_SOURCE_DIR = Path(os.environ.get("PAPER_RADAR_TEMP_SOURCE", "/tmp/paperradar"))
SCHEMA = "riverbank.paper-knowledge/v1"
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|html|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
SPACE_RE = re.compile(r"\s+")
SKIP_SECTION_RE = re.compile(r"references|bibliography|acknowledg", re.I)
MAX_SOURCE_CHARS = 320_000
MAX_CHUNKS = 180


def clean_text(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def arxiv_id_from_paper(paper: dict[str, Any]) -> str:
    for value in (paper.get("arxiv_id"), paper.get("html_url"), paper.get("url")):
        match = ARXIV_RE.search(str(value or ""))
        if match:
            return match.group(1)
    return ""


def paper_qa_id(paper: dict[str, Any]) -> str:
    arxiv_id = arxiv_id_from_paper(paper)
    identity = f"arxiv:{arxiv_id}" if arxiv_id else clean_text(paper.get("url"))
    if not identity:
        identity = clean_text(paper.get("title")).lower()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def selected_papers(report: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        list(report.get("papers", []))
        + list(report.get("potential_methods", []))
        + list(report.get("special_focus", []))
    )


def annotate_report(report: dict[str, Any]) -> None:
    for paper in selected_papers(report):
        paper["qa_id"] = paper_qa_id(paper)


def text_chunks(text: str, *, size: int = 2100, overlap: int = 180) -> list[str]:
    value = clean_text(text)
    if not value:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + size)
        if end < len(value):
            boundary = max(value.rfind(". ", start + size // 2, end), value.rfind("。", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunks.append(value[start:end].strip())
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)
    return [item for item in chunks if item]


def report_chunks(paper: dict[str, Any]) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    fields = (
        ("内容概述", paper.get("summary")),
        ("核心创新", "\n".join(str(item) for item in paper.get("innovations", []))),
        ("方法", paper.get("method")),
        ("实验结果", paper.get("results")),
        ("局限与判断", paper.get("limitations")),
        ("潜在机器人影响", paper.get("robotics_outlook")),
    )
    for label, value in fields:
        for index, part in enumerate(text_chunks(str(value or "")), 1):
            chunks.append(
                {
                    "label": label if index == 1 else f"{label}（续）",
                    "text": part,
                    "source": "daily_report",
                }
            )
    return chunks


def read_temp_source(arxiv_id: str) -> dict[str, Any] | None:
    path = TEMP_SOURCE_DIR / f"{arxiv_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def fetch_arxiv_source(arxiv_id: str, session: requests.Session) -> dict[str, Any]:
    # Import the optional HTML parser only for a real full-text fetch. Cache-only
    # rebuilds and report-note fallbacks should not depend on BeautifulSoup.
    from fetch_html import extract

    url = f"https://arxiv.org/html/{arxiv_id}"
    payload = ArxivAccess().fetch_bytes(
        url,
        session=session,
        timeout=60,
        max_bytes=12 * 1024 * 1024,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    data = extract(payload.body.decode("utf-8", errors="replace"))
    data["url"] = url
    return data


def source_chunks(source: dict[str, Any]) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    consumed = 0
    abstract = clean_text(source.get("abstract"))
    if abstract:
        chunks.append({"label": "Abstract", "text": abstract, "source": "arxiv_html"})
        consumed += len(abstract)
    for section in source.get("sections", []):
        if not isinstance(section, dict):
            continue
        heading = clean_text(section.get("heading")) or "正文"
        if SKIP_SECTION_RE.search(heading):
            continue
        body = clean_text(section.get("body"))
        if not body:
            continue
        remaining = MAX_SOURCE_CHARS - consumed
        if remaining <= 0:
            break
        body = body[:remaining]
        for index, part in enumerate(text_chunks(body), 1):
            chunks.append(
                {
                    "label": heading if index == 1 else f"{heading}（续 {index}）",
                    "text": part,
                    "source": "arxiv_html",
                }
            )
            if len(chunks) >= MAX_CHUNKS:
                return chunks
        consumed += len(body)
    return chunks


def structured_notes(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": clean_text(paper.get("summary")),
        "innovations": [clean_text(item) for item in paper.get("innovations", []) if clean_text(item)],
        "method": clean_text(paper.get("method")),
        "results": clean_text(paper.get("results")),
        "limitations": clean_text(paper.get("limitations")),
        "robotics_outlook": clean_text(paper.get("robotics_outlook")),
    }


def load_reusable_current(report_date: str, *, allow_previous: bool = False) -> dict[str, Any]:
    try:
        current = json.loads(CURRENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        (current.get("date") != report_date and not allow_previous)
        or not isinstance(current.get("papers"), dict)
    ):
        return {}
    return current["papers"]


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


def build_daily_knowledge(
    report: dict[str, Any],
    *,
    fetch_full: bool = True,
    output_path: Path = CURRENT_PATH,
) -> dict[str, Any]:
    """Replace the current knowledge buffer; never create dated source archives."""
    annotate_report(report)
    report_date = str(report["date"])
    reusable = (
        load_reusable_current(
            report_date,
            allow_previous=report.get("paper_source_carried_forward") is True,
        )
        if output_path == CURRENT_PATH
        else {}
    )
    session = requests.Session()
    records: dict[str, dict[str, Any]] = {}
    full_source_count = 0

    for paper in selected_papers(report):
        qa_id = str(paper["qa_id"])
        arxiv_id = arxiv_id_from_paper(paper)
        chunks = report_chunks(paper)
        source_state = "daily_report"
        source_error = ""
        prior = reusable.get(qa_id, {}) if isinstance(reusable.get(qa_id), dict) else {}
        prior_source = [
            item
            for item in prior.get("chunks", [])
            if isinstance(item, dict)
            and str(item.get("source", "")).startswith("arxiv_")
        ]
        if prior_source:
            chunks.extend(prior_source)
            source_state = clean_text(prior.get("source_state")) or "arxiv_html"
        elif fetch_full and paper.get("reading_depth") == "full" and arxiv_id:
            try:
                source = read_temp_source(arxiv_id) or fetch_arxiv_source(arxiv_id, session)
                extracted = source_chunks(source)
                if extracted:
                    chunks.extend(extracted)
                    source_state = "arxiv_html"
            except Exception as exc:  # The daily report must still publish if arXiv is unavailable.
                source_error = clean_text(exc)[:500]
        if source_state.startswith("arxiv_"):
            full_source_count += 1
        records[qa_id] = {
            "id": qa_id,
            "date": report_date,
            "title": clean_text(paper.get("title")),
            "authors": [clean_text(item) for item in paper.get("authors", []) if clean_text(item)],
            "url": clean_text(paper.get("url")),
            "arxiv_id": arxiv_id,
            "reading_depth": clean_text(paper.get("reading_depth")) or "abstract",
            "source_state": source_state,
            "source_error": source_error,
            "notes": structured_notes(paper),
            "chunks": chunks[:MAX_CHUNKS],
        }
        prior_on_demand = prior.get("on_demand") if isinstance(prior, dict) else None
        if prior_source and isinstance(prior_on_demand, dict):
            records[qa_id]["on_demand"] = prior_on_demand

    payload = {
        "schema": SCHEMA,
        "date": report_date,
        "generated_at": datetime.now().astimezone().isoformat(),
        "retention": "replace_on_next_successful_daily_run",
        "paper_count": len(records),
        "full_source_count": full_source_count,
        "papers": records,
    }
    atomic_write_json(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=CURRENT_PATH)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    payload = build_daily_knowledge(
        report,
        fetch_full=not args.no_fetch,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "date": payload["date"],
                "paper_count": payload["paper_count"],
                "full_source_count": payload["full_source_count"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
