#!/usr/bin/env python3
"""Contract, policy, streaming and cancellation checks for Agent Runtime v1."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "apps" / "agent-runtime"
sys.path.insert(0, str(RUNTIME_DIR))

from agent_runtime_service import AgentRuntimeServer  # noqa: E402
from riverbank_agent import (  # noqa: E402
    AgentCancelled,
    AgentRunRequest,
    AgentRuntimeError,
    HermesCLIAdapter,
    SocketAgentRuntime,
)


class FakeAdapter:
    name = "fake-agent"

    @staticmethod
    def available() -> bool:
        return True

    async def run(self, request, emit_snapshot, cancel_event):
        await emit_snapshot("正在处理")
        if request.prompt == "wait":
            while not cancel_event.is_set():
                await asyncio.sleep(0.01)
            raise AgentCancelled()
        await emit_snapshot("处理完成")
        return f"answer:{request.prompt}"


class AgentRuntimeProtocolTests(unittest.TestCase):
    def test_wire_round_trip_preserves_only_validated_fields(self) -> None:
        request = AgentRunRequest(
            prompt="你好",
            purpose="chat",
            workspace="daily",
            toolsets=("skills", "web", "skills"),
            source="test-suite",
            session="conversation-1",
            create_session=True,
        )
        restored = AgentRunRequest.from_wire(request.to_wire())
        self.assertEqual(restored.prompt, "你好")
        self.assertEqual(restored.toolsets, ("skills", "web"))
        self.assertEqual(restored.session, "conversation-1")

    def test_autonomy_is_denied_for_interactive_chat(self) -> None:
        with self.assertRaises(AgentRuntimeError) as caught:
            AgentRunRequest(prompt="run", purpose="chat", autonomy=True)
        self.assertEqual(caught.exception.code, "policy_denied")

    def test_hermes_adapter_translates_policy_not_arbitrary_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "hermes"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o700)
            workspace = root / "daily"
            image_root = root / "images"
            image_root.mkdir()
            image = image_root / "reference.png"
            image.write_bytes(b"image")
            adapter = HermesCLIAdapter(
                hermes_bin=binary,
                profile_home=root / "profile",
                workspaces={"daily": workspace},
                allowed_toolsets={"skills", "web"},
                image_roots=(image_root,),
            )
            request = AgentRunRequest(
                prompt="research",
                purpose="chat",
                toolsets=("skills", "web"),
                source="test-suite",
                image_path=str(image),
            )
            command, cwd, _environment = adapter.command(request)
            self.assertEqual(cwd, workspace.resolve(strict=False))
            self.assertIn("skills,web", command)
            self.assertIn(str(image.resolve()), command)
            self.assertNotIn("--yolo", command)

            denied = AgentRunRequest(
                prompt="research",
                purpose="chat",
                toolsets=("file",),
                source="test-suite",
            )
            with self.assertRaises(AgentRuntimeError) as caught:
                adapter.command(denied)
            self.assertEqual(caught.exception.code, "policy_denied")


class AgentRuntimeSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.socket = Path(self.temporary.name) / "runtime.sock"
        self.server = AgentRuntimeServer(
            socket_path=self.socket,
            adapter=FakeAdapter(),
            max_concurrency=2,
        )
        try:
            await self.server.start()
        except PermissionError:
            self.temporary.cleanup()
            self.skipTest("current host sandbox does not permit Unix-domain sockets")
        self.client = SocketAgentRuntime(self.socket)

    async def asyncTearDown(self) -> None:
        await self.server.close()
        self.temporary.cleanup()

    async def test_health_streaming_and_final_result(self) -> None:
        health = await asyncio.to_thread(self.client.health)
        self.assertTrue(health["ok"])
        self.assertEqual(health["protocol"], "riverbank.agent/v1")
        snapshots: list[str] = []
        result = await self.client.run_async(
            AgentRunRequest(prompt="hello", purpose="chat", toolsets=("skills",)),
            on_snapshot=snapshots.append,
        )
        self.assertEqual(result.text, "answer:hello")
        self.assertEqual(snapshots, ["正在处理", "处理完成"])

    async def test_async_cancel_callback_stops_remote_request(self) -> None:
        polls = 0

        async def should_cancel() -> bool:
            nonlocal polls
            polls += 1
            return polls > 2

        with self.assertRaises(AgentCancelled):
            await self.client.run_async(
                AgentRunRequest(prompt="wait", purpose="chat", toolsets=("skills",)),
                should_cancel=should_cancel,
            )


if __name__ == "__main__":
    unittest.main()
