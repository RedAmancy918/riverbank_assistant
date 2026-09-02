#!/usr/bin/env python3
"""Small trusted client used by voice and round-display Workshop surfaces."""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any


DEFAULT_SOCKET = Path("/run/riverbank-workshop/control.sock")


def request(payload: dict[str, Any], *, timeout: float = 2.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(max(0.1, timeout))
    try:
        client.connect(str(DEFAULT_SOCKET))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65535)
            if not chunk:
                break
            total += len(chunk)
            if total > 512 * 1024:
                raise ValueError("Workshop response is too large")
            chunks.append(chunk)
    finally:
        client.close()
    value = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Workshop response must be an object")
    return value


def error_message(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "工坊请求失败")
    return "工坊请求失败"


def create_app(requirement: str, source: str = "voice") -> dict[str, Any]:
    return request(
        {
            "command": "create",
            "requirement": requirement,
            "source": source,
        },
        timeout=3.0,
    )


def approve_proposal(proposal_id: str, capabilities: list[str]) -> dict[str, Any]:
    return request(
        {
            "command": "approve",
            "proposalId": proposal_id,
            "capabilities": capabilities,
        },
        timeout=15.0,
    )


def reject_proposal(proposal_id: str) -> dict[str, Any]:
    return request({"command": "reject", "proposalId": proposal_id}, timeout=3.0)


def launch_app_by_name(name: str) -> dict[str, Any]:
    return request({"command": "launch_by_name", "name": name}, timeout=5.0)


def stop_app() -> dict[str, Any]:
    return request({"command": "stop"}, timeout=5.0)


def is_workshop_create_intent(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text).lower()
    if "工坊" in compact and any(word in compact for word in ("创建", "做", "开发", "新增", "写")):
        return True
    has_artifact = any(
        word in compact
        for word in ("应用", "app", "小程序", "程序", "小工具", "功能")
    )
    has_creation = any(word in compact for word in ("创建", "做一个", "做个", "开发", "写一个", "写个", "新增"))
    return has_artifact and has_creation


def workshop_launch_name(text: str) -> str | None:
    compact = re.sub(r"[，。！？、,.!?;；:：]", "", text).strip()
    match = re.fullmatch(
        r"(?:请|帮我|给我)?(?:打开|启动|运行|进入)(?:一下)?(.{1,20}?)(?:应用|app)?",
        compact,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    name = match.group(1).strip()
    if name in {"相机", "摄像头", "相册", "音乐", "播放器", "番茄钟", "性能", "通话", "工坊"}:
        return None
    return name or None
