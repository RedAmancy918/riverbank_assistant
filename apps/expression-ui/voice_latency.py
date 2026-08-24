#!/usr/bin/env python3
"""Privacy-preserving rolling latency metrics for the Daily voice path."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


TIMING_PAIRS = {
    "recording_seconds": ("recording_started", "speech_end"),
    "stt_seconds": ("stt_started", "stt_finished"),
    "llm_ttft_seconds": ("llm_started", "llm_first_delta"),
    "llm_total_seconds": ("llm_started", "llm_finished"),
    "tts_first_frame_seconds": ("tts_started", "tts_first_frame"),
    "speech_end_to_first_audio_seconds": ("speech_end", "tts_first_frame"),
    "speech_end_to_playback_complete_seconds": (
        "speech_end",
        "playback_finished",
    ),
}
SUMMARY_METRICS = (
    "speech_end_to_first_audio_seconds",
    "stt_seconds",
    "llm_ttft_seconds",
    "llm_total_seconds",
    "tts_first_frame_seconds",
)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)


class VoiceLatencyMonitor:
    """Track one foreground turn and persist only timings, never transcripts."""

    def __init__(self, path: Path, max_records: int = 500) -> None:
        self.path = path
        self.max_records = max(20, int(max_records))
        self.lock = threading.RLock()
        self.current: dict | None = None
        self.records = self._load_records()
        self.cached_summary = self._summarize(self.records)

    def _load_records(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict] = []
        for line in lines[-self.max_records :]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    @staticmethod
    def _summarize(records: list[dict]) -> dict:
        completed = [record for record in records if record.get("status") == "completed"]
        metrics: dict[str, dict] = {}
        for name in SUMMARY_METRICS:
            values = [
                float(record[name])
                for record in completed
                if isinstance(record.get(name), (int, float))
            ]
            metrics[name] = {
                "samples": len(values),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
            }
        return {
            "completed_turns": len(completed),
            "stored_turns": len(records),
            "metrics": metrics,
        }

    def start(self, source: str) -> None:
        with self.lock:
            self.current = {
                "schema": 1,
                "source": str(source),
                "started_at": time.time(),
                "marks": {"turn_started": time.monotonic()},
            }

    def mark(self, name: str, value: float | None = None) -> None:
        with self.lock:
            if self.current is None:
                return
            marks = self.current.setdefault("marks", {})
            marks[str(name)] = time.monotonic() if value is None else float(value)

    def finish(self, status: str) -> dict | None:
        with self.lock:
            if self.current is None:
                return None
            current = self.current
            self.current = None
            marks = dict(current.pop("marks", {}))
            record = {
                **current,
                "finished_at": time.time(),
                "status": str(status),
            }
            for output_name, (start_name, end_name) in TIMING_PAIRS.items():
                start = marks.get(start_name)
                end = marks.get(end_name)
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    record[output_name] = round(max(0.0, end - start), 3)
            self.records.append(record)
            self.records = self.records[-self.max_records :]
            self._persist()
            self.cached_summary = self._summarize(self.records)
            return record

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(
            self.path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in self.records
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def state(self) -> dict:
        with self.lock:
            return {
                "history_path": str(self.path),
                "privacy": "timings-only-no-transcripts",
                **json.loads(json.dumps(self.cached_summary)),
            }
