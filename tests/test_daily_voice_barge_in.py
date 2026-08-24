#!/usr/bin/env python3
"""Focused regression checks for wake-word barge-in state transitions."""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path


for dependency in ("edge_tts", "numpy", "sounddevice"):
    sys.modules.setdefault(dependency, types.ModuleType(dependency))
faster_whisper = types.ModuleType("faster_whisper")
faster_whisper.WhisperModel = object
sys.modules.setdefault("faster_whisper", faster_whisper)


MODULE_PATH = Path(
    os.environ.get(
        "RIVERBANK_VOICE_MODULE_UNDER_TEST",
        Path(__file__).resolve().parents[1]
        / "apps"
        / "expression-ui"
        / "daily_voice_assistant.py",
    )
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("daily_voice_assistant", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
voice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice)


class FakeSpeechSession:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class FakePlayer:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class FakeHermes:
    def __init__(self) -> None:
        self.interrupted = False

    def interrupt(self) -> bool:
        self.interrupted = True
        return True


class FakeAgent:
    def __init__(self) -> None:
        self.hard_interrupted = False

    def hard_interrupt(self) -> None:
        self.hard_interrupted = True


class WakeBargeInTests(unittest.TestCase):
    @staticmethod
    def assistant() -> object:
        assistant = voice.DailyVoiceAssistant.__new__(voice.DailyVoiceAssistant)
        assistant.busy = True
        assistant.last_token = "old"
        assistant.barge_in_lock = threading.Lock()
        assistant.pending_wake = None
        assistant.interrupt_event = threading.Event()
        assistant.active_speech_session = FakeSpeechSession()
        assistant.active_player = FakePlayer()
        assistant.active_async_loop = None
        assistant.active_async_task = None
        assistant.barge_in_count = 0
        assistant.last_barge_in_at = None
        assistant.last_interrupted_stage = None
        assistant.last_result = "asking_hermes"
        assistant.hermes = FakeHermes()
        assistant.publish_speech_bubble = lambda *_args, **_kwargs: None
        assistant.write_state = lambda: None
        return assistant

    def test_new_wake_interrupts_and_replaces_active_turn(self) -> None:
        assistant = self.assistant()
        wake = {"received_at": 123.0, "message_id": 9, "angle": 30}

        self.assertTrue(assistant.request_barge_in(wake))
        self.assertTrue(assistant.interrupt_event.is_set())
        self.assertTrue(assistant.active_speech_session.aborted)
        self.assertTrue(assistant.active_player.terminated)
        self.assertTrue(assistant.hermes.interrupted)
        self.assertEqual(assistant.pop_pending_wake(), wake)
        self.assertEqual(assistant.last_interrupted_stage, "asking_hermes")
        self.assertEqual(assistant.barge_in_count, 1)

    def test_duplicate_wake_token_is_ignored(self) -> None:
        assistant = self.assistant()
        wake = {"received_at": 123.0, "message_id": 9}
        assistant.last_token = voice.wake_token(wake)

        self.assertFalse(assistant.request_barge_in(wake))
        self.assertFalse(assistant.interrupt_event.is_set())
        self.assertIsNone(assistant.pending_wake)

    def test_runtime_uses_agent_hard_interrupt(self) -> None:
        runtime = voice.PersistentHermesRuntime()
        runtime.agent = FakeAgent()
        runtime.request_active.set()

        self.assertTrue(runtime.interrupt())
        self.assertTrue(runtime.agent.hard_interrupted)
        self.assertTrue(runtime.interrupt_requested.is_set())

    def test_native_whisper_wait_can_be_abandoned_immediately(self) -> None:
        assistant = voice.DailyVoiceAssistant.__new__(voice.DailyVoiceAssistant)
        assistant.interrupt_event = threading.Event()

        def slow_transcribe(_path, _cancel_event) -> str:
            time.sleep(0.8)
            return "stale transcript"

        assistant.transcribe = slow_transcribe
        threading.Timer(0.05, assistant.interrupt_event.set).start()
        started = time.monotonic()
        with self.assertRaises(voice.InteractionInterrupted):
            assistant.transcribe_interruptibly(Path("unused.wav"))
        self.assertLess(time.monotonic() - started, 0.3)


if __name__ == "__main__":
    unittest.main()
