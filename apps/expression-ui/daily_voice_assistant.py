#!/usr/bin/env python3
"""Hands-free Daily Hermes loop driven by ListenGo hardware wake events."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import io
import json
import logging
import os
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from flight_search import (
    extract_flight_route,
    format_spoken_result,
    is_flight_price_request,
    search_next_week,
)
from voice_latency import VoiceLatencyMonitor
from expression_events import ExpressionEventPublisher
from response_emotions import (
    ResponseEmotionStreamRouter,
    SEMANTIC_EXPRESSION_STATES,
    parse_response_expression,
    strip_response_emotion_tags,
)
from pomodoro_voice import (
    parse_application_voice_command,
    parse_pomodoro_voice_command,
)
from video_call_client import activate_call, hangup_call
from workshop_client import (
    create_app as create_workshop_app,
    error_message as workshop_error_message,
    is_workshop_create_intent,
    launch_app_by_name as launch_workshop_app_by_name,
    stop_app as stop_workshop_app,
    workshop_launch_name,
)

try:
    import sherpa_onnx
except (ImportError, OSError):
    sherpa_onnx = None


APP_DIR = Path(__file__).resolve().parent
RIVERBANK_HOME = Path(os.environ.get("RIVERBANK_HOME", Path.home()))
LISTENGO_STATE = Path("/run/listengo-mic/state.json")
RUNTIME_DIR = Path("/run/hermes-voice-control")
SOCKET_PATH = RUNTIME_DIR / "control.sock"
STATE_PATH = RUNTIME_DIR / "state.json"
DAILY_HOME = Path(
    os.environ.get(
        "RIVERBANK_DAILY_HOME",
        RIVERBANK_HOME / ".hermes/profiles/daily",
    )
)
WORKSPACE = Path(
    os.environ.get("RIVERBANK_DAILY_WORKSPACE", DAILY_HOME / "workspace")
)
REPORTS_DIR = Path(
    os.environ.get("RIVERBANK_REPORTS_DIR", WORKSPACE / "reports")
)
DAILY = os.environ.get("RIVERBANK_DAILY_BIN", str(RIVERBANK_HOME / ".local/bin/daily"))
EXPRESSION_SOCKET = Path("/run/riverbank-expression/control.sock")
EXPRESSION_EVENTS = ExpressionEventPublisher()
CAMERA_SNAPSHOT_URL = os.environ.get(
    "RIVERBANK_CAMERA_SNAPSHOT_URL",
    "http://127.0.0.1:19733/snapshot",
)
CAMERA_QUICK_FRAME = WORKSPACE / "rgb_now.jpg"
BACKGROUND_TASK_API_URL = os.environ.get(
    "RIVERBANK_BACKGROUND_TASK_API_URL",
    "http://127.0.0.1:19734/api/v1/tasks",
)
BACKGROUND_TASK_TOKEN_FILE = Path(
    os.environ.get(
        "RIVERBANK_BACKGROUND_TASK_TOKEN_FILE",
        RIVERBANK_HOME / ".config/riverbank-video-call/token",
    )
)
DEFAULT_WAKE_ACK_PATH = APP_DIR / "assets/audio/wake_ack.wav"
if not DEFAULT_WAKE_ACK_PATH.is_file():
    installed_wake_ack = APP_DIR / "assets/wake_ack_geo.wav"
    if installed_wake_ack.is_file():
        DEFAULT_WAKE_ACK_PATH = installed_wake_ack
WAKE_ACK_PATH = Path(
    os.environ.get(
        "RIVERBANK_WAKE_ACK_PATH",
        DEFAULT_WAKE_ACK_PATH,
    )
)
WAKE_ACK_TEXT = os.environ.get("RIVERBANK_WAKE_ACK_TEXT", "嗨，Geo。")
HARDWARE_WAKE_PHRASE = os.environ.get(
    "RIVERBANK_HARDWARE_WAKE_PHRASE",
    "小飞小飞",
).strip() or "小飞小飞"
WAKE_ACK_TAIL_SECONDS = 0.10
SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.05
PRE_ROLL_SECONDS = 0.45
DEBOUNCE_SECONDS = 8.0
FOLLOW_UP_NO_SPEECH_SECONDS = 12.0
MAX_VOICE_FOLLOW_UP_TURNS = 4
LIVE_CAPTION_FINAL_HOLD_SECONDS = 1.0
STREAM_TTS_ENABLED = os.environ.get(
    "RIVERBANK_STREAM_TTS_ENABLED",
    "1",
).strip().lower() in {"1", "true", "yes", "on"}
STREAM_TTS_MIN_CHARS = max(
    6,
    int(os.environ.get("RIVERBANK_STREAM_TTS_MIN_CHARS", "10")),
)
STREAM_TTS_HARD_CHARS = max(
    STREAM_TTS_MIN_CHARS + 4,
    int(os.environ.get("RIVERBANK_STREAM_TTS_HARD_CHARS", "24")),
)
STREAM_TTS_VOICE = os.environ.get(
    "RIVERBANK_STREAM_TTS_VOICE",
    "zh-CN-XiaoxiaoNeural",
)
VOICE_METRICS_PATH = Path(
    os.environ.get(
        "RIVERBANK_VOICE_METRICS_PATH",
        RIVERBANK_HOME / ".local/state/riverbank/voice-latency.jsonl",
    )
)
WHISPER_MODEL = os.environ.get("RIVERBANK_WHISPER_MODEL", "base").strip() or "base"
WHISPER_POOL_SIZE = max(
    1,
    min(3, int(os.environ.get("RIVERBANK_WHISPER_POOL_SIZE", "2"))),
)
WHISPER_HOTWORDS = os.environ.get(
    "RIVERBANK_WHISPER_HOTWORDS",
    "",
).strip()
WHISPER_PRIMARY_OPTIONS = {
    "beam_size": 5,
    "best_of": 5,
    "temperature": 0.0,
    "condition_on_previous_text": False,
    "without_timestamps": True,
    "vad_filter": False,
}
WHISPER_FALLBACK_OPTIONS = {
    "beam_size": 1,
    "best_of": 5,
    "temperature": 0.2,
    "condition_on_previous_text": False,
    "without_timestamps": True,
    "vad_filter": False,
}
DAILY_TOOLSETS = tuple(
    value.strip()
    for value in os.environ.get(
        "RIVERBANK_DAILY_TOOLSETS",
        "browser,clarify,cronjob,file,memory,session_search,skills,todo,vision,web",
    ).split(",")
    if value.strip()
)
DAILY_AGENT_MAX_TURNS = max(
    4,
    int(os.environ.get("RIVERBANK_DAILY_AGENT_MAX_TURNS", "12")),
)
DAILY_AGENT_TIMEOUT_SECONDS = max(
    20.0,
    float(os.environ.get("RIVERBANK_DAILY_AGENT_TIMEOUT", "75")),
)
DAILY_MCP_DISCOVERY = os.environ.get(
    "RIVERBANK_DAILY_MCP_DISCOVERY",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}
STREAMING_ASR_MODEL_DIR = Path(
    os.environ.get(
        "RIVERBANK_STREAMING_ASR_MODEL_DIR",
        "/mnt/nvme64/ai/models/"
        "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23",
    )
)
FINAL_ASR_MODEL_ROOT = Path(
    os.environ.get(
        "RIVERBANK_FINAL_ASR_MODEL_ROOT",
        "/mnt/nvme64/ai/models/asr-final",
    )
)
FINAL_ASR_SENSEVOICE_DIR = Path(
    os.environ.get(
        "RIVERBANK_FINAL_ASR_SENSEVOICE_DIR",
        FINAL_ASR_MODEL_ROOT / "sensevoice-int8",
    )
)
FINAL_ASR_ZIPFORMER_DIR = Path(
    os.environ.get(
        "RIVERBANK_FINAL_ASR_ZIPFORMER_DIR",
        FINAL_ASR_MODEL_ROOT / "zipformer-ctc-int8",
    )
)
FINAL_ASR_THREADS = max(
    1,
    min(2, int(os.environ.get("RIVERBANK_FINAL_ASR_THREADS", "1"))),
)
FINAL_ASR_MIN_CENTERED_RMS = max(
    0.0,
    float(os.environ.get("RIVERBANK_FINAL_ASR_MIN_CENTERED_RMS", "350")),
)
FINAL_ASR_MIN_CENTERED_PEAK = max(
    0.0,
    float(os.environ.get("RIVERBANK_FINAL_ASR_MIN_CENTERED_PEAK", "900")),
)
FINAL_ASR_MIN_AGREEMENT = max(
    0.0,
    min(1.0, float(os.environ.get("RIVERBANK_FINAL_ASR_MIN_AGREEMENT", "0.22"))),
)
WORKSHOP_EXPLICIT_MIN_CENTERED_RMS = max(
    0.0,
    float(os.environ.get("RIVERBANK_WORKSHOP_EXPLICIT_MIN_CENTERED_RMS", "120")),
)
WORKSHOP_EXPLICIT_MIN_CENTERED_PEAK = max(
    0.0,
    float(os.environ.get("RIVERBANK_WORKSHOP_EXPLICIT_MIN_CENTERED_PEAK", "700")),
)
WORKSHOP_CREATE_REQUEST_MAX_AGE_SECONDS = max(
    1.0,
    min(
        float(os.environ.get("RIVERBANK_WORKSHOP_CREATE_REQUEST_MAX_AGE", "3")),
        10.0,
    ),
)
VOICE_FOLLOW_UP_MARKER = "[[AWAITING_VOICE_REPLY]]"
VOICE_DIALOGUE_PROTOCOL = f"""
[语音连续对话协议]
你正在通过扬声器和用户进行口语对话。回复要自然、简短、适合朗读。
只有当你确实需要用户补充、选择或确认后才能继续时，才在回复最后单独追加
{VOICE_FOLLOW_UP_MARKER}
不要在其他情况下输出该标记，也不要解释这个标记。

[表情控制协议]
每次面向用户的最终回复必须把下面九个标签之一放在第一个字符位置，然后紧接正常回复：
[[emotion:thinking]]、[[emotion:happy]]、[[emotion:love]]、[[emotion:proud]]、
[[emotion:cool]]、[[emotion:sad]]、[[emotion:cry]]、[[emotion:afraid]]、[[emotion:angry]]。
标签是本地表情控制信息，不是对用户说的话；不要解释、翻译或重复标签。
按你这次回复对用户表达的主要态度选择，而不是看到某个情绪词就选择：中性事实和解释用 thinking；
友好积极用 happy；关爱与温暖用 love；祝贺成就用 proud；自信或轻松俏皮用 cool；
同理坏消息用 sad；强烈悲伤才用 cry；紧急危险警告用 afraid；强烈原则性不满才用 angry。
不确定时必须使用 thinking。工具调用过程不要输出标签，只在最终回复开头输出一次。

[实时信息协议]
当用户询问票价、酒店、价格、天气、新闻或其他会变化的信息时，必须先尝试已配置的 web 或 browser 工具；需要动态网页、交互式搜索或多日价格比较时优先使用 browser。只有实际调用失败后，才可以说无法查询，不要未尝试就声称“没有联网工具”。
机票比价如果只给出“接下来一周”，默认按 1 名成人、经济舱、单程比较未来 7 个自然日，并在回答中说明假设、最低可见价格、日期、查询时间与来源；价格只用于参考，不自动下单。

[文件整理协议]
用户明确要求整理文件时可以使用 file 工具查看、分类、创建目录、重命名或移动文件。先确认用户指定的目录和整理目标；若范围较大或规则存在歧义，先用一句话说明拟执行的分类规则并取得确认。删除文件、覆盖已有文件、清空目录、处理密钥或系统目录等不可逆或高风险操作，必须在执行前取得用户明确确认。不要为整理文件调用 terminal 或 code_execution。

