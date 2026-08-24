#!/usr/bin/env python3
"""Hermes shell hook that maps agent lifecycle and response tone to expressions."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path


SOCKET_PATH = Path("/run/riverbank-expression/control.sock")


def send(state: str, ttl: float | None = None) -> None:
    payload = {"state": state, "ttl": ttl}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(json.dumps(payload).encode("utf-8"), str(SOCKET_PATH))
        client.close()
    except OSError:
        pass


def classify(text: str) -> str:
    lowered = text.lower()
    groups = [
        ("angry", ("生气", "愤怒", "火大", "岂有此理")),
        ("afraid", ("危险", "立即断电", "紧急", "警告", "小心")),
        ("sad", ("抱歉", "遗憾", "对不起", "失败", "无法完成", "难过")),
        ("proud", ("成功", "已完成", "太好了", "恭喜", "很棒", "搞定")),
        ("love", ("喜欢", "感谢", "谢谢", "爱你")),
        ("cool", ("无语", "不建议", "不能执行", "不会执行", "拒绝")),
        ("happy", ("开心", "当然", "可以", "没问题", "好的", "好呀", "哈哈")),
    ]
    for state, words in groups:
        if any(word in lowered for word in words):
            return state
    return "happy"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "response"
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    extra = payload.get("extra") if isinstance(payload, dict) else {}
    extra = extra if isinstance(extra, dict) else {}

    if mode == "thinking":
        send("thinking")
    elif mode == "error":
        send("error", 8)
    else:
        response = str(extra.get("assistant_response") or extra.get("response_text") or "")
        ttl = max(8.0, min(30.0, 4.0 + len(response) * 0.18))
        send(classify(response), ttl)
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
