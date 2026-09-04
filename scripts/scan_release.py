#!/usr/bin/env python3
"""Check a release tree for common secrets, private data and large artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".build",
    "build",
    "dist",
    "DerivedData",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
}
BANNED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".hef", ".onnx", ".pt", ".pth",
    ".safetensors", ".wav", ".mp3", ".gif", ".so",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{15,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"
)

WORKSHOP_USER_PREFIXES = (
    "apps/workshop/drafts",
    "apps/workshop/packages",
    "apps/workshop/app-data",
    "apps/workshop/generated",
    "apps/workshop/runtime-data",
    "apps/workshop/user-data",
    "apps/workshop/data",
    "data/workshop",
    "riverbank-user/workshop",
    "workshop-data",
)
WORKSHOP_USER_FILENAMES = {
    "apps/workshop/registry.json",
    "apps/workshop/proposals.json",
    "apps/workshop/audit.jsonl",
    "apps/workshop/.registry.lock",
    "apps/workshop/.proposals.lock",
}


def workshop_user_artifact_reason(relative: Path) -> str | None:
    """Identify device-local Workshop output that must never ship from Git."""

    normalized = relative.as_posix().lstrip("./")
    if relative.suffix.lower() == ".rbapp":
        return "Workshop user package"
    if normalized in WORKSHOP_USER_FILENAMES:
        return "Workshop user state"
    if any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in WORKSHOP_USER_PREFIXES
    ):
        return "Workshop user data"
    return None


def files() -> list[Path]:
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
    ]


def main() -> int:
    errors: list[str] = []
    for path in files():
        relative = path.relative_to(ROOT)
        workshop_reason = workshop_user_artifact_reason(relative)
        if workshop_reason:
            errors.append(f"{workshop_reason}: {relative}")
        if path.stat().st_size > 10 * 1024 * 1024:
            errors.append(f"large file: {relative}")
        if path.suffix.lower() in BANNED_SUFFIXES:
            errors.append(f"excluded artifact type: {relative}")
        if path.name.startswith(".env") and path.name != ".env.example":
            errors.append(f"environment file: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{label}: {relative}")
        for match in PRIVATE_IPV4.finditer(text):
            tail = text[match.end():match.end() + 3]
            if tail.startswith("/10") or tail.startswith("/16"):
                continue
            errors.append(f"private address: {relative}")
            break
        if path.suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid JSON: {relative}: {exc}")

    if errors:
        print("release scan failed:")
        for error in sorted(set(errors)):
            print(f"  - {error}")
        return 1
    print("release scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
