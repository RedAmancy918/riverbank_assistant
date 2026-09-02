#!/usr/bin/env python3
"""Validate a Hermes report JSON and atomically render the private static site."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from paper_qa import annotate_report, build_daily_knowledge
from special_focus import mark_consumed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
PUBLIC = ROOT / "public"
REPORTS = ROOT / "reports"
LATEST_INPUT = ROOT / "data" / "generated" / "latest-report.json"
CANDIDATE_DIR = ROOT / "data" / "candidates"
DB_PATH = ROOT / "data" / "papers.db"
CONFIG_PATH = ROOT / "config" / "topics.json"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    required = ["date", "headline", "summary", "papers", "industry_updates"]
    missing = [key for key in required if key not in report]
    if missing:
        raise ValueError(f"report is missing required fields: {', '.join(missing)}")
    datetime.strptime(report["date"], "%Y-%m-%d")
    report.setdefault("potential_methods", [])
    report.setdefault("special_focus", [])
    if not all(
        isinstance(report[key], list)
        for key in ("papers", "potential_methods", "special_focus", "industry_updates")
    ):
        raise ValueError("papers, potential_methods, special_focus and industry_updates must be arrays")
    config = load_config()
    max_special_focus = int(config.get("max_special_focus_papers", 5))
    if len(report["special_focus"]) > max_special_focus:
        raise ValueError(f"special_focus cannot contain more than {max_special_focus} items")
    for paper in report["papers"] + report["potential_methods"] + report["special_focus"]:
        for key in ("title", "url", "reading_depth", "summary", "innovations"):
            if key not in paper:
                raise ValueError(f"paper is missing {key}: {paper.get('title', '<untitled>')}")
    for paper in report["potential_methods"]:
        outlook = paper.get("robotics_outlook", "")
        if not outlook.startswith("分析与推断："):
            raise ValueError(f"potential method must label robotics_outlook as 分析与推断: {paper.get('title')}")
    report.setdefault("generated_at", datetime.now().astimezone().isoformat())
    selected = report["papers"] + report["potential_methods"] + report["special_focus"]
    report.setdefault("candidate_count", len(selected))
    report.setdefault("deep_read_count", sum(p.get("reading_depth") == "full" for p in selected))
    report.setdefault("topics", ["VLA", "World Model", "Robot Learning"])
    report.setdefault("report_score_threshold", int(config.get("report_score", 40)))
    report.setdefault("max_report_papers", int(config.get("max_report_papers", 10)))
    report.setdefault("max_special_focus_papers", max_special_focus)
    return report


def load_candidates(report_date: str) -> dict[str, Any]:
    path = CANDIDATE_DIR / f"{report_date}.json"
    if not path.exists():
        return {
            "date": report_date,
            "generated_at": "",
            "candidate_count": 0,
            "embodied_robotics": [],
            "world_models": [],
            "spatial_foundation": [],
            "vlm": [],
            "potential_methods": [],
            "candidate_score_threshold": 10,
            "max_candidates": 30,
            "special_focus": None,
        }
    manifest = json.loads(path.read_text(encoding="utf-8"))
    candidates = manifest.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("candidate manifest candidates must be an array")
    for paper in candidates:
        for key in ("title", "abstract", "url"):
            if key not in paper:
                raise ValueError(f"candidate is missing {key}: {paper.get('title', '<untitled>')}")
    return {
        "date": report_date,
        "generated_at": manifest.get("generated_at", ""),
        "candidate_count": len(candidates),
        "candidate_score_threshold": manifest.get("selection_policy", {}).get("candidate_score", 10),
        "max_candidates": manifest.get("selection_policy", {}).get("max_candidates", 30),
        "special_focus": manifest.get("special_focus"),
        "embodied_robotics": [
            paper for paper in candidates if paper.get("candidate_section", "embodied_robotics") == "embodied_robotics"
        ],
        "world_models": [
            paper for paper in candidates if paper.get("candidate_section") == "world_model"
        ],
        "spatial_foundation": [
            paper for paper in candidates if paper.get("candidate_section") == "spatial_foundation"
        ],
        "vlm": [paper for paper in candidates if paper.get("candidate_section") == "vlm"],
        "potential_methods": [
            paper for paper in candidates if paper.get("candidate_section") == "potential_method"
        ],
    }


def carry_forward_industry(report: dict[str, Any]) -> None:
    if report.get("industry_updates"):
        return
    prior: list[tuple[str, Path]] = []
    for path in REPORTS.glob("*/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date", "") < report["date"] and data.get("industry_updates"):
                prior.append((data["date"], path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if not prior:
        return
    _, latest_path = max(prior, key=lambda item: item[0])
    previous = json.loads(latest_path.read_text(encoding="utf-8"))
    report["industry_updates"] = previous.get("industry_updates", [])
    report["industry_carried_forward"] = True


def report_arxiv_id(paper: dict[str, Any]) -> str:
    value = str(paper.get("arxiv_id", ""))
    if not value:
        url = str(paper.get("url", ""))
        if "arxiv.org/" not in url:
            return ""
        value = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", value)


def mark_reported(report: dict[str, Any]) -> None:
    if not DB_PATH.exists():
        return
    connection = sqlite3.connect(DB_PATH)
    for paper in (
        report.get("papers", [])
        + report.get("potential_methods", [])
        + report.get("special_focus", [])
    ):
        arxiv_id = report_arxiv_id(paper)
        if arxiv_id:
            connection.execute(
                "UPDATE papers SET reported_on = COALESCE(reported_on, ?) WHERE arxiv_id = ?",
                (report["date"], arxiv_id),
            )
    connection.commit()
    connection.close()


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['headline']}",
        "",
        f"> {report['date']} · 具身智讯 / Embodied Intelligence Daily",
        "",
        report["summary"],
        "",
    ]
    if report.get("special_focus_request"):
        lines.extend(
            [
                "## 特别关注",
                "",
                f"> 本期一次性关注：{report['special_focus_request']['description']}",
                "",
            ]
        )
        for index, paper in enumerate(report.get("special_focus", []), 1):
            depth = "全文精读" if paper.get("reading_depth") == "full" else "摘要速览"
            lines.extend(
                [
                    f"### F{index}. [{paper['title']}]({paper['url']})",
                    "",
                    f"- 阅读深度：{depth}",
                    f"- 作者：{', '.join(paper.get('authors', [])) or '未标注'}",
                    f"- 相关度：{paper.get('relevance_score', '—')}",
                    "",
                    "#### 内容概述",
                    "",
                    paper["summary"],
                    "",
                    "#### 核心创新",
                    "",
                ]
            )
            lines.extend(f"- {item}" for item in paper.get("innovations", []))
            if paper.get("method"):
                lines.extend(["", "#### 方法与实验", "", paper["method"]])
            if paper.get("limitations"):
                lines.extend(["", "#### 局限与判断", "", paper["limitations"]])
            lines.append("")
        if not report.get("special_focus"):
            lines.extend(["本次定向检索未发现足够匹配且有实质贡献的内容。", ""])
    lines.extend(["## 今日论文", ""])
    for index, paper in enumerate(report["papers"], 1):
        depth = "全文精读" if paper.get("reading_depth") == "full" else "摘要速览"
        lines.extend(
            [
                f"### {index}. [{paper['title']}]({paper['url']})",
                "",
                f"- 阅读深度：{depth}",
                f"- 作者：{', '.join(paper.get('authors', [])) or '未标注'}",
                f"- 相关度：{paper.get('relevance_score', '—')}",
                "",
                "#### 内容概述",
                "",
                paper["summary"],
                "",
                "#### 核心创新",
                "",
            ]
        )
        lines.extend(f"- {item}" for item in paper.get("innovations", []))
        if paper.get("method"):
            lines.extend(["", "#### 方法与实验", "", paper["method"]])
        if paper.get("limitations"):
            lines.extend(["", "#### 局限与判断", "", paper["limitations"]])
        lines.append("")
    if report["potential_methods"]:
        lines.extend(["## 潜在方法", ""])
        for index, paper in enumerate(report["potential_methods"], 1):
            depth = "全文精读" if paper.get("reading_depth") == "full" else "摘要速览"
            lines.extend(
                [
                    f"### {index}. [{paper['title']}]({paper['url']})",
                    "",
                    f"- 阅读深度：{depth}",
                    f"- 作者：{', '.join(paper.get('authors', [])) or '未标注'}",
                    f"- 相关度：{paper.get('relevance_score', '—')}",
                    "",
                    "#### 方法概述",
                    "",
                    paper["summary"],
                    "",
                    "#### 核心创新",
                    "",
                ]
            )
            lines.extend(f"- {item}" for item in paper.get("innovations", []))
            lines.extend(["", "#### 潜在机器人影响", "", paper["robotics_outlook"], ""])
    if report["industry_updates"]:
        lines.extend(["## 产业与实验室动态", ""])
        for item in report["industry_updates"]:
            lines.extend([f"### [{item['title']}]({item['url']})", "", item["summary"], ""])
    return "\n".join(lines).rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def archive_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in REPORTS.glob("*/*/*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            entries.append(
                {
                    "date": report["date"],
                    "headline": report["headline"],
                    "summary": report["summary"],
                    "paper_count": (
                        len(report.get("papers", []))
                        + len(report.get("potential_methods", []))
                        + len(report.get("special_focus", []))
                    ),
                    "deep_count": sum(
                        p.get("reading_depth") == "full"
                        for p in (
                            report.get("papers", [])
                            + report.get("potential_methods", [])
                            + report.get("special_focus", [])
                        )
                    ),
                    "url": f"/archive/{report['date']}/",
                }
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return sorted(entries, key=lambda item: item["date"], reverse=True)


def render(input_path: Path) -> None:
    report = load_report(input_path)
    carry_forward_industry(report)
    candidates = load_candidates(report["date"])
    manifest_focus = candidates.get("special_focus")
    existing_focus = report.get("special_focus_request")
    if manifest_focus:
        report["special_focus_request"] = manifest_focus
    elif (
        isinstance(existing_focus, dict)
        and existing_focus.get("target_date") == report["date"]
    ):
        report["special_focus_request"] = existing_focus
    else:
        report["special_focus_request"] = None
    if not report["special_focus_request"]:
        report["special_focus"] = []
    report["candidate_count"] = candidates["candidate_count"]
    report["deep_read_count"] = sum(
        paper.get("reading_depth") == "full"
        for paper in report["papers"] + report["potential_methods"] + report["special_focus"]
    )
    annotate_report(report)
    try:
        knowledge = build_daily_knowledge(report)
        report["paper_qa"] = {
            "enabled": True,
            "paper_count": knowledge["paper_count"],
            "full_source_count": knowledge["full_source_count"],
            "retention": knowledge["retention"],
        }
    except Exception as exc:
        report["paper_qa"] = {
            "enabled": False,
            "error": str(exc)[:500],
        }
    date = datetime.strptime(report["date"], "%Y-%m-%d")
    report_dir = REPORTS / date.strftime("%Y") / date.strftime("%m")
    report_json = report_dir / f"{report['date']}.json"
    report_md = report_dir / f"{report['date']}.md"
    atomic_write(report_json, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_write(report_md, markdown_report(report))
    atomic_write(input_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    mark_reported(report)

    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    report_template = environment.get_template("report.html")
    archive_template = environment.get_template("archive.html")
    candidates_template = environment.get_template("candidates.html")
    archive = archive_entries()
    report["display_date"] = date.strftime("%Y.%m.%d")
    report["archive_count"] = len(archive)

    PUBLIC.mkdir(parents=True, exist_ok=True)
    assets = PUBLIC / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATIC / "site.css", assets / "site.css")
    shutil.copy2(STATIC / "site.js", assets / "site.js")

    archive_html = report_template.render(report=report, canonical=f"/archive/{report['date']}/", is_latest=False)
    latest_html = report_template.render(report=report, canonical="/", is_latest=True)
    atomic_write(PUBLIC / "archive" / report["date"] / "index.html", archive_html)
    atomic_write(PUBLIC / "index.html", latest_html)
    atomic_write(PUBLIC / "candidates" / "index.html", candidates_template.render(candidates=candidates))
    atomic_write(PUBLIC / "candidates.json", json.dumps(candidates, ensure_ascii=False, indent=2) + "\n")
    atomic_write(PUBLIC / "archive" / "index.html", archive_template.render(entries=archive))
    atomic_write(PUBLIC / "archive.json", json.dumps(archive, ensure_ascii=False, indent=2) + "\n")
    if report["special_focus_request"]:
        consumed = mark_consumed(
            str(report["special_focus_request"]["id"]),
            report["date"],
            len(report["special_focus"]),
            str(load_config().get("timezone", "Asia/Shanghai")),
        )
        if not consumed:
            raise RuntimeError("special-focus request could not be marked as consumed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", type=Path, default=LATEST_INPUT)
    args = parser.parse_args()
    render(args.input.resolve())
    print(f"Rendered latest report from {args.input}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
