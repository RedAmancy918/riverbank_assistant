from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "paper-radar" / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from paper_chat_proxy import PaperChatProxy  # noqa: E402
from paper_context import (  # noqa: E402
    build_paper_prompt,
    paper_enrichment_plan,
    retrieve_chunks,
)
from paper_enrich import PDF_SOURCE, enrich_current_paper  # noqa: E402
from paper_qa import annotate_report, build_daily_knowledge, paper_qa_id  # noqa: E402


def sample_paper(arxiv_id: str, title: str) -> dict[str, object]:
    return {
        "title": title,
        "authors": ["RiverBank Lab"],
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "reading_depth": "full",
        "summary": "该工作研究动作条件世界模型。",
        "innovations": ["提出新的预测目标。"],
        "method": "模型使用动作条件潜空间动力学。",
        "results": "实验包含三个机器人任务和消融研究。",
        "limitations": "长时预测仍可能累积误差。",
    }


class FakePaperChatProxy(PaperChatProxy):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.messages: list[dict[str, object]] = []

    def backend_request(self, path, *, method="GET", body=None, timeout=20.0):
        if path == "/api/v1/chats" and method == "POST":
            return {"conversation": {"id": "conversation-123456"}}
        if path.endswith("/messages") and method == "POST":
            user = {"id": "u1", "role": "user", "content": body["content"], "state": "completed"}
            assistant = {"id": "a1", "role": "assistant", "content": "", "state": "queued"}
            self.messages.extend([user, assistant])
            return {"user_message": user, "assistant_message": assistant}
        if path.endswith("/messages") and method == "GET":
            return {"messages": list(self.messages)}
        raise AssertionError((path, method, body, timeout))


class PaperKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_daily_buffer_is_replaced_not_accumulated(self) -> None:
        output = self.root / "current.json"
        first = {"date": "2026-09-01", "papers": [sample_paper("2609.00001", "First")], "potential_methods": [], "special_focus": []}
        second = {"date": "2026-09-02", "papers": [sample_paper("2609.00002", "Second")], "potential_methods": [], "special_focus": []}
        build_daily_knowledge(first, fetch_full=False, output_path=output)
        build_daily_knowledge(second, fetch_full=False, output_path=output)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["date"], "2026-09-02")
        self.assertEqual(list(payload["papers"]), [paper_qa_id(second["papers"][0])])

    def test_temp_full_text_is_added_to_current_buffer(self) -> None:
        temp_sources = self.root / "sources"
        temp_sources.mkdir()
        (temp_sources / "2609.00003.json").write_text(
            json.dumps(
                {
                    "abstract": "Action-conditioned predictive model.",
                    "sections": [
                        {"heading": "Method", "body": "The loss aligns future latent states with action-conditioned predictions."},
                        {"heading": "Experiments", "body": "The ablation removes action inputs and reduces success rate."},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = {"date": "2026-09-02", "papers": [sample_paper("2609.00003", "Full")], "potential_methods": [], "special_focus": []}
        with patch("paper_qa.TEMP_SOURCE_DIR", temp_sources):
            payload = build_daily_knowledge(report, output_path=self.root / "current.json")
        record = next(iter(payload["papers"].values()))
        self.assertEqual(record["source_state"], "arxiv_html")
        self.assertTrue(any(item["label"] == "Experiments" for item in record["chunks"]))

    def test_prompt_retrieves_experimental_evidence(self) -> None:
        paper = sample_paper("2609.00004", "Evidence")
        report = {"date": "2026-09-02", "papers": [paper], "potential_methods": [], "special_focus": []}
        path = self.root / "current.json"
        payload = build_daily_knowledge(report, fetch_full=False, output_path=path)
        record = next(iter(payload["papers"].values()))
        selected = retrieve_chunks(record, "消融实验验证了什么？")
        self.assertTrue(any("实验" in item["label"] for item in selected))
        prompt = build_paper_prompt("消融实验验证了什么？", paper_qa_id(paper), knowledge_path=path)
        self.assertIn("优先复用下方", prompt)
        self.assertIn("实验包含三个机器人任务", prompt)

    def test_specific_followup_requests_original_paper_enrichment(self) -> None:
        paper = sample_paper("2609.00006", "Detailed hardware")
        path = self.root / "current.json"
        build_daily_knowledge(
            {
                "date": "2026-09-02",
                "papers": [paper],
                "potential_methods": [],
                "special_focus": [],
            },
            fetch_full=False,
            output_path=path,
        )
        paper_id = paper_qa_id(paper)
        plan = paper_enrichment_plan(
            "那你进一步确认一下，灵巧手用的是什么型号？",
            paper_id,
            knowledge_path=path,
        )
        self.assertTrue(plan["needed"])
        self.assertIn(plan["reason"], {"explicit_followup", "specific_detail"})

    def test_on_demand_pdf_is_written_to_current_day_only_and_reused(self) -> None:
        paper = sample_paper("2609.00007", "Hardware details")
        path = self.root / "current.json"
        report = {
            "date": "2026-09-02",
            "papers": [paper],
            "potential_methods": [],
            "special_focus": [],
        }
        build_daily_knowledge(
            report,
            fetch_full=False,
            output_path=path,
        )
        paper_id = paper_qa_id(paper)
        result = enrich_current_paper(
            paper_id,
            knowledge_path=path,
            pdf_loader=lambda _arxiv_id: b"%PDF-1.7 fake",
            pdf_extractor=lambda _payload: [
                {
                    "label": "PDF 第 6 页",
                    "text": "The platform uses two Shadow Robot Dexterous Hands with tactile fingertips.",
                    "source": PDF_SOURCE,
                }
            ],
        )
        self.assertTrue(result["changed"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload["papers"][paper_id]
        self.assertEqual(record["source_state"], PDF_SOURCE)
        self.assertEqual(record["on_demand"]["retention"], "replace_on_next_successful_daily_run")
        self.assertTrue(any(item["source"] == PDF_SOURCE for item in record["chunks"]))
        with patch("paper_qa.CURRENT_PATH", path):
            build_daily_knowledge(report, fetch_full=False, output_path=path)
        rerendered = json.loads(path.read_text(encoding="utf-8"))["papers"][paper_id]
        self.assertEqual(rerendered["source_state"], PDF_SOURCE)
        self.assertTrue(any(item["source"] == PDF_SOURCE for item in rerendered["chunks"]))
        followup = paper_enrichment_plan(
            "它具体是哪种型号？",
            paper_id,
            knowledge_path=path,
        )
        self.assertFalse(followup["needed"])
        prompt = build_paper_prompt(
            "它具体是哪种型号？",
            paper_id,
            knowledge_path=path,
            investigation_note="已按需重新读取论文 PDF。",
        )
        self.assertIn("Shadow Robot Dexterous Hands", prompt)
        self.assertIn("已按需重新读取论文 PDF", prompt)

    def test_proxy_persists_mapping_and_returns_history(self) -> None:
        paper = sample_paper("2609.00005", "Chat")
        report = {"date": "2026-09-02", "papers": [paper], "potential_methods": [], "special_focus": []}
        knowledge = self.root / "current.json"
        build_daily_knowledge(report, fetch_full=False, output_path=knowledge)
        proxy = FakePaperChatProxy(
            knowledge_path=knowledge,
            mapping_path=self.root / "conversations.json",
            token_file=self.root / "unused-token",
        )
        paper_id = paper_qa_id(paper)
        queued = proxy.enqueue(paper_id, "方法的核心是什么？")
        self.assertEqual(queued["assistant_message"]["state"], "queued")
        history = proxy.history(paper_id)
        self.assertTrue(history["knowledge_available"])
        self.assertEqual(len(history["messages"]), 2)
        self.assertTrue((self.root / "conversations.json").exists())

    def test_report_renders_one_lazy_chat_panel_per_selected_paper(self) -> None:
        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ModuleNotFoundError:
            self.skipTest("Jinja2 is not installed in this lightweight test environment")
        report = json.loads(
            (ROOT / "apps" / "paper-radar" / "sample-report.json").read_text(
                encoding="utf-8"
            )
        )
        annotate_report(report)
        report.update(
            {
                "display_date": "2026.09.02",
                "archive_count": 1,
                "special_focus_request": None,
            }
        )
        environment = Environment(
            loader=FileSystemLoader(ROOT / "apps" / "paper-radar" / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        html = environment.get_template("report.html").render(
            report=report,
            canonical="/",
            is_latest=True,
        )
        selected_count = len(report["papers"]) + len(report["potential_methods"])
        self.assertEqual(html.count("data-paper-chat data-paper-id="), selected_count)
        for paper in report["papers"] + report["potential_methods"]:
            self.assertIn(f'data-paper-id="{paper["qa_id"]}"', html)


if __name__ == "__main__":
    unittest.main()