[任务报告归档协议]
当用户明确要求调研、搜集或整理资料，并要求形成报告、文档、Markdown 或保存结果时：
1. 先使用 web 或 browser 核实会变化的信息，不要只凭记忆撰写；
2. 使用 file 工具把完整报告保存到 {REPORTS_DIR}，不要保存到工作区根目录；
3. 文件名使用“YYYY-MM-DD_HHMM_简短主题.md”，不得覆盖已有文件；
4. 第一行必须是清晰的一级标题，正文应包含生成时间、摘要、结构化内容和来源链接；分析、推断与来源事实要明确区分；
5. 最终口语回复只简短说明报告已经生成及其标题，不要通过扬声器朗读整篇报告。
普通问答、无需保存的临时查询不要自动生成报告。
""".strip()
FOLLOW_UP_HINTS = (
    "请问",
    "请告诉我",
    "请补充",
    "需要你确认",
    "需要您确认",
    "你希望",
    "您希望",
    "你想要",
    "您想要",
    "能否确认",
    "可以确认",
    "选择哪",
    "哪一个",
    "哪一种",
)
VOICE_END_PHRASES = (
    "结束对话",
    "退出对话",
    "不用了",
    "先这样",
    "取消",
    "停止",
)
BEEP_ENABLED = os.environ.get("RIVERBANK_VOICE_BEEP", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

# Hermes modules resolve profile paths at import time.  Pin the Daily profile
# before the resident agent is imported lazily below.
os.environ.setdefault("HERMES_HOME", str(DAILY_HOME))
os.environ.setdefault("HERMES_YOLO_MODE", "1")
os.environ.setdefault("HERMES_ACCEPT_HOOKS", "1")

DIRECT_VISUAL_PHRASES = (
    "看到什么",
    "看到什麼",
    "看见什么",
    "看見什麼",
    "能看到",
    "能看见",
    "能看見",
    "看看我",
    "看一下我",
    "我手上",
    "我身上",
    "我穿的",
    "周围有什么",
    "周圍有什麼",
    "这里有什么",
    "這裡有什麼",
    "前面有什么",
    "前面有什麼",
    "有没有人",
    "有沒有人",
)
VISUAL_NOUNS = ("摄像头", "攝像頭", "相机", "相機", "镜头", "鏡頭", "画面", "畫面", "照片")
VISUAL_ACTIONS = ("看", "拍", "识别", "識別", "检查", "檢查", "什么", "什麼", "有人")

CHINESE_NUMBER_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def log(message: str) -> None:
    print(message, flush=True)


class InteractionInterrupted(RuntimeError):
    """Raised when a newer hardware wake word supersedes the active turn."""


def load_wake() -> dict | None:
    try:
        payload = json.loads(LISTENGO_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    wake = payload.get("last_wake")
    return wake if isinstance(wake, dict) else None


def wake_token(wake: dict | None) -> str | None:
    if not wake:
        return None
    return f"{wake.get('received_at')}:{wake.get('message_id')}"


def expression(
    state: str,
    ttl: float | None = None,
    *,
    stage: str | None = None,
) -> None:
    try:
        EXPRESSION_EVENTS.publish(state, ttl, stage=stage)
    except (OSError, TypeError, ValueError) as exc:
        log(f"expression event publish failed state={state}: {exc}")


def vision_activity(
    active: bool,
    source: str = "qwen_visual_request",
    ttl_seconds: float = 180.0,
) -> None:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(
            json.dumps(
                {
                    "command": "vision_activity",
                    "active": bool(active),
                    "source": source,
                    "ttl_seconds": ttl_seconds,
                }
            ).encode("utf-8"),
            str(EXPRESSION_SOCKET),
        )
        client.close()
    except OSError:
        pass


def audio_activity(
    active: bool,
    source: str = "voice_recording",
    ttl_seconds: float = 60.0,
) -> None:
    send_expression_command(
        "audio_activity",
        active=bool(active),
        source=source,
        ttl_seconds=ttl_seconds,
    )


def send_expression_command(command: str, **payload: object) -> bool:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(
            json.dumps({"command": command, **payload}).encode("utf-8"),
            str(EXPRESSION_SOCKET),
        )
        client.close()
        return True
    except OSError as exc:
        log(f"expression command failed command={command}: {exc}")
        return False


def speech_bubble(
    active: bool,
    text: str = "",
    stable_chars: int = 0,
    final: bool = False,
    ttl: float = 0.0,
) -> bool:
    """Publish private, wake-session-only caption state to the local display."""
    value = str(text).strip()
    return send_expression_command(
        "speech_bubble",
        active=bool(active),
        text=value,
        stable_chars=max(0, min(int(stable_chars), len(value))),
        final=bool(final),
        ttl=max(0.0, float(ttl)),
    )


def common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def clean_for_speech(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = strip_response_emotion_tags(text)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#>|~-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_voice_response(text: str) -> tuple[str, bool]:
    explicit = VOICE_FOLLOW_UP_MARKER in text
    cleaned = text.replace(VOICE_FOLLOW_UP_MARKER, "").strip()
    tail = cleaned[-140:]
    heuristic = (
        tail.rstrip().endswith(("？", "?"))
        and any(hint in tail for hint in FOLLOW_UP_HINTS)
    )
    return cleaned, explicit or heuristic


def is_voice_session_end(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text)
    return any(phrase in compact for phrase in VOICE_END_PHRASES)


def is_visual_intent(text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text)
    if any(phrase in compact for phrase in DIRECT_VISUAL_PHRASES):
        return True
    return any(noun in compact for noun in VISUAL_NOUNS) and any(
        action in compact for action in VISUAL_ACTIONS
    )


def parse_chinese_number(text: str) -> int | None:
    value = text.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value == "百":
        return 100
    if "百" in value:
        prefix, _, suffix = value.partition("百")
        hundreds = CHINESE_NUMBER_DIGITS.get(prefix, 1)
        remainder = parse_chinese_number(suffix) if suffix else 0
        return hundreds * 100 + (remainder or 0)
    if "十" in value:
        prefix, _, suffix = value.partition("十")
        tens = CHINESE_NUMBER_DIGITS.get(prefix, 1) if prefix else 1
        ones = CHINESE_NUMBER_DIGITS.get(suffix, 0) if suffix else 0
        return tens * 10 + ones
    digits: list[str] = []
    for character in value:
        digit = CHINESE_NUMBER_DIGITS.get(character)
        if digit is None:
            return None
        digits.append(str(digit))
    return int("".join(digits)) if digits else None


def parse_local_device_command(text: str) -> dict | None:
    """Recognize a deliberately small, safe set of local voice commands."""
    compact = re.sub(r"[\s，。！？、,.!?;；:：]", "", text)

    pomodoro_command = parse_pomodoro_voice_command(text)
    if pomodoro_command is not None:
        return pomodoro_command

    application_command = parse_application_voice_command(text)
    if application_command is not None:
        return application_command

    volume_target = re.search(
        r"(?:系统音量|系統音量|音量|營量|营量|声音|聲音)"
        r"(?:调整|調整|调节|調節|调|調|设置|設置|设|設|改)?(?:到|成|为|為)?"
        r"(?:百分之)?([0-9]{1,3}|[零〇一二两兩三四五六七八九十百]{1,5})(?:%|％)?",
        compact,
    )
    if volume_target:
        percent = parse_chinese_number(volume_target.group(1))
        if percent is not None:
            return {
                "action": "set_volume",
                "percent": max(0, min(percent, 100)),
            }
    if any(
        phrase in compact
        for phrase in (
            "音量最大",
            "營量最大",
            "营量最大",
            "声音最大",
            "聲音最大",
            "把音量开满",
            "把音量開滿",
        )
    ):
        return {"action": "set_volume", "percent": 100}
    if any(
        phrase in compact
        for phrase in ("音量最小", "營量最小", "营量最小", "声音最小", "聲音最小")
    ):
        return {"action": "set_volume", "percent": 0}

    if re.search(r"(?:关闭|关掉|退出)(?:一下)?(?:相机|摄像头)", compact):
        return {"action": "close_camera"}
    if re.search(r"(?:打开|进入|查看|看看)(?:一下)?相册", compact):
        return {"action": "open_gallery"}
    if any(
        phrase in compact
        for phrase in (
            "帮我拍照",
            "给我拍照",
            "拍一张照片",
            "拍张照片",
            "拍照保存",
        )
    ):
        return {"action": "capture_photo"}
    if re.search(r"(?:打开|开启|启动|进入)(?:一下)?(?:相机|摄像头)", compact):
        return {"action": "open_camera"}
    if re.fullmatch(r"(?:(?:打开|开启|启动|进入|唤醒)(?:一下)?)?工坊", compact):
        return {"action": "open_workshop"}
    if re.search(r"(?:关闭|退出|收起)(?:一下)?工坊", compact):
        return {"action": "close_workshop"}
    if re.search(r"(?:退出|停止|关闭)(?:当前|这个)?(?:自定义)?应用", compact):
        return {"action": "stop_workshop_app"}
    return None


def route_user_request(transcript: str, force_visual: bool = False) -> tuple[str, bool]:
    voice_contract = """

[语音界面回答要求]
这是通过扬声器进行的现场对话，请像身边的日常助手一样说话，而不是写报告：
- 你的统一产品身份是“RiverBank 旗下智能产品小灰”；用户询问你是谁、名字、身份或归属时直接这样回答，平常无需反复自我介绍，也不要自称底层模型、Hermes 或泛称“AI 助手”；
- 先顺着用户的话直接回应，不复述问题，不用“首先、其次、综上所述、建议您”等书面套话；
- 默认用自然、轻松的中文口语回答 1 至 4 个短句，一句话只表达一个重点，可以适度使用“嗯、可以、对、这样就行”等自然衔接，但不要刻意卖萌；
- 不使用 Markdown 标题、项目符号或表格，不朗读链接、文件路径、代码和冗长参数；
- 简单问题直接说答案；复杂问题先讲最有用的结论，再用一句话说明原因。只有用户明确要求展开时才详细解释；
- 信息不足时只追问一个最关键、最容易口头回答的问题。
保持 Daily profile 的已有身份、记忆、工具权限和事实准确性，不要为了口语化而编造信息。"""
    if not force_visual and not is_visual_intent(transcript):
        return transcript + voice_contract, False
    routed = f"""{transcript}

