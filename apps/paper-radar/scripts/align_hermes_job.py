#!/usr/bin/env python3
"""Keep the installed Hermes Paper Radar job on the canonical Edge OS tree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


JOB_NAME = "具身智讯日报"
WRAPPER_NAMES = ("paper-radar-collect.sh", "paper-radar-preview.sh")
LEGACY_PAPER_ROOT = "/home/geo/paper-radar"


def install_wrapper(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.riverbank")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o755)
    temporary.replace(target)


def align_daily_profile(home: Path, paper_root: Path) -> bool:
    """Replace the retired Paper Radar path without changing profile policy."""
    soul_path = home / ".hermes/profiles/daily/SOUL.md"
    if not soul_path.is_file():
        return False
    original = soul_path.read_text(encoding="utf-8")
    updated = original.replace(LEGACY_PAPER_ROOT, str(paper_root))
    if updated == original:
        return False
    temporary = soul_path.with_name(f".{soul_path.name}.riverbank")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, soul_path.stat().st_mode & 0o777)
    temporary.replace(soul_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--job-name", default=JOB_NAME)
    args = parser.parse_args()
    paper_root = args.repo.resolve() / "apps/paper-radar"
    if not (paper_root / "scripts/collect.py").is_file():
        raise SystemExit(f"invalid Paper Radar root: {paper_root}")

    script_dir = args.home / ".hermes/scripts"
    for wrapper_name in WRAPPER_NAMES:
        install_wrapper(paper_root / "hermes" / wrapper_name, script_dir / wrapper_name)
    profile_aligned = align_daily_profile(args.home, paper_root)

    jobs_path = args.home / ".hermes/cron/jobs.json"
    if not jobs_path.is_file():
        print(f"Hermes jobs file not present; installed wrappers in {script_dir}")
        return 0
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    matches = [item for item in jobs if isinstance(item, dict) and item.get("name") == args.job_name]
    if not matches:
        print(f"Hermes job {args.job_name!r} not created yet; installed wrappers only")
        return 0
    if len(matches) > 1:
        raise SystemExit(f"expected one Hermes job named {args.job_name!r}, found {len(matches)}")
    job = matches[0]
    hermes = args.home / ".local/bin/hermes"
    if not hermes.is_file():
        resolved = shutil.which("hermes")
        if not resolved:
            raise SystemExit("Hermes CLI is unavailable")
        hermes = Path(resolved)
    subprocess.run(
        [
            str(hermes),
            "cron",
            "--accept-hooks",
            "edit",
            str(job["id"]),
            "--workdir",
            str(paper_root),
            "--script",
            "paper-radar-collect.sh",
        ],
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )
    suffix = " and daily profile" if profile_aligned else ""
    print(f"aligned Hermes job {job.get('id')} to {paper_root}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
