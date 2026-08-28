#!/usr/bin/env python3
"""Regression checks for semantic response expressions and streaming tag removal."""

from __future__ import annotations

import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "expression-ui"
sys.path.insert(0, str(APP_DIR))

from response_emotions import (  # noqa: E402
    ResponseEmotionStreamRouter,
    parse_response_expression,
    strip_response_emotion_tags,
)
from expression_events import ALLOWED_STATES  # noqa: E402


def main() -> None:
    assert {
        "thinking",
        "happy",
        "love",
        "proud",
        "cool",
        "sad",
        "cry",
        "afraid",
        "angry",
    }.issubset(ALLOWED_STATES)
    text, state, source = parse_response_expression(
        "[[emotion:proud]]恭喜你，这次真的做得很好。"
    )
    assert text == "恭喜你，这次真的做得很好。"
    assert (state, source) == ("proud", "model_tag")

    text, state, source = parse_response_expression("很遗憾，这次没有成功。")
    assert text == "很遗憾，这次没有成功。"
    assert (state, source) == ("sad", "content_fallback")

    text, state, source = parse_response_expression(
        "[[emotion:unsupported]]当然可以。"
    )
    assert text == "当然可以。"
    assert (state, source) == ("happy", "invalid_tag_fallback")

    forwarded: list[object] = []
    selected: list[tuple[str, str]] = []
    router = ResponseEmotionStreamRouter(
        forwarded.append,
        lambda state, source: selected.append((state, source)),
    )
    router.feed("[[emo")
    router.feed("tion:love]]很")
    router.feed("温暖。")
    final = router.finalize("[[emotion:love]]很温暖。")
    assert final == "很温暖。"
    assert forwarded == ["很", "温暖。"]
    assert selected == [("love", "model_tag")]

    assert strip_response_emotion_tags("[[emotion:angry]]不能这样。") == "不能这样。"
    assert strip_response_emotion_tags("[[emotion:sad") == ""

    text, state, source = parse_response_expression(
        "这是引用内容：[[emotion:angry]]，并不是控制标签。"
    )
    assert "emotion" in text
    assert (state, source) == ("thinking", "content_fallback")
    print("response emotions: all regression checks passed")


if __name__ == "__main__":
    main()
