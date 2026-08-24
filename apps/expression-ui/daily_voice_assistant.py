#!/usr/bin/env python3
"""Hands-free Daily Hermes loop driven by ListenGo hardware wake events."""

from __future__ import annotations

import asyncio
import contextlib
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
import urllib.request
import wave
from collections import deque
from pathlib import Path

import edge_tts
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

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
DAILY = os.environ.get("RIVERBANK_DAILY_BIN", str(RIVERBANK_HOME / ".local/bin/daily"))
EXPRESSION_SOCKET = Path("/run/riverbank-expression/control.sock")
CAMERA_SNAPSHOT_URL = os.environ.get(
    "RIVERBANK_CAMERA_SNAPSHOT_URL",
    "http://127.0.0.1:19733/snapshot",
)
CAMERA_QUICK_FRAME = WORKSPACE / "rgb_now.jpg"
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
WAKE_ACK_TAIL_SECONDS = 0.10
SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.05
PRE_ROLL_SECONDS = 0.45
DEBOUNCE_SECONDS = 8.0
FOLLOW_UP_NO_SPEECH_SECONDS = 12.0
MAX_VOICE_FOLLOW_UP_TURNS = 4
LIVE_CAPTION_FINAL_HOLD_SECONDS = 1.0
STREAMING_ASR_MODEL_DIR = Path(
    os.environ.get(
        "RIVERBANK_STREAMING_ASR_MODEL_DIR",
        "/mnt/nvme64/ai/models/"
        "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23",
    )
)
VOICE_FOLLOW_UP_MARKER = "[[AWAITING_VOICE_REPLY]]"
VOICE_DIALOGUE_PROTOCOL = f"""
[语音连续对话协议]
你正在通过扬声器和用户进行口语对话。回复要自然、简短、适合朗读。
只有当你确实需要用户补充、选择或确认后才能继续时，才在回复最后单独追加
{VOICE_FOLLOW_UP_MARKER}
不要在其他情况下输出该标记，也不要解释这个标记。
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


def expression(state: str, ttl: float | None = None) -> None:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(
            json.dumps({"state": state, "ttl": ttl}).encode("utf-8"),
            str(EXPRESSION_SOCKET),
        )
        client.close()
    except OSError:
        pass


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
    return None


def route_user_request(transcript: str, force_visual: bool = False) -> tuple[str, bool]:
    voice_contract = """

[语音界面回答要求]
这是通过扬声器进行的现场对话，请像身边的日常助手一样说话，而不是写报告：
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


class PersistentHermesRuntime:
    """Reuse one Daily-profile AIAgent instead of spawning Hermes every turn."""

    def __init__(self) -> None:
        self.agent = None
        self.session_db = None
        self.turn_count = 0
        self.lock = threading.RLock()
        self.clarify_handler = None

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
        toolsets = sorted(_get_platform_tools(config, "cli"))
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
        log(f"Persistent Daily Hermes ready in {time.monotonic() - started:.2f}s")

    def warmup(self) -> None:
        """Build the Daily-profile agent before the first wake-up."""
        with self.lock:
            if self.agent is None:
                try:
                    self._build()
                except Exception as exc:
                    log(f"Persistent Daily Hermes warmup warning: {exc}")
                    self.close()

    def ask(self, prompt: str) -> str:
        with self.lock:
            if self.agent is None or self.turn_count >= 20:
                self.close()
                self._build()
            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    result = self.agent.run_conversation(prompt)
            except Exception:
                self.close()
                raise
            self.turn_count += 1
            response = str(result.get("final_response") or "").strip()
            if not response:
                raise RuntimeError("Daily Hermes returned an empty response")
            return response

    def close(self) -> None:
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
    """Low-latency first-pass captions; final text still comes from Whisper."""

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


