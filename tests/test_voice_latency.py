#!/usr/bin/env python3
"""Regression checks for privacy-preserving rolling voice latency metrics."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "expression-ui"
    / "voice_latency.py"
)
SPEC = importlib.util.spec_from_file_location("voice_latency", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
latency = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(latency)


class VoiceLatencyTests(unittest.TestCase):
    def test_percentile_interpolates_small_samples(self) -> None:
        self.assertEqual(latency.percentile([1.0, 3.0], 0.5), 2.0)
        self.assertEqual(latency.percentile([1.0, 3.0], 0.95), 2.9)

    def test_completed_turn_persists_only_timings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.jsonl"
            monitor = latency.VoiceLatencyMonitor(path)
            monitor.start("hardware_voice")
            monitor.mark("speech_end", 10.0)
            monitor.mark("stt_started", 10.0)
            monitor.mark("stt_finished", 12.0)
            monitor.mark("llm_started", 12.0)
            monitor.mark("llm_first_delta", 15.0)
            monitor.mark("llm_finished", 16.0)
            monitor.mark("tts_started", 15.2)
            monitor.mark("tts_first_frame", 16.2)
            record = monitor.finish("completed")

            self.assertEqual(record["stt_seconds"], 2.0)
            self.assertEqual(record["llm_ttft_seconds"], 3.0)
            self.assertEqual(record["tts_first_frame_seconds"], 1.0)
            self.assertEqual(record["speech_end_to_first_audio_seconds"], 6.2)
            self.assertNotIn("transcript", path.read_text(encoding="utf-8"))
            summary = monitor.state()
            self.assertEqual(summary["completed_turns"], 1)
            self.assertEqual(
                summary["metrics"]["speech_end_to_first_audio_seconds"]["p50"],
                6.2,
            )


if __name__ == "__main__":
    unittest.main()
