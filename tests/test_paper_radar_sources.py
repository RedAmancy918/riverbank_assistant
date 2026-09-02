#!/usr/bin/env python3
"""Configuration checks for the Paper Radar industry source registry."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "paper-radar"
    / "config"
    / "topics.json"
)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class PaperRadarSourceTests(unittest.TestCase):
    def test_industry_source_registry_is_well_formed(self) -> None:
        config = load_config()
        sources = config["company_sources"]
        names = [source["name"] for source in sources]
        urls: list[str] = []

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(config["max_industry_updates"], 9)
        self.assertEqual(config["max_industry_updates_per_source"], 1)

        for source in sources:
            self.assertTrue(source["focus"].strip())
            source_urls = [source["url"], *source.get("additional_urls", [])]
            self.assertTrue(source_urls)
            self.assertTrue(all(url.startswith("https://") for url in source_urls))
            urls.extend(source_urls)

        self.assertEqual(len(urls), len(set(urls)))

    def test_high_signal_robotics_sources_are_registered(self) -> None:
        names = {source["name"] for source in load_config()["company_sources"]}
        expected = {
            "Boston Dynamics",
            "Agility Robotics",
            "Apptronik",
            "Sanctuary AI",
            "FieldAI",
            "Intrinsic",
            "Amazon Science / Amazon Robotics",
            "Robotics and AI Institute",
            "Toyota Research Institute",
        }
        self.assertLessEqual(expected, names)


if __name__ == "__main__":
    unittest.main()
