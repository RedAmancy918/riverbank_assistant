#!/usr/bin/env python3
"""Compare deterministic Faster-Whisper decoding profiles on real recordings."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from faster_whisper import WhisperModel


PROFILES = {
    "fast": {
        "beam_size": 1,
        "best_of": 1,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "vad_filter": False,
    },
    "balanced": {
        "beam_size": 3,
        "best_of": 3,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "vad_filter": False,
    },
    "accurate": {
        "beam_size": 5,
        "best_of": 5,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "vad_filter": False,
    },
}


def transcribe(model: WhisperModel, path: Path, profile: str) -> dict:
    started = time.monotonic()
    segments, info = model.transcribe(
        str(path),
        language="zh",
        **PROFILES[profile],
    )
    resolved = list(segments)
    elapsed = time.monotonic() - started
    return {
        "file": str(path),
        "profile": profile,
        "elapsed_seconds": round(elapsed, 3),
        "audio_seconds": round(float(getattr(info, "duration", 0.0) or 0.0), 3),
        "text": "".join(segment.text for segment in resolved).strip(),
        "segments": len(resolved),
        "avg_logprob": round(
            statistics.fmean(segment.avg_logprob for segment in resolved), 4,
        )
        if resolved
        else None,
        "max_compression_ratio": round(
            max(segment.compression_ratio for segment in resolved), 4,
        )
        if resolved
        else None,
        "max_no_speech_probability": round(
            max(segment.no_speech_prob for segment in resolved), 4,
        )
        if resolved
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=tuple(PROFILES),
        default=tuple(PROFILES),
    )
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    missing = [str(path) for path in args.audio if not path.is_file()]
    if missing:
        parser.error(f"missing audio files: {', '.join(missing)}")

    load_started = time.monotonic()
    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )
    results = []
    for _iteration in range(max(1, args.repeat)):
        for path in args.audio:
            for profile in args.profiles:
                result = transcribe(model, path, profile)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    print(
        json.dumps(
            {
                "summary": True,
                "model_load_and_run_seconds": round(
                    time.monotonic() - load_started,
                    3,
                ),
                "results": len(results),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
