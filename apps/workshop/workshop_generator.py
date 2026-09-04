#!/usr/bin/env python3
"""AI-assisted, policy-constrained Workshop application planning.

The model can only propose metadata and a finite declarative pipeline. The
trusted service derives permissions and the final manifest itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from workshop_contract import ContractError, validate_manifest
from workshop_declarative import permissions_for_app, validate_declarative_app


MODEL_PLAN_SCHEMA = "riverbank.workshop-plan/v1"
FORBIDDEN_REQUIREMENT_PATTERNS = (
    r"(?:删除|清空|格式化|抹掉).{0,8}(?:系统|根目录|主目录|整块磁盘|所有文件)",
    r"(?:关闭|绕过|禁用).{0,8}(?:安全|自检|权限|审计|沙箱)",
    r"(?:执行|运行).{0,8}(?:sudo|shell|终端命令|任意命令)",
    r"(?:读取|导出|显示).{0,8}(?:密钥|密码|token|令牌|凭据)",
    r"(?:修改|停止|删除).{0,8}(?:systemd|系统服务|内核|防火墙)",
)


def requirement_policy_error(requirement: str) -> str | None:
    compact = re.sub(r"\s+", "", requirement).lower()
    for pattern in FORBIDDEN_REQUIREMENT_PATTERNS:
        if re.search(pattern, compact, flags=re.IGNORECASE):
            return "需求要求修改或绕过受保护的系统能力，工坊不会生成这类应用。"
    return None


def _clean_text(value: Any, *, fallback: str, maximum: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return (text or fallback)[:maximum]


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ContractError("generator_invalid_json", "应用规划模型没有返回有效 JSON。", "generator")


def _class_from_requirement(requirement: str) -> list[str]:
    mapping = (
        (("小猫", "猫咪", "猫"), "cat"),
        (("小狗", "狗狗", "狗"), "dog"),
        (("人脸", "面孔", "脸"), "person"),
        (("行人", "人员", "人体", "人"), "person"),
        (("汽车", "车辆", "车"), "car"),
        (("自行车", "单车"), "bicycle"),
        (("鸟",), "bird"),
        (("杯子", "水杯"), "cup"),
        (("手机",), "cell_phone"),
        (("书",), "book"),
    )
    compact = re.sub(r"\s+", "", requirement)
    result: list[str] = []
    for aliases, label in mapping:
        if any(alias in compact for alias in aliases) and label not in result:
            result.append(label)
    return result or ["person"]


def fallback_plan(requirement: str) -> dict[str, Any]:
    """Produce a useful safe plan when the language model is unavailable."""

    compact = re.sub(r"\s+", "", requirement)
    name_source = re.sub(
        r"^(?:请|帮我|给我)?(?:创建|做|开发|新增|写)(?:一个|个)?",
        "",
        compact,
    )
    name_source = re.sub(r"(?:应用|app|功能).*$", "", name_source, flags=re.IGNORECASE)
    name = (name_source[:10] or "我的应用") + ("助手" if len(name_source) <= 6 else "")
    notification = any(word in compact for word in ("提醒", "通知", "告诉我", "提示"))
    if any(word in compact for word in ("相机", "摄像头", "识别", "检测", "看到", "路过")):
        classes = _class_from_requirement(requirement)
        pipeline: list[dict[str, Any]] = [
            {
                "source": "camera.stream",
                "leaseSeconds": 30,
                "privacyIndicator": True,
            },
            {
                "operator": "vision.detect",
                "model": "host.default-object-detector",
                "classes": classes,
                "minimumConfidence": 0.55,
                "maximumFps": 6,
            },
            {
                "operator": "counter.increment",
                "key": "detections",
                "whenClass": classes[0],
            },
            {"sink": "ui.present", "view": "detection-counter", "title": name},
        ]
        if notification:
            pipeline.append(
                {
                    "sink": "notifications.show",
                    "title": name,
                    "body": "检测到关注目标。",
                    "cooldownSeconds": 30,
                }
            )
    elif any(
        word in compact
        for word in (
            "麦克风",
            "声音",
            "音量",
            "噪声",
            "噪音",
            "拍手",
            "哭声",
            "叫声",
        )
    ):
        pipeline = [
            {
                "source": "microphone.stream",
                "leaseSeconds": 120,
                "sampleRate": 16000,
                "frameMilliseconds": 100,
                "privacyIndicator": True,
            },
            {
                "operator": "audio.level",
                "minimumDbfs": -38,
                "holdMilliseconds": 300,
            },
            {
                "operator": "counter.increment",
                "key": "sound_events",
                "whenSound": True,
            },
            {"sink": "ui.present", "view": "sound-meter", "title": name},
        ]
        if notification:
            pipeline.append(
                {
                    "sink": "notifications.show",
                    "title": name,
                    "body": "检测到超过阈值的声音。",
                    "cooldownSeconds": 15,
                }
            )
    elif any(word in compact for word in ("定时", "计时", "倒计时", "每隔", "周期")):
        minute_match = re.search(r"([0-9]{1,4})分钟", compact)
        seconds = max(60, min(int(minute_match.group(1)) * 60, 86400)) if minute_match else 1500
        pipeline = [
            {"source": "timer.interval", "seconds": seconds, "repeat": True},
            {"operator": "counter.increment", "key": "rounds"},
            {"sink": "ui.present", "view": "timer-status", "title": name},
            {
                "sink": "notifications.show",
                "title": name,
                "body": "本轮计时已经结束。",
                "cooldownSeconds": max(5, seconds - 1),
            },
        ]
    elif any(word in compact for word in ("调研", "搜索", "整理", "报告", "新闻")):
        pipeline = [
            {"source": "app.lifecycle.foreground"},
            {"sink": "tasks.create", "prompt": requirement, "kind": "research"},
            {"sink": "ui.present", "view": "task-status", "title": name},
        ]
    else:
        pipeline = [
            {"source": "app.lifecycle.foreground"},
            {"operator": "counter.increment", "key": "opens"},
            {"sink": "ui.present", "view": "simple-dashboard", "title": name},
        ]
    return {
        "schema": MODEL_PLAN_SCHEMA,
        "name": name[:20],
        "description": requirement[:160],
        "menuLabel": name[:6],
        "glyph": name[0],
        "pipeline": pipeline,
        "safety": {
            "runOnlyInForeground": True,
            "stopOnUserExit": True,
            "retainImages": False,
            "retainAudio": False,
        },
        "generator": "local-safe-fallback",
    }


def generator_prompt(requirement: str) -> str:
    return f"""
