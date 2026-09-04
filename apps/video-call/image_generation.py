#!/usr/bin/env python3
"""Qwen image generation adapter for persistent RiverBank Chat."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse


DEFAULT_MODEL = "qwen-image-3.0-pro"
DEFAULT_NATIVE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PROMPT_CHARS = 6_000

_ZH_IMAGE_NOUN = r"(?:图片|图像|插画|海报|壁纸|头像|封面|logo|LOGO|图标)"
_ZH_GENERATE_VERB = r"(?:生成|绘制|画|制作|设计|创建|做)"
_IMAGE_REQUEST_PATTERNS = (
    re.compile(rf"(?:帮我|请|给我|麻烦你|替我)?\s*{_ZH_GENERATE_VERB}.{{0,14}}{_ZH_IMAGE_NOUN}"),
    re.compile(r"(?:帮我|请|给我|麻烦你|替我)\s*(?:画|绘制)\s*(?:一|几|个|只|幅|张)?"),
    re.compile(r"^\s*(?:画|绘制)\s*(?:一|几|个|只|幅|张)"),
    re.compile(
        r"\b(?:generate|create|draw|make|design)\b.{0,30}"
        r"\b(?:image|picture|illustration|poster|wallpaper|avatar|cover|logo|icon)\b",
        re.I,
    ),
)
_QUESTION_ONLY_PREFIX = re.compile(
    r"^\s*(?:为什么|为何|怎么没|怎么没有|是否支持|支不支持|会不会|能否生成图片)"
)


class ImageGenerationError(RuntimeError):
    """Raised when the provider cannot produce a valid, persistent image."""


@dataclass(frozen=True)
class GeneratedImage:
    payload: bytes
    filename: str
    media_type: str
    model: str
    request_id: str


def is_image_generation_request(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or _QUESTION_ONLY_PREFIX.search(text):
        return False
    return any(pattern.search(text) for pattern in _IMAGE_REQUEST_PATTERNS)


def _native_base_url(environment: dict[str, str]) -> str:
    raw = (
        environment.get("DASHSCOPE_IMAGE_BASE_URL")
        or environment.get("DASHSCOPE_NATIVE_BASE_URL")
        or environment.get("DASHSCOPE_BASE_URL")
        or DEFAULT_NATIVE_BASE_URL
    ).strip().rstrip("/")
    if "/compatible-mode" in raw:
        raw = raw.split("/compatible-mode", 1)[0].rstrip("/") + "/api/v1"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ImageGenerationError("图片模型地址必须使用 HTTPS")
    if not parsed.hostname.lower().endswith(".aliyuncs.com"):
        raise ImageGenerationError("图片模型地址不是受信任的阿里云中国区端点")
    if not parsed.path or parsed.path == "/":
        raw += "/api/v1"
    return raw.rstrip("/")


def _provider_error(payload: bytes, status: int) -> ImageGenerationError:
    detail = payload.decode("utf-8", errors="replace")[:2_000]
    try:
        decoded = json.loads(detail)
        if isinstance(decoded, dict):
            detail = str(decoded.get("message") or decoded.get("code") or detail)
    except json.JSONDecodeError:
        pass
    return ImageGenerationError(f"Qwen 图片生成失败（{status}）：{detail[:300]}")


class DashScopeImageGenerator:
    def __init__(
        self,
        *,
        environment: dict[str, str] | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        self.api_key = self.environment.get("DASHSCOPE_API_KEY", "").strip()
        self.model = (
            self.environment.get("RIVERBANK_IMAGE_GENERATION_MODEL", DEFAULT_MODEL).strip()
            or DEFAULT_MODEL
        )
        self.base_url = _native_base_url(self.environment)
        self.urlopen = urlopen
        self.timeout_seconds = max(30.0, min(float(timeout_seconds), 600.0))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _read_response(self, request: urllib.request.Request, maximum: int) -> bytes:
        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                payload = response.read(maximum + 1)
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_API_RESPONSE_BYTES)
            raise _provider_error(payload, int(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageGenerationError(f"无法连接 Qwen 图片生成服务：{exc}") from exc
        if len(payload) > maximum:
            raise ImageGenerationError("图片生成服务返回内容过大")
        if not 200 <= status < 300:
            raise _provider_error(payload, status)
        return payload

    def generate(self, prompt: Any) -> GeneratedImage:
        if not self.api_key:
            raise ImageGenerationError("尚未配置阿里云中国区 DASHSCOPE_API_KEY")
        clean_prompt = str(prompt or "").strip()[:MAX_PROMPT_CHARS]
        if not clean_prompt:
            raise ImageGenerationError("图片描述不能为空")
        parameters: dict[str, Any] = {
            "n": 1,
            "prompt_extend": True,
            "watermark": False,
        }
        configured_size = self.environment.get("RIVERBANK_IMAGE_GENERATION_SIZE", "").strip()
        if configured_size:
            parameters["size"] = configured_size
        body = json.dumps(
            {
                "model": self.model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"text": clean_prompt}],
                        }
                    ]
                },
                "parameters": parameters,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/services/aigc/multimodal-generation/generation",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "RiverBank-Assistant/0.25",
            },
            method="POST",
        )
        raw_response = self._read_response(request, MAX_API_RESPONSE_BYTES)
        try:
            response = json.loads(raw_response)
            request_id = str(response.get("request_id") or "")
            content = response["output"]["choices"][0]["message"]["content"]
            image_url = next(
                str(item["image"])
                for item in content
                if isinstance(item, dict) and item.get("image")
            )
        except (json.JSONDecodeError, KeyError, IndexError, StopIteration, TypeError) as exc:
            raise ImageGenerationError("Qwen 返回结果中没有生成图片") from exc
        parsed = urlparse(image_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not hostname.endswith(".aliyuncs.com") or ".oss-" not in hostname:
            raise ImageGenerationError("Qwen 返回了不受信任的图片下载地址")
        image_request = urllib.request.Request(
            image_url,
            headers={"Accept": "image/png,image/jpeg,image/webp", "User-Agent": "RiverBank-Assistant/0.25"},
        )
        payload = self._read_response(image_request, MAX_IMAGE_BYTES)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = request_id.replace("-", "")[:8] or "qwen"
        return GeneratedImage(
            payload=payload,
            filename=f"riverbank-generated-{stamp}-{suffix}.png",
            media_type="image/png",
            model=self.model,
            request_id=request_id,
        )
