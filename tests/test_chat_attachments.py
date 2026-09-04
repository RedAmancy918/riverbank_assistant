from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from attachment_store import AttachmentStore, AttachmentValidationError  # noqa: E402
from chat_store import ChatStore  # noqa: E402


class ChatAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = ChatStore(root / "chat.db")
        self.attachments = AttachmentStore(root / "attachments")
        self.conversation = self.store.create_conversation(source="desktop")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_text_attachment_is_persisted_and_private_fields_are_hidden(self) -> None:
        attachment = self.attachments.save(
            self.conversation["id"],
            "notes.md",
            "# 测试\n这是附件内容。".encode("utf-8"),
        )
        user, assistant = self.store.create_turn(
            self.conversation["id"],
            content="请总结附件",
            attachments=[attachment],
        )
        self.assertEqual(assistant["state"], "queued")
        self.assertEqual(user["attachments"][0]["original_name"], "notes.md")
        self.assertNotIn("storage_path", user["attachments"][0])
        self.assertNotIn("extracted_text", user["attachments"][0])

        claimed = self.store.claim_next()
        assert claimed is not None
        preceding = self.store.preceding_user_message(claimed["id"])
        assert preceding is not None
        private = preceding["attachments"][0]
        self.assertIn("这是附件内容", private["extracted_text"])
        self.assertTrue(self.attachments.resolve(private["storage_path"]).is_file())

    def test_attachment_only_message_uses_filename_as_title(self) -> None:
        image = Image.new("RGB", (24, 18), (12, 120, 200))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        attachment = self.attachments.save(
            self.conversation["id"],
            "camera.png",
            payload.getvalue(),
        )
        user, _assistant = self.store.create_turn(
            self.conversation["id"],
            content="",
            attachments=[attachment],
        )
        self.assertEqual(user["attachments"][0]["kind"], "image")
        self.assertEqual(
            self.store.get_conversation(self.conversation["id"])["title"],
            "附件：camera.png",
        )

    def test_rejects_executable_and_fake_image(self) -> None:
        with self.assertRaises(AttachmentValidationError):
            self.attachments.save(
                self.conversation["id"],
                "run.sh",
                b"#!/bin/sh\necho unsafe\n",
            )
        with self.assertRaises(AttachmentValidationError):
            self.attachments.save(
                self.conversation["id"],
                "fake.png",
                b"not a png",
            )
        with self.assertRaises(AttachmentValidationError):
            self.attachments.save(
                self.conversation["id"],
                "data.csv",
                b"name,value\nalpha,1\n",
            )

    def test_first_release_document_allowlist(self) -> None:
        markdown = self.attachments.save(
            self.conversation["id"],
            "research.markdown",
            "# 研究记录\n正文".encode("utf-8"),
        )
        text = self.attachments.save(
            self.conversation["id"],
            "notes.txt",
            "安全的纯文本".encode("utf-8"),
        )
        self.assertEqual(markdown["media_type"], "text/markdown")
        self.assertEqual(text["media_type"], "text/plain")

    def test_attachment_lookup_is_isolated_by_conversation_owner(self) -> None:
        conversation = self.store.create_conversation(owner_user_id="alice-user-id")
        attachment = self.attachments.save(
            conversation["id"],
            "private.md",
            b"private attachment",
        )
        self.store.create_turn(
            conversation["id"],
            content="read this",
            attachments=[attachment],
            owner_user_id="alice-user-id",
        )
        self.assertIsNotNone(
            self.store.get_attachment(
                conversation["id"],
                attachment["id"],
                owner_user_id="alice-user-id",
            )
        )
        self.assertIsNone(
            self.store.get_attachment(
                conversation["id"],
                attachment["id"],
                owner_user_id="bob-user-id",
            )
        )

    def test_worker_can_attach_generated_image_to_assistant_message(self) -> None:
        image = Image.new("RGB", (32, 32), (60, 170, 220))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        _user, assistant = self.store.create_turn(
            self.conversation["id"],
            content="帮我生成一张蓝色的图片",
        )
        generated = self.attachments.save(
            self.conversation["id"],
            "riverbank-generated.png",
            payload.getvalue(),
        )
        attached = self.store.add_attachment_to_message(
            assistant["id"],
            generated,
        )
        self.assertEqual(attached["message_id"], assistant["id"])
        self.store.finish(assistant["id"], "已经生成好了。")
        messages = self.store.list_messages(self.conversation["id"])
        self.assertEqual(messages[-1]["attachments"][0]["kind"], "image")
        self.assertEqual(
            messages[-1]["attachments"][0]["original_name"],
            "riverbank-generated.png",
        )


if __name__ == "__main__":
    unittest.main()