你是 RiverBank 工坊的应用规划器。只输出一个 JSON 对象，不要 Markdown、解释或代码。

用户需求：{requirement}

输出结构：
{{
  "schema": "{MODEL_PLAN_SCHEMA}",
  "name": "中文应用名，最多20字",
  "description": "最多160字",
  "menuLabel": "最多6字",
  "glyph": "一个或两个中文字符",
  "pipeline": [声明式节点],
  "safety": {{"runOnlyInForeground": true, "stopOnUserExit": true, "retainImages": false, "retainAudio": false}}
}}

pipeline 必须有且仅有一个 source，且放第一项；至少有一个 sink，最多12项。
允许的 source：
- {{"source":"app.lifecycle.foreground"}}
- {{"source":"camera.stream","leaseSeconds":1到300,"privacyIndicator":true}}
- {{"source":"microphone.stream","leaseSeconds":1到300,"sampleRate":16000或48000,"frameMilliseconds":20到500的整数,"privacyIndicator":true}}
- {{"source":"timer.interval","seconds":1到86400,"repeat":true或false}}
允许的 operator：
- {{"operator":"vision.detect","model":"host.default-object-detector","classes":[英文COCO类别],"minimumConfidence":0.1到0.99,"maximumFps":0.2到12}}
- {{"operator":"audio.level","minimumDbfs":-80到-3,"holdMilliseconds":0到5000}}
- {{"operator":"counter.increment","key":"安全短名称","whenClass":"可选英文类别","whenSound":"可选布尔值；不得与whenClass同时使用"}}
- {{"operator":"text.compose","template":"最多240字"}}
- {{"operator":"assistant.query","prompt":"最多1200字"}}
允许的 sink：
- {{"sink":"ui.present","view":"安全短名称","title":"可选标题"}}
- {{"sink":"notifications.show","title":"标题","body":"正文","cooldownSeconds":5到86400}}
- {{"sink":"storage.put","path":"安全相对文件名","value":{{}}}}
- {{"sink":"tasks.create","prompt":"任务","kind":"general|research|file"}}

