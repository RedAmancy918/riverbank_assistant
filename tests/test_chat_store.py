from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from chat_store import ChatStore  # noqa: E402


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ChatStore(Path(self.temporary.name) / "tasks.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_conversation_turn_lifecycle(self) -> None:
        conversation = self.store.create_conversation(source="ios")
        user, assistant = self.store.create_turn(
            conversation["id"], content="你好，介绍一下今天的计划"
        )
        self.assertEqual(user["state"], "completed")
        self.assertEqual(assistant["state"], "queued")

        claimed = self.store.claim_next()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], assistant["id"])
        self.assertEqual(claimed["state"], "running")

        self.store.replace_content(assistant["id"], "今天可以先处理日报")
        messages = self.store.list_messages(conversation["id"])
        self.assertEqual(messages[-1]["content"], "今天可以先处理日报")
        self.store.finish(assistant["id"], "今天可以先处理日报，再检查设备。")

        messages = self.store.list_messages(conversation["id"])
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["state"], "completed")
        self.assertIn("介绍一下", self.store.list_conversations()[0]["title"])

    def test_only_one_active_turn_per_conversation(self) -> None:
        conversation = self.store.create_conversation()
        self.store.create_turn(conversation["id"], content="第一个问题")
        with self.assertRaises(RuntimeError):
            self.store.create_turn(conversation["id"], content="第二个问题")

    def test_cancel_and_delete(self) -> None:
        conversation = self.store.create_conversation()
        _, assistant = self.store.create_turn(conversation["id"], content="停止这个回答")
        cancelled = self.store.request_cancel(assistant["id"])
        self.assertTrue(cancelled and cancelled["cancel_requested"])
        self.store.mark_cancelled(assistant["id"])
        self.assertTrue(self.store.delete_conversation(conversation["id"]))
        self.assertIsNone(self.store.get_conversation(conversation["id"]))

    def test_internal_paper_conversations_are_hidden_from_general_chat(self) -> None:
        visible = self.store.create_conversation(source="ios")
        hidden = self.store.create_conversation(
            source="paper-radar-internal",
            device_name="0123456789abcdefabcd",
        )
        listed = self.store.list_conversations()
        self.assertEqual([item["id"] for item in listed], [visible["id"]])
        with_internal = self.store.list_conversations(include_internal=True)
        self.assertEqual({item["id"] for item in with_internal}, {visible["id"], hidden["id"]})

    def test_conversations_and_messages_are_isolated_by_owner(self) -> None:
        alice = self.store.create_conversation(
            owner_user_id="alice-user-id",
            title="Alice private chat",
        )
        bob = self.store.create_conversation(
            owner_user_id="bob-user-id",
            title="Bob private chat",
        )
        self.store.create_turn(
            alice["id"],
            content="Alice secret",
            owner_user_id="alice-user-id",
        )

        self.assertEqual(
            [item["id"] for item in self.store.list_conversations(
                owner_user_id="alice-user-id"
            )],
            [alice["id"]],
        )
        self.assertEqual(
            [item["id"] for item in self.store.list_conversations(
                owner_user_id="bob-user-id"
            )],
            [bob["id"]],
        )
        self.assertIsNone(
            self.store.get_conversation(
                alice["id"], owner_user_id="bob-user-id"
            )
        )
        self.assertEqual(
            self.store.list_messages(
                alice["id"], owner_user_id="bob-user-id"
            ),
            [],
        )
        with self.assertRaises(KeyError):
            self.store.create_turn(
                alice["id"],
                content="Bob must not enter",
                owner_user_id="bob-user-id",
            )
        self.assertFalse(
            self.store.delete_conversation(
                alice["id"], owner_user_id="bob-user-id"
            )
        )

    def test_first_admin_can_claim_legacy_conversations(self) -> None:
        legacy = self.store.create_conversation(title="Old chat")
        internal = self.store.create_conversation(
            source="paper-radar-internal",
            title="Daily paper cache",
        )
        claimed = self.store.claim_unowned_conversations("first-admin-id")
        self.assertEqual(claimed, 1)
        self.assertIsNotNone(
            self.store.get_conversation(
                legacy["id"], owner_user_id="first-admin-id"
            )
        )
        self.assertIsNotNone(
            self.store.get_conversation(
                internal["id"], owner_user_id=""
            )
        )

    def test_owner_usage_and_cache_cleanup(self) -> None:
        conversation = self.store.create_conversation(
            owner_user_id="alice-user-id"
        )
        _user, assistant = self.store.create_turn(
            conversation["id"],
            content="Keep this private",
            owner_user_id="alice-user-id",
        )
        usage = self.store.owner_usage("alice-user-id")
        self.assertEqual(usage["conversations"], 1)
        self.assertEqual(usage["messages"], 2)
        self.assertEqual(usage["active_messages"], 1)
        with self.assertRaises(RuntimeError):
            self.store.delete_owner_cache("alice-user-id")
        self.store.mark_cancelled(assistant["id"])
        result = self.store.delete_owner_cache("alice-user-id")
        self.assertEqual(result["deleted_conversations"], 1)
        self.assertEqual(self.store.owner_usage("alice-user-id")["messages"], 0)


if __name__ == "__main__":
    unittest.main()
