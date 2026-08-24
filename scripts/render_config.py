#!/usr/bin/env python3
"""Render machine-specific RiverBank configuration without changing the host."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import shutil
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOKENS = (
    "RIVERBANK_USER",
    "RIVERBANK_UID",
    "RIVERBANK_HOME",
    "RIVERBANK_REPO",
    "RIVERBANK_DATA",
    "RIVERBANK_HAILO_VENV",
)


def absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute: {value}")
    return path


def user_record(username: str) -> tuple[int, Path]:
    try:
        record = pwd.getpwnam(username)
    except KeyError as exc:
        raise ValueError(f"unknown local user: {username}") from exc
    return record.pw_uid, Path(record.pw_dir)


def substitute_tree(root: Path, replacements: dict[str, str]) -> None:
    unresolved = tuple(f"@{name}@".encode() for name in TOKENS)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        rendered = payload
        for name, value in replacements.items():
            rendered = rendered.replace(f"@{name}@".encode(), value.encode())
        if rendered != payload:
            path.write_bytes(rendered)
        if any(token in rendered for token in unresolved):
            raise ValueError(f"unresolved placeholder in {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=getpass.getuser())
    parser.add_argument("--home")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--hailo-venv")
    parser.add_argument("--output", default=str(REPO / "build/generated"))
    args = parser.parse_args()

    detected_uid, detected_home = user_record(args.user)
    uid = args.uid if args.uid is not None else detected_uid
    home = absolute_path(args.home, "home") if args.home else detected_home
    data_dir = absolute_path(args.data_dir, "data-dir")
    hailo_venv = absolute_path(
        args.hailo_venv
        or str(data_dir / "ai/apps/hailo-rpi5-examples/venv_hailo_rpi_examples"),
        "hailo-venv",
    )
    output = absolute_path(args.output, "output")

    safe_default = REPO / "build/generated"
    if output == REPO or REPO in output.parents and output != safe_default:
        if output.parent != REPO / "build":
            raise ValueError("output inside the repository must be under build/")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    shutil.copytree(REPO / "config", output / "config")
    shutil.copy2(
        REPO / "apps/health-monitor/config.example.json",
        output / "health-monitor.json",
    )

    replacements = {
        "RIVERBANK_USER": args.user,
        "RIVERBANK_UID": str(uid),
        "RIVERBANK_HOME": str(home),
        "RIVERBANK_REPO": str(REPO),
        "RIVERBANK_DATA": str(data_dir),
        "RIVERBANK_HAILO_VENV": str(hailo_venv),
    }
    substitute_tree(output, replacements)

    manifest = {
        "version": 1,
        "user": args.user,
        "uid": uid,
        "home": str(home),
        "repository": str(REPO),
        "data_dir": str(data_dir),
        "hailo_venv": str(hailo_venv),
        "generated_dir": str(output),
        "changes_host": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