[现场语音视觉路由]
用户正在询问树莓派摄像头此刻能看到的真实场景，这句话已经构成调用摄像头的明确授权。
请立即使用 camera-vision 技能，从 camera-hub 获取一张全新的当前帧，再调用已配置的 Qwen 辅助视觉模型分析并用中文回答。
不要询问用户是否需要拍照，不要复用旧截图，也不要声称文本主模型直接看到了像素；若抓帧或视觉分析失败，请如实说明。{voice_contract}"""
    return routed, True


def is_background_task_request(text: str) -> bool:
    """Only divert explicit long-form work; ordinary questions stay conversational."""
    compact = re.sub(r"\s+", "", text).lower()
    explicit_background = any(
        phrase in compact
        for phrase in ("后台任务", "异步任务", "后台执行", "慢慢处理")
    )
    action = any(
        phrase in compact
        for phrase in ("帮我", "请你", "给我", "整理", "调研", "搜集", "收集", "查找", "生成")
    )
    artifact = any(
        phrase in compact
        for phrase in ("markdown", "md文件", "md文档", "报告", "调研文档", "保存下来", "归档")
    )
    return explicit_background or (action and artifact)


def background_task_output_format(text: str) -> str:
    compact = re.sub(r"\s+", "", text).lower()
    if any(
        phrase in compact
        for phrase in ("图文并茂", "图文报告", "带配图", "配上图片", "配一张图")
    ):
        return "illustrated"
    if any(
        phrase in compact
        for phrase in ("生成图片", "生成一张图", "生成图像", "画一张", "插画", "海报")
    ):
        return "image"
    return "text"


def enqueue_background_task(transcript: str, source: str = "voice") -> dict | None:
    payload = json.dumps(
        {
            "prompt": transcript,
            "kind": "research",
            "output_format": background_task_output_format(transcript),
            "source": source,
            "device_name": "riverbank-tech",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        pairing_token = BACKGROUND_TASK_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log(f"Background task token warning: {exc}")
        return None
    request = urllib.request.Request(
        BACKGROUND_TASK_API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": f"voice-{time.time_ns()}",
            "Authorization": f"Bearer {pairing_token}",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=4.0) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log(f"Background task enqueue warning: {exc}")
        return None
    task = result.get("task") if isinstance(result, dict) else None
    return task if isinstance(task, dict) else None


class PersistentMicrophone:
    """Keep the native 16 kHz USB stream open and expose wake-gated blocks."""

    def __init__(self) -> None:
        self.blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self.noise_levels: deque[float] = deque(maxlen=600)
        self.stream: sd.InputStream | None = None
        self.learning_enabled = True
        self.last_status_log = 0.0
        self.dropped_blocks = 0

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        timing: object,
        status: object,
    ) -> None:
        chunk = indata[:, 0].copy()
        level = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        if self.learning_enabled and level < 5000:
            self.noise_levels.append(level)
        if status:
            now = time.monotonic()
            if now - self.last_status_log >= 2.0:
                log(f"audio status: {status}")
                self.last_status_log = now
        try:
            self.blocks.put_nowait(chunk)
        except queue.Full:
            self.dropped_blocks += 1
            try:
                self.blocks.get_nowait()
            except queue.Empty:
                pass
            try:
                self.blocks.put_nowait(chunk)
            except queue.Full:
                pass

    def start(self) -> None:
        if self.stream is not None:
            return
        blocksize = int(SAMPLE_RATE * BLOCK_SECONDS)
        self.stream = sd.InputStream(
            device="pulse",
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            latency=0.2,
            callback=self._callback,
        )
        self.stream.start()
        log(
            f"Persistent microphone ready: {SAMPLE_RATE} Hz, "
            f"{blocksize} samples/block"
        )

    def stop(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.stream = None

    def set_learning(self, enabled: bool) -> None:
        self.learning_enabled = enabled

    def prepare_recording(self) -> tuple[list[np.ndarray], float]:
        recent: list[np.ndarray] = []
        while True:
            try:
                recent.append(self.blocks.get_nowait())
            except queue.Empty:
                break
        pre_roll_blocks = max(1, round(PRE_ROLL_SECONDS / BLOCK_SECONDS))
        noise = list(self.noise_levels)
        if noise:
            noise_floor = float(np.percentile(np.asarray(noise), 35))
            threshold = min(900.0, max(80.0, noise_floor * 2.2 + 35.0))
        else:
            threshold = 120.0
        return recent[-pre_roll_blocks:], threshold

    def discard_buffer(self) -> int:
        """Discard queued microphone blocks, typically after local playback."""
        discarded = 0
        while True:
            try:
                self.blocks.get_nowait()
                discarded += 1
            except queue.Empty:
                return discarded

    def get(self, timeout: float = 0.5) -> np.ndarray:
        return self.blocks.get(timeout=timeout)


class WakeAcknowledgementPlayer:
    """Preload the wake acknowledgement and play it without a network call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sample_rate = 0
        self.samples: np.ndarray | None = None
        self.stream: sd.OutputStream | None = None
        self.lock = threading.Lock()
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            with wave.open(str(self.path), "rb") as handle:
                channels = handle.getnchannels()
                sample_width = handle.getsampwidth()
                self.sample_rate = handle.getframerate()
                frame_count = handle.getnframes()
                raw = handle.readframes(frame_count)
            if channels != 1 or sample_width != 2 or self.sample_rate <= 0:
                raise ValueError(
                    "wake acknowledgement must be mono 16-bit PCM WAV"
                )
            self.samples = np.frombuffer(raw, dtype=np.int16).copy().reshape(-1, 1)
            if not len(self.samples):
                raise ValueError("wake acknowledgement is empty")
            self.last_error = None
            log(
                "Wake acknowledgement cached: "
                f"{self.path} ({self.duration_ms} ms)"
            )
        except Exception as exc:
            self.samples = None
            self.last_error = str(exc)
            log(f"wake acknowledgement load warning: {exc}")

    @property
    def ready(self) -> bool:
        return self.samples is not None and self.sample_rate > 0

    @property
    def duration_ms(self) -> int:
        if self.samples is None or self.sample_rate <= 0:
            return 0
        return round(len(self.samples) * 1000 / self.sample_rate)

    def start(self) -> None:
        if not self.ready or self.stream is not None:
            return
        try:
            self.stream = sd.OutputStream(
                device="pulse",
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                latency="low",
            )
            self.stream.start()
            self.last_error = None
            log("Wake acknowledgement output stream ready")
        except Exception as exc:
            self.stream = None
            self.last_error = str(exc)
            log(f"wake acknowledgement stream warning: {exc}")

    def play(self) -> bool:
        if not self.ready:
            return False
        with self.lock:
            try:
                self.start()
                if self.stream is None or self.samples is None:
                    raise RuntimeError("wake acknowledgement stream unavailable")
                underflowed = self.stream.write(self.samples)
                if underflowed:
                    log("wake acknowledgement output underflow recovered")
                self.last_error = None
                return True
            except Exception as exc:
                self.last_error = str(exc)
                log(f"wake acknowledgement stream failed, using ffplay: {exc}")
                self.stop()
                try:
                    completed = subprocess.run(
                        [
                            "/usr/bin/ffplay",
                            "-nodisp",
                            "-autoexit",
                            "-loglevel",
                            "error",
                            str(self.path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                    if completed.returncode != 0:
                        raise RuntimeError(
                            f"ffplay exited with {completed.returncode}"
                        )
                    self.last_error = None
                    return True
                except Exception as fallback_exc:
                    self.last_error = str(fallback_exc)
                    log(f"wake acknowledgement playback warning: {fallback_exc}")
                    return False

    def stop(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        finally:
            self.stream = None


class StreamingSpeechChunker:
    """Cut append-only model deltas into natural, safely buffered clauses."""

    STRONG_BOUNDARIES = frozenset("。！？!?\n")
    SOFT_BOUNDARIES = frozenset("，；：,;:")

    def __init__(self, min_chars: int, hard_chars: int) -> None:
        self.min_chars = max(1, int(min_chars))
        self.hard_chars = max(self.min_chars + 1, int(hard_chars))
        self.buffer = ""

    @staticmethod
    def visible_length(value: str) -> int:
        return sum(not character.isspace() for character in value)

    def _strip_closed_think_blocks(self) -> None:
        self.buffer = re.sub(
            r"<think(?:\s[^>]*)?>.*?</think>",
            "",
            self.buffer,
            flags=re.DOTALL | re.IGNORECASE,
        )

    def _speakable_prefix(self) -> str:
        limit = len(self.buffer)
        for marker in ("[[", "<think", "<THINK"):
            marker_index = self.buffer.find(marker)
            if marker_index >= 0:
                limit = min(limit, marker_index)
        return self.buffer[:limit]

    def _next_cut(self) -> int | None:
        prefix = self._speakable_prefix()
        if not prefix:
            return None
        visible = 0
        last_soft: int | None = None
        last_soft_visible = 0
        last_space: int | None = None
        for index, character in enumerate(prefix):
            if not character.isspace():
                visible += 1
            else:
                last_space = index + 1
            if character in self.SOFT_BOUNDARIES:
                last_soft = index + 1
                last_soft_visible = visible
            if (
                visible >= self.min_chars
                and character in self.STRONG_BOUNDARIES | self.SOFT_BOUNDARIES
            ):
                return index + 1
            if visible >= self.hard_chars:
                if last_soft is not None and last_soft_visible >= self.min_chars:
                    return last_soft
                if last_space is not None:
                    return last_space
                return index + 1
        return None

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self.buffer += str(delta)
        self._strip_closed_think_blocks()
        chunks: list[str] = []
        while (cut := self._next_cut()) is not None:
            chunk = clean_for_speech(self.buffer[:cut])
            self.buffer = self.buffer[cut:]
            if chunk:
                chunks.append(chunk)
        return chunks

    def segment_boundary(self) -> None:
        """Drop an unspeakable tool-round fragment before the next segment."""
        self._strip_closed_think_blocks()
        if self.buffer.strip():
            self.buffer = ""

    def flush(self) -> list[str]:
        self._strip_closed_think_blocks()
        tail = self.buffer.replace(VOICE_FOLLOW_UP_MARKER, "")
        if "[[" in tail:
            tail = tail.split("[[", 1)[0]
        self.buffer = ""
        cleaned = clean_for_speech(tail)
        return [cleaned] if cleaned else []


class StreamingSpeechSession:
    """Stream completed clauses through one continuous Edge-TTS player."""

    _DONE = object()

    def __init__(
        self,
        on_first_delta=None,
        on_first_chunk=None,
        on_first_audio=None,
    ) -> None:
        self.chunker = StreamingSpeechChunker(
            STREAM_TTS_MIN_CHARS,
            STREAM_TTS_HARD_CHARS,
        )
        self.on_first_delta = on_first_delta
        self.on_first_chunk = on_first_chunk
        self.on_first_audio = on_first_audio
        self.text_queue: queue.Queue[object] = queue.Queue(maxsize=64)
        self.started_at = time.monotonic()
        self.first_delta_at: float | None = None
        self.first_chunk_at: float | None = None
        self.llm_complete_at: float | None = None
        self.first_audio_at: float | None = None
        self.playback_complete_at: float | None = None
        self.delta_chars = 0
        self.chunk_chars = 0
        self.chunk_count = 0
        self.boundary_count = 0
        self.audible = False
        self.error: str | None = None
        self.player: subprocess.Popen | None = None
        self.aborted = threading.Event()
        self.worker = threading.Thread(
            target=self._run,
            name="riverbank-streaming-tts",
            daemon=True,
        )
        self.worker.start()

    @staticmethod
    def elapsed(started_at: float, value: float | None) -> float | None:
        return round(value - started_at, 3) if value is not None else None

    def _enqueue(self, text: str) -> None:
        value = clean_for_speech(text)
        if not value or self.aborted.is_set():
            return
        if self.first_chunk_at is None:
            self.first_chunk_at = time.monotonic()
            if callable(self.on_first_chunk):
                try:
                    self.on_first_chunk()
                except Exception as exc:
                    log(f"streaming TTS first-chunk callback warning: {exc}")
        try:
            self.text_queue.put_nowait(value)
            self.chunk_chars += len(value)
            self.chunk_count += 1
        except queue.Full:
            self.error = "streaming TTS text queue full"
            self.abort()

    def feed(self, delta) -> None:
        if self.aborted.is_set():
            return
        if delta is None:
            self.boundary_count += 1
            self.chunker.segment_boundary()
            return
        value = str(delta)
        if not value:
            return
        if self.first_delta_at is None:
            self.first_delta_at = time.monotonic()
            if callable(self.on_first_delta):
                try:
                    self.on_first_delta()
                except Exception as exc:
                    log(f"streaming TTS first-delta callback warning: {exc}")
        self.delta_chars += len(value)
        for chunk in self.chunker.feed(value):
            self._enqueue(chunk)

    async def _stream_chunk(self, text: str) -> None:
        communicate = edge_tts.Communicate(text, STREAM_TTS_VOICE)
        async for chunk in communicate.stream():
            if self.aborted.is_set():
                return
            if chunk.get("type") != "audio":
                continue
            payload = chunk.get("data") or b""
            if not payload or self.player is None or self.player.stdin is None:
                continue
            self.player.stdin.write(payload)
            self.player.stdin.flush()
            if not self.audible:
                self.audible = True
                self.first_audio_at = time.monotonic()
                if callable(self.on_first_audio):
                    try:
                        self.on_first_audio()
                    except Exception as exc:
                        log(f"streaming TTS first-audio callback warning: {exc}")

    def _start_player(self) -> None:
        if self.player is not None:
            return
        self.player = subprocess.Popen(
            [
                "/usr/bin/ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _run(self) -> None:
        try:
            while not self.aborted.is_set():
                item = self.text_queue.get()
                if item is self._DONE:
                    break
                if not isinstance(item, str):
                    continue
                self._start_player()
                asyncio.run(self._stream_chunk(item))
            if self.player is not None and self.player.stdin is not None:
                self.player.stdin.close()
                self.player.stdin = None
            if self.player is not None:
                return_code = self.player.wait(timeout=300)
                if return_code != 0 and not self.aborted.is_set():
                    raise RuntimeError(f"ffplay exited with {return_code}")
        except Exception as exc:
            if not self.aborted.is_set():
                self.error = str(exc)
                log(f"streaming TTS warning: {exc}")
        finally:
            self.playback_complete_at = time.monotonic()

    def mark_llm_complete(self) -> None:
        self.llm_complete_at = time.monotonic()

    def finish(self) -> bool:
        if not self.aborted.is_set():
            for chunk in self.chunker.flush():
                self._enqueue(chunk)
            try:
                self.text_queue.put(self._DONE, timeout=2.0)
            except queue.Full:
                self.error = "streaming TTS completion queue full"
                self.abort()
        deadline = time.monotonic() + 300.0
        while self.worker.is_alive() and not self.aborted.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.worker.join(timeout=min(0.1, remaining))
        if self.aborted.is_set():
            self.worker.join(timeout=0.15)
        elif self.worker.is_alive():
            self.error = "streaming TTS playback timed out"
            self.abort()
        log(
            "Streaming TTS metrics "
            f"first_delta={self.elapsed(self.started_at, self.first_delta_at)}s "
            f"first_chunk={self.elapsed(self.started_at, self.first_chunk_at)}s "
            f"llm_complete={self.elapsed(self.started_at, self.llm_complete_at)}s "
            f"first_audio={self.elapsed(self.started_at, self.first_audio_at)}s "
            f"playback_complete={self.elapsed(self.started_at, self.playback_complete_at)}s "
            f"deltas={self.delta_chars} chunks={self.chunk_count} "
            f"chunk_chars={self.chunk_chars} boundaries={self.boundary_count} "
            f"audible={self.audible} aborted={self.aborted.is_set()} "
            f"error={self.error}"
        )
        return self.audible

    def abort(self) -> None:
        self.aborted.set()
        try:
            self.text_queue.put_nowait(self._DONE)
        except queue.Full:
            pass
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:
                pass


class PersistentHermesRuntime:
    """Reuse one Daily-profile AIAgent instead of spawning Hermes every turn."""

    def __init__(self) -> None:
        self.agent = None
        self.session_db = None
        self.turn_count = 0
        self.lock = threading.RLock()
        self.clarify_handler = None
        self.request_active = threading.Event()
        self.interrupt_requested = threading.Event()

    def handle_clarification(
        self,
        question: str,
        choices: list | None = None,
        multi_select: bool = False,
    ) -> str:
        if callable(self.clarify_handler):
            return str(self.clarify_handler(question, choices, multi_select))
        return "[语音界面当前无法取得补充信息，请简短说明需要用户稍后补充什么。]"

    def _build(self) -> None:
        started = time.monotonic()
        from gateway.session_context import declare_stateless_channel
        from hermes_cli.config import load_config, split_model_config_default
        from hermes_cli.env_loader import load_hermes_dotenv
        from hermes_cli.fallback_config import get_fallback_chain
        from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.tools_config import _get_platform_tools
        from hermes_state import SessionDB
        from run_agent import AIAgent

        load_hermes_dotenv()
        declare_stateless_channel()
        config = load_config()
        model_config = config.get("model") or {}
        if isinstance(model_config, str):
            model = model_config
            provider = None
        else:
            raw_model = model_config.get("default") or model_config.get("model") or ""
            if isinstance(raw_model, dict):
                model, _ = split_model_config_default(raw_model)
            else:
                model = str(raw_model or "")
            provider = str(model_config.get("provider") or "").strip() or None
        runtime = resolve_runtime_provider(
            requested=provider,
            target_model=model or None,
        )
        available_toolsets = set(_get_platform_tools(config, "cli"))
        toolsets = sorted(available_toolsets.intersection(DAILY_TOOLSETS))
        if not toolsets:
            raise RuntimeError("Daily Hermes toolset filter matched no available tools")
        missing_toolsets = sorted(set(DAILY_TOOLSETS).difference(available_toolsets))
        if missing_toolsets:
            log(f"Daily Hermes unavailable toolsets ignored: {missing_toolsets}")
        if DAILY_MCP_DISCOVERY:
            ensure_mcp_discovery_before_agent_build(
                logger=logging.getLogger(__name__),
                single_query=True,
            )
        self.session_db = SessionDB()
        self.agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=model,
            enabled_toolsets=toolsets,
            quiet_mode=True,
            platform="cli",
            session_db=self.session_db,
            credential_pool=runtime.get("credential_pool"),
            fallback_model=get_fallback_chain(config) or None,
            clarify_callback=self.handle_clarification,
        )
        self.agent.suppress_status_output = True
        self.agent.stream_delta_callback = None
        self.agent.tool_gen_callback = None
        self.turn_count = 0
        log(
            "Persistent Daily Hermes ready "
            f"in {time.monotonic() - started:.2f}s toolsets={toolsets}"
        )

    def warmup(self) -> None:
        """Build the Daily-profile agent before the first wake-up."""
        with self.lock:
            if self.agent is None:
                try:
                    self._build()
                except Exception as exc:
                    log(f"Persistent Daily Hermes warmup warning: {exc}")
                    self.close()

    def ask(self, prompt: str, stream_callback=None) -> str:
        with self.lock:
            if self.agent is None or self.turn_count >= DAILY_AGENT_MAX_TURNS:
                self.close()
                self._build()
            captured = io.StringIO()
            previous_stream_callback = self.agent.stream_delta_callback
            self.agent.stream_delta_callback = stream_callback
            self.request_active.set()
            timed_out = threading.Event()

            def abort_timed_out_request() -> None:
                timed_out.set()
                self.interrupt()

            watchdog = threading.Timer(
                DAILY_AGENT_TIMEOUT_SECONDS,
                abort_timed_out_request,
            )
            watchdog.daemon = True
            watchdog.start()
            try:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    result = self.agent.run_conversation(prompt)
            except Exception:
                self.close()
                raise
            finally:
                watchdog.cancel()
                self.request_active.clear()
                if self.agent is not None:
                    self.agent.stream_delta_callback = previous_stream_callback
                    if self.interrupt_requested.is_set():
                        clear_interrupt = getattr(self.agent, "clear_interrupt", None)
                        if callable(clear_interrupt):
                            try:
                                clear_interrupt()
                            except Exception as exc:
                                log(f"Hermes interrupt reset warning: {exc}")
                self.interrupt_requested.clear()
            if timed_out.is_set():
                self.close()
                raise TimeoutError(
                    f"Daily Hermes request exceeded {DAILY_AGENT_TIMEOUT_SECONDS:.0f}s"
                )
            self.turn_count += 1
            response = str(result.get("final_response") or "").strip()
            if not response:
                raise RuntimeError("Daily Hermes returned an empty response")
            return response

    def interrupt(self) -> bool:
        """Hard-cancel the active model/tool request from another thread."""
        if not self.request_active.is_set():
            return False
        agent = self.agent
        if agent is None:
            return False
        self.interrupt_requested.set()
        hard_interrupt = getattr(agent, "hard_interrupt", None)
        try:
            if callable(hard_interrupt):
                hard_interrupt()
            else:
                agent.interrupt(hard_cancel=True)
            return True
        except Exception as exc:
            log(f"Hermes hard interrupt warning: {exc}")
            return False

    def close(self) -> None:
        self.request_active.clear()
        self.interrupt_requested.clear()
        if self.agent is not None:
            try:
                self.agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                self.agent.close()
            except Exception:
                pass
            self.agent = None
        if self.session_db is not None:
            try:
                self.session_db.close()
            except Exception:
                pass
            self.session_db = None


class StreamingCaptionRecognizer:
    """Low-latency first-pass captions; the final ensemble runs at utterance end."""

    def __init__(self, model_dir: Path = STREAMING_ASR_MODEL_DIR) -> None:
        self.model_dir = model_dir
        self.recognizer: object | None = None
        self.last_error: str | None = None
        if sherpa_onnx is None:
            self.last_error = "sherpa-onnx is not installed"
            log(f"Streaming caption unavailable: {self.last_error}")
            return
        files = {
            "tokens": model_dir / "tokens.txt",
            "encoder": model_dir / "encoder-epoch-99-avg-1.int8.onnx",
            "decoder": model_dir / "decoder-epoch-99-avg-1.int8.onnx",
            "joiner": model_dir / "joiner-epoch-99-avg-1.int8.onnx",
        }
        missing = [path.name for path in files.values() if not path.is_file()]
        if missing:
            self.last_error = f"missing model files: {', '.join(missing)}"
            log(f"Streaming caption unavailable: {self.last_error}")
            return
        try:
            started = time.monotonic()
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(files["tokens"]),
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                num_threads=2,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
                model_type="zipformer",
            )
            log(
                "Streaming sherpa-onnx caption model ready "
                f"in {time.monotonic() - started:.2f}s"
            )
        except Exception as exc:
            self.recognizer = None
            self.last_error = str(exc)
            log(f"Streaming caption unavailable: {exc}")

    @property
    def ready(self) -> bool:
        return self.recognizer is not None

    def create_stream(self) -> object | None:
        if self.recognizer is None:
            return None
        return self.recognizer.create_stream()

    def accept(self, stream: object, samples: np.ndarray) -> str:
        if self.recognizer is None:
            return ""
        audio = samples.reshape(-1).astype(np.float32) / 32768.0
        stream.accept_waveform(SAMPLE_RATE, audio)
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return str(self.recognizer.get_result(stream) or "").strip()

    def finish(self, stream: object) -> str:
        if self.recognizer is None:
            return ""
        stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return str(self.recognizer.get_result(stream) or "").strip()


def normalize_asr_comparison(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text)).lower()
    value = re.sub(r"<[^>]+>", "", value)
    digit_words = dict(zip("0123456789", "零一二三四五六七八九"))
    value = "".join(digit_words.get(char, char) for char in value)
    return "".join(
        char
        for char in value
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def correct_asr_domain_terms(text: str) -> str:
    """Apply only context-constrained corrections observed in local validation."""
    result = str(text).strip()
    result = re.sub(r"完整的从[一易]数到[十时]", "完整地从一数到十", result)
    result = re.sub(
        r"(?<![A-Za-z])herm+es(?![A-Za-z])",
        "Hermes",
        result,
        flags=re.IGNORECASE,
    )
    if "人脸检测" in result:
        result = re.sub(
            r"(?:hello|黑\s*lo|黑漏|黑)\s*[8八]",
            "Hailo 八",
            result,
            flags=re.IGNORECASE,
        )
    if "预测式学习架构" in result:
        result = re.sub(
            r"^(?:gepa|gpa)",
            "JEPA",
            result,
            flags=re.IGNORECASE,
        )
    if "正在本地运行" in result:
        result = re.sub(
            r"r+i+v+(?:e+v+e*r|e*r)bank\s+assistant",
            "RiverBank Assistant",
            result,
            flags=re.IGNORECASE,
        )
    lower = result.lower()
    industry_cues = sum(
        cue in lower
        for cue in ("deep", "gem", "cloud", "claud", "quan", "qwen")
    )
    if "有哪些新进展" in result and industry_cues >= 3:
        result = re.sub(
            r"^.*?(?=有哪些新进展)",
            "Qwen、DeepSeek、Gemini 和 Claude ",
            result,
        )
    return result


def suspicious_asr_transcript(text: str) -> bool:
    normalized = normalize_asr_comparison(text)
    if not normalized:
        return True
    if len(normalized) >= 20:
        diversity = len(set(normalized)) / len(normalized)
        if diversity < 0.16:
            return True
        if re.search(r"(.{1,10})\1{3,}", normalized):
            return True
    return False


class FinalASREnsemble:
    """Resident SenseVoice + Zipformer CTC final recognizer with safe routing."""

    ENGINE_NAME = "sensevoice-int8+zipformer-ctc-int8"
    DECODER_NAME = "parallel-context-router-signal-disagreement-gate"

    def __init__(self) -> None:
        self.sense_recognizer: object | None = None
        self.zip_recognizer: object | None = None
        self.load_lock = threading.Lock()
        self.sense_lock = threading.Lock()
        self.zip_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="final-asr",
        )
        self.last_error: str | None = None
        self.last_load_seconds: float | None = None
        self.last_decision = "not-run"
        self.last_elapsed_seconds: float | None = None
        self.last_signal: dict[str, float] | None = None
        self.rejected_count = 0

    @property
    def ready(self) -> bool:
        return self.sense_recognizer is not None and self.zip_recognizer is not None

    @property
    def models_loaded(self) -> int:
        return int(self.sense_recognizer is not None) + int(self.zip_recognizer is not None)

    def ensure_ready(self) -> None:
        if self.ready:
            return
        if sherpa_onnx is None:
            self.last_error = "sherpa-onnx is not installed"
            raise RuntimeError(self.last_error)
        with self.load_lock:
            if self.ready:
                return
            sense_files = (
                FINAL_ASR_SENSEVOICE_DIR / "model.int8.onnx",
                FINAL_ASR_SENSEVOICE_DIR / "tokens.txt",
            )
            zip_files = (
                FINAL_ASR_ZIPFORMER_DIR / "model.int8.onnx",
                FINAL_ASR_ZIPFORMER_DIR / "tokens.txt",
            )
            missing = [
                str(path)
                for path in (*sense_files, *zip_files)
                if not path.is_file()
            ]
            if missing:
                self.last_error = "missing final ASR files: " + ", ".join(missing)
                raise RuntimeError(self.last_error)
            started = time.monotonic()
            try:
                self.sense_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(sense_files[0]),
                    tokens=str(sense_files[1]),
                    num_threads=FINAL_ASR_THREADS,
                    language="zh",
                    use_itn=True,
                )
                self.zip_recognizer = sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
                    model=str(zip_files[0]),
                    tokens=str(zip_files[1]),
                    num_threads=FINAL_ASR_THREADS,
                )
            except Exception as exc:
                self.sense_recognizer = None
                self.zip_recognizer = None
                self.last_error = str(exc)
                raise
            self.last_load_seconds = time.monotonic() - started
            self.last_error = None
            log(
                "Final ASR ensemble ready "
                f"threads={FINAL_ASR_THREADS} "
                f"elapsed={self.last_load_seconds:.2f}s"
            )

    @staticmethod
    def read_audio(wav_path: Path) -> tuple[int, np.ndarray, dict[str, float]]:
        with wave.open(str(wav_path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            if width != 2:
                raise ValueError(f"unsupported ASR sample width: {width}")
            samples = np.frombuffer(handle.readframes(frames), dtype=np.int16)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
        floating = samples.astype(np.float32)
        if len(floating):
            floating -= float(np.mean(floating))
        centered_rms = (
            float(np.sqrt(np.mean(floating.astype(np.float64) ** 2)))
            if len(floating)
            else 0.0
        )
        centered_peak = float(np.max(np.abs(floating))) if len(floating) else 0.0
        signal = {
            "duration_seconds": len(floating) / max(sample_rate, 1),
            "centered_rms": centered_rms,
            "centered_peak": centered_peak,
        }
        normalized = np.clip(floating / 32768.0, -1.0, 1.0).astype(np.float32)
        return sample_rate, normalized, signal

    def decode_sense(self, sample_rate: int, samples: np.ndarray) -> tuple[str, dict]:
        with self.sense_lock:
            recognizer = self.sense_recognizer
            if recognizer is None:
                return "", {}
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            result = stream.result
            return str(result.text or "").strip(), {
                "language": str(result.lang),
                "emotion": str(result.emotion),
                "event": str(result.event),
            }

    def decode_zip(self, sample_rate: int, samples: np.ndarray) -> tuple[str, dict]:
        with self.zip_lock:
            recognizer = self.zip_recognizer
            if recognizer is None:
                return "", {}
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            return str(stream.result.text or "").strip(), {}

    @staticmethod
    def route(sense_text: str, zip_text: str) -> tuple[str, str, float]:
        sense_normalized = normalize_asr_comparison(sense_text)
        zip_normalized = normalize_asr_comparison(zip_text)
        agreement = difflib.SequenceMatcher(
            None,
            sense_normalized,
            zip_normalized,
        ).ratio()
        professional = bool(re.search(r"[A-Za-z]{2,}", sense_text))
        zip_unknown = "<unk>" in zip_text.lower()
        if professional or zip_unknown:
            return sense_text, "sense-professional", agreement
        if not zip_normalized:
            return sense_text, "sense-only", agreement
        if not sense_normalized:
            return zip_text, "zip-only", agreement
        return zip_text, "zip-chinese", agreement

    def transcribe(
        self,
        wav_path: Path,
        *,
        minimum_centered_rms: float = FINAL_ASR_MIN_CENTERED_RMS,
        minimum_centered_peak: float = FINAL_ASR_MIN_CENTERED_PEAK,
    ) -> tuple[str, dict]:
        self.ensure_ready()
        sample_rate, samples, signal = self.read_audio(wav_path)
        self.last_signal = signal
        if (
            signal["centered_rms"] < minimum_centered_rms
            or signal["centered_peak"] < minimum_centered_peak
        ):
            self.last_decision = "rejected-low-signal"
            self.rejected_count += 1
            return "", {
                "decision": self.last_decision,
                "signal": signal,
                "agreement": 0.0,
            }
        started = time.monotonic()
        sense_future = self.executor.submit(self.decode_sense, sample_rate, samples)
        zip_future = self.executor.submit(self.decode_zip, sample_rate, samples)
        sense_text, sense_meta = sense_future.result()
        zip_text, _zip_meta = zip_future.result()
        selected, decision, agreement = self.route(sense_text, zip_text)
        has_professional = bool(re.search(r"[A-Za-z]{2,}", sense_text))
        if (
            normalize_asr_comparison(sense_text)
            and normalize_asr_comparison(zip_text)
            and agreement < FINAL_ASR_MIN_AGREEMENT
            and not has_professional
        ):
            selected = ""
            decision = "rejected-model-disagreement"
        selected = correct_asr_domain_terms(selected)
        if selected and suspicious_asr_transcript(selected):
            selected = ""
            decision = "rejected-suspicious-repetition"
        if not selected:
            self.rejected_count += 1
        self.last_elapsed_seconds = time.monotonic() - started
        self.last_decision = decision
        return selected, {
            "decision": decision,
            "agreement": agreement,
            "signal": signal,
            "sense_text_length": len(sense_text),
            "zip_text_length": len(zip_text),
            "sense_event": sense_meta.get("event"),
            "elapsed_seconds": self.last_elapsed_seconds,
        }

    def state(self) -> dict:
        return {
            "engine": self.ENGINE_NAME,
            "decoder": self.DECODER_NAME,
            "ready": self.ready,
            "last_error": self.last_error,
            "models_loaded": self.models_loaded,
            "configured_models": 2,
            "threads_per_model": FINAL_ASR_THREADS,
            "sensevoice_model_directory": str(FINAL_ASR_SENSEVOICE_DIR),
            "zipformer_model_directory": str(FINAL_ASR_ZIPFORMER_DIR),
            "minimum_centered_rms": FINAL_ASR_MIN_CENTERED_RMS,
            "minimum_centered_peak": FINAL_ASR_MIN_CENTERED_PEAK,
            "minimum_agreement": FINAL_ASR_MIN_AGREEMENT,
            "last_load_seconds": self.last_load_seconds,
            "last_decision": self.last_decision,
            "last_elapsed_seconds": self.last_elapsed_seconds,
            "last_signal": self.last_signal,
            "rejected_count": self.rejected_count,
            "privacy": "metrics-only-no-transcripts",
        }

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class DailyVoiceAssistant:
    def __init__(self) -> None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.busy = False
        self.interrupt_event = threading.Event()
        self.barge_in_lock = threading.Lock()
        self.pending_wake: dict | None = None
        self.pending_workshop_requirement = False
        self.active_speech_session: StreamingSpeechSession | None = None
        self.active_player: subprocess.Popen | None = None
        self.active_async_loop: asyncio.AbstractEventLoop | None = None
        self.active_async_task: asyncio.Task | None = None
        self.barge_in_count = 0
        self.last_barge_in_at: float | None = None
        self.last_interrupted_stage: str | None = None
        self.latency = VoiceLatencyMonitor(VOICE_METRICS_PATH)
        self.started_at = time.time()
        self.last_token = wake_token(load_wake())
        self.last_trigger_monotonic = 0.0
        self.last_trigger_at: float | None = None
        self.last_wake: dict | None = None
        self.last_error: str | None = None
        self.last_result = "waiting"
        self.interaction_count = 0
        self.visual_mode_active = False
        self.visual_request_active = False
        vision_activity(False)
        self.whisper_models: list[WhisperModel] = []
        self.whisper_pool_lock = threading.Lock()
        self.whisper_available: queue.Queue[int] = queue.Queue()
        self.last_streaming_transcript = ""
        self.streaming_caption = StreamingCaptionRecognizer()
        self.final_asr = FinalASREnsemble()
        self.live_caption_active = False
        self.live_caption_final = False
        self.live_caption_text_length = 0
        self.live_caption_stable_chars = 0
        self.live_caption_expires_at = 0.0
        self.microphone = PersistentMicrophone()
        self.wake_ack = WakeAcknowledgementPlayer(WAKE_ACK_PATH)
        self.last_wake_ack_at: float | None = None
        self.hermes = PersistentHermesRuntime()
        self.hermes.clarify_handler = self.handle_agent_clarification
        self.conversation_active = False
        self.awaiting_follow_up = False
        self.conversation_turn = 0
        self.last_follow_up_at: float | None = None
        self.last_local_command: dict | None = None
        self.last_response_expression: str | None = None
        self.last_response_expression_source: str | None = None
        self.last_response_expression_at: float | None = None

    def write_state(self) -> None:
        live_caption_visible = bool(
            self.live_caption_active
            and (
                not self.live_caption_expires_at
                or time.monotonic() < self.live_caption_expires_at
            )
        )
        payload = {
            "ok": self.last_error is None,
            "service": "running" if self.running else "stopping",
            "mode": "listengo-hardware-wake",
            "interaction_mode": "visual_voice" if self.visual_mode_active else "voice",
            "visual_mode_active": self.visual_mode_active,
            "visual_request_active": self.visual_request_active,
            "reports_dir": str(REPORTS_DIR),
            "hardware_keyword": HARDWARE_WAKE_PHRASE,
            "wake_acknowledgement": {
                "enabled": self.wake_ack.ready,
                "text": WAKE_ACK_TEXT,
                "path": str(self.wake_ack.path),
                "duration_ms": self.wake_ack.duration_ms,
                "tail_guard_ms": round(WAKE_ACK_TAIL_SECONDS * 1000),
                "last_played_at": self.last_wake_ack_at,
                "last_error": self.wake_ack.last_error,
            },
            "response_expression": {
                "state": self.last_response_expression,
                "source": self.last_response_expression_source,
                "selected_at": self.last_response_expression_at,
            },
            "busy": self.busy,
            "wake_barge_in": {
                "enabled": True,
                "pending": self.pending_wake is not None,
                "count": self.barge_in_count,
                "last_at": self.last_barge_in_at,
                "last_interrupted_stage": self.last_interrupted_stage,
            },
            "last_result": self.last_result,
            "last_error": self.last_error,
            "interaction_count": self.interaction_count,
            "conversation_active": self.conversation_active,
            "awaiting_follow_up": self.awaiting_follow_up,
            "conversation_turn": self.conversation_turn,
            "follow_up_timeout_seconds": FOLLOW_UP_NO_SPEECH_SECONDS,
            "last_follow_up_at": self.last_follow_up_at,
            "last_local_command": self.last_local_command,
            "live_caption": {
                "active": live_caption_visible,
                "final": bool(live_caption_visible and self.live_caption_final),
                "text_length": (
                    self.live_caption_text_length if live_caption_visible else 0
                ),
                "stable_chars": (
                    self.live_caption_stable_chars if live_caption_visible else 0
                ),
                "privacy": "text-kept-in-display-memory-only",
                "scope": "wake-session-only",
                "draft_engine": "sherpa-onnx-streaming-zipformer-zh-14M",
                "draft_engine_ready": self.streaming_caption.ready,
                "draft_engine_error": self.streaming_caption.last_error,
                "model_directory": str(self.streaming_caption.model_dir),
                "final_engine": self.final_asr.ENGINE_NAME,
                "final_decoder": self.final_asr.DECODER_NAME,
                "final_engine_ready": self.final_asr.ready,
                "resident_model_pool": self.final_asr.models_loaded,
                "configured_model_pool": 2,
            },
            "final_asr": self.final_asr.state(),
            "streaming_tts": {
                "enabled": STREAM_TTS_ENABLED,
                "voice": STREAM_TTS_VOICE,
                "strategy": "punctuation-first-continuous-mp3",
                "minimum_chunk_chars": STREAM_TTS_MIN_CHARS,
                "hard_chunk_chars": STREAM_TTS_HARD_CHARS,
            },
            "expression_events": EXPRESSION_EVENTS.state(),
            "latency_monitor": self.latency.state(),
            "last_trigger_at": self.last_trigger_at,
            "last_wake": self.last_wake,
            "started_at": self.started_at,
            "updated_at": time.time(),
        }
        temporary = STATE_PATH.with_suffix(f".{threading.get_ident()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, STATE_PATH)

    def raise_if_interrupted(self, event: threading.Event | None = None) -> None:
        target = event if event is not None else self.interrupt_event
        if target.is_set():
            raise InteractionInterrupted("superseded by a newer wake word")

    def finish_latency(self, status: str) -> None:
        record = self.latency.finish(status)
        if record is None:
            return
        metrics = " ".join(
            f"{name}={value}s"
            for name, value in record.items()
            if name.endswith("_seconds")
        )
        log(f"Voice latency metrics status={status} {metrics}".rstrip())

    def has_pending_wake(self) -> bool:
        with self.barge_in_lock:
            return self.pending_wake is not None

    def pop_pending_wake(self) -> dict | None:
        with self.barge_in_lock:
            wake = self.pending_wake
            self.pending_wake = None
            return wake

    def pop_pending_workshop_requirement(self) -> bool:
        pending = self.pending_workshop_requirement
        self.pending_workshop_requirement = False
        return pending

    def set_active_speech_session(
        self,
        session: StreamingSpeechSession | None,
    ) -> None:
        with self.barge_in_lock:
            self.active_speech_session = session

    def set_active_player(self, player: subprocess.Popen | None) -> None:
        with self.barge_in_lock:
            self.active_player = player

    def request_barge_in(self, wake: dict) -> bool:
        """Supersede the active turn and queue the newest hardware wake."""
        if not self.busy:
            return False
        token = wake_token(wake)
        with self.barge_in_lock:
            if token and token == self.last_token:
                return False
            if token:
                self.last_token = token
            self.pending_wake = wake
            # A new wake word always replaces the previous interaction.  Do
            # not begin Workshop's deferred follow-up after the replacement
            # turn has arrived.
            self.pending_workshop_requirement = False
            self.interrupt_event.set()
            speech_session = self.active_speech_session
            player = self.active_player
            async_loop = self.active_async_loop
            async_task = self.active_async_task
            interrupted_stage = self.last_result
            self.barge_in_count += 1
            self.last_barge_in_at = time.time()
            self.last_interrupted_stage = interrupted_stage
        if speech_session is not None:
            speech_session.abort()
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass
        if async_loop is not None and async_task is not None:
            try:
                async_loop.call_soon_threadsafe(async_task.cancel)
            except (RuntimeError, OSError):
                pass
        hermes_interrupted = self.hermes.interrupt()
        self.publish_speech_bubble(False)
        self.last_result = "interrupting_for_wake"
        expression("listening", stage=self.last_result)
        self.write_state()
        log(
            "Wake barge-in requested "
            f"stage={interrupted_stage} hermes_interrupted={hermes_interrupted}"
        )
        return True

    def monitor_hardware_wake(self) -> None:
        """Watch wake events even while the foreground interaction is blocked."""
        while self.running:
            if self.busy:
                wake = load_wake()
                token = wake_token(wake)
                if wake is not None and token and token != self.last_token:
                    self.request_barge_in(wake)
            time.sleep(0.05)

    def publish_speech_bubble(
        self,
        active: bool,
        text: str = "",
        stable_chars: int = 0,
        final: bool = False,
        ttl: float = 0.0,
    ) -> None:
        value = str(text).strip()
        stable_chars = max(0, min(int(stable_chars), len(value)))
        self.live_caption_active = bool(active)
        self.live_caption_final = bool(final and active)
        self.live_caption_text_length = len(value) if active else 0
        self.live_caption_stable_chars = stable_chars if active else 0
        self.live_caption_expires_at = (
            time.monotonic() + max(0.0, float(ttl))
            if active and final and ttl > 0
            else 0.0
        )
        speech_bubble(active, value, stable_chars, final, ttl)

    @staticmethod
    def beep(frequency: int, count: int = 1) -> None:
        if not BEEP_ENABLED:
            return
        try:
            for index in range(count):
                subprocess.run(
                    [
                        "/usr/bin/ffplay",
                        "-nodisp",
                        "-autoexit",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        f"sine=frequency={frequency}:sample_rate=48000:duration=0.18",
                    ],
                    stdin=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if index + 1 < count:
                    time.sleep(0.08)
        except Exception as exc:
            log(f"beep warning: {exc}")

    def record_until_silence(
        self,
        *,
        follow_up: bool = False,
        no_speech_timeout: float = 10.0,
        source_label: str | None = None,
    ) -> Path | None:
        if not follow_up:
            self.beep(880)
            # The cue is intentionally audible, but it must not become the
            # first "voice" frame in the same open microphone stream.
            time.sleep(0.08)
            self.microphone.discard_buffer()
        expression(
            "listening",
            stage="awaiting_follow_up" if follow_up else "recording",
        )
        self.publish_speech_bubble(True)
        source = source_label or ("follow-up" if follow_up else "hardware wake")
        log(f"● Recording... ({source}, auto-stops on silence)")
        if follow_up:
            # Let the speaker tail decay, then discard it so TTS is not transcribed
            # as the user's next turn.
            time.sleep(0.18)
        initial_pre_roll, threshold = self.microphone.prepare_recording()
        if follow_up:
            initial_pre_roll = []
        pre_roll: deque[np.ndarray] = deque(
            initial_pre_roll,
            maxlen=max(1, round(PRE_ROLL_SECONDS / BLOCK_SECONDS)),
        )
        frames_out: list[np.ndarray] = []
        speech_started = False
        speech_started_at = 0.0
        last_voice_at = 0.0
        started = time.monotonic()
        consecutive_voice = 0
        caption_stream: object | None = None
        partial_previous = ""
        self.last_streaming_transcript = ""
        log(f"Adaptive VAD threshold={threshold:.0f}")
        while self.running:
            self.raise_if_interrupted()
            now = time.monotonic()
            if now - started >= 45.0:
                break
            try:
                chunk = self.microphone.get(timeout=0.5)
            except queue.Empty:
                continue
            level = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            if not speech_started:
                pre_roll.append(chunk)
                consecutive_voice = consecutive_voice + 1 if level >= threshold else 0
                if consecutive_voice >= 2:
                    speech_started = True
                    speech_started_at = now
                    last_voice_at = now
                    frames_out.extend(pre_roll)
                    pre_roll.clear()
                    caption_stream = self.streaming_caption.create_stream()
                    if caption_stream is not None:
                        try:
                            partial_text = self.streaming_caption.accept(
                                caption_stream,
                                np.concatenate(frames_out).astype(
                                    np.int16,
                                    copy=False,
                                ),
                            )
                            if partial_text:
                                self.last_streaming_transcript = partial_text
                                self.publish_speech_bubble(
                                    True,
                                    partial_text,
                                    stable_chars=0,
                                )
                                partial_previous = partial_text
                        except Exception as exc:
                            caption_stream = None
                            log(f"streaming caption warning: {exc}")
                elif now - started >= no_speech_timeout:
                    log(f"No speech detected during {source} window")
                    self.publish_speech_bubble(False)
                    return None
                continue

            frames_out.append(chunk)
            if caption_stream is not None:
                try:
                    partial_text = self.streaming_caption.accept(
                        caption_stream,
                        chunk,
                    )
                except Exception as exc:
                    caption_stream = None
                    log(f"streaming caption warning: {exc}")
                    partial_text = ""
                if partial_text and partial_text != partial_previous:
                    self.last_streaming_transcript = partial_text
                    stable_chars = common_prefix_length(
                        partial_previous,
                        partial_text,
                    )
                    self.publish_speech_bubble(
                        True,
                        partial_text,
                        stable_chars=stable_chars,
                    )
                    partial_previous = partial_text
            if level >= threshold:
                last_voice_at = now
            elif now - last_voice_at >= 1.1 and now - speech_started_at >= 0.5:
                break

        if caption_stream is not None:
            try:
                partial_text = self.streaming_caption.finish(caption_stream)
                if partial_text:
                    self.last_streaming_transcript = partial_text
                    if partial_text != partial_previous:
                        stable_chars = common_prefix_length(
                            partial_previous,
                            partial_text,
                        )
                        self.publish_speech_bubble(
                            True,
                            partial_text,
                            stable_chars=stable_chars,
                        )
            except Exception as exc:
                log(f"streaming caption finalization warning: {exc}")

        if not frames_out:
            self.publish_speech_bubble(False)
            return None
        if not follow_up:
            self.beep(660, count=2)
        path = RUNTIME_DIR / f"utterance-{os.getpid()}.wav"
        samples = np.concatenate(frames_out).astype(np.int16)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(samples.tobytes())
        return path

    def acknowledge_hardware_wake(self) -> None:
        """Acknowledge a hardware wake before opening the utterance gate."""
        self.last_result = "wake_acknowledgement"
        expression("listening", stage=self.last_result)
        self.publish_speech_bubble(True)
        self.write_state()
        started = time.monotonic()
        played = self.wake_ack.play()
        if played:
            self.last_wake_ack_at = time.time()
            time.sleep(WAKE_ACK_TAIL_SECONDS)
        self.raise_if_interrupted()
        discarded = self.microphone.discard_buffer()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log(
            "Wake acknowledgement "
            f"played={played} elapsed={elapsed_ms}ms "
            f"discarded_mic_blocks={discarded}"
        )
        self.last_result = "recording"
        self.write_state()

    def ensure_final_asr(self) -> None:
        try:
            self.final_asr.ensure_ready()
            self.last_error = None
        except Exception as exc:
            self.final_asr.last_error = str(exc)
            log(f"Final ASR ensemble warmup failed: {exc}")
        self.write_state()

    def ensure_whisper_pool(self) -> list[WhisperModel]:
        """Preload independent decoders so barge-in never waits on stale native work."""
        with self.whisper_pool_lock:
            if len(self.whisper_models) == WHISPER_POOL_SIZE:
                return self.whisper_models
            started = time.monotonic()
            models = [
                WhisperModel(
                    WHISPER_MODEL,
                    device="cpu",
                    compute_type="int8",
                    local_files_only=True,
                )
                for _index in range(WHISPER_POOL_SIZE)
            ]
            self.whisper_models = models
            while not self.whisper_available.empty():
                try:
                    self.whisper_available.get_nowait()
                except queue.Empty:
                    break
            for index in range(len(models)):
                self.whisper_available.put(index)
            log(
                f"Local faster-whisper {WHISPER_MODEL} pool="
                f"{WHISPER_POOL_SIZE} ready in {time.monotonic() - started:.2f}s"
            )
            self.write_state()
            return self.whisper_models

    def acquire_whisper_slot(
        self,
        cancel_event: threading.Event | None,
    ) -> tuple[int, WhisperModel]:
        models = self.ensure_whisper_pool()
        while True:
            self.raise_if_interrupted(cancel_event)
            try:
                index = self.whisper_available.get(timeout=0.05)
                return index, models[index]
            except queue.Empty:
                continue

    @staticmethod
    def whisper_quality(segments: list[object]) -> tuple[float, float, float]:
        if not segments:
            return -10.0, 0.0, 1.0
        logprob = float(
            sum(float(segment.avg_logprob) for segment in segments) / len(segments)
        )
        compression = max(float(segment.compression_ratio) for segment in segments)
        no_speech = max(float(segment.no_speech_prob) for segment in segments)
        return logprob, compression, no_speech

    def decode_whisper(
        self,
        model: WhisperModel,
        wav_path: Path,
        options: dict,
        cancel_event: threading.Event | None,
    ) -> tuple[str, tuple[float, float, float]]:
        started = time.monotonic()
        segments, _ = model.transcribe(
            str(wav_path),
            language="zh",
            hotwords=WHISPER_HOTWORDS or None,
            **options,
        )
        resolved: list[object] = []
        transcript_parts: list[str] = []
        for segment in segments:
            self.raise_if_interrupted(cancel_event)
            resolved.append(segment)
            transcript_parts.append(segment.text)
        transcript = "".join(transcript_parts).strip()
        quality = self.whisper_quality(resolved)
        log(
            "Whisper decode "
            f"temperature={options['temperature']} beam={options['beam_size']} "
            f"elapsed={time.monotonic() - started:.2f}s "
            f"logprob={quality[0]:.3f} compression={quality[1]:.3f} "
            f"no_speech={quality[2]:.3f}"
        )
        return transcript, quality

    def transcribe(
        self,
        wav_path: Path,
        cancel_event: threading.Event | None = None,
        *,
        minimum_centered_rms: float = FINAL_ASR_MIN_CENTERED_RMS,
        minimum_centered_peak: float = FINAL_ASR_MIN_CENTERED_PEAK,
    ) -> str:
        expression("thinking", stage=self.last_result)
        log("Transcribing with resident SenseVoice + Zipformer CTC ensemble...")
        self.raise_if_interrupted(cancel_event)
        transcript, metadata = self.final_asr.transcribe(
            wav_path,
            minimum_centered_rms=minimum_centered_rms,
            minimum_centered_peak=minimum_centered_peak,
        )
        signal = metadata.get("signal") or {}
        log(
            "Final ASR decision "
            f"route={metadata.get('decision')} "
            f"agreement={float(metadata.get('agreement') or 0.0):.3f} "
            f"rms={float(signal.get('centered_rms') or 0.0):.1f} "
            f"peak={float(signal.get('centered_peak') or 0.0):.0f} "
            f"elapsed={float(metadata.get('elapsed_seconds') or 0.0):.2f}s"
        )
        self.raise_if_interrupted(cancel_event)
        if transcript:
            self.publish_speech_bubble(
                True,
                transcript,
                stable_chars=len(transcript),
                final=True,
                ttl=LIVE_CAPTION_FINAL_HOLD_SECONDS,
            )
        else:
            self.publish_speech_bubble(False)
        return transcript

    def transcribe_interruptibly(
        self,
        wav_path: Path,
        *,
        minimum_centered_rms: float | None = None,
        minimum_centered_peak: float | None = None,
    ) -> str:
        """Let a new wake proceed while stale native ASR calls finish safely."""
        cancel_event = self.interrupt_event
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                threshold_overrides = {}
                if minimum_centered_rms is not None:
                    threshold_overrides["minimum_centered_rms"] = minimum_centered_rms
                if minimum_centered_peak is not None:
                    threshold_overrides["minimum_centered_peak"] = minimum_centered_peak
                result_queue.put(
                    (
                        "result",
                        self.transcribe(
                            wav_path,
                            cancel_event,
                            **threshold_overrides,
                        ),
                    )
                )
            except BaseException as exc:
                result_queue.put(("error", exc))

        worker = threading.Thread(
            target=run,
            name="interruptible-final-asr-turn",
            daemon=True,
        )
        worker.start()
        while worker.is_alive():
            if cancel_event.wait(0.05):
                raise InteractionInterrupted(
                    "speech recognition superseded by a newer wake word"
                )
        kind, value = result_queue.get_nowait()
        self.raise_if_interrupted(cancel_event)
        if kind == "error":
            raise value
        return str(value)

    def handle_agent_clarification(
        self,
        question: str,
        choices: list | None = None,
        multi_select: bool = False,
    ) -> str:
        spoken_question = str(question).strip()
        choice_labels: list[str] = []
        for choice in choices or []:
            if isinstance(choice, dict):
                label = choice.get("label") or choice.get("name") or choice.get("value")
            else:
                label = choice
            if label:
                choice_labels.append(str(label))
        if choice_labels:
            prefix = "可以多选：" if multi_select else "可选项有："
            spoken_question = f"{spoken_question}。{prefix}{'、'.join(choice_labels)}"

        self.conversation_active = True
        self.awaiting_follow_up = True
        self.last_result = "awaiting_clarification"
        self.last_follow_up_at = time.time()
        self.write_state()
        log(f"Agent clarification: {spoken_question}")
        wav_path: Path | None = None
        try:
            self.speak(spoken_question)
            wav_path = self.record_until_silence(
                follow_up=True,
                no_speech_timeout=FOLLOW_UP_NO_SPEECH_SECONDS,
            )
            if wav_path is None:
                return "[用户在连续对话等待窗口内没有回答，请结束本轮并说明稍后可以再次唤醒补充。]"
            answer = self.transcribe_interruptibly(wav_path).strip()
            self.raise_if_interrupted()
            if not answer:
                return "[没有识别到用户的补充，请结束本轮并允许稍后再次唤醒。]"
            self.conversation_turn += 1
            self.last_follow_up_at = time.time()
            log(f"Clarification transcript: {answer}")
            if is_voice_session_end(answer):
                return "[用户要求结束当前连续对话。请简短确认并停止追问。]"
            return answer
        finally:
            self.awaiting_follow_up = False
            if wav_path:
                try:
                    wav_path.unlink()
                except FileNotFoundError:
                    pass
            self.write_state()

    def handle_streaming_tts_first_delta(self) -> None:
        self.latency.mark("llm_first_delta")

    def handle_streaming_tts_first_chunk(self) -> None:
        self.latency.mark("tts_started")

    def handle_streaming_tts_first_audio(self) -> None:
        if self.interrupt_event.is_set():
            return
        self.latency.mark("tts_first_frame")
        self.last_result = "speaking"
        self.write_state()
        log("Streaming TTS first audio submitted")

    def apply_response_expression(self, state: str, source: str) -> None:
        if self.interrupt_event.is_set():
            return
        if state not in SEMANTIC_EXPRESSION_STATES:
            state = "thinking"
            source = "invalid_state_fallback"
        self.last_response_expression = state
        self.last_response_expression_source = source
        self.last_response_expression_at = time.time()
        expression(state, stage=f"response_emotion:{source}")
        self.write_state()
        log(f"Response expression selected state={state} source={source}")

    def ask_hermes(
        self,
        transcript: str,
        force_visual: bool = False,
    ) -> tuple[str, bool, bool]:
        log(f"Transcript: {transcript}")
        if is_flight_price_request(transcript):
            route = extract_flight_route(transcript)
            if route is None:
                log("Flight fare intent detected, but route was incomplete")
                return "可以。你想从哪个城市飞到哪个城市？", True, False
            origin, destination = route
            self.last_result = "searching_flights"
            self.write_state()
            self.latency.mark("live_info_started")
            log(f"Bounded flight search started: {origin} -> {destination}")
            try:
                result = search_next_week(origin, destination)
                response = format_spoken_result(result)
                self.latency.mark("live_info_finished")
                log(
                    "Bounded flight search completed: "
                    f"{len(result.quotes)}/7 days, lowest={result.cheapest.price_cny}"
                )
                return response, False, False
            except Exception as exc:
                self.latency.mark("live_info_finished")
                log(f"Bounded flight search failed: {exc}")
                return (
                    "我已经实际尝试了实时查询，但这次票价页面没有正常返回。"
                    "你可以稍后再试，我不会拿旧价格冒充当前报价。",
                    False,
                    False,
                )
        prompt, visual_routed = route_user_request(transcript, force_visual=force_visual)
        speech_session = (
            StreamingSpeechSession(
                on_first_delta=self.handle_streaming_tts_first_delta,
                on_first_chunk=self.handle_streaming_tts_first_chunk,
                on_first_audio=self.handle_streaming_tts_first_audio,
            )
            if STREAM_TTS_ENABLED
            else None
        )
        self.set_active_speech_session(speech_session)
        emotion_router = ResponseEmotionStreamRouter(
            speech_session.feed if speech_session is not None else None,
            self.apply_response_expression,
        )
        if visual_routed:
            log("Visual intent detected -> fresh camera-hub frame + Qwen vision")
            self.visual_request_active = True
            vision_activity(True)
            self.write_state()
        try:
            routed_prompt = f"{VOICE_DIALOGUE_PROTOCOL}\n\n用户本轮输入：{prompt}"
            self.latency.mark("llm_started")
            try:
                raw_response = ANSI_RE.sub(
                    "",
                    self.hermes.ask(
                        routed_prompt,
                        stream_callback=(
                            emotion_router.feed if speech_session is not None else None
                        ),
                    ),
                ).strip()
            except Exception:
                if speech_session is not None:
                    speech_session.abort()
                self.raise_if_interrupted()
                raise
            self.raise_if_interrupted()
            self.latency.mark("llm_finished")
            if speech_session is not None:
                speech_session.mark_llm_complete()
            if visual_routed:
                self.visual_request_active = False
                vision_activity(False)
                self.write_state()
                visual_routed = False
            response_without_emotion = emotion_router.finalize(raw_response)
            response, needs_follow_up = parse_voice_response(response_without_emotion)
            log(f"Hermes: {response}")
            log(f"Voice follow-up required={needs_follow_up}")
            streamed = speech_session.finish() if speech_session is not None else False
            self.raise_if_interrupted()
            return response, needs_follow_up, streamed
        finally:
            self.set_active_speech_session(None)
            if self.interrupt_event.is_set() and speech_session is not None:
                speech_session.abort()
            if visual_routed:
                self.visual_request_active = False
                vision_activity(False)
                self.write_state()

    def ask_camera_direct(self, transcript: str) -> tuple[str, bool]:
        """Grab one current camera-hub frame and send it straight to Qwen."""
        started = time.monotonic()
        self.visual_request_active = True
        vision_activity(True)
        self.last_result = "camera_snapshot"
        self.write_state()
        temporary = CAMERA_QUICK_FRAME.with_suffix(".tmp")
        try:
            request = urllib.request.Request(
                CAMERA_SNAPSHOT_URL,
                headers={"Cache-Control": "no-cache"},
            )
            local_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            with local_opener.open(request, timeout=4.0) as response:
                image_bytes = response.read(12 * 1024 * 1024 + 1)
            self.raise_if_interrupted()
            if (
                len(image_bytes) < 1024
                or len(image_bytes) > 12 * 1024 * 1024
                or not image_bytes.startswith(b"\xff\xd8")
            ):
                raise RuntimeError("camera-hub 返回的不是有效 JPEG")
            temporary.write_bytes(image_bytes)
            os.replace(temporary, CAMERA_QUICK_FRAME)
            snapshot_elapsed = time.monotonic() - started
            log(
                f"Fresh camera frame captured bytes={len(image_bytes)} "
                f"elapsed={snapshot_elapsed:.3f}s"
            )

            self.last_result = "qwen_direct_vision"
            self.write_state()
            question = f"""用户刚才通过语音问：{transcript}
请只根据这张当前摄像头画面直接回答。使用自然、简短的中文口语，默认两到四个短句；
不要使用 Markdown，不要介绍分析过程，不要把不确定的推断说成事实。"""

            async def analyze() -> str:
                from tools.vision_tools import vision_analyze_tool

                return await vision_analyze_tool(
                    image_url=str(CAMERA_QUICK_FRAME),
                    user_prompt=question,
                )

            analysis_loop = asyncio.new_event_loop()
            analysis_task = analysis_loop.create_task(analyze())
            with self.barge_in_lock:
                self.active_async_loop = analysis_loop
                self.active_async_task = analysis_task
            try:
                self.latency.mark("llm_started")
                raw_result = analysis_loop.run_until_complete(analysis_task)
            except asyncio.CancelledError as exc:
                raise InteractionInterrupted(
                    "visual inference superseded by a newer wake word"
                ) from exc
            finally:
                with self.barge_in_lock:
                    if self.active_async_task is analysis_task:
                        self.active_async_task = None
                        self.active_async_loop = None
                analysis_loop.close()
            self.raise_if_interrupted()
            self.latency.mark("llm_first_delta")
            self.latency.mark("llm_finished")
            try:
                payload = json.loads(raw_result)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Qwen 视觉结果格式无效") from exc
            if not payload.get("success"):
                raise RuntimeError(str(payload.get("error") or "Qwen 视觉分析失败"))
            analysis = str(payload.get("analysis") or "").strip()
            analysis = re.sub(r"^\[[^\]]+\]\s*", "", analysis)
            if not analysis:
                raise RuntimeError("Qwen 没有返回画面描述")
            log(
                "Direct Qwen camera response ready "
                f"elapsed={time.monotonic() - started:.2f}s"
            )
            return analysis, False
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            self.visual_request_active = False
            vision_activity(False)
            self.write_state()

    def run_local_device_command(self, transcript: str) -> str | None:
        if is_workshop_create_intent(transcript):
            result = create_workshop_app(transcript, source="voice")
            if not result.get("ok"):
                return workshop_error_message(result)
            proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else {}
            send_expression_command("workshop_view", active=True)
            self.last_local_command = {
                "action": "create_workshop_app",
                "proposal_id": proposal.get("id"),
                "transcript": transcript,
                "executed_at": time.time(),
            }
            self.last_result = "local_command:create_workshop_app"
            self.write_state()
            log(f"Workshop proposal queued id={proposal.get('id')}")
            return "好的，工坊开始生成受控应用方案了。完成后会在圆屏显示完整权限，只有你批准才会安装。"
        if is_background_task_request(transcript):
            task = enqueue_background_task(transcript)
            if task is not None:
                self.last_local_command = {
                    "action": "enqueue_background_task",
                    "task_id": task.get("id"),
                    "transcript": transcript,
                    "executed_at": time.time(),
                }
                self.last_result = "local_command:enqueue_background_task"
                self.write_state()
                log(f"Background task queued id={task.get('id')}")
                return (
                    "好的，已经放到后台执行了。你可以在手机或电脑上查看进度，"
                    "完成后的报告会自动出现在报告库里。"
                )
        command = parse_local_device_command(transcript)
        if command is None:
            app_name = workshop_launch_name(transcript)
            if app_name:
                try:
                    result = launch_workshop_app_by_name(app_name)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    log(f"Workshop app launch lookup warning: {exc}")
                else:
                    if result.get("ok"):
                        app = result.get("app") if isinstance(result.get("app"), dict) else {}
                        send_expression_command(
                            "workshop_app_view",
                            active=True,
                            app_id=str(app.get("id") or ""),
                        )
                        return f"好的，{app.get('name') or app_name}已经打开。"
            return None

        action = str(command["action"])
        response: str
        if action == "set_volume":
            percent = int(command["percent"])
            environment = os.environ.copy()
            environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            result = subprocess.run(
                [
                    "/usr/bin/wpctl",
                    "set-volume",
                    "@DEFAULT_AUDIO_SINK@",
                    f"{percent / 100.0:.2f}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
                env=environment,
            )
            if result.returncode != 0:
                raise RuntimeError("系统音量设置失败")
            response = f"好的，音量已经调到百分之{percent}。"
        elif action == "open_camera":
            if not send_expression_command("camera_view", active=True):
                raise RuntimeError("相机界面启动失败")
            self.set_visual_mode(True)
            response = "好的，相机已经打开。"
        elif action == "close_camera":
            if not send_expression_command("camera_view", active=False):
                raise RuntimeError("相机界面关闭失败")
            self.set_visual_mode(False)
            response = "好的，已经退出相机。"
        elif action == "open_performance":
            if not send_expression_command("performance_view", active=True):
                raise RuntimeError("性能界面启动失败")
            response = "好的，性能页面已经打开。"
        elif action == "close_performance":
            if not send_expression_command("performance_view", active=False):
                raise RuntimeError("性能界面关闭失败")
            response = "好的，性能页面已经收起来了。"
        elif action == "open_video_call":
            result = activate_call()
            if not bool(result.get("ok")):
                raise RuntimeError("视频通话服务启动失败")
            response = "好的，视频通话已经打开，正在等待对方连接。"
        elif action == "hangup_video_call":
            result = hangup_call()
            if not bool(result.get("ok")):
                raise RuntimeError("视频通话挂断失败")
            response = "好的，通话已经结束。"
        elif action == "open_music":
            if not send_expression_command("music_view", active=True):
                raise RuntimeError("音乐界面启动失败")
            response = "好的，音乐播放器已经打开。"
        elif action == "close_music":
            if not send_expression_command("music_view", active=False):
                raise RuntimeError("音乐界面关闭失败")
            response = "好的，音乐页面已经收起来了，播放不会中断。"
        elif action == "play_music":
            if not send_expression_command("music_view", active=True):
                raise RuntimeError("音乐界面启动失败")
            if not send_expression_command("music_control", action="play"):
                raise RuntimeError("音乐播放失败")
            response = "好的，开始播放。"
        elif action == "pause_music":
            if not send_expression_command("music_control", action="pause"):
                raise RuntimeError("音乐暂停失败")
            response = "好的，音乐已经暂停。"
        elif action in {"next_music", "previous_music"}:
            music_action = "next" if action == "next_music" else "previous"
            if not send_expression_command("music_control", action=music_action):
                raise RuntimeError("音乐切换失败")
            response = "好的，下一首。" if action == "next_music" else "好的，上一首。"
        elif action == "set_music_mode":
            mode = str(command["mode"])
            if not send_expression_command(
                "music_control",
                action="set_mode",
                mode=mode,
            ):
                raise RuntimeError("播放模式设置失败")
            mode_label = {
                "single_repeat": "单曲循环",
                "list_loop": "列表循环",
                "shuffle": "乱序播放",
            }[mode]
            response = f"好的，已经切换到{mode_label}。"
        elif action == "set_music_lyrics":
            enabled = bool(command["enabled"])
            if not send_expression_command(
                "music_control",
                action="set_lyrics",
                enabled=enabled,
            ):
                raise RuntimeError("歌词显示设置失败")
            response = "好的，主页歌词已经打开。" if enabled else "好的，主页歌词已经关闭。"
        elif action == "open_gallery":
            if not send_expression_command("camera_view", active=True):
                raise RuntimeError("相机界面启动失败")
            if not send_expression_command("gallery_view", active=True):
                raise RuntimeError("相册启动失败")
            self.set_visual_mode(False)
            response = "好的，相册已经打开。"
        elif action == "capture_photo":
            if not send_expression_command("camera_view", active=True):
                raise RuntimeError("相机界面启动失败")
            if not send_expression_command("camera_capture"):
                raise RuntimeError("拍照失败")
            self.set_visual_mode(True)
            response = "拍好了，照片已经保存到相册。"
        elif action == "open_workshop":
            if not send_expression_command("workshop_view", active=True):
                raise RuntimeError("工坊界面启动失败")
            # Opening Workshop by voice is an entry into the creation flow, not
            # just navigation.  Defer the second recording until the current
            # acknowledgement has finished playing so it cannot record its own
            # TTS or be rejected by the interaction debounce.
            self.pending_workshop_requirement = True
            response = "好的，工坊已经打开。告诉我你想做一个什么应用？"
        elif action == "close_workshop":
            if not send_expression_command("workshop_view", active=False):
                raise RuntimeError("工坊界面关闭失败")
            response = "好的，工坊已经收起来了。"
        elif action == "stop_workshop_app":
            result = stop_workshop_app()
            if not result.get("ok"):
                raise RuntimeError(workshop_error_message(result))
            send_expression_command("workshop_app_view", active=False)
            response = "好的，当前自定义应用已经退出。"
        elif action == "open_pomodoro":
            if not send_expression_command("pomodoro_view", active=True):
                raise RuntimeError("番茄钟界面启动失败")
            response = "好的，番茄钟已经打开。"
        elif action == "close_pomodoro":
            if not send_expression_command("pomodoro_view", active=False):
                raise RuntimeError("番茄钟界面关闭失败")
            response = "好的，番茄钟会继续计时，我先把界面收起来。"
        elif action == "start_pomodoro":
            if command.get("invalid_duration"):
                response = "番茄钟可以设置一到一百八十分钟，你换个时长告诉我。"
            else:
                minutes = int(command.get("minutes", 25))
                if not send_expression_command("pomodoro_view", active=True):
                    raise RuntimeError("番茄钟界面启动失败")
                if not send_expression_command(
                    "pomodoro_action",
                    action="start_custom",
                    seconds=minutes * 60,
                ):
                    raise RuntimeError("番茄钟启动失败")
                response = f"好的，{minutes}分钟的番茄钟已经开始。"
        elif action in {
            "pause_pomodoro",
            "resume_pomodoro",
            "reset_pomodoro",
            "skip_pomodoro",
        }:
            action_map = {
                "pause_pomodoro": ("pause", "好的，番茄钟已经暂停。"),
                "resume_pomodoro": ("start", "好的，番茄钟继续。"),
                "reset_pomodoro": ("reset", "好的，这一轮番茄钟已经重置。"),
                "skip_pomodoro": ("skip", "好的，已经跳到下一阶段。"),
            }
            pomodoro_action, response = action_map[action]
            if not send_expression_command(
                "pomodoro_action",
                action=pomodoro_action,
            ):
                raise RuntimeError("番茄钟控制失败")
        else:
            return None

        self.last_local_command = {
            **command,
            "transcript": transcript,
            "executed_at": time.time(),
        }
        self.last_result = f"local_command:{action}"
        self.write_state()
        log(f"Local device command action={action} transcript={transcript}")
        return response

    def speak(self, response: str) -> None:
        speech = clean_for_speech(response)
        if not speech:
            return
        self.latency.mark("tts_started")

        async def stream_to_player() -> None:
            first_audio_submitted = False
            player = subprocess.Popen(
                [
                    "/usr/bin/ffplay",
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "error",
                    "-f",
                    "mp3",
                    "-i",
                    "pipe:0",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.set_active_player(player)
            try:
                async for chunk in edge_tts.Communicate(
                    speech, STREAM_TTS_VOICE
                ).stream():
                    self.raise_if_interrupted()
                    if chunk["type"] == "audio" and player.stdin is not None:
                        player.stdin.write(chunk["data"])
                        player.stdin.flush()
                        if not first_audio_submitted:
                            first_audio_submitted = True
                            self.latency.mark("tts_first_frame")
            finally:
                try:
                    if player.stdin is not None:
                        player.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    return_code = player.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    player.terminate()
                    return_code = player.wait(timeout=5)
                self.set_active_player(None)
                if return_code != 0 and not self.interrupt_event.is_set():
                    raise RuntimeError(f"ffplay exited with {return_code}")

        try:
            asyncio.run(stream_to_player())
        except Exception:
            self.raise_if_interrupted()
            raise
        self.raise_if_interrupted()

    def interact(
        self,
        wake: dict | None,
        force_visual: bool | None = None,
        bypass_debounce: bool = False,
    ) -> None:
        now = time.monotonic()
        if self.busy or (
            not bypass_debounce
            and now - self.last_trigger_monotonic < DEBOUNCE_SECONDS
        ):
            return
        EXPRESSION_EVENTS.begin_interaction(
            "hardware_voice" if wake is not None else "voice_control"
        )
        camera_mode_at_start = self.visual_mode_active
        self.interrupt_event = threading.Event()
        self.busy = True
        self.latency.start("hardware_voice" if wake is not None else "voice_control")
        self.microphone.set_learning(False)
        self.last_error = None
        self.last_response_expression = None
        self.last_response_expression_source = None
        self.last_response_expression_at = None
        self.last_result = "recording"
        self.last_trigger_monotonic = now
        self.last_trigger_at = time.time()
        self.last_wake = wake
        self.write_state()
        log(
            "Wake word detected: "
            f"angle={wake.get('angle') if wake else None}, beam={wake.get('beam') if wake else None}"
        )
        wav_path: Path | None = None
        try:
            if wake is not None:
                self.acknowledge_hardware_wake()
            self.raise_if_interrupted()
            self.latency.mark("recording_started")
            wav_path = self.record_until_silence()
            self.latency.mark("speech_end")
            self.raise_if_interrupted()
            if wav_path is None:
                self.last_result = "no_speech"
                expression("idle", stage=self.last_result)
                return
            self.last_result = "transcribing"
            self.write_state()
            self.latency.mark("stt_started")
            transcript = self.transcribe_interruptibly(wav_path)
            self.latency.mark("stt_finished")
            self.raise_if_interrupted()
            if not transcript:
                self.last_result = "empty_transcript"
                expression("idle", stage=self.last_result)
                log("Final ASR rejected or returned an empty transcript")
                return
            self.conversation_active = True
            self.conversation_turn = 1
            current_transcript = transcript
            while self.running:
                self.last_response_expression = None
                self.last_response_expression_source = None
                self.last_response_expression_at = None
                response_streamed = False
                response = self.run_local_device_command(current_transcript)
                if response is None:
                    if camera_mode_at_start and is_visual_intent(current_transcript):
                        response, needs_follow_up = self.ask_camera_direct(
                            current_transcript
                        )
                    else:
                        self.last_result = "asking_hermes"
                        self.write_state()
                        response, needs_follow_up, response_streamed = self.ask_hermes(
                            current_transcript,
                            force_visual=bool(force_visual)
                            and is_visual_intent(current_transcript),
                        )
                else:
                    needs_follow_up = False
                    self.apply_response_expression("happy", "local_command")
                if self.last_response_expression is None:
                    _, fallback_state, fallback_source = parse_response_expression(response)
                    self.apply_response_expression(fallback_state, fallback_source)
                self.raise_if_interrupted()
                self.last_result = "speaking"
                self.write_state()
                if response_streamed:
                    log("Streaming TTS playback completed")
                else:
                    log("TTS playback started")
                    self.speak(response)
                    log("TTS playback completed")
                self.raise_if_interrupted()
                self.latency.mark("playback_finished")
                self.finish_latency("completed")
                if not needs_follow_up:
                    self.last_result = "completed"
                    break
                if self.conversation_turn >= MAX_VOICE_FOLLOW_UP_TURNS:
                    self.last_result = "follow_up_limit"
                    log("Voice follow-up turn limit reached")
                    break

                self.awaiting_follow_up = True
                self.last_result = "awaiting_follow_up"
                self.last_follow_up_at = time.time()
                self.write_state()
                self.latency.start("voice_follow_up")
                self.latency.mark("recording_started")
                follow_up_wav: Path | None = None
                try:
                    follow_up_wav = self.record_until_silence(
                        follow_up=True,
                        no_speech_timeout=FOLLOW_UP_NO_SPEECH_SECONDS,
                    )
                    self.latency.mark("speech_end")
                    self.raise_if_interrupted()
                    if follow_up_wav is None:
                        self.last_result = "follow_up_timeout"
                        log("Voice follow-up window timed out")
                        break
                    self.last_result = "transcribing_follow_up"
                    self.write_state()
                    self.latency.mark("stt_started")
                    current_transcript = self.transcribe_interruptibly(
                        follow_up_wav
                    ).strip()
                    self.latency.mark("stt_finished")
                    self.raise_if_interrupted()
                    if not current_transcript:
                        self.last_result = "empty_follow_up"
                        log("Final ASR rejected or returned an empty follow-up transcript")
                        break
                    if is_voice_session_end(current_transcript):
                        self.last_result = "conversation_ended"
                        log("Voice conversation ended by user")
                        break
                    self.conversation_turn += 1
                    self.last_follow_up_at = time.time()
                    log(f"Follow-up transcript: {current_transcript}")
                finally:
                    self.awaiting_follow_up = False
                    if follow_up_wav:
                        try:
                            follow_up_wav.unlink()
                        except FileNotFoundError:
                            pass
            self.interaction_count += 1
        except InteractionInterrupted:
            self.last_error = None
            self.last_result = "interrupted_by_wake"
            self.publish_speech_bubble(False)
            log("Voice interaction interrupted by a newer wake word")
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = "error"
            self.publish_speech_bubble(False)
            expression("error", 8, stage=self.last_result)
            log(f"voice interaction error: {exc}")
        finally:
            if wav_path:
                try:
                    wav_path.unlink()
                except FileNotFoundError:
                    pass
            self.conversation_active = False
            self.awaiting_follow_up = False
            self.conversation_turn = 0
            self.busy = False
            self.microphone.set_learning(True)
            latency_status = (
                "interrupted"
                if self.last_result == "interrupted_by_wake"
                else "error"
                if self.last_result == "error"
                else self.last_result
            )
            self.finish_latency(latency_status)
            if self.last_result != "error" and not self.has_pending_wake():
                expression("idle", stage=self.last_result)
            self.write_state()

    def interact_text(self, transcript: str, source: str = "screen_menu") -> None:
        transcript = transcript.strip()
        now = time.monotonic()
        if not transcript or self.busy or now - self.last_trigger_monotonic < 1.0:
            return
        EXPRESSION_EVENTS.begin_interaction(f"text:{source}")
        self.interrupt_event = threading.Event()
        self.busy = True
        self.latency.start(f"text:{source}")
        self.microphone.set_learning(False)
        self.last_error = None
        self.last_response_expression = None
        self.last_response_expression_source = None
        self.last_response_expression_at = None
        self.last_result = "asking_hermes"
        self.last_trigger_monotonic = now
        self.last_trigger_at = time.time()
        self.last_wake = {"source": source}
        expression("thinking", stage=self.last_result)
        self.write_state()
        log(f"Text interaction requested source={source}: {transcript}")
        try:
            response_streamed = False
            response = self.run_local_device_command(transcript)
            if response is None:
                response, _, response_streamed = self.ask_hermes(transcript)
            else:
                self.apply_response_expression("happy", "local_command")
            if self.last_response_expression is None:
                _, fallback_state, fallback_source = parse_response_expression(response)
                self.apply_response_expression(fallback_state, fallback_source)
            self.raise_if_interrupted()
            self.last_result = "speaking"
            self.write_state()

            if response_streamed:
                log("Streaming TTS playback completed")
            else:
                log("TTS playback started")
                self.speak(response)
                log("TTS playback completed")
            self.raise_if_interrupted()
            self.latency.mark("playback_finished")
            self.finish_latency("completed")
            self.interaction_count += 1
            self.last_result = "completed"
        except InteractionInterrupted:
            self.last_error = None
            self.last_result = "interrupted_by_wake"
            self.publish_speech_bubble(False)
            log("Text interaction interrupted by a newer wake word")
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = "error"
            expression("error", 8, stage=self.last_result)
            log(f"text interaction error: {exc}")
        finally:
            self.busy = False
            self.microphone.set_learning(True)
            latency_status = (
                "interrupted"
                if self.last_result == "interrupted_by_wake"
                else "error"
                if self.last_result == "error"
                else self.last_result
            )
            self.finish_latency(latency_status)
            if self.last_result != "error" and not self.has_pending_wake():
                expression("idle", stage=self.last_result)
            self.write_state()

    def interact_workshop_prompt(
        self,
        *,
        prompt_user: bool = True,
        bypass_debounce: bool = False,
    ) -> None:
        """Collect one spoken app requirement and submit it to the trusted gate."""

        now = time.monotonic()
        if self.busy or (
            not bypass_debounce and now - self.last_trigger_monotonic < 1.0
        ):
            return
        EXPRESSION_EVENTS.begin_interaction("workshop_requirement")
        self.interrupt_event = threading.Event()
        self.busy = True
        self.microphone.set_learning(False)
        self.last_error = None
        self.last_result = "workshop_invitation"
        self.last_trigger_monotonic = now
        self.last_trigger_at = time.time()
        self.last_wake = {"source": "workshop"}
        self.write_state()
        wav_path: Path | None = None
        try:
            self.apply_response_expression("happy", "workshop_invitation")
            if prompt_user:
                self.speak("可以，告诉我你想做一个什么应用？")
            self.raise_if_interrupted()
            self.last_result = "workshop_recording"
            self.write_state()
            audio_activity(True, source="workshop_requirement", ttl_seconds=60.0)
            wav_path = self.record_until_silence(
                # A screen tap is already an explicit invitation.  Start with
                # the local cue immediately instead of making the user talk
                # over a network-generated TTS prompt.  Voice-opened Workshop
                # remains a natural follow-up after its spoken invitation.
                follow_up=prompt_user,
                no_speech_timeout=FOLLOW_UP_NO_SPEECH_SECONDS,
                source_label=(
                    "workshop screen" if not prompt_user else "workshop follow-up"
                ),
            )
            audio_activity(False, source="workshop_requirement")
            if wav_path is None:
                response = "这次没有听到需求。你准备好后再打开工坊就行。"
            else:
                self.last_result = "workshop_transcribing"
                self.write_state()
                requirement = self.transcribe_interruptibly(
                    wav_path,
                    minimum_centered_rms=(
                        FINAL_ASR_MIN_CENTERED_RMS
                        if prompt_user
                        else WORKSHOP_EXPLICIT_MIN_CENTERED_RMS
                    ),
                    minimum_centered_peak=(
                        FINAL_ASR_MIN_CENTERED_PEAK
                        if prompt_user
                        else WORKSHOP_EXPLICIT_MIN_CENTERED_PEAK
                    ),
                ).strip()
                self.raise_if_interrupted()
                if not requirement:
                    response = "我没有识别清楚。你可以稍后再试一次。"
                else:
                    result = create_workshop_app(requirement, source="screen_voice")
                    if result.get("ok"):
                        proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else {}
                        self.last_local_command = {
                            "action": "create_workshop_app",
                            "proposal_id": proposal.get("id"),
                            "transcript": requirement,
                            "executed_at": time.time(),
                        }
                        response = "收到。工坊正在生成受控方案，稍后请在圆屏审核它申请的全部权限。"
                        send_expression_command("workshop_view", active=True)
                    else:
                        response = workshop_error_message(result)
            self.apply_response_expression("happy", "workshop_submitted")
            self.last_result = "speaking"
            self.write_state()
            self.speak(response)
            self.last_result = "completed"
        except InteractionInterrupted:
            self.last_error = None
            self.last_result = "interrupted_by_wake"
            self.publish_speech_bubble(False)
            log("Workshop requirement interaction interrupted")
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = "error"
            expression("error", 8, stage=self.last_result)
            log(f"Workshop requirement interaction error: {exc}")
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink()
                except FileNotFoundError:
                    pass
            self.busy = False
            self.microphone.set_learning(True)
            audio_activity(False, source="workshop_requirement")
            self.publish_speech_bubble(False)
            if self.last_result != "error" and not self.has_pending_wake():
                expression("idle", stage=self.last_result)
            self.write_state()

    def set_visual_mode(self, active: bool) -> None:
        self.visual_mode_active = bool(active)
        log(f"Visual voice mode active={self.visual_mode_active}")
        self.write_state()

    def check_hardware(self) -> None:
        wake = load_wake()
        token = wake_token(wake)
        if token and token != self.last_token:
            self.last_token = token
            self.interact(wake, bypass_debounce=True)


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    assistant = DailyVoiceAssistant()
    signal.signal(signal.SIGTERM, lambda *_: setattr(assistant, "running", False))
    signal.signal(signal.SIGINT, lambda *_: setattr(assistant, "running", False))
    control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    control.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o660)
    assistant.microphone.start()
    assistant.wake_ack.start()
    # The display is persistent across voice-service restarts. Reconcile any
    # stale non-idle state left by an interrupted or older text interaction
    # before background workers can accept a new wake event.
    expression("idle", stage="service_started")
    threading.Thread(
        target=assistant.hermes.warmup,
        name="hermes-daily-warmup",
        daemon=True,
    ).start()
    threading.Thread(
        target=assistant.ensure_final_asr,
        name="final-asr-ensemble-warmup",
        daemon=True,
    ).start()
    threading.Thread(
        target=assistant.monitor_hardware_wake,
        name="hardware-wake-barge-in",
        daemon=True,
    ).start()
    assistant.write_state()
    log(f"Daily voice assistant ready; hardware wake phrase: {HARDWARE_WAKE_PHRASE}")
    try:
        while assistant.running:
            pending_wake = assistant.pop_pending_wake()
            if pending_wake is not None:
                assistant.interact(
                    pending_wake,
                    bypass_debounce=True,
                )
                if assistant.pop_pending_workshop_requirement():
                    assistant.interact_workshop_prompt(
                        prompt_user=False,
                        bypass_debounce=True,
                    )
                continue
            ready, _, _ = select.select([control], [], [], 0.15)
            if ready:
                try:
                    request = json.loads(control.recv(65535).decode("utf-8"))
                    if request.get("command") == "trigger":
                        assistant.interact(None)
                    elif request.get("command") == "wake_acknowledgement":
                        if not assistant.busy:
                            EXPRESSION_EVENTS.begin_interaction(
                                "wake_acknowledgement_control"
                            )
                            assistant.microphone.set_learning(False)
                            try:
                                assistant.acknowledge_hardware_wake()
                            finally:
                                assistant.last_result = "waiting"
                                assistant.microphone.set_learning(True)
                                assistant.publish_speech_bubble(False)
                                expression("idle", stage=assistant.last_result)
                                assistant.write_state()
                    elif request.get("command") == "visual_mode":
                        active = bool(request.get("active", True))
                        assistant.set_visual_mode(active)
                        if active and bool(request.get("trigger", False)):
                            assistant.interact(
                                None,
                                force_visual=True,
                                bypass_debounce=True,
                            )
                    elif request.get("command") == "query":
                        assistant.interact_text(
                            str(request.get("text", "")),
                            source=str(request.get("source", "screen_menu")),
                        )
                    elif request.get("command") == "workshop_create":
                        requested_at = float(request.get("requested_at") or 0.0)
                        request_age = (
                            time.time() - requested_at if requested_at else 0.0
                        )
                        if (
                            requested_at
                            and request_age > WORKSHOP_CREATE_REQUEST_MAX_AGE_SECONDS
                        ):
                            log(
                                "stale Workshop create request ignored "
                                f"age={request_age:.2f}s"
                            )
                        else:
                            assistant.interact_workshop_prompt(
                                prompt_user=bool(request.get("prompt_user", False)),
                                bypass_debounce=True,
                            )
                except Exception as exc:
                    log(f"invalid voice control command: {exc}")
            if assistant.pop_pending_workshop_requirement():
                assistant.interact_workshop_prompt(
                    prompt_user=False,
                    bypass_debounce=True,
                )
            assistant.check_hardware()
    finally:
        expression("idle", stage="service_stopping")
        assistant.wake_ack.stop()
        assistant.microphone.stop()
        assistant.final_asr.close()
        assistant.hermes.close()
        assistant.publish_speech_bubble(False)
        control.close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
