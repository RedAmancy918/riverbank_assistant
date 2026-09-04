#!/usr/bin/env python3
"""Persistent background-task queue shared by RiverBank clients and worker."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = "riverbank.tasks/v1"
TASK_STATUSES = {
    "queued",
    "running",
    "waiting_input",
    "completed",
    "failed",
    "cancelled",
}
ACTIVE_STATUSES = {"queued", "running", "waiting_input"}
TASK_KINDS = {"research", "general", "file"}
TASK_OUTPUT_FORMATS = {"text", "image", "illustrated"}
MAX_TITLE_CHARS = 160
MAX_PROMPT_CHARS = 12_000
MAX_ANSWER_CHARS = 8_000
MAX_RESULT_CHARS = 24_000


def now_epoch() -> float:
    return time.time()


def clean_text(value: Any, *, maximum: int, field: str, minimum: int = 0) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) < minimum:
        raise ValueError(f"{field} is too short")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


class TaskStore:
    """Small SQLite queue using rollback journals for Raspberry Pi compatibility."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    output_format TEXT NOT NULL DEFAULT 'text',
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    status_message TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL DEFAULT '',
                    answers_json TEXT NOT NULL DEFAULT '[]',
                    result_summary TEXT NOT NULL DEFAULT '',
                    report_id TEXT NOT NULL DEFAULT '',
                    report_filename TEXT NOT NULL DEFAULT '',
                    artifact_filename TEXT NOT NULL DEFAULT '',
                    artifact_media_type TEXT NOT NULL DEFAULT '',
                    artifact_size_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS tasks_status_created
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS tasks_updated
                    ON tasks(updated_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "owner_user_id" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''"
                )
            migrations = {
                "output_format": "TEXT NOT NULL DEFAULT 'text'",
                "artifact_filename": "TEXT NOT NULL DEFAULT ''",
                "artifact_media_type": "TEXT NOT NULL DEFAULT ''",
                "artifact_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS tasks_owner_updated
                ON tasks(owner_user_id, updated_at DESC)
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record.pop("idempotency_key", None)
        try:
            record["answers"] = json.loads(record.pop("answers_json"))
        except (TypeError, ValueError):
            record["answers"] = []
            record.pop("answers_json", None)
        record["cancel_requested"] = bool(record["cancel_requested"])
        return record

    def create_task(
        self,
        *,
        prompt: Any,
        title: Any = "",
        kind: Any = "research",
        output_format: Any = "text",
        source: Any = "api",
        device_name: Any = "",
        idempotency_key: Any = "",
        owner_user_id: Any = "",
    ) -> tuple[dict[str, Any], bool]:
        clean_prompt = clean_text(
            prompt,
            maximum=MAX_PROMPT_CHARS,
            minimum=3,
            field="prompt",
        )
        clean_title = clean_text(title, maximum=MAX_TITLE_CHARS, field="title")
        if not clean_title:
            first_line = next(
                (line.strip() for line in clean_prompt.splitlines() if line.strip()),
                "后台任务",
            )
            clean_title = first_line[:MAX_TITLE_CHARS]
        clean_kind = clean_text(kind, maximum=32, field="kind") or "research"
        if clean_kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of {sorted(TASK_KINDS)}")
        clean_output_format = (
            clean_text(output_format, maximum=32, field="output format") or "text"
        )
        if clean_output_format not in TASK_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {sorted(TASK_OUTPUT_FORMATS)}"
            )
        clean_source = clean_text(source, maximum=48, field="source") or "api"
        clean_device = clean_text(
            device_name,
            maximum=120,
            field="device_name",
        )
        clean_key = clean_text(
            idempotency_key,
            maximum=128,
            field="idempotency_key",
        )
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            field="owner user id",
        )
        stored_key = f"{clean_owner}:{clean_key}" if clean_owner and clean_key else clean_key
        timestamp = now_epoch()
        task_id = uuid.uuid4().hex
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if stored_key:
                    existing = connection.execute(
                        "SELECT * FROM tasks WHERE idempotency_key = ?",
                        (stored_key,),
                    ).fetchone()
                    if existing is not None:
                        connection.commit()
                        return self._record(existing) or {}, False
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id, idempotency_key, owner_user_id, title, prompt, kind,
                        output_format, status,
                        source, device_name, progress, status_message,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        task_id,
                        stored_key or None,
                        clean_owner,
                        clean_title,
                        clean_prompt,
                        clean_kind,
                        clean_output_format,
                        clean_source,
                        clean_device,
                        "等待后台执行",
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                connection.commit()
        except sqlite3.IntegrityError:
            if not stored_key:
                raise
            existing = self.find_by_idempotency_key(
                clean_key,
                owner_user_id=clean_owner,
            )
            if existing is None:
                raise
            return existing, False
        return self._record(row) or {}, True

    def find_by_idempotency_key(
        self,
        key: str,
        *,
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        stored_key = f"{owner_user_id}:{key}" if owner_user_id and key else key
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (stored_key,),
            ).fetchone()
        return self._record(row)

    def get_task(
        self,
        task_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        task_id = clean_text(task_id, maximum=64, minimum=8, field="task id")
        with self.connect() as connection:
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ? AND owner_user_id = ?",
                    (task_id, str(owner_user_id)),
                ).fetchone()
        return self._record(row)

    def list_tasks(
        self,
        *,
        limit: int = 100,
        status: str = "",
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        conditions: list[str] = []
        parameters: list[Any] = []
        query = "SELECT * FROM tasks"
        if status:
            if status not in TASK_STATUSES:
                raise ValueError("invalid task status")
            conditions.append("status = ?")
            parameters.append(status)
        if owner_user_id is not None:
            conditions.append("owner_user_id = ?")
            parameters.append(str(owner_user_id))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [record for row in rows if (record := self._record(row)) is not None]

    def counts(self, *, owner_user_id: str | None = None) -> dict[str, int]:
        counts = {status: 0 for status in TASK_STATUSES}
        with self.connect() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM tasks
                    WHERE owner_user_id = ? GROUP BY status
                    """,
                    (str(owner_user_id),),
                ).fetchall()
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = int(row["count"])
        counts["active"] = sum(counts[status] for status in ACTIVE_STATUSES)
        return counts

    def report_owners(self) -> dict[str, set[str]]:
        """Return task-owned report IDs without exposing task contents."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT report_id, owner_user_id FROM tasks
                WHERE report_id != '' AND owner_user_id != ''
                """
            ).fetchall()
        owners: dict[str, set[str]] = {}
        for row in rows:
            owners.setdefault(str(row["report_id"]), set()).add(
                str(row["owner_user_id"])
            )
        return owners

    def claim_next(self) -> dict[str, Any] | None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued' AND cancel_requested = 0
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', progress = 0.05,
                    status_message = 'Hermes 正在处理',
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?, attempt = attempt + 1
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, timestamp, row["id"]),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return self._record(claimed)

    def update_progress(self, task_id: str, progress: float, message: str) -> None:
        progress = max(0.0, min(float(progress), 0.99))
        clean_message = clean_text(
            message,
            maximum=300,
            field="status message",
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks SET progress = ?, status_message = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (progress, clean_message, now_epoch(), task_id),
            )

    def complete(
        self,
        task_id: str,
        *,
        summary: Any,
        report_id: Any = "",
        report_filename: Any = "",
        artifact_filename: Any = "",
        artifact_media_type: Any = "",
        artifact_size_bytes: int = 0,
    ) -> None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'completed', progress = 1,
                    status_message = '任务已完成', result_summary = ?,
                    report_id = ?, report_filename = ?, error = '',
                    artifact_filename = ?, artifact_media_type = ?,
                    artifact_size_bytes = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_text(summary, maximum=MAX_RESULT_CHARS, field="summary"),
                    clean_text(report_id, maximum=1024, field="report id"),
                    clean_text(report_filename, maximum=255, field="report filename"),
                    clean_text(
                        artifact_filename,
                        maximum=255,
                        field="artifact filename",
                    ),
                    clean_text(
                        artifact_media_type,
                        maximum=120,
                        field="artifact media type",
                    ),
                    max(0, int(artifact_size_bytes)),
                    timestamp,
                    timestamp,
                    task_id,
                ),
            )

    def fail(self, task_id: str, error: Any) -> None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'failed', progress = 1,
                    status_message = '任务执行失败', error = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_text(error, maximum=4000, field="error") or "未知错误",
                    timestamp,
                    timestamp,
                    task_id,
                ),
            )

    def wait_for_input(self, task_id: str, question: Any) -> None:
        clean_question = clean_text(
            question,
            maximum=2000,
            minimum=2,
            field="question",
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'waiting_input', progress = 0.5,
                    status_message = '需要补充信息', question = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (clean_question, now_epoch(), task_id),
            )

    def answer(
        self,
        task_id: str,
        answer: Any,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        clean_answer = clean_text(
            answer,
            maximum=MAX_ANSWER_CHARS,
            minimum=1,
            field="answer",
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ? AND owner_user_id = ?",
                    (task_id, str(owner_user_id)),
                ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] != "waiting_input":
                connection.rollback()
                raise ValueError("task is not waiting for input")
            try:
                answers = json.loads(row["answers_json"])
            except (TypeError, ValueError):
                answers = []
            answers.append(
                {
                    "question": row["question"],
                    "answer": clean_answer,
                    "answered_at": now_epoch(),
                }
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'queued', progress = 0,
                    status_message = '已收到补充，等待继续执行', question = '',
                    answers_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(answers, ensure_ascii=False), now_epoch(), task_id),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            connection.commit()
        return self._record(updated)

    def request_cancel(
        self,
        task_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ? AND owner_user_id = ?",
                    (task_id, str(owner_user_id)),
                ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] in {"queued", "waiting_input"}:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', cancel_requested = 1, progress = 1,
                        status_message = '任务已取消', completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, task_id),
                )
            elif row["status"] == "running":
                connection.execute(
                    """
                    UPDATE tasks
                    SET cancel_requested = 1,
                        status_message = '正在取消', updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, task_id),
                )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            connection.commit()
        return self._record(updated)

    def is_cancel_requested(self, task_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def mark_cancelled(self, task_id: str) -> None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', progress = 1,
                    status_message = '任务已取消', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, task_id),
            )

    def recover_interrupted(self) -> int:
        """Return running jobs to the queue after an unclean worker restart."""
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE tasks
                SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    progress = CASE WHEN cancel_requested = 1 THEN 1 ELSE 0 END,
                    status_message = CASE
                        WHEN cancel_requested = 1 THEN '任务已取消'
                        ELSE 'Worker 重启后恢复排队'
                    END,
                    completed_at = CASE WHEN cancel_requested = 1 THEN ? ELSE NULL END,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now_epoch(), now_epoch()),
            )
        return int(result.rowcount)

    def claim_unowned_tasks(self, owner_user_id: Any) -> int:
        """Assign legacy client tasks to the first administrator."""
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            minimum=8,
            field="owner user id",
        )
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE tasks SET owner_user_id = ? WHERE owner_user_id = ''",
                (clean_owner,),
            )
        return int(result.rowcount)

    def delete_owner_tasks(self, owner_user_id: Any) -> dict[str, Any]:
        """Delete one user's terminal tasks without disrupting active workers."""
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            minimum=8,
            field="owner user id",
        )
        placeholders = ",".join("?" for _status in ACTIVE_STATUSES)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM tasks
                    WHERE owner_user_id = ? AND status IN ({placeholders})
                    """,
                    (clean_owner, *sorted(ACTIVE_STATUSES)),
                ).fetchone()[0]
            )
            if active:
                connection.rollback()
                raise RuntimeError("user has active tasks; cancel them before cleanup")
            artifact_task_ids = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM tasks
                    WHERE owner_user_id = ? AND artifact_filename != ''
                    """,
                    (clean_owner,),
                ).fetchall()
            ]
            result = connection.execute(
                "DELETE FROM tasks WHERE owner_user_id = ?",
                (clean_owner,),
            )
            connection.commit()
        return {
            "deleted": int(result.rowcount),
            "active": 0,
            "artifact_task_ids": artifact_task_ids,
        }
