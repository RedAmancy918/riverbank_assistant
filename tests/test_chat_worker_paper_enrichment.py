from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))
sys.path.insert(0, str(ROOT / "apps" / "paper-radar" / "scripts"))

from attachment_store import AttachmentStore  # noqa: E402
from chat_store import ChatStore  # noqa: E402
from chat_worker import ChatWorker  # noqa: E402
from image_generation import DashScopeImageGenerator  # noqa: E402
from paper_context import PAPER_SOURCE  # noqa: E402
from paper_qa import build_daily_knowledge, paper_qa_id  # noqa: E402


class ChatWorkerPaperEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.knowledge = self.root / "current.json"
        paper = {
            "title": "A Dexterous Platform",
            "authors": ["RiverBank Lab"],
            "url": "https://arxiv.org/abs/2609.00123",
            "reading_depth": "abstract",
            "summary": "The platform has dual dexterous hands.",
            "innovations": [],
            "method": "",
            "results": "",
            "limitations": "",
        }
        build_daily_knowledge(
            {
                "date": "2026-09-03",
                "papers": [paper],
                "potential_methods": [],
                "special_focus": [],
            },
            fetch_full=False,
            output_path=self.knowledge,
        )
        self.paper_id = paper_qa_id(paper)
        self.hermes = self.root / "fake-hermes"
        self.hermes.write_text(
            """#!/usr/bin/env python3
import sys
query = sys.argv[sys.argv.index('--query') + 1]
print('确认使用 Shadow Hand。' if 'Shadow Hand' in query else '没有补证。')
""",
            encoding="utf-8",
        )
        self.hermes.chmod(self.hermes.stat().st_mode | stat.S_IXUSR)
        self.enricher = self.root / "fake-enricher.py"
        self.enricher.write_text(
            """import json, sys
from pathlib import Path
path = Path(sys.argv[sys.argv.index('--knowledge') + 1])
payload = json.loads(path.read_text(encoding='utf-8'))
paper = payload['papers'][sys.argv[1]]
paper['chunks'].append({'label': 'PDF 第 6 页', 'text': 'The robot uses a Shadow Hand.', 'source': 'arxiv_pdf_on_demand'})
paper['source_state'] = 'arxiv_pdf_on_demand'
paper['on_demand'] = {'status': 'ready', 'target': 'original_paper'}
path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
print(json.dumps({'ok': True, 'changed': True, 'source_state': 'arxiv_pdf_on_demand'}))
""",
            encoding="utf-8",
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_detail_question_enriches_before_hermes_answer(self) -> None:
        store = ChatStore(self.root / "chat.db")
        conversation = store.create_conversation(
            title="Paper",
            source=PAPER_SOURCE,
            device_name=self.paper_id,
        )
        _user, assistant = store.create_turn(
            conversation["id"],
            content="请进一步确认它用的是什么型号？",
        )
        worker = ChatWorker(
            store=store,
            hermes_bin=self.hermes,
            workspace=self.root,
            attachment_store=AttachmentStore(self.root / "attachments"),
            image_generator=DashScopeImageGenerator(environment={}),
            paper_python=Path(sys.executable),
            paper_enricher=self.enricher,
            paper_enrichment_timeout=30,
            toolsets="skills",
            timeout_seconds=60,
            poll_seconds=0.1,
        )
        claimed = store.claim_next()
        assert claimed is not None
        with patch("chat_worker.DEFAULT_KNOWLEDGE_PATH", self.knowledge):
            await worker.run_turn(claimed)
        messages = store.list_messages(conversation["id"])
        self.assertEqual(messages[-1]["state"], "completed", messages[-1])
        self.assertEqual(messages[-1]["content"], "确认使用 Shadow Hand。")
        current = json.loads(self.knowledge.read_text(encoding="utf-8"))
        self.assertEqual(
            current["papers"][self.paper_id]["source_state"],
            "arxiv_pdf_on_demand",
        )


if __name__ == "__main__":
    unittest.main()
