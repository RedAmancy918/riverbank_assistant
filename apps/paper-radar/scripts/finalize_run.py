#!/usr/bin/env python3
"""Stable final validation and render entrypoint for every Paper Radar run."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from arxiv_access import ArxivAccess


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "data" / "generated" / "latest-report.json"
PYTHON = ROOT / ".venv" / "bin" / "python"
RENDER = ROOT / "scripts" / "render.py"


def load_object(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return payload


def title_key(item: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(item.get("title") or "").lower())


def validate(report_path: Path, candidate_path: Path) -> tuple[dict, dict]:
    report = load_object(report_path)
    candidates = load_object(candidate_path)
    report_date = str(report.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("报告 date 缺失或格式错误")
    if str(candidates.get("report_date") or "") != report_date:
        raise ValueError("报告日期与 candidate_file 日期不一致")

    for field in ("papers", "potential_methods", "special_focus", "industry_updates"):
        if not isinstance(report.get(field), list):
            raise ValueError(f"报告字段 {field} 必须是数组")
    items = candidates.get("candidates")
    if not isinstance(items, list):
        raise ValueError("candidate_file.candidates 必须是数组")
    if len(items) > 30:
        raise ValueError(f"候选数量 {len(items)} 超过 30")
    missing = [item.get("title") for item in items if not str(item.get("summary_zh") or "").strip()]
    if missing:
        raise ValueError(f"仍有 {len(missing)} 篇候选缺少 summary_zh")

    selected = report["papers"] + report["potential_methods"]
    if len(selected) > 10:
        raise ValueError(f"精选数量 {len(selected)} 超过 10")
    if len(report["special_focus"]) > 5:
        raise ValueError("特别关注数量超过 5")
    if len(report["industry_updates"]) > 9:
        raise ValueError("产业动态数量超过 9")
    keys = [title_key(item) for item in selected]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("精选内容包含空标题或重复标题")
    for item in selected:
        if float(item.get("relevance_score", -1)) < 40:
            raise ValueError(f"精选低于 40 分：{item.get('title')}")
    for item in report["potential_methods"]:
        outlook = str(item.get("robotics_outlook") or "")
        if not outlook.startswith("分析与推断："):
            raise ValueError(f"潜在方法缺少明确推断标记：{item.get('title')}")
    return report, candidates


def finalized_source_status(
    report: dict,
    candidates: dict,
    access_state: dict,
) -> dict:
    source = dict(candidates.get("source_status") or {})
    report_date = str(report.get("date") or "")
    carried_forward = report.get("paper_source_carried_forward") is True
    paper_source_date = (
        str(report.get("paper_source_date") or source.get("last_successful_report_date") or "")
        if carried_forward
        else report_date
    )
    cooldown_active = bool(access_state.get("cooldown_active"))
    source.update(
        {
            "latest_report_date": report_date,
            "last_successful_report_date": paper_source_date,
            "paper_source_date": paper_source_date,
            "report_ready": True,
            "retry_at": (
                str(access_state.get("cooldown_until") or "")
                if cooldown_active
                else ""
            ),
            "retry_after_seconds": (
                int(access_state.get("retry_after_seconds") or 0)
                if cooldown_active
                else 0
            ),
        }
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--candidate-file", type=Path)
    args = parser.parse_args()
    try:
        report = load_object(args.report)
        report_date = str(report.get("date") or "")
        candidate_path = args.candidate_file or (
            ROOT / "data" / "candidates" / f"{report_date}.json"
        )
        report, candidates = validate(args.report, candidate_path)
        subprocess.run(
            [str(PYTHON), str(RENDER), str(args.report)],
            cwd=ROOT,
            check=True,
        )
        access = ArxivAccess()
        access.write_source_status(
            finalized_source_status(report, candidates, access.status())
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"finalize failed: {exc}", file=sys.stderr)
        return 1
    print(
        "finalize ok: "
        f"date={report['date']} candidates={len(candidates['candidates'])} "
        f"selected={len(report['papers']) + len(report['potential_methods'])} "
        f"special={len(report['special_focus'])} industry={len(report['industry_updates'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
