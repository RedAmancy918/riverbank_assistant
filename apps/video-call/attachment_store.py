#!/usr/bin/env python3
"""Validated private attachment storage for RiverBank Chat."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


DEFAULT_ATTACHMENT_ROOT = Path("/var/lib/riverbank-tasks/chat-attachments")
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 4
MAX_MESSAGE_ATTACHMENT_BYTES = 30 * 1024 * 1024
MAX_EXTRACTED_TEXT = 60_000
MAX_IMAGE_PIXELS = 50_000_000
CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
TEXT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


class AttachmentValidationError(ValueError):
    """Raised when an uploaded attachment is unsafe or unsupported."""


def _clean_filename(value: Any) -> str:
    name = Path(str(value or "attachment").replace("\\", "/")).name
    name = "".join(
        character
        for character in unicodedata.normalize("NFC", name)
        if character >= " " and character != "\x7f"
    ).strip()
    return (name or "attachment")[:180]


def _decode_text(payload: bytes) -> str:
    if b"\x00" in payload:
        raise AttachmentValidationError("文本文件包含二进制内容")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AttachmentValidationError("文本文件编码不是 UTF-8 或 GB18030")


def _normalize_text(value: str) -> str:
    return value.replace("\x00", "").replace("\ufffd", "").strip()[:MAX_EXTRACTED_TEXT]


class AttachmentStore:
    def __init__(self, root: Path = DEFAULT_ATTACHMENT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_conversation_id(conversation_id: Any) -> str:
        value = str(conversation_id or "").strip().lower()
        if not CONVERSATION_ID_RE.fullmatch(value):
            raise AttachmentValidationError("对话编号无效")
        return value

    @staticmethod
    def _validate_image(payload: bytes, suffix: str) -> None:
        if suffix in {".jpg", ".jpeg"} and not payload.startswith(b"\xff\xd8\xff"):
            raise AttachmentValidationError("JPEG 文件内容无效")
        if suffix == ".png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AttachmentValidationError("PNG 文件内容无效")
        if suffix == ".webp" and not (
            len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
        ):
            raise AttachmentValidationError("WebP 文件内容无效")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise AttachmentValidationError("图片尺寸过大或无效")
                image.verify()
        except AttachmentValidationError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise AttachmentValidationError("图片无法识别或已损坏") from exc

    @staticmethod
    def _extract_pdf(path: Path) -> str:
        try:
            result = subprocess.run(
                ["/usr/bin/pdftotext", "-f", "1", "-l", "40", "-layout", str(path), "-"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AttachmentValidationError("PDF 文本提取失败") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise AttachmentValidationError(detail[:240] or "PDF 文件无效")
        return _normalize_text(result.stdout.decode("utf-8", errors="replace"))

    def save(self, conversation_id: Any, filename: Any, payload: bytes) -> dict[str, Any]:
        clean_conversation_id = self._validate_conversation_id(conversation_id)
        original_name = _clean_filename(filename)
        suffix = Path(original_name).suffix.lower()
        if not payload:
            raise AttachmentValidationError("附件为空")
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise AttachmentValidationError("单个附件不能超过 15 MB")

        extracted_text = ""
        if suffix in IMAGE_TYPES:
            self._validate_image(payload, suffix)
            kind = "image"
            media_type = IMAGE_TYPES[suffix]
            canonical_suffix = ".jpg" if suffix == ".jpeg" else suffix
        elif suffix == ".pdf":
            if not payload.lstrip().startswith(b"%PDF-"):
                raise AttachmentValidationError("PDF 文件内容无效")
            kind = "document"
            media_type = "application/pdf"
            canonical_suffix = ".pdf"
        elif suffix in TEXT_TYPES:
            kind = "document"
            media_type = TEXT_TYPES[suffix]
            canonical_suffix = ".md" if suffix == ".markdown" else suffix
            extracted_text = _normalize_text(_decode_text(payload))
        else:
            raise AttachmentValidationError(
                "仅支持 JPG、PNG、WebP、PDF、Markdown 和 TXT"
            )

        attachment_id = uuid.uuid4().hex
        directory = self.root / clean_conversation_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{attachment_id}{canonical_suffix}"
        temporary = directory / f".{attachment_id}.part"
        try:
            temporary.write_bytes(payload)
            temporary.chmod(0o600)
            os.replace(temporary, destination)
            if suffix == ".pdf":
                extracted_text = self._extract_pdf(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

        return {
            "id": attachment_id,
            "original_name": original_name,
            "media_type": media_type,
            "kind": kind,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "storage_path": str(destination),
            "extracted_text": extracted_text,
        }

    def resolve(self, storage_path: Any) -> Path:
        candidate = Path(str(storage_path or ""))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            raise FileNotFoundError("attachment not found") from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise FileNotFoundError("attachment not found")
        return resolved

    def delete_records(self, records: list[dict[str, Any]]) -> None:
        directories: set[Path] = set()
        for record in records:
            try:
                path = self.resolve(record.get("storage_path"))
                directories.add(path.parent)
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass

    def delete_conversation(self, conversation_id: Any) -> None:
        clean_id = self._validate_conversation_id(conversation_id)
        directory = self.root / clean_id
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
