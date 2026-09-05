#!/usr/bin/env python3
"""Collect recent arXiv candidates for the Hermes daily paper-radar job."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from arxiv_access import (
    ArxivAccess,
    ArxivAccessError,
    ArxivCooldownError,
    ArxivNetworkError,
    atomic_write_json,
    cache_dates_around,
)
from special_focus import get_due_request

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "topics.json"
DB_PATH = ROOT / "data" / "papers.db"
CANDIDATE_DIR = ROOT / "data" / "candidates"
REPORTS_DIR = ROOT / "reports"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_OAI = "https://oaipmh.arxiv.org/oai"
ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
OAI = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/",
}


class NetworkUnavailableError(ArxivNetworkError):
    """The host cannot currently reach arXiv; stop multiplying identical retries."""


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def normalize_space(value: str) -> str:
    return " ".join(html.unescape(value or "").split())


def arxiv_id_from_url(url: str) -> str:
    value = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", value)


def fetch_feed(
    query: str,
    max_results: int = 75,
    *,
    report_date: str | None = None,
    access: ArxivAccess | None = None,
) -> list[dict[str, Any]]:
    access = access or ArxivAccess()
    cache_date = report_date or datetime.now().astimezone().date().isoformat()
    cache_key = json.dumps(
        {"query": query, "max_results": max_results, "sort": "submittedDate:descending"},
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = access.read_daily_cache("api-query", cache_date, cache_key)
    if isinstance(cached, list):
        return cached
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    try:
        payload = access.fetch_bytes(
            f"{ARXIV_API}?{params}",
            timeout=60,
            max_bytes=8 * 1024 * 1024,
            headers={"Accept": "application/atom+xml"},
        ).body
    except ArxivNetworkError as exc:
        raise NetworkUnavailableError(str(exc)) from exc
    records = parse_feed(payload)
    access.write_daily_cache("api-query", cache_date, cache_key, records)
    return records


def parse_feed(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    records: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ATOM):
        abs_url = normalize_space(entry.findtext("atom:id", default="", namespaces=ATOM))
        arxiv_id = arxiv_id_from_url(abs_url)
        links = {
            node.attrib.get("rel", "alternate"): node.attrib.get("href", "")
            for node in entry.findall("atom:link", ATOM)
        }
        categories = [node.attrib.get("term", "") for node in entry.findall("atom:category", ATOM)]
        primary = entry.find("arxiv:primary_category", ATOM)
        records.append(
            {
                "arxiv_id": arxiv_id,
                "title": normalize_space(entry.findtext("atom:title", default="", namespaces=ATOM)),
                "abstract": normalize_space(entry.findtext("atom:summary", default="", namespaces=ATOM)),
                "authors": [
                    normalize_space(node.findtext("atom:name", default="", namespaces=ATOM))
                    for node in entry.findall("atom:author", ATOM)
                ],
                "published": normalize_space(entry.findtext("atom:published", default="", namespaces=ATOM)),
                "updated": normalize_space(entry.findtext("atom:updated", default="", namespaces=ATOM)),
                "categories": categories,
                "primary_category": primary.attrib.get("term", "") if primary is not None else "",
                "url": abs_url or f"https://arxiv.org/abs/{arxiv_id}",
                "html_url": f"https://arxiv.org/html/{arxiv_id}",
                "pdf_url": links.get("related", f"https://arxiv.org/pdf/{arxiv_id}"),
            }
        )
    return records


def _oai_text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    return normalize_space(node.findtext(path, default="", namespaces=OAI))


def parse_oai_page(payload: bytes) -> tuple[list[dict[str, Any]], str]:
    root = ET.fromstring(payload)
    error = root.find("oai:error", OAI)
    if error is not None:
        code = str(error.attrib.get("code", ""))
        if code == "noRecordsMatch":
            return [], ""
        raise RuntimeError(f"arXiv OAI error {code or 'unknown'}: {normalize_space(error.text or '')}")
    records: list[dict[str, Any]] = []
    for item in root.findall(".//oai:record", OAI):
        header = item.find("oai:header", OAI)
        if header is None or header.attrib.get("status") == "deleted":
            continue
        metadata = item.find("oai:metadata/arxiv:arXiv", OAI)
        if metadata is None:
            continue
        arxiv_id = _oai_text(metadata, "arxiv:id")
        if not arxiv_id:
            identifier = _oai_text(header, "oai:identifier")
            arxiv_id = identifier.rsplit(":", 1)[-1]
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        authors: list[str] = []
        for author in metadata.findall("arxiv:authors/arxiv:author", OAI):
            forenames = _oai_text(author, "arxiv:forenames")
            keyname = _oai_text(author, "arxiv:keyname")
            name = normalize_space(f"{forenames} {keyname}")
            if name:
                authors.append(name)
        created = _oai_text(metadata, "arxiv:created")
        updated = _oai_text(metadata, "arxiv:updated") or created
        categories = _oai_text(metadata, "arxiv:categories").split()
        records.append(
            {
                "arxiv_id": arxiv_id,
                "title": _oai_text(metadata, "arxiv:title"),
                "abstract": _oai_text(metadata, "arxiv:abstract"),
                "authors": authors,
                "published": f"{created}T00:00:00Z" if created else "",
                "updated": f"{updated}T00:00:00Z" if updated else "",
                "categories": categories,
                "primary_category": categories[0] if categories else "",
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "html_url": f"https://arxiv.org/html/{arxiv_id}",
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            }
        )
    token = _oai_text(root, ".//oai:resumptionToken")
    return records, token


def fetch_oai_records(
    set_spec: str,
    from_date: str,
    categories: list[str],
    *,
    report_date: str,
    access: ArxivAccess,
    max_pages: int = 8,
) -> list[dict[str, Any]]:
    cache_key = json.dumps(
        {"set": set_spec, "from": from_date, "categories": sorted(categories)},
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = access.read_daily_cache("oai", report_date, cache_key)
    if isinstance(cached, list):
        return cached
    records: list[dict[str, Any]] = []
    token = ""
    for page in range(max_pages):
        if token:
            params = {"verb": "ListRecords", "resumptionToken": token}
        else:
            params = {
                "verb": "ListRecords",
                "metadataPrefix": "arXiv",
                "from": from_date,
                "set": set_spec,
            }
        url = f"{ARXIV_OAI}?{urllib.parse.urlencode(params)}"
        payload = access.fetch_bytes(
            url,
            timeout=90,
            max_bytes=16 * 1024 * 1024,
            headers={"Accept": "application/xml"},
        ).body
        page_records, token = parse_oai_page(payload)
        records.extend(page_records)
        if not token:
            break
    else:
        raise RuntimeError(f"arXiv OAI exceeded the safe page limit ({max_pages})")
    allowed = set(categories)
    if allowed:
        records = [record for record in records if allowed.intersection(record["categories"])]
    access.write_daily_cache("oai", report_date, cache_key, records)
    return records


def merge_stream_records(
    target: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    stream_id: str,
    stream_label: str,
) -> None:
    for record in records:
        existing = target.get(record["arxiv_id"])
        if existing is None:
            record["retrieval_streams"] = [stream_id]
            record["retrieval_stream_labels"] = [stream_label]
            target[record["arxiv_id"]] = record
        elif stream_id not in existing["retrieval_streams"]:
            existing["retrieval_streams"].append(stream_id)
            existing["retrieval_stream_labels"].append(stream_label)


def weighted_matches(haystack: str, weights: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    matches: list[str] = []
    for keyword, weight in weights.items():
        if keyword.lower() in haystack:
            score += int(weight)
            matches.append(keyword)
    return score, matches


def score_record(record: dict[str, Any], config: dict[str, Any]) -> tuple[int, list[str], str]:
    haystack = f"{record['title']} {record['abstract']}".lower()
    domain_score, domain_matches = weighted_matches(haystack, config["domain_keyword_weights"])
    spatial_score, spatial_matches = weighted_matches(haystack, config.get("foundation_vision_weights", {}))
    world_score, world_matches = weighted_matches(haystack, config.get("world_model_weights", {}))
    method_score, method_matches = weighted_matches(haystack, config["method_signal_weights"])
    impact_score, impact_matches = weighted_matches(haystack, config["robotics_impact_weights"])
    streams = record.get("retrieval_streams", [])
    direct_robotics = any(term.lower() in haystack for term in config["robot_anchor_terms"])
    negative_override_robotics = any(
        term.lower() in haystack for term in config.get("negative_override_robot_terms", [])
    )
    spatial_discovery = "foundation_vision_spatial" in streams and spatial_score >= 14
    world_model_discovery = "predictive_world_models" in streams and world_score >= 14
    score = domain_score + spatial_score + world_score + method_score + impact_score
    matches = domain_matches + spatial_matches + world_matches + method_matches + impact_matches
    if record.get("primary_category") == "cs.RO":
        score += 15
        matches.append("cs.RO")
    if direct_robotics:
        score += 15
    if "embodied_intelligence" in streams:
        score += 10
    if "robot_method_crossovers" in streams:
        score += 8
    if "upstream_methods" in streams and method_matches:
        score += 6
    if "vision_language_models" in streams:
        score += 6
        matches.append("VLM 专项检索")
    if spatial_discovery:
        score += 12
        matches.append("基础视觉与空间智能专项检索")
    if world_model_discovery:
        score += 15
        matches.append("JEPA 与预测世界模型专项检索")
    negatives = [item for item in config["negative_keywords"] if item.lower() in haystack]
    if negatives and not negative_override_robotics:
        score = 0
        matches.extend(f"排除领域：{item}" for item in negatives)
    direct_vlm = "vision_language_models" in streams and bool(domain_matches or impact_matches)
    direct_spatial = spatial_discovery
    direct_world_model = world_model_discovery
    if direct_world_model:
        candidate_section = "world_model"
    elif direct_robotics:
        candidate_section = "embodied_robotics"
    elif direct_spatial:
        candidate_section = "spatial_foundation"
    elif direct_vlm:
        candidate_section = "vlm"
    else:
        candidate_section = "potential_method"
    return max(0, min(100, score)), list(dict.fromkeys(matches)), candidate_section


def init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            published TEXT,
            first_seen_at TEXT NOT NULL,
            prefilter_score INTEGER NOT NULL,
            reported_on TEXT,
            candidate_on TEXT
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(papers)")}
    if "candidate_on" not in columns:
        connection.execute("ALTER TABLE papers ADD COLUMN candidate_on TEXT")
    connection.commit()


def history_arxiv_id(item: dict[str, Any]) -> str:
    if item.get("arxiv_id"):
        return arxiv_id_from_url(str(item["arxiv_id"]))
    url = str(item.get("url", ""))
    if "arxiv.org/" not in url:
        return ""
    return arxiv_id_from_url(url)


def upsert_history_paper(
    connection: sqlite3.Connection,
    item: dict[str, Any],
    date: str,
    column: str,
) -> None:
    arxiv_id = history_arxiv_id(item)
    if not arxiv_id:
        return
    connection.execute(
        """
        INSERT INTO papers
            (arxiv_id, title, published, first_seen_at, prefilter_score, reported_on, candidate_on)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE papers.title END,
            reported_on = COALESCE(papers.reported_on, excluded.reported_on),
            candidate_on = COALESCE(papers.candidate_on, excluded.candidate_on)
        """,
        (
            arxiv_id,
            item.get("title", ""),
            item.get("published"),
            date,
            int(item.get("prefilter_score", item.get("relevance_score", 0)) or 0),
            date if column == "reported_on" else None,
            date if column == "candidate_on" else None,
        ),
    )


def sync_history(connection: sqlite3.Connection) -> None:
    """Recover durable deduplication state from rendered archives and candidate manifests."""
    if CANDIDATE_DIR.exists():
        for path in sorted(CANDIDATE_DIR.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                date = str(manifest.get("report_date") or path.stem)
                for item in manifest.get("candidates", []):
                    upsert_history_paper(connection, item, date, "candidate_on")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    if REPORTS_DIR.exists():
        for path in sorted(REPORTS_DIR.glob("*/*/*.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                date = str(report.get("date") or path.stem)
                for item in report.get("papers", []) + report.get("potential_methods", []):
                    upsert_history_paper(connection, item, date, "reported_on")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    connection.commit()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_diverse_candidates(
    candidates: list[dict[str, Any]],
    max_candidates: int,
    section_targets: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve world-model, spatial-foundation, VLM, and method discovery without hard quotas."""
    sort_key = lambda item: (item["prefilter_score"], item["published"])
    fresh = sorted(
        (item for item in candidates if item["selection_bucket"] == "fresh"), key=sort_key, reverse=True
    )
    backfill = sorted(
        (item for item in candidates if item["selection_bucket"] == "backfill"), key=sort_key, reverse=True
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    for section, raw_target in section_targets.items():
        target = max(0, int(raw_target))
        section_pool = [item for item in fresh if item["candidate_section"] == section]
        section_pool.extend(item for item in backfill if item["candidate_section"] == section)
        for item in section_pool[:target]:
            if item["arxiv_id"] not in selected_ids:
                selected.append(item)
                selected_ids.add(item["arxiv_id"])

    for item in fresh + backfill:
        if len(selected) >= max_candidates:
            break
        if item["arxiv_id"] not in selected_ids:
            selected.append(item)
            selected_ids.add(item["arxiv_id"])

    bucket_order = {"fresh": 1, "backfill": 0}
    return sorted(
        selected[:max_candidates],
        key=lambda item: (bucket_order[item["selection_bucket"]], item["prefilter_score"], item["published"]),
        reverse=True,
    )


def latest_successful_report_date() -> str:
    path = ROOT / "data" / "generated" / "latest-report.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("paper_source_carried_forward") is True:
        return str(payload.get("paper_source_date") or "")
    return str(payload.get("date") or "")


def collect(
    config: dict[str, Any],
    force_all: bool = False,
    *,
    automatic: bool = False,
) -> dict[str, Any]:
    tz = ZoneInfo(config["timezone"])
    local_now = datetime.now(tz)
    report_date = local_now.date().isoformat()
    special_focus = get_due_request(report_date)
    utc_now = local_now.astimezone(timezone.utc)
    fresh_window_hours = int(config.get("fresh_window_hours", 72))
    paper_lookback_days = int(config.get("paper_lookback_days", 183))
    fresh_cutoff = utc_now - timedelta(hours=fresh_window_hours)
    archive_cutoff = utc_now - timedelta(days=paper_lookback_days)
    all_records: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    access = ArxivAccess(
        min_interval_seconds=float(config.get("request_interval_seconds", 4.0)),
        default_cooldown_seconds=int(config.get("rate_limit_cooldown_seconds", 1800)),
        max_cooldown_seconds=int(config.get("rate_limit_max_cooldown_seconds", 7200)),
    )
    access.prune_daily_cache(cache_dates_around(local_now.date(), 2))
    previous_status = access.read_source_status()
    previous_automatic_attempts = (
        int(previous_status.get("automatic_attempts") or 0)
        if previous_status.get("attempted_report_date") == report_date
        else 0
    )
    automatic_attempts = previous_automatic_attempts + (1 if automatic else 0)
    if automatic and automatic_attempts > int(config.get("max_automatic_attempts", 3)):
        raise RuntimeError("当天自动采集已达到初次运行加两次补跑的上限")
    access.write_source_status(
        {
            "state": "collecting",
            "message": "正在增量读取 arXiv 元数据",
            "attempted_report_date": report_date,
            "automatic_attempts": automatic_attempts,
            "last_successful_report_date": latest_successful_report_date(),
        }
    )

    cooldown_error: ArxivCooldownError | None = None
    consecutive_network_failures = 0
    network_open = False
    oai_from = (local_now.date() - timedelta(days=max(2, (fresh_window_hours // 24) + 1))).isoformat()
    for stream in config["retrieval_streams"]:
        for oai_set in stream.get("oai_sets", []):
            try:
                records = fetch_oai_records(
                    str(oai_set["set"]),
                    oai_from,
                    [str(item) for item in oai_set.get("categories", [])],
                    report_date=report_date,
                    access=access,
                )
                merge_stream_records(all_records, records, stream["id"], stream["label"])
                consecutive_network_failures = 0
            except ArxivCooldownError as exc:
                cooldown_error = exc
                errors.append(f"{stream['id']} OAI: {exc}")
                break
            except ArxivNetworkError as exc:
                consecutive_network_failures += 1
                errors.append(f"{stream['id']} OAI: {exc}")
                if consecutive_network_failures >= 2:
                    network_open = True
                    errors.append("network circuit opened; skipped remaining arXiv sources")
                    break
            except (ArxivAccessError, RuntimeError, ValueError, ET.ParseError) as exc:
                errors.append(f"{stream['id']} OAI: {exc}")
        if cooldown_error or network_open:
            break

    stream_queries = [
        (stream["id"], stream["label"], query)
        for stream in config["retrieval_streams"]
        for query in stream.get("queries", [])
    ]
    if not cooldown_error and not network_open:
        for index, (stream_id, stream_label, query) in enumerate(stream_queries):
            try:
                records = fetch_feed(
                    query,
                    int(config.get("max_results_per_query", 75)),
                    report_date=report_date,
                    access=access,
                )
                merge_stream_records(all_records, records, stream_id, stream_label)
                consecutive_network_failures = 0
            except ArxivCooldownError as exc:
                cooldown_error = exc
                errors.append(f"{stream_id} query {index + 1}: {exc}")
                errors.append(
                    f"rate-limit circuit opened; skipped {len(stream_queries) - index - 1} remaining queries"
                )
                break
            except NetworkUnavailableError as exc:
                consecutive_network_failures += 1
                errors.append(f"{stream_id} query {index + 1}: {exc}")
                if consecutive_network_failures >= 2:
                    errors.append(
                        f"network circuit opened; skipped {len(stream_queries) - index - 1} remaining queries"
                    )
                    break
            except (ArxivAccessError, RuntimeError, ValueError, ET.ParseError) as exc:
                errors.append(f"{stream_id} query {index + 1}: {exc}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    init_db(connection)
    sync_history(connection)
    connection.execute(
        "UPDATE papers SET candidate_on = NULL WHERE candidate_on = ?",
        (report_date,),
    )
    candidates: list[dict[str, Any]] = []
    new_seen = 0

    for record in all_records.values():
        try:
            published_at = parse_timestamp(record["published"])
            if published_at < archive_cutoff:
                continue
        except ValueError:
            published_at = utc_now
        score, matches, candidate_section = score_record(record, config)
        existing = connection.execute(
            "SELECT candidate_on, reported_on FROM papers WHERE arxiv_id = ?", (record["arxiv_id"],)
        ).fetchone()
        is_new = existing is None
        if is_new:
            new_seen += 1
            connection.execute(
                "INSERT INTO papers (arxiv_id, title, published, first_seen_at, prefilter_score) VALUES (?, ?, ?, ?, ?)",
                (record["arxiv_id"], record["title"], record["published"], local_now.isoformat(), score),
            )
            candidate_on = None
            reported_on = None
        else:
            candidate_on, reported_on = existing
            connection.execute(
                "UPDATE papers SET title = ?, published = ?, prefilter_score = ? WHERE arxiv_id = ?",
                (record["title"], record["published"], score, record["arxiv_id"]),
            )
        already_used = (
            (candidate_on and candidate_on != report_date)
            or (reported_on and reported_on != report_date)
        )
        if (force_all or not already_used) and score >= int(config["candidate_score"]):
            record["prefilter_score"] = score
            record["candidate_score"] = score
            record["candidate_section"] = candidate_section
            record["matched_keywords"] = matches
            record["selection_bucket"] = "fresh" if published_at >= fresh_cutoff else "backfill"
            record["age_days"] = max(0, (utc_now - published_at).days)
            candidates.append(record)

    candidates = select_diverse_candidates(
        candidates,
        int(config["max_candidates"]),
        config.get("candidate_section_targets", {}),
    )
    for item in candidates:
        connection.execute(
            "UPDATE papers SET candidate_on = COALESCE(candidate_on, ?) WHERE arxiv_id = ?",
            (report_date, item["arxiv_id"]),
        )
    connection.commit()
    connection.close()

    if cooldown_error:
        source_state = "cooldown"
    elif errors and not all_records:
        source_state = "unavailable"
    elif errors:
        source_state = "degraded"
    else:
        source_state = "ready"
    access_state = access.status()
    retry_at = str(access_state.get("cooldown_until") or "")
    last_successful = latest_successful_report_date()
    source_message = {
        "ready": "arXiv 增量元数据采集完成",
        "degraded": "arXiv 部分来源暂不可用，已使用成功结果与当天缓存",
        "cooldown": "arXiv 已触发限流冷却，保留最近一次成功日报并等待补跑",
        "unavailable": "arXiv 当前不可用，保留最近一次成功日报并等待补跑",
    }[source_state]
    source_status = {
        "state": source_state,
        "message": source_message,
        "attempted_report_date": report_date,
        "automatic_attempts": automatic_attempts,
        "last_successful_report_date": last_successful,
        "retry_at": retry_at,
        "retry_after_seconds": int(access_state.get("retry_after_seconds") or 0),
        "total_fetched": len(all_records),
        "error_count": len(errors),
    }
    access.write_source_status(source_status)

    manifest = {
        "report_date": report_date,
        "generated_at": local_now.isoformat(),
        "timezone": config["timezone"],
        "fresh_window_hours": fresh_window_hours,
        "paper_lookback_days": paper_lookback_days,
        "total_fetched": len(all_records),
        "new_seen": new_seen,
        "candidate_count": len(candidates),
        "fresh_candidate_count": sum(item["selection_bucket"] == "fresh" for item in candidates),
        "backfill_candidate_count": sum(item["selection_bucket"] == "backfill" for item in candidates),
        "errors": errors,
        "source_status": source_status,
        "retrieval_streams": config["retrieval_streams"],
        "selection_policy": {
            "candidate_score": config["candidate_score"],
            "max_candidates": config["max_candidates"],
            "candidate_section_targets": config.get("candidate_section_targets", {}),
            "paper_lookback_days": paper_lookback_days,
            "fresh_window_hours": fresh_window_hours,
            "max_report_papers": config["max_report_papers"],
            "max_deep_reads": config["max_deep_reads"],
            "report_score": config["report_score"],
            "deep_read_score": config["deep_read_score"],
            "industry_lookback_days": config["industry_lookback_days"],
            "max_industry_updates": config["max_industry_updates"],
            "max_industry_updates_per_source": config["max_industry_updates_per_source"],
            "max_special_focus_papers": int(config.get("max_special_focus_papers", 5)),
        },
        "special_focus": special_focus if source_state not in {"cooldown", "unavailable"} else None,
        "company_sources": config["company_sources"],
        "candidates": candidates,
    }
    output_path = CANDIDATE_DIR / f"{report_date}.json"
    atomic_write_json(output_path, manifest)
    return {**manifest, "candidate_file": str(output_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-all", action="store_true", help="include already-seen papers for a manual dry run")
    parser.add_argument("--automatic", action="store_true", help="count this as a scheduled run or catch-up")
    args = parser.parse_args()
    try:
        result = collect(load_config(), force_all=args.force_all, automatic=args.automatic)
    except Exception as exc:
        print(f"PAPER_RADAR_COLLECTION_FAILED\nerror={exc}")
        return 1
    print("PAPER_RADAR_MANIFEST")
    print(f"report_date={result['report_date']}")
    print(f"candidate_file={result['candidate_file']}")
    print(f"candidate_count={result['candidate_count']}")
    print(f"fresh_candidate_count={result['fresh_candidate_count']}")
    print(f"backfill_candidate_count={result['backfill_candidate_count']}")
    print(f"total_fetched={result['total_fetched']}")
    print(f"source_state={result['source_status']['state']}")
    print(f"automatic_attempts={result['source_status']['automatic_attempts']}")
    print(f"retry_at={result['source_status']['retry_at']}")
    print(f"special_focus={json.dumps(result['special_focus'], ensure_ascii=False)}")
    print(f"errors={json.dumps(result['errors'], ensure_ascii=False)}")
    print("Read the candidate JSON and follow AGENTS.md Daily Run Procedure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