不得输出 Python、Shell、命令、URL、绝对路径、系统服务、密钥、sudo、软件安装、设备节点或未列出的字段。
涉及摄像头就必须使用 camera.stream，涉及麦克风就必须使用 microphone.stream，并且 privacyIndicator=true。麦克风 v1 只提供实时音量指标，不提供原始音频保存，retainAudio 必须为 false。不要为了显得强大而申请与需求无关的能力。
""".strip()


class HermesPlanGenerator:
    def __init__(
        self,
        *,
        hermes_bin: Path = Path("/home/geo/.local/bin/hermes"),
        workspace: Path = Path("/mnt/nvme64/riverbank-user/workshop/generator-workspace"),
        timeout_seconds: float = 180.0,
    ) -> None:
        self.hermes_bin = Path(hermes_bin)
        self.workspace = Path(workspace)
        self.timeout_seconds = max(30.0, min(float(timeout_seconds), 300.0))

    def generate(self, requirement: str) -> dict[str, Any]:
        policy_error = requirement_policy_error(requirement)
        if policy_error:
            raise ContractError("forbidden_requirement", policy_error, "requirement")
        if not self.hermes_bin.is_file():
            return fallback_plan(requirement)
        self.workspace.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.hermes_bin),
            "chat",
            "--query",
            generator_prompt(requirement),
            "--quiet",
            "--toolsets",
            "clarify",
            "--reasoning",
            "medium",
            "--max-turns",
            "4",
            "--source",
            "riverbank-workshop",
            "--in",
            str(self.workspace),
        ]
        environment = os.environ.copy()
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            raise ContractError("generator_timeout", "应用规划模型响应超时。", "generator") from exc
        if process.returncode != 0:
            return fallback_plan(requirement)
        try:
            plan = _extract_json(output)
            plan["generator"] = "hermes-plan"
            return plan
        except ContractError:
            return fallback_plan(requirement)


def normalize_plan(requirement: str, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema") != MODEL_PLAN_SCHEMA:
        raise ContractError(
            "unsupported_plan_schema",
            f"应用规划必须使用 {MODEL_PLAN_SCHEMA}。",
            "plan.schema",
        )
    app = validate_declarative_app(
        {
            "schema": "riverbank.declarative-app/v1",
            "title": _clean_text(plan.get("name"), fallback="我的应用", maximum=40),
            "pipeline": plan.get("pipeline"),
            "safety": plan.get("safety", {}),
        }
    )
    name = _clean_text(plan.get("name"), fallback="我的应用", maximum=20)
    description = _clean_text(
        plan.get("description"),
        fallback=requirement,
        maximum=160,
    )
    menu_label = _clean_text(plan.get("menuLabel"), fallback=name, maximum=6)
    glyph = _clean_text(plan.get("glyph"), fallback=name[:1] or "应", maximum=2)
    return {
        "schema": MODEL_PLAN_SCHEMA,
        "name": name,
        "description": description,
        "menuLabel": menu_label,
        "glyph": glyph,
        "app": app,
        "generator": str(plan.get("generator") or "unknown")[:40],
    }


def build_candidate(requirement: str, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = normalize_plan(requirement, plan)
    app = normalized["app"]
    permissions = permissions_for_app(app)
    suffix = hashlib.sha256(
        f"{requirement}\n{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:12]
    app_id = f"local.workshop.app{suffix}"
    capability_count = len(permissions)
    resources = {
        "cpuPercent": min(35, 5 + capability_count * 4),
        "memoryMB": min(384, 64 + capability_count * 32),
        "storageMB": 128,
        "maxProcesses": 2,
    }
    manifest = {
        "apiVersion": "riverbank.workshop/v1",
        "kind": "RiverBankApp",
        "metadata": {
            "id": app_id,
            "name": normalized["name"],
            "version": "0.1.0",
            "description": normalized["description"],
            "vendor": "RiverBank Local Workshop",
        },
        "spec": {
            "runtime": {
                "kind": "declarative-v1",
                "entrypoint": "app/main.json",
                "protocol": "riverbank.app-host/v1",
            },
            "permissions": permissions,
            "resources": resources,
            "lifecycle": {
                "autostart": False,
                "startTimeoutSeconds": 10,
                "stopTimeoutSeconds": 5,
            },
            "ui": {
                "menuLabel": normalized["menuLabel"],
                "glyph": normalized["glyph"],
            },
        },
    }
    return validate_manifest(manifest), app
