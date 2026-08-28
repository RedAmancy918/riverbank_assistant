#!/usr/bin/env python3
"""Read-only task report catalogue shared by RiverBank desktop clients."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_REPORTS_DIR = Path(
    os.environ.get(
        "RIVERBANK_REPORTS_DIR",
        "/home/geo/.hermes/profiles/daily/workspace/reports",
    )
)
REPORT_EXTENSIONS = {".md", ".markdown"}
MAX_REPORT_PREVIEW_BYTES = 4 * 1024 * 1024


class ReportLibrary:
    """Read-only Markdown report catalogue rooted inside the Daily workspace."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    @staticmethod
    def encode_id(relative_path: str) -> str:
        payload = base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii")
        return payload.rstrip("=")

    @staticmethod
    def decode_id(report_id: str) -> str:
        value = str(report_id or "").strip()
        if not value or len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("invalid report id")
        padding = "=" * (-len(value) % 4)
        try:
            return base64.urlsafe_b64decode(value + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid report id") from exc

    def resolve(self, report_id: str) -> Path:
        relative = Path(self.decode_id(report_id))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("report path escapes library")
        root = self.root.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("report path escapes library") from exc
        if candidate.suffix.lower() not in REPORT_EXTENSIONS:
            raise ValueError("unsupported report format")
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return candidate

    @staticmethod
    def extract_title_and_summary(path: Path) -> tuple[str, str]:
        try:
            source = path.read_text(encoding="utf-8")[:64_000]
        except (OSError, UnicodeDecodeError):
            return path.stem.replace("_", " "), ""
        title = ""
        summary_lines: list[str] = []
        in_frontmatter = source.startswith("---\n")
        in_code = False
        for index, raw_line in enumerate(source.splitlines()):
            line = raw_line.strip()
            if in_frontmatter:
                if index > 0 and line == "---":
                    in_frontmatter = False
                continue
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not line:
                if summary_lines:
                    break
                continue
            if not title and line.startswith("# "):
                title = line[2:].strip()
                continue
            if line.startswith("#") or line in {"---", "***"}:
                continue
            cleaned = re.sub(r"!\[[^]]*]\([^)]*\)", "", line)
            cleaned = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", cleaned)
            cleaned = re.sub(r"^[>*+\-\d.\s]+", "", cleaned)
            cleaned = re.sub(r"[`*_~]", "", cleaned).strip()
            if cleaned:
                summary_lines.append(cleaned)
                if len(" ".join(summary_lines)) >= 180:
                    break
        title = title or path.stem.replace("_", " ")
        summary = " ".join(summary_lines)
        if len(summary) > 200:
            summary = summary[:197].rstrip() + "…"
        return title[:160], summary

    def list_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[tuple[float, dict[str, Any]]] = []
        root = self.root.resolve()
        for path in self.root.rglob("*"):
            if len(records) >= 1000:
                break
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.lower() not in REPORT_EXTENSIONS
                or any(part.startswith(".") for part in path.relative_to(self.root).parts)
            ):
                continue
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root).as_posix()
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            title, summary = self.extract_title_and_summary(resolved)
            records.append(
                (
                    stat.st_mtime,
                    {
                        "id": self.encode_id(relative),
                        "title": title,
                        "summary": summary,
                        "filename": resolved.name,
                        "relative_path": relative,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                        "format": "markdown",
                    },
                )
            )
        records.sort(key=lambda item: item[0], reverse=True)
        return [record for _mtime, record in records[: max(1, min(limit, 500))]]

    def read_report(self, report_id: str) -> tuple[Path, str]:
        path = self.resolve(report_id)
        if path.stat().st_size > MAX_REPORT_PREVIEW_BYTES:
            raise ValueError("report is too large to preview")
        return path, path.read_text(encoding="utf-8")
