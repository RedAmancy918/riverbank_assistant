#!/usr/bin/env python3
"""Strict declarative-app contract used by every generated Workshop app.

The model may propose a pipeline, but this module owns the accepted language.
There is deliberately no generic command, import, expression, script, URL, or
host path node in the language.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from workshop_contract import ContractError


DECLARATIVE_SCHEMA = "riverbank.declarative-app/v1"
SURFACE_SCHEMA = "riverbank.surface/v1"
MAX_PIPELINE_NODES = 12
MAX_SURFACE_COMPONENTS = 6

SUPPORTED_UI_VIEWS = frozenset(
    {
        "adaptive",
        "cat-counter",
        "clock",
        "detection-counter",
        "main",
        "simple-dashboard",
        "sound-meter",
        "status",
        "task-status",
        "timer-status",
    }
)
SURFACE_LAYOUTS = frozenset({"hero", "dashboard", "list"})
SURFACE_ACCENTS = frozenset({"cyan", "green", "amber", "red", "neutral"})
SURFACE_COMPONENT_FIELDS: dict[str, set[str]] = {
    "clock": {"id", "type", "format", "showSeconds", "showDate", "showWeekday"},
    "metric": {"id", "type", "valueKey", "label", "unit", "precision"},
    "progress": {"id", "type", "valueKey", "label", "minimum", "maximum"},
    "status": {"id", "type", "valueKey", "label"},
    "text": {"id", "type", "valueKey", "text", "role"},
}

SOURCE_FIELDS: dict[str, set[str]] = {
    "app.lifecycle.foreground": {"source"},
    "camera.stream": {"source", "leaseSeconds", "privacyIndicator"},
    "microphone.stream": {
        "source",
        "leaseSeconds",
        "sampleRate",
        "frameMilliseconds",
        "privacyIndicator",
    },
    "timer.interval": {"source", "seconds", "repeat"},
}
OPERATOR_FIELDS: dict[str, set[str]] = {
    "vision.detect": {
        "operator",
        "model",
        "classes",
        "minimumConfidence",
        "maximumFps",
    },
    "audio.level": {"operator", "minimumDbfs", "holdMilliseconds"},
    "counter.increment": {"operator", "key", "whenClass", "whenSound"},
    "text.compose": {"operator", "template"},
    "assistant.query": {"operator", "prompt"},
}
SINK_FIELDS: dict[str, set[str]] = {
    "ui.present": {"sink", "view", "title", "presentation"},
    "notifications.show": {"sink", "title", "body", "cooldownSeconds"},
    "storage.put": {"sink", "path", "value"},
    "tasks.create": {"sink", "prompt", "kind"},
}

NODE_CAPABILITY: dict[tuple[str, str], str] = {
    ("source", "camera.stream"): "camera.stream",
    ("source", "microphone.stream"): "microphone.stream",
    ("operator", "vision.detect"): "vision.inference",
    ("operator", "counter.increment"): "storage.app",
    ("operator", "assistant.query"): "assistant.query",
    ("sink", "ui.present"): "ui.surface",
    ("sink", "notifications.show"): "notifications.local",
    ("sink", "storage.put"): "storage.app",
    ("sink", "tasks.create"): "tasks.submit",
}

CAPABILITY_REASON = {
    "ui.surface": "在圆屏显示这个应用的受控界面。",
    "storage.app": "只在该应用自己的私有目录保存状态。",
    "camera.stream": "应用位于前台时读取 Camera Hub 画面。",
    "microphone.stream": "应用位于前台时读取不落盘的麦克风音量分析流。",
    "vision.inference": "调用宿主白名单视觉模型进行推理。",
    "notifications.local": "在设备本地显示限频提示。",
    "assistant.query": "在用户可见会话中请求 Daily 助手处理内容。",
    "tasks.submit": "向受控后台任务队列提交工作。",
}


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("invalid_declarative_type", "字段必须是对象。", field)
    return value


def _text(
    value: Any,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 160,
) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_declarative_text", "字段必须是文本。", field)
    result = value.replace("\x00", "").strip()
    if len(result) < minimum or len(result) > maximum:
        raise ContractError(
            "invalid_declarative_text",
            f"文本长度必须在 {minimum} 到 {maximum} 之间。",
            field,
        )
    return result


def _number(
    value: Any,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("invalid_declarative_number", "字段必须是数字。", field)
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ContractError(
            "invalid_declarative_number",
            f"数字必须在 {minimum:g} 到 {maximum:g} 之间。",
            field,
        )
    return result


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(
            "unknown_declarative_field",
            f"声明式节点包含未知字段：{', '.join(unknown)}。",
            field,
        )


def _safe_name(value: Any, field: str, maximum: int = 48) -> str:
    text = _text(value, field, maximum=maximum)
    if not all(character.isalnum() or character in "._-" for character in text):
        raise ContractError(
            "invalid_declarative_name",
            "名称只能包含字母、数字、点、下划线和短横线。",
            field,
        )
    return text


def _validate_surface_component(value: Any, field: str) -> dict[str, Any]:
    node = dict(_mapping(value, field))
    kind = _text(node.get("type"), f"{field}.type", maximum=24)
    allowed = SURFACE_COMPONENT_FIELDS.get(kind)
    if allowed is None:
        raise ContractError(
            "unsupported_surface_component",
            f"宿主不能渲染组件 {kind}。",
            f"{field}.type",
        )
    _reject_unknown(node, allowed, field)
    result: dict[str, Any] = {
        "id": _safe_name(node.get("id", kind), f"{field}.id", 32),
        "type": kind,
    }
    if kind == "clock":
        clock_format = str(node.get("format", "24h")).strip().lower()
        if clock_format not in {"24h", "12h"}:
            raise ContractError(
                "invalid_clock_format",
                "时钟格式只允许 24h 或 12h。",
                f"{field}.format",
            )
        result.update(
            {
                "format": clock_format,
                "showSeconds": bool(node.get("showSeconds", True)),
                "showDate": bool(node.get("showDate", True)),
                "showWeekday": bool(node.get("showWeekday", True)),
            }
        )
    elif kind == "metric":
        result.update(
            {
                "valueKey": _safe_name(node.get("valueKey", "value"), f"{field}.valueKey", 48),
                "label": _text(node.get("label", "当前值"), f"{field}.label", maximum=24),
                "unit": _text(node.get("unit", ""), f"{field}.unit", minimum=0, maximum=12),
                "precision": int(_number(node.get("precision", 0), f"{field}.precision", 0, 3)),
            }
        )
    elif kind == "progress":
        minimum = _number(node.get("minimum", 0), f"{field}.minimum", -1_000_000, 1_000_000)
        maximum = _number(node.get("maximum", 100), f"{field}.maximum", -1_000_000, 1_000_000)
        if maximum <= minimum:
            raise ContractError(
                "invalid_progress_range",
                "进度组件 maximum 必须大于 minimum。",
                field,
            )
        result.update(
            {
                "valueKey": _safe_name(node.get("valueKey", "value"), f"{field}.valueKey", 48),
                "label": _text(node.get("label", "进度"), f"{field}.label", maximum=24),
                "minimum": minimum,
                "maximum": maximum,
            }
        )
    elif kind == "status":
        result.update(
            {
                "valueKey": _safe_name(node.get("valueKey", "status"), f"{field}.valueKey", 48),
                "label": _text(node.get("label", "状态"), f"{field}.label", maximum=24),
            }
        )
    else:
        value_key = str(node.get("valueKey") or "").strip()
        fixed_text = str(node.get("text") or "").replace("\x00", "").strip()
        if bool(value_key) == bool(fixed_text):
            raise ContractError(
                "invalid_text_component",
                "文本组件必须且只能提供 valueKey 或 text。",
                field,
            )
        role = str(node.get("role", "body")).strip().lower()
        if role not in {"title", "body", "caption"}:
            raise ContractError(
                "invalid_text_role",
                "文本角色只允许 title、body 或 caption。",
                f"{field}.role",
            )
        result["role"] = role
        if value_key:
            result["valueKey"] = _safe_name(value_key, f"{field}.valueKey", 48)
        else:
            result["text"] = _text(fixed_text, f"{field}.text", maximum=80)
    return result


def _validate_presentation(value: Any, field: str) -> dict[str, Any]:
    presentation = dict(_mapping(value, field))
    _reject_unknown(presentation, {"schema", "layout", "accent", "components"}, field)
    if presentation.get("schema") != SURFACE_SCHEMA:
        raise ContractError(
            "unsupported_surface_schema",
            f"界面必须使用 {SURFACE_SCHEMA}。",
            f"{field}.schema",
        )
    layout = str(presentation.get("layout", "hero")).strip().lower()
    if layout not in SURFACE_LAYOUTS:
        raise ContractError("unsupported_surface_layout", "宿主不能渲染这个布局。", f"{field}.layout")
    accent = str(presentation.get("accent", "cyan")).strip().lower()
    if accent not in SURFACE_ACCENTS:
        raise ContractError("unsupported_surface_accent", "界面强调色不受支持。", f"{field}.accent")
    raw_components = presentation.get("components")
    if not isinstance(raw_components, list) or not 1 <= len(raw_components) <= MAX_SURFACE_COMPONENTS:
        raise ContractError(
            "invalid_surface_components",
            f"界面必须包含 1 到 {MAX_SURFACE_COMPONENTS} 个受控组件。",
            f"{field}.components",
        )
    components = [
        _validate_surface_component(item, f"{field}.components[{index}]")
        for index, item in enumerate(raw_components)
    ]
    identifiers = [str(item["id"]) for item in components]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("duplicate_surface_component", "界面组件 id 不能重复。", field)
    if layout == "hero" and components[0]["type"] not in {"clock", "metric", "progress"}:
        raise ContractError(
            "invalid_hero_component",
            "hero 布局的第一个组件必须是时钟、指标或进度。",
            f"{field}.components[0]",
        )
    return {
        "schema": SURFACE_SCHEMA,
        "layout": layout,
        "accent": accent,
        "components": components,
    }


def _validate_source(node: Mapping[str, Any], index: int) -> dict[str, Any]:
    field = f"pipeline[{index}]"
    name = _text(node.get("source"), f"{field}.source", maximum=64)
    allowed = SOURCE_FIELDS.get(name)
    if allowed is None:
        raise ContractError("unknown_declarative_source", "未知数据源。", f"{field}.source")
    _reject_unknown(node, allowed, field)
    result: dict[str, Any] = {"source": name}
    if name == "camera.stream":
        if node.get("privacyIndicator") is not True:
            raise ContractError(
                "privacy_indicator_required",
                "摄像头数据源必须持续显示隐私指示。",
                f"{field}.privacyIndicator",
            )
        result.update(
            {
                "leaseSeconds": round(
                    _number(node.get("leaseSeconds", 30), f"{field}.leaseSeconds", 1, 300),
                    3,
                ),
                "privacyIndicator": True,
            }
        )
    elif name == "microphone.stream":
        if node.get("privacyIndicator") is not True:
            raise ContractError(
                "privacy_indicator_required",
                "麦克风数据源必须持续显示隐私指示。",
                f"{field}.privacyIndicator",
            )
        sample_rate = node.get("sampleRate", 16000)
        if isinstance(sample_rate, bool) or sample_rate not in {16000, 48000}:
            raise ContractError(
                "invalid_microphone_sample_rate",
                "麦克风采样率只允许 16000 或 48000 Hz。",
                f"{field}.sampleRate",
            )
        frame_milliseconds = node.get("frameMilliseconds", 100)
        if (
            isinstance(frame_milliseconds, bool)
            or not isinstance(frame_milliseconds, int)
            or frame_milliseconds < 20
            or frame_milliseconds > 500
        ):
            raise ContractError(
                "invalid_microphone_frame",
                "麦克风分析窗口必须是 20 到 500 毫秒的整数。",
                f"{field}.frameMilliseconds",
            )
        result.update(
            {
                "leaseSeconds": round(
                    _number(
                        node.get("leaseSeconds", 30),
                        f"{field}.leaseSeconds",
                        1,
                        300,
                    ),
                    3,
                ),
                "sampleRate": int(sample_rate),
                "frameMilliseconds": frame_milliseconds,
                "privacyIndicator": True,
            }
        )
    elif name == "timer.interval":
        result.update(
            {
                "seconds": round(
                    _number(node.get("seconds", 60), f"{field}.seconds", 1, 86400),
                    3,
                ),
                "repeat": bool(node.get("repeat", True)),
            }
        )
    return result


def _validate_operator(node: Mapping[str, Any], index: int) -> dict[str, Any]:
    field = f"pipeline[{index}]"
    name = _text(node.get("operator"), f"{field}.operator", maximum=64)
    allowed = OPERATOR_FIELDS.get(name)
    if allowed is None:
        raise ContractError("unknown_declarative_operator", "未知处理节点。", f"{field}.operator")
    _reject_unknown(node, allowed, field)
    result: dict[str, Any] = {"operator": name}
    if name == "vision.detect":
        model = _text(
            node.get("model", "host.default-object-detector"),
            f"{field}.model",
            maximum=80,
        )
        if model != "host.default-object-detector":
            raise ContractError(
                "vision_model_denied",
                "当前只启用宿主默认目标检测模型。",
                f"{field}.model",
            )
        raw_classes = node.get("classes", [])
        if not isinstance(raw_classes, list) or not 1 <= len(raw_classes) <= 12:
            raise ContractError(
                "invalid_vision_classes",
                "视觉类别必须包含 1 到 12 项。",
                f"{field}.classes",
            )
        classes = []
        for class_index, raw_class in enumerate(raw_classes):
            class_name = _safe_name(
                raw_class,
                f"{field}.classes[{class_index}]",
                maximum=40,
            ).lower()
            if class_name not in classes:
                classes.append(class_name)
        result.update(
            {
                "model": model,
                "classes": classes,
                "minimumConfidence": round(
                    _number(
                        node.get("minimumConfidence", 0.55),
                        f"{field}.minimumConfidence",
                        0.1,
                        0.99,
                    ),
                    3,
                ),
                "maximumFps": round(
                    _number(node.get("maximumFps", 6), f"{field}.maximumFps", 0.2, 12),
                    3,
                ),
            }
        )
    elif name == "audio.level":
        result.update(
            {
                "minimumDbfs": round(
                    _number(
                        node.get("minimumDbfs", -38),
                        f"{field}.minimumDbfs",
                        -80,
                        -3,
                    ),
                    2,
                ),
                "holdMilliseconds": round(
                    _number(
                        node.get("holdMilliseconds", 300),
                        f"{field}.holdMilliseconds",
                        0,
                        5000,
                    ),
                    1,
                ),
            }
        )
    elif name == "counter.increment":
        result["key"] = _safe_name(node.get("key", "count"), f"{field}.key")
        when_class = str(node.get("whenClass") or "").strip().lower()
        when_sound = node.get("whenSound", False)
        if not isinstance(when_sound, bool):
            raise ContractError(
                "invalid_sound_condition",
                "whenSound 必须是布尔值。",
                f"{field}.whenSound",
            )
        if when_class and when_sound:
            raise ContractError(
                "ambiguous_counter_condition",
                "计数器不能同时使用视觉类别和声音触发条件。",
                field,
            )
        if when_class:
            result["whenClass"] = _safe_name(when_class, f"{field}.whenClass", 40)
        if when_sound:
            result["whenSound"] = True
    elif name == "text.compose":
        result["template"] = _text(
            node.get("template", "{value}"),
            f"{field}.template",
            maximum=240,
        )
    elif name == "assistant.query":
        result["prompt"] = _text(node.get("prompt"), f"{field}.prompt", maximum=1200)
    return result


def _validate_sink(node: Mapping[str, Any], index: int) -> dict[str, Any]:
    field = f"pipeline[{index}]"
    name = _text(node.get("sink"), f"{field}.sink", maximum=64)
    allowed = SINK_FIELDS.get(name)
    if allowed is None:
        raise ContractError("unknown_declarative_sink", "未知输出节点。", f"{field}.sink")
    _reject_unknown(node, allowed, field)
    result: dict[str, Any] = {"sink": name}
    if name == "ui.present":
        view = _safe_name(node.get("view", "status"), f"{field}.view")
        if view not in SUPPORTED_UI_VIEWS:
            raise ContractError(
                "unsupported_ui_view",
                f"宿主不能渲染视图 {view}，应用不会用通用状态页代替。",
                f"{field}.view",
            )
        result["view"] = view
        if str(node.get("title") or "").strip():
            result["title"] = _text(node["title"], f"{field}.title", maximum=40)
        if "presentation" in node:
            result["presentation"] = _validate_presentation(
                node["presentation"],
                f"{field}.presentation",
            )
        elif view == "adaptive":
            raise ContractError(
                "surface_presentation_required",
                "adaptive 视图必须声明受控界面组件。",
                f"{field}.presentation",
            )
    elif name == "notifications.show":
        result.update(
            {
                "title": _text(node.get("title", "应用提醒"), f"{field}.title", maximum=40),
                "body": _text(node.get("body", "应用有新的状态。"), f"{field}.body", maximum=160),
                "cooldownSeconds": round(
                    _number(
                        node.get("cooldownSeconds", 30),
                        f"{field}.cooldownSeconds",
                        5,
                        86400,
                    ),
                    3,
                ),
            }
        )
    elif name == "storage.put":
        result["path"] = _safe_name(node.get("path", "state.json"), f"{field}.path", 80)
        result["value"] = copy.deepcopy(node.get("value", {"value": "{value}"}))
    elif name == "tasks.create":
        result["prompt"] = _text(node.get("prompt"), f"{field}.prompt", maximum=1200)
        kind = str(node.get("kind", "general")).strip().lower()
        if kind not in {"general", "research", "file"}:
            raise ContractError("invalid_task_kind", "后台任务类型不受支持。", f"{field}.kind")
        result["kind"] = kind
    return result


def validate_declarative_app(value: Mapping[str, Any]) -> dict[str, Any]:
    root = dict(_mapping(value, "app"))
    _reject_unknown(root, {"schema", "title", "pipeline", "safety"}, "app")
    if root.get("schema") != DECLARATIVE_SCHEMA:
        raise ContractError(
            "unsupported_declarative_schema",
            f"只支持 {DECLARATIVE_SCHEMA}。",
            "schema",
        )
    title = _text(root.get("title"), "title", maximum=40)
    raw_pipeline = root.get("pipeline")
    if not isinstance(raw_pipeline, list) or not 2 <= len(raw_pipeline) <= MAX_PIPELINE_NODES:
        raise ContractError(
            "invalid_declarative_pipeline",
            f"pipeline 必须包含 2 到 {MAX_PIPELINE_NODES} 个节点。",
            "pipeline",
        )
    pipeline: list[dict[str, Any]] = []
    source_count = 0
    sink_count = 0
    for index, raw_node in enumerate(raw_pipeline):
        node = dict(_mapping(raw_node, f"pipeline[{index}]"))
        kinds = [key for key in ("source", "operator", "sink") if key in node]
        if len(kinds) != 1:
            raise ContractError(
                "invalid_declarative_node",
                "每个节点必须且只能声明 source、operator 或 sink 之一。",
                f"pipeline[{index}]",
            )
        kind = kinds[0]
        if kind == "source":
            source_count += 1
            if index != 0:
                raise ContractError(
                    "source_must_be_first",
                    "数据源必须是 pipeline 的第一个节点。",
                    f"pipeline[{index}]",
                )
            pipeline.append(_validate_source(node, index))
        elif kind == "operator":
            pipeline.append(_validate_operator(node, index))
        else:
            sink_count += 1
            pipeline.append(_validate_sink(node, index))
    if source_count != 1 or sink_count < 1:
        raise ContractError(
            "invalid_declarative_flow",
            "pipeline 必须恰好有一个数据源且至少有一个输出。",
            "pipeline",
        )
    source_name = str(pipeline[0].get("source") or "")
    clock_surfaces = [
        node
        for node in pipeline
        if node.get("sink") == "ui.present"
        and (
            node.get("view") == "clock"
            or any(
                component.get("type") == "clock"
                for component in node.get("presentation", {}).get("components", [])
                if isinstance(component, Mapping)
            )
        )
    ]
    if clock_surfaces and (
        source_name != "timer.interval" or float(pipeline[0].get("seconds", 86400)) > 60
    ):
        raise ContractError(
            "clock_source_required",
            "时钟界面必须连接刷新间隔不超过 60 秒的 timer.interval。",
            "pipeline",
        )
    has_audio_level = any(
        node.get("operator") == "audio.level" for node in pipeline
    )
    has_sound_counter = any(
        node.get("operator") == "counter.increment" and node.get("whenSound") is True
        for node in pipeline
    )
    if (has_audio_level or has_sound_counter) and source_name != "microphone.stream":
        raise ContractError(
            "microphone_source_required",
            "声音分析节点只能连接受控麦克风数据源。",
            "pipeline",
        )
    if has_sound_counter and not has_audio_level:
        raise ContractError(
            "audio_level_required",
            "声音触发计数器前必须先加入 audio.level 节点。",
            "pipeline",
        )
    safety = dict(_mapping(root.get("safety", {}), "safety"))
    _reject_unknown(
        safety,
        {"runOnlyInForeground", "stopOnUserExit", "retainImages", "retainAudio"},
        "safety",
    )
    if safety.get("runOnlyInForeground", True) is not True:
        raise ContractError(
            "foreground_policy_required",
            "v1 生成应用必须只在前台运行。",
            "safety.runOnlyInForeground",
        )
    normalized_safety = {
        "runOnlyInForeground": True,
        "stopOnUserExit": bool(safety.get("stopOnUserExit", True)),
        "retainImages": bool(safety.get("retainImages", False)),
        "retainAudio": False,
    }
    if safety.get("retainAudio", False) is not False:
        raise ContractError(
            "audio_retention_forbidden",
            "声明式工坊应用不能保存原始麦克风音频。",
            "safety.retainAudio",
        )
    return {
        "schema": DECLARATIVE_SCHEMA,
        "title": title,
        "pipeline": pipeline,
        "safety": normalized_safety,
    }


def required_capabilities(app: Mapping[str, Any]) -> list[str]:
    normalized = validate_declarative_app(app)
    capabilities: set[str] = set()
    for node in normalized["pipeline"]:
        for kind in ("source", "operator", "sink"):
            name = node.get(kind)
            if name:
                capability = NODE_CAPABILITY.get((kind, str(name)))
                if capability:
                    capabilities.add(capability)
    return sorted(capabilities)


def permissions_for_app(app: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized = validate_declarative_app(app)
    capabilities = required_capabilities(normalized)
    permissions: list[dict[str, Any]] = []
    for capability in capabilities:
        constraints: dict[str, Any] = {}
        if capability in {"camera.stream", "microphone.stream"}:
            constraints["privacyIndicator"] = True
        permissions.append(
            {
                "capability": capability,
                "reason": CAPABILITY_REASON[capability],
                "constraints": constraints,
            }
        )
    return permissions
