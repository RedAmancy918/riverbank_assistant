from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from attachment_store import AttachmentStore  # noqa: E402
from chat_store import ChatStore  # noqa: E402
from chat_worker import ChatWorker  # noqa: E402
from image_generation import GeneratedImage  # noqa: E402


class FakeImageGenerator:
    available = True
    model = "fake-qwen-image"

    def generate(self, _prompt: object) -> GeneratedImage:
        image = Image.new("RGB", (36, 28), (44, 160, 210))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        return GeneratedImage(
            payload=payload.getvalue(),
            filename="generated.png",
            media_type="image/png",
            model=self.model,
            request_id="mock-request",
        )


class ChatWorkerImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.root = root
        self.generated_cache = root / "cache"
        self.generated_cache.mkdir()
        self.store = ChatStore(root / "chat.db")
        self.attachments = AttachmentStore(root / "attachments")
        self.worker = ChatWorker(
            store=self.store,
            hermes_bin=root / "hermes-must-not-run",
            workspace=root,
            attachment_store=self.attachments,
            image_generator=FakeImageGenerator(),
            paper_python=root / "paper-python",
            paper_enricher=root / "paper-enricher.py",
            paper_enrichment_timeout=60,
            toolsets="skills",
            timeout_seconds=60,
            poll_seconds=0.1,
            generated_image_roots=(self.generated_cache,),
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_explicit_image_request_bypasses_hermes_and_returns_attachment(self) -> None:
        conversation = self.store.create_conversation(owner_user_id="alice-user-id")
        _user, assistant = self.store.create_turn(
            conversation["id"],
            content="请生成一张清晨河岸的插画",
            owner_user_id="alice-user-id",
        )
        claimed = self.store.claim_next()
        assert claimed is not None
        await self.worker.run_turn(claimed)
        messages = self.store.list_messages(
            conversation["id"],
            owner_user_id="alice-user-id",
        )
        generated = messages[-1]
        self.assertEqual(generated["id"], assistant["id"])
        self.assertEqual(generated["state"], "completed")
        self.assertEqual(generated["attachments"][0]["kind"], "image")
        self.assertEqual(generated["attachments"][0]["original_name"], "generated.png")

    async def test_imports_hermes_generated_image_from_trusted_cache(self) -> None:
        conversation = self.store.create_conversation(owner_user_id="alice-user-id")
        _user, assistant = self.store.create_turn(
            conversation["id"],
            content="请生成一张热力环流图",
            owner_user_id="alice-user-id",
        )
        generated_path = self.generated_cache / "browser-result.png"
        Image.new("RGB", (40, 30), (190, 70, 50)).save(generated_path)

        response = await self.worker.import_generated_images(
            assistant["id"],
            conversation["id"],
            f"图片已经生成：\n{generated_path}\n请查看。",
        )

        self.assertNotIn(str(generated_path), response)
        self.assertIn("图片已附在本条消息中", response)
        messages = self.store.list_messages(
            conversation["id"],
            owner_user_id="alice-user-id",
        )
        self.assertEqual(messages[-1]["attachments"][0]["kind"], "image")
        self.assertEqual(
            messages[-1]["attachments"][0]["original_name"],
            "browser-result.png",
        )

    async def test_does_not_import_image_outside_trusted_cache(self) -> None:
        outside = self.root / "outside.png"
        Image.new("RGB", (20, 20), (10, 20, 30)).save(outside)
        self.assertEqual(
            self.worker.trusted_generated_image_paths(str(outside)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
