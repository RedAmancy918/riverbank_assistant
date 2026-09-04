#!/usr/bin/env python3
"""Private generated-file storage for authenticated RiverBank tasks."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


DEFAULT_TASK_ARTIFACT_ROOT = Path(
    os.environ.get(
        "RIVERBANK_TASK_ARTIFACT_ROOT",
        "/var/lib/riverbank-tasks/artifacts",
    )
)
MAX_TASK_ARTIFACT_BYTES = 32 * 1024 * 1024
TASK_ID_RE = re.compile(r"[0-9a-f]{32}")
FILENAME_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")


class TaskArtifactError(ValueError):
    """Raised when a task artifact path or payload is unsafe."""


class TaskArtifactStore:
    def __init__(self, root: Path | str = DEFAULT_TASK_ARTIFACT_ROOT) -> None:
        self.root = Path(root).expanduser()

    @staticmethod
    def validate_task_id(value: Any) -> str:
        task_id = str(value or "").strip().lower()
        if not TASK_ID_RE.fullmatch(task_id):
            raise TaskArtifactError("invalid task id")
        return task_id

    @staticmethod
    def validate_filename(value: Any) -> str:
        filename = str(value or "").strip()
        if not FILENAME_RE.fullmatch(filename) or filename in {".", ".."}:
            raise TaskArtifactError("invalid artifact filename")
        return filename

    def task_directory(self, task_id: Any) -> Path:
        return self.root / self.validate_task_id(task_id)

    def save(self, task_id: Any, filename: Any, payload: bytes) -> Path:
        task_directory = self.task_directory(task_id)
        clean_filename = self.validate_filename(filename)
        if not payload:
            raise TaskArtifactError("artifact is empty")
        if len(payload) > MAX_TASK_ARTIFACT_BYTES:
            raise TaskArtifactError("artifact exceeds 32 MB")
        task_directory.mkdir(parents=True, exist_ok=True)
        destination = task_directory / clean_filename
        temporary = task_directory / f".{clean_filename}.tmp-{os.getpid()}"
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
        return destination

    def resolve(self, task_id: Any, filename: Any) -> Path:
        task_directory = self.task_directory(task_id).resolve()
        unresolved = task_directory / self.validate_filename(filename)
        if unresolved.is_symlink():
            raise TaskArtifactError("artifact symlinks are not allowed")
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(task_directory)
        except ValueError as exc:
            raise TaskArtifactError("artifact path escapes task directory") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise FileNotFoundError(candidate)
        if candidate.stat().st_size > MAX_TASK_ARTIFACT_BYTES:
            raise TaskArtifactError("artifact exceeds 32 MB")
        return candidate

    def delete_tasks(self, task_ids: list[str]) -> int:
        deleted = 0
        for raw_task_id in task_ids:
            directory = self.task_directory(raw_task_id)
            if directory.is_symlink():
                raise TaskArtifactError("artifact directory symlinks are not allowed")
            if directory.is_dir():
                shutil.rmtree(directory)
                deleted += 1
        return deleted