class DailyVoiceAssistant:
    def __init__(self) -> None:
        self.running = True
        self.busy = False
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
        self.whisper: WhisperModel | None = None
        self.whisper_lock = threading.Lock()
        self.whisper_inference_lock = threading.Lock()
        self.streaming_caption = StreamingCaptionRecognizer()
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
            "hardware_keyword": "猪逼猪逼",
            "wake_acknowledgement": {
                "enabled": self.wake_ack.ready,
                "text": WAKE_ACK_TEXT,
                "path": str(self.wake_ack.path),
                "duration_ms": self.wake_ack.duration_ms,
                "tail_guard_ms": round(WAKE_ACK_TAIL_SECONDS * 1000),
                "last_played_at": self.last_wake_ack_at,
                "last_error": self.wake_ack.last_error,
            },
            "busy": self.busy,
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
                "final_engine": "faster-whisper-base",
            },
            "last_trigger_at": self.last_trigger_at,
            "last_wake": self.last_wake,
            "started_at": self.started_at,
            "updated_at": time.time(),
        }
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, STATE_PATH)

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
    ) -> Path | None:
        if not follow_up:
            self.beep(880)
        expression("listening")
        self.publish_speech_bubble(True)
        source = "follow-up" if follow_up else "hardware wake"
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
        log(f"Adaptive VAD threshold={threshold:.0f}")
        while self.running:
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
                if partial_text and partial_text != partial_previous:
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
        expression("listening")
        self.publish_speech_bubble(True)
        self.write_state()
        started = time.monotonic()
        played = self.wake_ack.play()
        if played:
            self.last_wake_ack_at = time.time()
            time.sleep(WAKE_ACK_TAIL_SECONDS)
        discarded = self.microphone.discard_buffer()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log(
            "Wake acknowledgement "
            f"played={played} elapsed={elapsed_ms}ms "
            f"discarded_mic_blocks={discarded}"
        )
        self.last_result = "recording"
        self.write_state()

    def ensure_whisper(self) -> WhisperModel:
        with self.whisper_lock:
            if self.whisper is None:
                started = time.monotonic()
                self.whisper = WhisperModel("base", device="cpu", compute_type="int8")
                log(f"Local faster-whisper base ready in {time.monotonic() - started:.2f}s")
            return self.whisper

    def transcribe(self, wav_path: Path) -> str:
        expression("thinking")
        log("Transcribing with resident local faster-whisper base...")
        with self.whisper_inference_lock:
            segments, _ = self.ensure_whisper().transcribe(
                str(wav_path),
                language="zh",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400},
            )
            transcript = "".join(segment.text for segment in segments).strip()
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
            answer = self.transcribe(wav_path).strip()
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

    def ask_hermes(
        self,
        transcript: str,
        force_visual: bool = False,
    ) -> tuple[str, bool]:
        log(f"Transcript: {transcript}")
        prompt, visual_routed = route_user_request(transcript, force_visual=force_visual)
        if visual_routed:
            log("Visual intent detected -> fresh camera-hub frame + Qwen vision")
            self.visual_request_active = True
            vision_activity(True)
            self.write_state()
        try:
            routed_prompt = f"{VOICE_DIALOGUE_PROTOCOL}\n\n用户本轮输入：{prompt}"
            raw_response = ANSI_RE.sub("", self.hermes.ask(routed_prompt)).strip()
            response, needs_follow_up = parse_voice_response(raw_response)
            log(f"Hermes: {response}")
            log(f"Voice follow-up required={needs_follow_up}")
            return response, needs_follow_up
        finally:
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

            raw_result = asyncio.run(analyze())
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
        command = parse_local_device_command(transcript)
        if command is None:
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

    @staticmethod
    def speak(response: str) -> None:
        speech = clean_for_speech(response)
        if not speech:
            return
        async def stream_to_player() -> None:
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
            try:
                async for chunk in edge_tts.Communicate(
                    speech, "zh-CN-XiaoxiaoNeural"
                ).stream():
                    if chunk["type"] == "audio" and player.stdin is not None:
                        player.stdin.write(chunk["data"])
                        player.stdin.flush()
            finally:
                if player.stdin is not None:
                    player.stdin.close()
                try:
                    return_code = player.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    player.terminate()
                    return_code = player.wait(timeout=5)
                if return_code != 0:
                    raise RuntimeError(f"ffplay exited with {return_code}")

        asyncio.run(stream_to_player())

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
        camera_mode_at_start = self.visual_mode_active
        self.busy = True
        self.microphone.set_learning(False)
        self.last_error = None
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
            wav_path = self.record_until_silence()
            if wav_path is None:
                self.last_result = "no_speech"
                expression("idle")
                return
            self.last_result = "transcribing"
            self.write_state()
            transcript = self.transcribe(wav_path)
            if not transcript:
                self.last_result = "empty_transcript"
                expression("idle")
                log("Whisper returned an empty transcript")
                return
            self.conversation_active = True
            self.conversation_turn = 1
            current_transcript = transcript
            while self.running:
                response = self.run_local_device_command(current_transcript)
                if response is None:
                    if camera_mode_at_start and is_visual_intent(current_transcript):
                        response, needs_follow_up = self.ask_camera_direct(
                            current_transcript
                        )
                    else:
                        self.last_result = "asking_hermes"
                        self.write_state()
                        response, needs_follow_up = self.ask_hermes(
                            current_transcript,
                            force_visual=bool(force_visual)
                            and is_visual_intent(current_transcript),
                        )
                else:
                    needs_follow_up = False
                    expression("happy", 2.5)
                self.last_result = "speaking"
                self.write_state()
                log("TTS playback started")
                self.speak(response)
                log("TTS playback completed")
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
                follow_up_wav: Path | None = None
                try:
                    follow_up_wav = self.record_until_silence(
                        follow_up=True,
                        no_speech_timeout=FOLLOW_UP_NO_SPEECH_SECONDS,
                    )
                    if follow_up_wav is None:
                        self.last_result = "follow_up_timeout"
                        log("Voice follow-up window timed out")
                        break
                    self.last_result = "transcribing_follow_up"
                    self.write_state()
                    current_transcript = self.transcribe(follow_up_wav).strip()
                    if not current_transcript:
                        self.last_result = "empty_follow_up"
                        log("Whisper returned an empty follow-up transcript")
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
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = "error"
            self.publish_speech_bubble(False)
            expression("error", 8)
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
            self.last_token = wake_token(load_wake())
            if self.last_result != "error":
                expression("idle")
            self.write_state()

    def interact_text(self, transcript: str, source: str = "screen_menu") -> None:
        transcript = transcript.strip()
        now = time.monotonic()
        if not transcript or self.busy or now - self.last_trigger_monotonic < 1.0:
            return
        self.busy = True
        self.microphone.set_learning(False)
        self.last_error = None
        self.last_result = "asking_hermes"
        self.last_trigger_monotonic = now
        self.last_trigger_at = time.time()
        self.last_wake = {"source": source}
        expression("thinking")
        self.write_state()
        log(f"Text interaction requested source={source}: {transcript}")
        try:
            response = self.run_local_device_command(transcript)
            if response is None:
                response, _ = self.ask_hermes(transcript)
            else:
                expression("happy", 2.5)
            self.last_result = "speaking"
            self.write_state()
            log("TTS playback started")
            self.speak(response)
            self.interaction_count += 1
            self.last_result = "completed"
            log("TTS playback completed")
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = "error"
            expression("error", 8)
            log(f"text interaction error: {exc}")
        finally:
            self.busy = False
            self.microphone.set_learning(True)
            self.last_token = wake_token(load_wake())
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
            self.interact(wake)


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
    threading.Thread(
        target=assistant.hermes.warmup,
        name="hermes-daily-warmup",
        daemon=True,
    ).start()
    threading.Thread(
        target=assistant.ensure_whisper,
        name="whisper-base-warmup",
        daemon=True,
    ).start()
    assistant.write_state()
    log("Daily voice assistant ready; hardware wake phrase: 猪逼猪逼")
    try:
        while assistant.running:
            ready, _, _ = select.select([control], [], [], 0.15)
            if ready:
                try:
                    request = json.loads(control.recv(65535).decode("utf-8"))
                    if request.get("command") == "trigger":
                        assistant.interact(None)
                    elif request.get("command") == "wake_acknowledgement":
                        if not assistant.busy:
                            assistant.microphone.set_learning(False)
                            try:
                                assistant.acknowledge_hardware_wake()
                            finally:
                                assistant.last_result = "waiting"
                                assistant.microphone.set_learning(True)
                                assistant.publish_speech_bubble(False)
                                expression("idle")
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
                except Exception as exc:
                    log(f"invalid voice control command: {exc}")
            assistant.check_hardware()
    finally:
        assistant.wake_ack.stop()
        assistant.microphone.stop()
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
