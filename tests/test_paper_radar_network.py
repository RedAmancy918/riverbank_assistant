#!/usr/bin/env python3
"""Regression checks for coordinated arXiv access and cooldown behavior."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "apps/paper-radar/scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from arxiv_access import ArxivAccess, ArxivCooldownError, ArxivRateLimitError  # noqa: E402
from catchup import report_is_current_and_complete  # noqa: E402
from collect import parse_oai_page  # noqa: E402
from finalize_run import finalized_source_status  # noqa: E402


class Clock:
    def __init__(self, value: float = 1_000.0):
        self.value = value
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class Response:
    def __init__(self, status: int, body: bytes = b"ok", headers: dict[str, str] | None = None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.url = "https://export.arxiv.org/api/query"
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.body

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responses: list[Response]):
        self.responses = responses
        self.calls = 0

    def get(self, *_args, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class ArxivAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = Clock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def access(self) -> ArxivAccess:
        return ArxivAccess(
            self.root,
            clock=self.clock.now,
            sleeper=self.clock.sleep,
            min_interval_seconds=4,
            default_cooldown_seconds=1800,
            max_cooldown_seconds=7200,
        )

    def test_429_opens_persistent_cooldown_without_retry(self) -> None:
        session = Session([Response(429)])
        with self.assertRaises(ArxivRateLimitError) as raised:
            self.access().fetch_bytes("https://export.arxiv.org/api/query", session=session)
        self.assertEqual(session.calls, 1)
        self.assertEqual(raised.exception.retry_after_seconds, 1800)
        with self.assertRaises(ArxivCooldownError):
            self.access().fetch_bytes("https://export.arxiv.org/api/query", session=session)
        self.assertEqual(session.calls, 1)

    def test_retry_after_is_honored_and_requests_are_spaced(self) -> None:
        session = Session([Response(200, b"one"), Response(429, headers={"Retry-After": "90"})])
        access = self.access()
        first = access.fetch_bytes("https://export.arxiv.org/api/query", session=session)
        self.assertEqual(first.body, b"one")
        with self.assertRaises(ArxivRateLimitError) as raised:
            access.fetch_bytes("https://export.arxiv.org/api/query", session=session)
        self.assertEqual(self.clock.sleeps, [4.0])
        self.assertEqual(raised.exception.retry_after_seconds, 90)

    def test_daily_cache_round_trip(self) -> None:
        access = self.access()
        access.write_daily_cache("api-query", "2026-09-06", "cat:cs.RO", [{"id": "1"}])
        self.assertEqual(
            access.read_daily_cache("api-query", "2026-09-06", "cat:cs.RO"),
            [{"id": "1"}],
        )

    def test_expired_cooldown_is_removed_from_persistent_state(self) -> None:
        self.access()._save_state(
            {
                "cooldown_until_epoch": self.clock.value - 1,
                "cooldown_until": "expired-value",
                "consecutive_rate_limits": 1,
            }
        )
        status = self.access().status()
        self.assertFalse(status["cooldown_active"])
        self.assertEqual(status["cooldown_until"], "")
        self.assertEqual(status["cooldown_until_epoch"], 0)
        self.assertEqual(status["consecutive_rate_limits"], 1)

    def test_degraded_report_keeps_remaining_catchup_window(self) -> None:
        report = {"date": "2026-09-06"}
        self.assertFalse(
            report_is_current_and_complete(report, {"state": "degraded"}, "2026-09-06")
        )
        self.assertTrue(
            report_is_current_and_complete(report, {"state": "ready"}, "2026-09-06")
        )

    def test_finalize_records_current_degraded_report_and_clears_old_retry(self) -> None:
        status = finalized_source_status(
            {"date": "2026-09-06", "paper_source_carried_forward": False},
            {
                "source_status": {
                    "state": "degraded",
                    "last_successful_report_date": "2026-09-05",
                    "retry_at": "expired-value",
                    "automatic_attempts": 2,
                }
            },
            {
                "cooldown_active": False,
                "cooldown_until": "expired-value",
                "retry_after_seconds": 0,
            },
        )
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["latest_report_date"], "2026-09-06")
        self.assertEqual(status["last_successful_report_date"], "2026-09-06")
        self.assertEqual(status["paper_source_date"], "2026-09-06")
        self.assertEqual(status["retry_at"], "")
        self.assertTrue(status["report_ready"])

    def test_finalize_preserves_carried_paper_source_date(self) -> None:
        status = finalized_source_status(
            {
                "date": "2026-09-06",
                "paper_source_carried_forward": True,
                "paper_source_date": "2026-09-05",
            },
            {"source_status": {"state": "cooldown", "automatic_attempts": 1}},
            {
                "cooldown_active": True,
                "cooldown_until": "2026-09-06T09:00:00+08:00",
                "retry_after_seconds": 300,
            },
        )
        self.assertEqual(status["latest_report_date"], "2026-09-06")
        self.assertEqual(status["last_successful_report_date"], "2026-09-05")
        self.assertEqual(status["retry_at"], "2026-09-06T09:00:00+08:00")

    def test_oai_parser_extracts_arxiv_record(self) -> None:
        payload = b'''<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords><record><header><identifier>oai:arXiv.org:2609.00001</identifier><datestamp>2026-09-05</datestamp></header>
  <metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>2609.00001</id><created>2026-09-05</created>
  <authors><author><keyname>Li</keyname><forenames>Fei-Fei</forenames></author></authors>
  <title>World model</title><categories>cs.RO cs.AI</categories><abstract>Embodied learning.</abstract></arXiv></metadata>
  </record><resumptionToken /></ListRecords></OAI-PMH>'''
        records, token = parse_oai_page(payload)
        self.assertEqual(token, "")
        self.assertEqual(records[0]["arxiv_id"], "2609.00001")
        self.assertEqual(records[0]["authors"], ["Fei-Fei Li"])
        self.assertEqual(records[0]["primary_category"], "cs.RO")


if __name__ == "__main__":
    unittest.main()
