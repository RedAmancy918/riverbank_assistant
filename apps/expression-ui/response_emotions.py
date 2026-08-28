#!/usr/bin/env python3
"""Validated, speech-safe response emotion controls for RiverBank voice replies."""

from __future__ import annotations

import re
from collections.abc import Callable


EMOTION_TO_EXPRESSION = {
    "thinking": "thinking",
    "happy": "happy",
    "love": "love",
    "proud": "proud",
    "cool": "cool",
    "sad": "sad",
    "cry": "cry",
    "afraid": "afraid",
    "angry": "angry",
}
SEMANTIC_EXPRESSION_STATES = frozenset(EMOTION_TO_EXPRESSION.values())
EMOTION_TAG_RE = re.compile(
    r"\[\[\s*emotion\s*:\s*([a-z_]+)\s*\]\]",
    re.IGNORECASE,
)
LEADING_EMOTION_TAG_RE = re.compile(
    r"^\s*\[\[\s*emotion\s*:\s*([a-z_]+)\s*\]\]",
    re.IGNORECASE,
)
LEADING_INCOMPLETE_TAG_RE = re.compile(
    r"^\s*\[\[\s*emotion\s*:[^\]\r\n]{0,48}(?:\]\])?\s*",
    re.IGNORECASE,
)


def infer_response_expression(text: str) -> str:
    """Conservative fallback when a provider omits the explicit control tag."""
    value = re.sub(r"\s+", "", str(text)).lower()
    cue_groups = (
        ("cry", ("请节哀", "节哀顺变", "真的令人心碎")),
        ("afraid", ("请立即远离", "请立刻远离", "有生命危险", "马上报警")),
        ("angry", ("这完全不能接受", "这太过分了", "非常不负责任")),
        ("proud", ("值得骄傲", "为你骄傲", "做得太棒了", "恭喜你完成")),
        ("love", ("真的很温暖", "我很喜欢", "真替你开心", "好暖心")),
        ("sad", ("很遗憾", "不幸的是", "听起来很难受", "抱歉听到")),
        ("cool", ("太酷了", "很酷", "漂亮，搞定", "这就很帅")),
        ("happy", ("太好了", "好消息", "当然可以", "没问题", "搞定了")),
    )
    for expression_state, cues in cue_groups:
        if any(cue.lower() in value for cue in cues):
            return expression_state
    return "thinking"


def parse_response_expression(text: str) -> tuple[str, str, str]:
    """Return speech text, validated expression state and selection source."""
    value = str(text)
    match = LEADING_EMOTION_TAG_RE.match(value)
    if match:
        label = match.group(1).lower()
        cleaned = (value[: match.start()] + value[match.end() :]).strip()
        expression_state = EMOTION_TO_EXPRESSION.get(label)
        if expression_state is not None:
            return cleaned, expression_state, "model_tag"
        return cleaned, infer_response_expression(cleaned), "invalid_tag_fallback"
    cleaned = LEADING_INCOMPLETE_TAG_RE.sub("", value, count=1).strip()
    return cleaned, infer_response_expression(cleaned), "content_fallback"


def strip_response_emotion_tags(text: str) -> str:
    """Remove complete or truncated emotion controls before visible/speech output."""
    value = EMOTION_TAG_RE.sub("", str(text))
    return LEADING_INCOMPLETE_TAG_RE.sub("", value, count=1)


class ResponseEmotionStreamRouter:
    """Intercept a fragmented leading control tag before forwarding speech deltas."""

    TAG_PREFIX = "[[emotion:"

    def __init__(
        self,
        downstream: Callable[[object], None] | None,
        on_expression: Callable[[str, str], None],
    ) -> None:
        self.downstream = downstream
        self.on_expression = on_expression
        self.buffer = ""
        self.resolved = False
        self.expression_state: str | None = None
        self.selection_source: str | None = None

    def _forward(self, value: object) -> None:
        if callable(self.downstream):
            self.downstream(value)

    def _select(self, state: str, source: str) -> None:
        self.resolved = True
        self.expression_state = state
        self.selection_source = source
        self.on_expression(state, source)

    def feed(self, delta: object) -> None:
        if delta is None:
            if self.resolved or not self.buffer:
                self._forward(None)
            return
        value = str(delta)
        if not value:
            return
        if self.resolved:
            self._forward(value)
            return
        self.buffer += value
        stripped = self.buffer.lstrip()
        lowered = stripped.lower()
        match = EMOTION_TAG_RE.match(stripped)
        if match:
            label = match.group(1).lower()
            state = EMOTION_TO_EXPRESSION.get(label, "thinking")
            source = "model_tag" if label in EMOTION_TO_EXPRESSION else "invalid_tag"
            remainder = stripped[match.end() :].lstrip()
            self.buffer = ""
            self._select(state, source)
            if remainder:
                self._forward(remainder)
            return
        if self.TAG_PREFIX.startswith(lowered) or lowered.startswith(self.TAG_PREFIX):
            if len(stripped) <= 64 and "]]" not in stripped:
                return
            cleaned, state, source = parse_response_expression(stripped)
            self.buffer = ""
            self._select(state, source)
            if cleaned:
                self._forward(cleaned)
            return
        buffered = self.buffer
        self.buffer = ""
        state = infer_response_expression(buffered)
        self._select(state, "stream_content_fallback")
        self._forward(buffered)

    def finalize(self, final_text: str) -> str:
        cleaned, state, source = parse_response_expression(final_text)
        if not self.resolved or self.expression_state != state:
            self._select(state, source)
        self.buffer = ""
        return cleaned
