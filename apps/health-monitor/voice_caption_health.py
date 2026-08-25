#!/usr/bin/env python3
"""Validate the local streaming-caption link without exposing transcript text."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


STATE_PATH = Path("/run/hermes-voice-control/state.json")


def main() -> int:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"voice state unavailable: {exc}")
        return 1

    caption = payload.get("live_caption")
    if not isinstance(caption, dict):
        print("voice state has no live_caption object")
        return 1
    if not caption.get("draft_engine_ready"):
        print(
            "streaming caption engine unavailable: "
            f"{caption.get('draft_engine_error') or 'unknown error'}"
        )
        return 1
    model_directory = Path(str(caption.get("model_directory", "")))
    required = (
        "tokens.txt",
        "encoder-epoch-99-avg-1.int8.onnx",
        "decoder-epoch-99-avg-1.int8.onnx",
        "joiner-epoch-99-avg-1.int8.onnx",
    )
    missing = [name for name in required if not (model_directory / name).is_file()]
    if missing:
        print(f"streaming caption model incomplete: {', '.join(missing)}")
        return 1
    final_asr = payload.get("final_asr")
    if not isinstance(final_asr, dict):
        print("voice state has no final_asr object")
        return 1
    if final_asr.get("engine") != "sensevoice-int8+zipformer-ctc-int8":
        print("final ASR ensemble is not selected")
        return 1
    if final_asr.get("decoder") != "parallel-context-router-signal-disagreement-gate":
        print("final ASR safety router is not selected")
        return 1
    if not final_asr.get("ready") or int(final_asr.get("models_loaded") or 0) < 2:
        service_age = max(0.0, time.time() - float(payload.get("started_at") or 0.0))
        if service_age < 35.0 and not final_asr.get("last_error"):
            print(
                "voice interaction chain warming up "
                f"final_asr={final_asr.get('engine')} age={service_age:.1f}s"
            )
            return 0
        print(
            "final ASR ensemble is not resident: "
            f"{final_asr.get('last_error') or 'models still loading'}"
        )
        return 1
    model_requirements = {
        "sensevoice_model_directory": ("model.int8.onnx", "tokens.txt"),
        "zipformer_model_directory": ("model.int8.onnx", "tokens.txt"),
    }
    for key, names in model_requirements.items():
        directory = Path(str(final_asr.get(key, "")))
        missing = [name for name in names if not (directory / name).is_file()]
        if missing:
            print(f"final ASR model incomplete at {directory}: {', '.join(missing)}")
            return 1
    streaming_tts = payload.get("streaming_tts")
    if not isinstance(streaming_tts, dict) or not streaming_tts.get("enabled"):
        print("streaming TTS is not enabled")
        return 1
    barge_in = payload.get("wake_barge_in")
    if not isinstance(barge_in, dict) or not barge_in.get("enabled"):
        print("wake-word barge-in is not enabled")
        return 1
    latency = payload.get("latency_monitor")
    if not isinstance(latency, dict) or latency.get("privacy") != "timings-only-no-transcripts":
        print("privacy-preserving voice latency monitor is not ready")
        return 1
    print(
        "voice interaction chain ready "
        f"caption={caption.get('draft_engine')} "
        f"final_asr={final_asr.get('engine')} "
        f"stream_tts={streaming_tts.get('strategy')} barge_in=enabled "
        f"model={model_directory}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
