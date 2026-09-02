#!/usr/bin/env python3
"""Persistent proposal and audit storage for the trusted Workshop service."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from workshop_contract import ContractError
from workshop_manager import atomic_write_json


PROPOSAL_SCHEMA = "riverbank.workshop-proposals/v1"
PROPOSAL_STATUSES = {
    "queued",
    "generating",
    "awaiting_approval",
    "installing",
    "installed",
    "rejected",
    "failed",
}


class WorkshopStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "proposals.json"
        self.lock_path = self.data_root / ".proposals.lock"
        self.audit_path = self.data_root / "audit.jsonl"

    @contextlib.contextmanager
    def lock(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"schema": PROPOSAL_SCHEMA, "proposals": {}}
        if not isinstance(value, dict) or value.get("schema") != PROPOSAL_SCHEMA:
            raise ContractError("invalid_proposal_store", "工坊提案存储格式无效。", "store")
        if not isinstance(value.get("proposals"), dict):
            raise ContractError("invalid_proposal_store", "工坊提案列表格式无效。", "store")
        return value

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result.pop("packagePath", None)
        return result

    def create(self, requirement: str, source: str) -> dict[str, Any]:
        requirement = str(requirement).replace("\x00", "").strip()
        if not 3 <= len(requirement) <= 4000:
            raise ContractError(
                "invalid_requirement",
                "应用需求长度必须在 3 到 4000 字之间。",
                "requirement",
            )
        proposal_id = uuid.uuid4().hex
        now = time.time()
        record = {
            "id": proposal_id,
            "requirement": requirement,
            "source": str(source or "unknown")[:48],
            "status": "queued",
            "progress": 0.05,
            "statusMessage": "等待生成安全应用计划",
            "createdAt": now,
            "updatedAt": now,
            "error": "",
        }
        with self.lock():
            payload = self._read()
            payload["proposals"][proposal_id] = record
            atomic_write_json(self.path, payload)
        self.audit("proposal.created", proposal_id=proposal_id, source=source)
        return self._public(record)

    def update(self, proposal_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "progress",
            "statusMessage",
            "error",
            "manifest",
            "app",
            "packagePath",
            "generator",
            "risk",
            "installedAppId",
            "decisionAt",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"unsupported proposal fields: {unknown}")
        with self.lock():
            payload = self._read()
            record = payload["proposals"].get(str(proposal_id))
            if not isinstance(record, dict):
                raise ContractError("proposal_not_found", "找不到工坊提案。", "proposal_id")
            if "status" in changes and changes["status"] not in PROPOSAL_STATUSES:
                raise ValueError("invalid proposal status")
            record.update(changes)
            record["updatedAt"] = time.time()
            payload["proposals"][str(proposal_id)] = record
            atomic_write_json(self.path, payload)
        return self._public(record)

    def get(self, proposal_id: str, *, internal: bool = False) -> dict[str, Any] | None:
        with self.lock():
            record = self._read()["proposals"].get(str(proposal_id))
        if not isinstance(record, dict):
            return None
        return dict(record) if internal else self._public(record)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock():
            records = list(self._read()["proposals"].values())
        records = [record for record in records if isinstance(record, dict)]
        records.sort(key=lambda item: float(item.get("createdAt", 0)), reverse=True)
        return [self._public(record) for record in records[: max(1, min(limit, 200))]]

    def pending_review(self) -> dict[str, Any] | None:
        return next(
            (record for record in self.list(200) if record.get("status") == "awaiting_approval"),
            None,
        )

    def queued_ids(self) -> list[str]:
        return [
            str(record["id"])
            for record in reversed(self.list(200))
            if record.get("status") in {"queued", "generating"}
        ]

    def recover(self) -> int:
        count = 0
        with self.lock():
            payload = self._read()
            for record in payload["proposals"].values():
                if isinstance(record, dict) and record.get("status") in {"generating", "installing"}:
                    record.update(
                        {
                            "status": "queued",
                            "progress": 0.05,
                            "statusMessage": "服务重启后恢复生成",
                            "updatedAt": time.time(),
                        }
                    )
                    count += 1
            if count:
                atomic_write_json(self.path, payload)
        return count

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in PROPOSAL_STATUSES}
        for record in self.list(200):
            status = str(record.get("status"))
            if status in counts:
                counts[status] += 1
        counts["active"] = counts["queued"] + counts["generating"] + counts["installing"]
        counts["pendingReview"] = counts["awaiting_approval"]
        return counts

    def audit(self, event: str, **data: Any) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": "riverbank.workshop-audit/v1",
            "timestamp": time.time(),
            "event": str(event)[:96],
            "data": data,
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock():
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.audit_path, 0o600)
