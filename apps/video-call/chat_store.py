#!/usr/bin/env python3
"""Persistent conversation and message store for RiverBank Chat clients."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_CHAT_DB = Path("/var/lib/riverbank-tasks/chat.db")
VALID_ROLES = {"user", "assistant"}
VALID_MESSAGE_STATES = {"queued", "running", "completed", "failed", "cancelled"}
VALID_ATTACHMENT_KINDS = {"image", "document"}


def now_epoch() -> float:
    return time.time()


def clean_text(
    value: Any,
    *,
    maximum: int,
    minimum: int = 0,
    field: str,
) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) < minimum:
        raise ValueError(f"{field} is too short")
    if len(text) > maximum:
        raise ValueError(f"{field} is too long")
    return text


class ChatStore:
    """SQLite-backed chat history with resumable assistant generations."""

    def __init__(self, path: Path = DEFAULT_CHAT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    device_name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id)
                        REFERENCES chat_conversations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS chat_attachments (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id)
                        REFERENCES chat_conversations(id) ON DELETE CASCADE,
                    FOREIGN KEY(message_id)
                        REFERENCES chat_messages(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS chat_messages_conversation_created
                    ON chat_messages(conversation_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS chat_messages_state_created
                    ON chat_messages(state, created_at ASC);
                CREATE INDEX IF NOT EXISTS chat_attachments_message
                    ON chat_attachments(message_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS chat_attachments_conversation
                    ON chat_attachments(conversation_id, created_at ASC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(chat_conversations)"
                ).fetchall()
            }
            if "owner_user_id" not in columns:
                connection.execute(
                    """
                    ALTER TABLE chat_conversations
                    ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS chat_conversations_owner_updated
                ON chat_conversations(owner_user_id, updated_at DESC)
                """
            )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "title": row["title"],
            "source": row["source"],
            "device_name": row["device_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "state": row["state"],
            "error": row["error"],
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _attachment(
        row: sqlite3.Row,
        *,
        include_private: bool = False,
    ) -> dict[str, Any]:
        record = {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "message_id": row["message_id"],
            "original_name": row["original_name"],
            "media_type": row["media_type"],
            "kind": row["kind"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "created_at": row["created_at"],
        }
        if include_private:
            record["storage_path"] = row["storage_path"]
            record["extracted_text"] = row["extracted_text"]
        return record

    def _message_attachments(
        self,
        connection: sqlite3.Connection,
        message_id: str,
        *,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM chat_attachments
            WHERE message_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (message_id,),
        ).fetchall()
        return [
            self._attachment(row, include_private=include_private)
            for row in rows
        ]

    def _message_with_attachments(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_private: bool = False,
    ) -> dict[str, Any]:
        record = self._message(row)
        record["attachments"] = self._message_attachments(
            connection,
            record["id"],
            include_private=include_private,
        )
        return record

    def _messages_with_attachments(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        records = [self._message(row) for row in rows]
        if not records:
            return records
        by_message: dict[str, list[dict[str, Any]]] = {
            record["id"]: [] for record in records
        }
        message_ids = list(by_message)
        for offset in range(0, len(message_ids), 500):
            chunk = message_ids[offset : offset + 500]
            placeholders = ",".join("?" for _message_id in chunk)
            attachment_rows = connection.execute(
                f"""
                SELECT * FROM chat_attachments
                WHERE message_id IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC
                """,
                tuple(chunk),
            ).fetchall()
            for row in attachment_rows:
                by_message[row["message_id"]].append(
                    self._attachment(row, include_private=include_private)
                )
        for record in records:
            record["attachments"] = by_message[record["id"]]
        return records

    def create_conversation(
        self,
        *,
        owner_user_id: Any = "",
        title: Any = "",
        source: Any = "api",
        device_name: Any = "",
    ) -> dict[str, Any]:
        conversation_id = uuid.uuid4().hex
        timestamp = now_epoch()
        clean_title = clean_text(title, maximum=120, field="conversation title")
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            field="owner user id",
        )
        clean_source = clean_text(source, maximum=40, field="source") or "api"
        clean_device = clean_text(device_name, maximum=120, field="device name")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_conversations (
                    id, owner_user_id, title, source, device_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    clean_owner,
                    clean_title or "新对话",
                    clean_source,
                    clean_device,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM chat_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        assert row is not None
        return self._conversation(row)

    def get_conversation(
        self,
        conversation_id: Any,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        clean_id = clean_text(
            conversation_id,
            maximum=64,
            minimum=8,
            field="conversation id",
        )
        with self.connect() as connection:
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM chat_conversations WHERE id = ?",
                    (clean_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM chat_conversations
                    WHERE id = ? AND owner_user_id = ?
                    """,
                    (clean_id, str(owner_user_id)),
                ).fetchone()
        return self._conversation(row) if row is not None else None

    def list_conversations(
        self,
        *,
        limit: int = 100,
        include_internal: bool = False,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        conditions: list[str] = []
        parameters: list[Any] = []
        if not include_internal:
            conditions.append("c.source != 'paper-radar-internal'")
        if owner_user_id is not None:
            conditions.append("c.owner_user_id = ?")
            parameters.append(str(owner_user_id))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*,
                    (SELECT content FROM chat_messages m
                     WHERE m.conversation_id = c.id
                     ORDER BY m.created_at DESC LIMIT 1) AS preview,
                    (SELECT COUNT(*) FROM chat_messages m
                     WHERE m.conversation_id = c.id) AS message_count
                FROM chat_conversations c
                {where}
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        records = []
        for row in rows:
            record = self._conversation(row)
            record["preview"] = str(row["preview"] or "")[:180]
            record["message_count"] = int(row["message_count"] or 0)
            records.append(record)
        return records

    def delete_conversation(
        self,
        conversation_id: Any,
        *,
        owner_user_id: str | None = None,
    ) -> bool:
        clean_id = clean_text(
            conversation_id,
            maximum=64,
            minimum=8,
            field="conversation id",
        )
        with self.connect() as connection:
            if owner_user_id is None:
                result = connection.execute(
                    "DELETE FROM chat_conversations WHERE id = ?",
                    (clean_id,),
                )
            else:
                result = connection.execute(
                    """
                    DELETE FROM chat_conversations
                    WHERE id = ? AND owner_user_id = ?
                    """,
                    (clean_id, str(owner_user_id)),
                )
        return result.rowcount > 0

    def list_messages(
        self,
        conversation_id: Any,
        *,
        include_private_attachments: bool = False,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clean_id = clean_text(
            conversation_id,
            maximum=64,
            minimum=8,
            field="conversation id",
        )
        with self.connect() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (clean_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT m.* FROM chat_messages m
                    JOIN chat_conversations c ON c.id = m.conversation_id
                    WHERE m.conversation_id = ? AND c.owner_user_id = ?
                    ORDER BY m.created_at ASC, m.rowid ASC
                    """,
                    (clean_id, str(owner_user_id)),
                ).fetchall()
            return self._messages_with_attachments(
                connection,
                list(rows),
                include_private=include_private_attachments,
            )

    def create_turn(
        self,
        conversation_id: Any,
        *,
        content: Any,
        attachments: list[dict[str, Any]] | None = None,
        owner_user_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        clean_id = clean_text(
            conversation_id,
            maximum=64,
            minimum=8,
            field="conversation id",
        )
        clean_content = clean_text(
            content,
            maximum=24_000,
            minimum=0,
            field="message",
        )
        clean_attachments = list(attachments or [])
        if not clean_content and not clean_attachments:
            raise ValueError("message or attachment is required")
        if len(clean_attachments) > 4:
            raise ValueError("too many attachments")
        timestamp = now_epoch()
        user_id = uuid.uuid4().hex
        assistant_id = uuid.uuid4().hex
        with self.connect() as connection:
            if owner_user_id is None:
                conversation = connection.execute(
                    "SELECT * FROM chat_conversations WHERE id = ?",
                    (clean_id,),
                ).fetchone()
            else:
                conversation = connection.execute(
                    """
                    SELECT * FROM chat_conversations
                    WHERE id = ? AND owner_user_id = ?
                    """,
                    (clean_id, str(owner_user_id)),
                ).fetchone()
            if conversation is None:
                raise KeyError("conversation not found")
            active = connection.execute(
                """
                SELECT 1 FROM chat_messages
                WHERE conversation_id = ? AND role = 'assistant'
                    AND state IN ('queued', 'running')
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
            if active is not None:
                raise RuntimeError("conversation already has an active response")
            connection.execute(
                """
                INSERT INTO chat_messages (
                    id, conversation_id, role, content, state,
                    created_at, updated_at
                ) VALUES (?, ?, 'user', ?, 'completed', ?, ?)
                """,
                (user_id, clean_id, clean_content, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO chat_messages (
                    id, conversation_id, role, content, state,
                    created_at, updated_at
                ) VALUES (?, ?, 'assistant', '', 'queued', ?, ?)
                """,
                (assistant_id, clean_id, timestamp + 0.0001, timestamp + 0.0001),
            )
            seen_attachment_ids: set[str] = set()
            for index, attachment in enumerate(clean_attachments):
                attachment_id = clean_text(
                    attachment.get("id"),
                    maximum=64,
                    minimum=8,
                    field="attachment id",
                )
                if attachment_id in seen_attachment_ids:
                    raise ValueError("duplicate attachment id")
                seen_attachment_ids.add(attachment_id)
                kind = clean_text(
                    attachment.get("kind"),
                    maximum=20,
                    minimum=1,
                    field="attachment kind",
                )
                if kind not in VALID_ATTACHMENT_KINDS:
                    raise ValueError("invalid attachment kind")
                original_name = clean_text(
                    attachment.get("original_name"),
                    maximum=180,
                    minimum=1,
                    field="attachment filename",
                )
                media_type = clean_text(
                    attachment.get("media_type"),
                    maximum=100,
                    minimum=1,
                    field="attachment media type",
                )
                sha256 = clean_text(
                    attachment.get("sha256"),
                    maximum=64,
                    minimum=64,
                    field="attachment hash",
                )
                storage_path = clean_text(
                    attachment.get("storage_path"),
                    maximum=1_000,
                    minimum=1,
                    field="attachment storage path",
                )
                extracted_text = clean_text(
                    attachment.get("extracted_text", ""),
                    maximum=80_000,
                    field="attachment text",
                )
                size_bytes = int(attachment.get("size_bytes", 0))
                if size_bytes <= 0:
                    raise ValueError("invalid attachment size")
                connection.execute(
                    """
                    INSERT INTO chat_attachments (
                        id, conversation_id, message_id, original_name,
                        media_type, kind, size_bytes, sha256, storage_path,
                        extracted_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attachment_id,
                        clean_id,
                        user_id,
                        original_name,
                        media_type,
                        kind,
                        size_bytes,
                        sha256,
                        storage_path,
                        extracted_text,
                        timestamp + ((index + 1) / 1_000_000),
                    ),
                )
            current_title = str(conversation["title"] or "")
            title = current_title
            if current_title == "新对话":
                title = (
                    clean_content.replace("\n", " ")[:36]
                    or f"附件：{clean_attachments[0]['original_name']}"[:36]
                )
            connection.execute(
                """
                UPDATE chat_conversations SET title = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, timestamp, clean_id),
            )
            user_row = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (user_id,)
            ).fetchone()
            assistant_row = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (assistant_id,)
            ).fetchone()
        assert user_row is not None and assistant_row is not None
        with self.connect() as connection:
            return (
                self._message_with_attachments(connection, user_row),
                self._message_with_attachments(connection, assistant_row),
            )

    def get_attachment(
        self,
        conversation_id: Any,
        attachment_id: Any,
        *,
        include_private: bool = False,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        clean_conversation_id = clean_text(
            conversation_id,
            maximum=64,
            minimum=8,
            field="conversation id",
        )
        clean_attachment_id = clean_text(
            attachment_id,
            maximum=64,
            minimum=8,
            field="attachment id",
        )
        with self.connect() as connection:
            if owner_user_id is None:
                row = connection.execute(
                    """
                    SELECT * FROM chat_attachments
                    WHERE id = ? AND conversation_id = ?
                    """,
                    (clean_attachment_id, clean_conversation_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT a.* FROM chat_attachments a
                    JOIN chat_conversations c ON c.id = a.conversation_id
                    WHERE a.id = ? AND a.conversation_id = ?
                          AND c.owner_user_id = ?
                    """,
                    (
                        clean_attachment_id,
                        clean_conversation_id,
                        str(owner_user_id),
                    ),
                ).fetchone()
        return (
            self._attachment(row, include_private=include_private)
            if row is not None
            else None
        )

    def add_attachment_to_message(
        self,
        message_id: Any,
        attachment: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach a worker-produced file to an existing assistant message."""
        clean_message_id = clean_text(
            message_id,
            maximum=64,
            minimum=8,
            field="message id",
        )
        attachment_id = clean_text(
            attachment.get("id"),
            maximum=64,
            minimum=8,
            field="attachment id",
        )
        kind = clean_text(
            attachment.get("kind"),
            maximum=20,
            minimum=1,
            field="attachment kind",
        )
        if kind not in VALID_ATTACHMENT_KINDS:
            raise ValueError("invalid attachment kind")
        original_name = clean_text(
            attachment.get("original_name"),
            maximum=180,
            minimum=1,
            field="attachment filename",
        )
        media_type = clean_text(
            attachment.get("media_type"),
            maximum=100,
            minimum=1,
            field="attachment media type",
        )
        sha256 = clean_text(
            attachment.get("sha256"),
            maximum=64,
            minimum=64,
            field="attachment hash",
        )
        storage_path = clean_text(
            attachment.get("storage_path"),
            maximum=1_000,
            minimum=1,
            field="attachment storage path",
        )
        extracted_text = clean_text(
            attachment.get("extracted_text", ""),
            maximum=80_000,
            field="attachment text",
        )
        size_bytes = int(attachment.get("size_bytes", 0))
        if size_bytes <= 0:
            raise ValueError("invalid attachment size")
        timestamp = now_epoch()
        with self.connect() as connection:
            message = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ? AND role = 'assistant'",
                (clean_message_id,),
            ).fetchone()
            if message is None:
                raise KeyError("assistant message not found")
            connection.execute(
                """
                INSERT INTO chat_attachments (
                    id, conversation_id, message_id, original_name,
                    media_type, kind, size_bytes, sha256, storage_path,
                    extracted_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    message["conversation_id"],
                    clean_message_id,
                    original_name,
                    media_type,
                    kind,
                    size_bytes,
                    sha256,
                    storage_path,
                    extracted_text,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM chat_attachments WHERE id = ?",
                (attachment_id,),
            ).fetchone()
        assert row is not None
        return self._attachment(row)

    def claim_next(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM chat_messages
                WHERE role = 'assistant' AND state = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            timestamp = now_epoch()
            connection.execute(
                """
                UPDATE chat_messages SET state = 'running', updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (timestamp, row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return self._message(claimed)

    def preceding_user_message(self, assistant_id: Any) -> dict[str, Any] | None:
        clean_id = clean_text(
            assistant_id,
            maximum=64,
            minimum=8,
            field="message id",
        )
        with self.connect() as connection:
            assistant = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?",
                (clean_id,),
            ).fetchone()
            if assistant is None:
                return None
            row = connection.execute(
                """
                SELECT * FROM chat_messages
                WHERE conversation_id = ? AND role = 'user' AND created_at < ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (assistant["conversation_id"], assistant["created_at"]),
            ).fetchone()
            return (
                self._message_with_attachments(
                    connection,
                    row,
                    include_private=True,
                )
                if row is not None
                else None
            )

    def replace_content(self, message_id: Any, content: Any) -> None:
        clean_id = clean_text(message_id, maximum=64, minimum=8, field="message id")
        clean_content = clean_text(content, maximum=200_000, field="message content")
        with self.connect() as connection:
            connection.execute(
                "UPDATE chat_messages SET content = ?, updated_at = ? WHERE id = ?",
                (clean_content, now_epoch(), clean_id),
            )

    def finish(self, message_id: Any, content: Any) -> None:
        self._finish(message_id, state="completed", content=content, error="")

    def fail(self, message_id: Any, error: Any) -> None:
        self._finish(message_id, state="failed", content="", error=error)

    def mark_cancelled(self, message_id: Any) -> None:
        self._finish(message_id, state="cancelled", content="", error="")

    def _finish(self, message_id: Any, *, state: str, content: Any, error: Any) -> None:
        if state not in VALID_MESSAGE_STATES:
            raise ValueError("invalid message state")
        clean_id = clean_text(message_id, maximum=64, minimum=8, field="message id")
        clean_content = clean_text(content, maximum=200_000, field="message content")
        clean_error = clean_text(error, maximum=8_000, field="message error")
        timestamp = now_epoch()
        with self.connect() as connection:
            conversation = connection.execute(
                "SELECT conversation_id FROM chat_messages WHERE id = ?",
                (clean_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE chat_messages
                SET content = ?, state = ?, error = ?, cancel_requested = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (clean_content, state, clean_error, timestamp, clean_id),
            )
            if conversation is not None:
                connection.execute(
                    "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
                    (timestamp, conversation["conversation_id"]),
                )

    def request_cancel(
        self,
        message_id: Any,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        clean_id = clean_text(message_id, maximum=64, minimum=8, field="message id")
        with self.connect() as connection:
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM chat_messages WHERE id = ? AND role = 'assistant'",
                    (clean_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT m.* FROM chat_messages m
                    JOIN chat_conversations c ON c.id = m.conversation_id
                    WHERE m.id = ? AND m.role = 'assistant'
                          AND c.owner_user_id = ?
                    """,
                    (clean_id, str(owner_user_id)),
                ).fetchone()
            if row is None:
                return None
            if row["state"] in {"queued", "running"}:
                connection.execute(
                    """
                    UPDATE chat_messages SET cancel_requested = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_epoch(), clean_id),
                )
            updated = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (clean_id,)
            ).fetchone()
        assert updated is not None
        return self._message(updated)

    def is_cancel_requested(self, message_id: Any) -> bool:
        clean_id = clean_text(message_id, maximum=64, minimum=8, field="message id")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM chat_messages WHERE id = ?",
                (clean_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def claim_unowned_conversations(self, owner_user_id: Any) -> int:
        """Assign pre-account user chats to the first administrator.

        Daily paper Q&A conversations remain device-owned and are not exposed in
        the regular chat list.
        """
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            minimum=8,
            field="owner user id",
        )
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE chat_conversations SET owner_user_id = ?
                WHERE owner_user_id = '' AND source != 'paper-radar-internal'
                """,
                (clean_owner,),
            )
        return result.rowcount

    def owner_usage(self, owner_user_id: Any) -> dict[str, int]:
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            minimum=8,
            field="owner user id",
        )
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM chat_conversations
                     WHERE owner_user_id = ?) AS conversations,
                    (SELECT COUNT(*) FROM chat_messages m
                     JOIN chat_conversations c ON c.id = m.conversation_id
                     WHERE c.owner_user_id = ?) AS messages,
                    (SELECT COUNT(*) FROM chat_attachments a
                     JOIN chat_conversations c ON c.id = a.conversation_id
                     WHERE c.owner_user_id = ?) AS attachments,
                    (SELECT COALESCE(SUM(a.size_bytes), 0) FROM chat_attachments a
                     JOIN chat_conversations c ON c.id = a.conversation_id
                     WHERE c.owner_user_id = ?) AS attachment_bytes,
                    (SELECT COUNT(*) FROM chat_messages m
                     JOIN chat_conversations c ON c.id = m.conversation_id
                     WHERE c.owner_user_id = ? AND m.role = 'assistant'
                           AND m.state IN ('queued', 'running')) AS active_messages
                """,
                (clean_owner, clean_owner, clean_owner, clean_owner, clean_owner),
            ).fetchone()
        assert row is not None
        return {
            "conversations": int(row["conversations"] or 0),
            "messages": int(row["messages"] or 0),
            "attachments": int(row["attachments"] or 0),
            "attachment_bytes": int(row["attachment_bytes"] or 0),
            "active_messages": int(row["active_messages"] or 0),
        }

    def delete_owner_cache(self, owner_user_id: Any) -> dict[str, Any]:
        """Delete one user's chat history and return files for safe removal."""
        clean_owner = clean_text(
            owner_user_id,
            maximum=64,
            minimum=8,
            field="owner user id",
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM chat_messages m
                    JOIN chat_conversations c ON c.id = m.conversation_id
                    WHERE c.owner_user_id = ? AND m.role = 'assistant'
                          AND m.state IN ('queued', 'running')
                    """,
                    (clean_owner,),
                ).fetchone()[0]
            )
            if active:
                connection.rollback()
                raise RuntimeError(
                    "user has an active chat response; stop it before cleanup"
                )
            attachment_rows = connection.execute(
                """
                SELECT a.* FROM chat_attachments a
                JOIN chat_conversations c ON c.id = a.conversation_id
                WHERE c.owner_user_id = ?
                """,
                (clean_owner,),
            ).fetchall()
            attachments = [
                self._attachment(row, include_private=True)
                for row in attachment_rows
            ]
            result = connection.execute(
                "DELETE FROM chat_conversations WHERE owner_user_id = ?",
                (clean_owner,),
            )
            connection.commit()
        return {
            "deleted_conversations": int(result.rowcount),
            "deleted_attachments": len(attachments),
            "attachment_records": attachments,
        }

    def recover_interrupted(self) -> int:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE chat_messages
                SET state = 'queued', content = '', error = '', cancel_requested = 0,
                    updated_at = ?
                WHERE role = 'assistant' AND state = 'running'
                """,
                (now_epoch(),),
            )
        return result.rowcount
