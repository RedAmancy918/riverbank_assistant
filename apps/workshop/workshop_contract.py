#!/usr/bin/env python3
"""RiverBank Workshop manifest, capability, and host-message contract.

The module deliberately has no third-party dependencies so the exact same
validation can run in the Workshop UI, import service, health monitor, and CI.
It validates declarations and authorizes individual host API requests; it
never grants a permission by itself.
"""

from __future__ import annotations

import copy
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit


MANIFEST_API_VERSION = "riverbank.workshop/v1"
MANIFEST_KIND = "RiverBankApp"
HOST_PROTOCOL_VERSION = "riverbank.app-host/v1"
MANIFEST_MAX_BYTES = 64 * 1024
PACKAGE_MAX_BYTES = 64 * 1024 * 1024
PACKAGE_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
PACKAGE_MAX_FILES = 512

APP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class ContractError(ValueError):
    """Stable validation/authorization error suitable for protocol responses."""

    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    risk: str
    grant_scope: str
    methods: tuple[str, ...]
    description: str


def _capability(
    name: str,
    risk: str,
    grant_scope: str,
    methods: tuple[str, ...],
    description: str,
) -> CapabilitySpec:
    return CapabilitySpec(name, risk, grant_scope, methods, description)


CAPABILITY_CATALOG: dict[str, CapabilitySpec] = {
    item.name: item
    for item in (
        _capability(
            "ui.surface",
            "low",
            "install",
            ("ui.present", "ui.dismiss"),
            "在圆屏受控画布中呈现应用页面。",
        ),
        _capability(
            "storage.app",
            "low",
            "install",
            ("storage.get", "storage.put", "storage.list", "storage.delete"),
            "访问宿主分配给该应用的私有数据目录。",
        ),
        _capability(
            "events.subscribe",
            "low",
            "install",
            ("events.subscribe", "events.unsubscribe"),
            "订阅经过脱敏的宿主事件。",
        ),
        _capability(
            "notifications.local",
            "medium",
            "install",
            ("notifications.show",),
            "在设备本地显示限频通知。",
        ),
        _capability(
            "camera.snapshot",
            "medium",
            "session",
            ("camera.snapshot",),
            "通过 Camera Hub 获取单帧图像，并触发隐私指示。",
        ),
        _capability(
            "camera.stream",
            "high",
            "session",
            ("camera.stream.open", "camera.stream.close"),
            "读取受租约约束的摄像头帧流，并持续显示隐私指示。",
        ),
        _capability(
            "microphone.stream",
            "high",
            "session",
            ("microphone.stream.open", "microphone.stream.close"),
            "读取受控麦克风流，并持续显示录音状态。",
        ),
        _capability(
            "speaker.playback",
            "medium",
            "install",
            ("speaker.play", "speaker.stop"),
            "经 PipeWire 宿主代理播放音频。",
        ),
        _capability(
            "vision.inference",
            "medium",
            "session",
            ("vision.detect",),
            "通过 Hailo/视觉代理执行白名单模型推理。",
        ),
        _capability(
            "motor.pan_tilt",
            "high",
            "session",
            ("motor.move", "motor.stop"),
            "在限速、限角和急停约束下控制云台。",
        ),
        _capability(
            "assistant.query",
            "high",
            "session",
            ("assistant.query",),
            "向 Daily 助手提交受预算和工具白名单限制的请求。",
        ),
        _capability(
            "tasks.submit",
            "medium",
            "install",
            ("tasks.create", "tasks.status"),
            "创建或读取 RiverBank 持久化后台任务。",
        ),
        _capability(
            "reports.read",
            "medium",
            "install",
            ("reports.list", "reports.read"),
            "只读访问报告库中允许暴露的 Markdown。",
        ),
        _capability(
            "network.outbound",
            "high",
            "install",
            ("network.fetch",),
            "经宿主代理访问清单和授权共同允许的 HTTPS 域名。",
        ),
        _capability(
            "lifecycle.autostart",
            "high",
            "install",
            (),
            "允许应用在宿主启动后进入受限运行态。",
        ),
    )
}

METHOD_CAPABILITY = {
    method: capability.name
    for capability in CAPABILITY_CATALOG.values()
    for method in capability.methods
}

FORBIDDEN_CAPABILITIES = frozenset(
    {
        "credentials.read",
        "credentials.write",
        "filesystem.host",
        "kernel.configure",
        "network.raw",
        "packages.manage",
        "process.spawn",
        "service.manage",
        "shell.execute",
        "system.power",
    }
)
FORBIDDEN_METHOD_PREFIXES = (
    "credentials.",
    "filesystem.host.",
    "kernel.",
    "packages.",
    "process.",
    "service.",
    "shell.",
    "system.",
)
RUNTIME_KINDS = frozenset({"declarative-v1", "python-sandbox-v1"})


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    code: str
    reason: str
    capability: str | None = None
    sanitized_params: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capability_catalog() -> list[dict[str, Any]]:
    return [asdict(CAPABILITY_CATALOG[name]) for name in sorted(CAPABILITY_CATALOG)]


def safe_package_path(value: str, *, field: str = "path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("invalid_path", "路径不能为空。", field)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("unsafe_path", "路径必须是包内相对路径且不能包含 ..。", field)
    normalized = str(path)
    if normalized.startswith("/"):
        raise ContractError("unsafe_path", "路径不能指向宿主绝对位置。", field)
    return normalized


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("invalid_type", "字段必须是对象。", field)
    return value


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(
            "unknown_field",
            f"不支持字段：{', '.join(unknown[:5])}。",
            field,
        )


def _require_text(
    value: Any,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 200,
) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_type", "字段必须是字符串。", field)
    text = value.strip()
    if len(text) < minimum or len(text) > maximum:
        raise ContractError(
            "invalid_length",
            f"长度必须在 {minimum} 到 {maximum} 个字符之间。",
            field,
        )
    return text


def _bounded_integer(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("invalid_type", "字段必须是整数。", field)
    if value < minimum or value > maximum:
        raise ContractError(
            "out_of_range",
            f"值必须在 {minimum} 到 {maximum} 之间。",
            field,
        )
    return value


def _validate_domains(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError(
            "missing_domain_allowlist",
            "联网能力必须声明至少一个确切域名。",
            field,
        )
    domains: list[str] = []
    for index, raw in enumerate(value):
        domain = _require_text(raw, f"{field}[{index}]", maximum=253).lower().rstrip(".")
        if "*" in domain or not DOMAIN_RE.fullmatch(domain):
            raise ContractError(
                "invalid_domain",
                "域名必须是确切 DNS 名称，不能使用通配符、IP 或 URL。",
                f"{field}[{index}]",
            )
        if domain not in domains:
            domains.append(domain)
    return domains


def _validate_range(value: Any, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ContractError("invalid_range", "角度范围必须是 [最小值, 最大值]。", field)
    values: list[float] = []
    for index, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ContractError("invalid_number", "角度必须是有限数字。", f"{field}[{index}]")
        values.append(float(raw))
    if values[0] >= values[1] or values[0] < -180 or values[1] > 180:
        raise ContractError("invalid_range", "角度范围必须递增且位于 -180° 到 180°。", field)
    return values


def _validate_permission_constraints(
    capability: str,
    raw_constraints: Any,
    field: str,
) -> dict[str, Any]:
    constraints = dict(_require_mapping(raw_constraints or {}, field))
    allowed_fields = {
        "network.outbound": {"domains", "methods"},
        "camera.stream": {"privacyIndicator"},
        "microphone.stream": {"privacyIndicator"},
        "motor.pan_tilt": {
            "panDegrees",
            "tiltDegrees",
            "maxDegreesPerSecond",
            "emergencyStop",
        },
        "assistant.query": {"maxRequestsPerMinute"},
        "tasks.submit": {"maxConcurrent"},
        "speaker.playback": {"maxVolumePercent"},
    }.get(capability, set())
    _reject_unknown(constraints, allowed_fields, field)
    if capability == "network.outbound":
        constraints["domains"] = _validate_domains(
            constraints.get("domains"), f"{field}.domains"
        )
        constraints["methods"] = [
            method
            for method in constraints.get("methods", ["GET"])
            if method in {"GET", "HEAD", "POST"}
        ]
        if not constraints["methods"]:
            raise ContractError(
                "invalid_network_methods",
                "网络方法至少要包含 GET、HEAD 或 POST 之一。",
                f"{field}.methods",
            )
    elif capability in {"camera.stream", "microphone.stream"}:
        if constraints.get("privacyIndicator") is not True:
            raise ContractError(
                "privacy_indicator_required",
                "连续摄像头或麦克风能力必须启用宿主隐私指示。",
                f"{field}.privacyIndicator",
            )
    elif capability == "motor.pan_tilt":
        constraints["panDegrees"] = _validate_range(
            constraints.get("panDegrees"), f"{field}.panDegrees"
        )
        constraints["tiltDegrees"] = _validate_range(
            constraints.get("tiltDegrees"), f"{field}.tiltDegrees"
        )
        speed = constraints.get("maxDegreesPerSecond")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise ContractError(
                "invalid_motor_speed",
                "云台能力必须声明最大角速度。",
                f"{field}.maxDegreesPerSecond",
            )
        speed = float(speed)
        if not math.isfinite(speed) or speed < 1 or speed > 120:
            raise ContractError(
                "invalid_motor_speed",
                "最大角速度必须在 1 到 120 度每秒之间。",
                f"{field}.maxDegreesPerSecond",
            )
        constraints["maxDegreesPerSecond"] = speed
        if constraints.get("emergencyStop") is not True:
            raise ContractError(
                "emergency_stop_required",
                "云台能力必须接受宿主急停。",
                f"{field}.emergencyStop",
            )
    elif capability == "assistant.query":
        constraints["maxRequestsPerMinute"] = _bounded_integer(
            constraints.get("maxRequestsPerMinute", 6),
            f"{field}.maxRequestsPerMinute",
            1,
            30,
        )
    elif capability == "tasks.submit":
        constraints["maxConcurrent"] = _bounded_integer(
            constraints.get("maxConcurrent", 1),
            f"{field}.maxConcurrent",
            1,
            4,
        )
    elif capability == "speaker.playback":
        constraints["maxVolumePercent"] = _bounded_integer(
            constraints.get("maxVolumePercent", 80),
            f"{field}.maxVolumePercent",
            0,
            100,
        )
    return constraints


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    package_files: set[str] | None = None,
) -> dict[str, Any]:
    """Validate and return a normalized deep copy of a Workshop manifest."""

    root = _require_mapping(manifest, "manifest")
    _reject_unknown(root, {"apiVersion", "kind", "metadata", "spec"}, "manifest")
    try:
        encoded = json.dumps(root, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_json", f"清单包含不可序列化内容：{exc}", "manifest") from exc
    if len(encoded) > MANIFEST_MAX_BYTES:
        raise ContractError("manifest_too_large", "清单超过 64 KiB。", "manifest")
    if root.get("apiVersion") != MANIFEST_API_VERSION:
        raise ContractError(
            "unsupported_manifest_version",
            f"只支持 {MANIFEST_API_VERSION}。",
            "apiVersion",
        )
    if root.get("kind") != MANIFEST_KIND:
        raise ContractError("invalid_kind", f"kind 必须是 {MANIFEST_KIND}。", "kind")

    metadata = dict(_require_mapping(root.get("metadata"), "metadata"))
    _reject_unknown(
        metadata,
        {"id", "name", "version", "description", "vendor"},
        "metadata",
    )
    app_id = _require_text(metadata.get("id"), "metadata.id", maximum=80).lower()
    if not APP_ID_RE.fullmatch(app_id):
        raise ContractError(
            "invalid_app_id",
            "应用 ID 必须是小写反向域名形式，例如 tech.riverbank.catwatcher。",
            "metadata.id",
        )
    version = _require_text(metadata.get("version"), "metadata.version", maximum=64)
    if not SEMVER_RE.fullmatch(version):
        raise ContractError("invalid_version", "版本必须使用 SemVer。", "metadata.version")
    metadata["id"] = app_id
    metadata["version"] = version
    metadata["name"] = _require_text(metadata.get("name"), "metadata.name", maximum=40)
    metadata["description"] = _require_text(
        metadata.get("description"),
        "metadata.description",
        minimum=0,
        maximum=240,
    )
    metadata["vendor"] = _require_text(
        metadata.get("vendor", "local"), "metadata.vendor", maximum=80
    )

    spec = dict(_require_mapping(root.get("spec"), "spec"))
    _reject_unknown(
        spec,
        {"runtime", "permissions", "resources", "lifecycle", "ui"},
        "spec",
    )
    runtime = dict(_require_mapping(spec.get("runtime"), "spec.runtime"))
    runtime_kind = _require_text(runtime.get("kind"), "spec.runtime.kind", maximum=40)
    if runtime_kind not in RUNTIME_KINDS:
        raise ContractError(
            "unsupported_runtime",
            "只允许 declarative-v1 或 python-sandbox-v1。",
            "spec.runtime.kind",
        )
    if "command" in runtime or "arguments" in runtime:
        raise ContractError(
            "direct_process_forbidden",
            "应用不能声明宿主命令或参数，只能声明受控入口文件。",
            "spec.runtime",
        )
    _reject_unknown(
        runtime,
        {"kind", "entrypoint", "protocol"},
        "spec.runtime",
    )
    entrypoint = safe_package_path(
        _require_text(runtime.get("entrypoint"), "spec.runtime.entrypoint", maximum=180),
        field="spec.runtime.entrypoint",
    )
    if not entrypoint.startswith("app/"):
        raise ContractError(
            "invalid_entrypoint",
            "入口文件必须位于 app/ 目录。",
            "spec.runtime.entrypoint",
        )
    expected_suffix = ".json" if runtime_kind == "declarative-v1" else ".py"
    if not entrypoint.endswith(expected_suffix):
        raise ContractError(
            "invalid_entrypoint",
            f"{runtime_kind} 入口必须以 {expected_suffix} 结尾。",
            "spec.runtime.entrypoint",
        )
    if package_files is not None and entrypoint not in package_files:
        raise ContractError(
            "missing_entrypoint",
            "应用包中找不到清单声明的入口文件。",
            "spec.runtime.entrypoint",
        )
    runtime["kind"] = runtime_kind
    runtime["entrypoint"] = entrypoint
    runtime["protocol"] = _require_text(
        runtime.get("protocol", HOST_PROTOCOL_VERSION),
        "spec.runtime.protocol",
        maximum=64,
    )
    if runtime["protocol"] != HOST_PROTOCOL_VERSION:
        raise ContractError(
            "unsupported_host_protocol",
            f"只支持 {HOST_PROTOCOL_VERSION}。",
            "spec.runtime.protocol",
        )

    raw_permissions = spec.get("permissions")
    if not isinstance(raw_permissions, list):
        raise ContractError("invalid_type", "permissions 必须是数组。", "spec.permissions")
    permissions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_permissions):
        field = f"spec.permissions[{index}]"
        item = dict(_require_mapping(raw, field))
        _reject_unknown(item, {"capability", "reason", "constraints"}, field)
        capability = _require_text(item.get("capability"), f"{field}.capability", maximum=80)
        if capability in FORBIDDEN_CAPABILITIES:
            raise ContractError(
                "forbidden_capability",
                "该能力永远不会向工坊应用开放。",
                f"{field}.capability",
            )
        if capability not in CAPABILITY_CATALOG:
            raise ContractError(
                "unknown_capability",
                "未知能力不能安装。",
                f"{field}.capability",
            )
        if capability in seen:
            raise ContractError(
                "duplicate_capability",
                "同一能力只能声明一次。",
                f"{field}.capability",
            )
        seen.add(capability)
        reason = _require_text(item.get("reason"), f"{field}.reason", maximum=160)
        permissions.append(
            {
                "capability": capability,
                "reason": reason,
                "constraints": _validate_permission_constraints(
                    capability,
                    item.get("constraints", {}),
                    f"{field}.constraints",
                ),
            }
        )

    lifecycle = dict(_require_mapping(spec.get("lifecycle", {}), "spec.lifecycle"))
    _reject_unknown(
        lifecycle,
        {"autostart", "startTimeoutSeconds", "stopTimeoutSeconds"},
        "spec.lifecycle",
    )
    autostart = bool(lifecycle.get("autostart", False))
    if autostart and "lifecycle.autostart" not in seen:
        raise ContractError(
            "autostart_permission_required",
            "自动启动必须显式申请 lifecycle.autostart。",
            "spec.lifecycle.autostart",
        )
    lifecycle = {
        "autostart": autostart,
        "startTimeoutSeconds": _bounded_integer(
            lifecycle.get("startTimeoutSeconds", 10),
            "spec.lifecycle.startTimeoutSeconds",
            1,
            30,
        ),
        "stopTimeoutSeconds": _bounded_integer(
            lifecycle.get("stopTimeoutSeconds", 5),
            "spec.lifecycle.stopTimeoutSeconds",
            1,
            15,
        ),
    }

    resources = dict(_require_mapping(spec.get("resources", {}), "spec.resources"))
    _reject_unknown(
        resources,
        {"cpuPercent", "memoryMB", "storageMB", "maxProcesses"},
        "spec.resources",
    )
    resources = {
        "cpuPercent": _bounded_integer(
            resources.get("cpuPercent", 10), "spec.resources.cpuPercent", 1, 50
        ),
        "memoryMB": _bounded_integer(
            resources.get("memoryMB", 128), "spec.resources.memoryMB", 16, 512
        ),
        "storageMB": _bounded_integer(
            resources.get("storageMB", 128), "spec.resources.storageMB", 1, 2048
        ),
        "maxProcesses": _bounded_integer(
            resources.get("maxProcesses", 2), "spec.resources.maxProcesses", 1, 8
        ),
    }

    ui = dict(_require_mapping(spec.get("ui", {}), "spec.ui"))
    _reject_unknown(ui, {"menuLabel", "glyph"}, "spec.ui")
    ui = {
        "menuLabel": _require_text(
            ui.get("menuLabel", metadata["name"]), "spec.ui.menuLabel", maximum=6
        ),
        "glyph": _require_text(ui.get("glyph", "应"), "spec.ui.glyph", maximum=2),
    }

    normalized = copy.deepcopy(dict(root))
    normalized["metadata"] = metadata
    normalized["spec"] = {
        "runtime": runtime,
        "permissions": permissions,
        "lifecycle": lifecycle,
        "resources": resources,
        "ui": ui,
    }
    return normalized


def declared_permissions(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    normalized = validate_manifest(manifest)
    return {
        item["capability"]: item
        for item in normalized["spec"]["permissions"]
    }


def validate_host_request(request: Mapping[str, Any]) -> dict[str, Any]:
    message = dict(_require_mapping(request, "request"))
    if message.get("protocol") != HOST_PROTOCOL_VERSION:
        raise ContractError(
            "unsupported_protocol",
            f"只支持 {HOST_PROTOCOL_VERSION}。",
            "protocol",
        )
    if message.get("type") != "request":
        raise ContractError("invalid_message_type", "type 必须是 request。", "type")
    request_id = _require_text(message.get("id"), "id", maximum=128)
    method = _require_text(message.get("method"), "method", maximum=96)
    if any(method.startswith(prefix) for prefix in FORBIDDEN_METHOD_PREFIXES):
        raise ContractError("forbidden_method", "该宿主方法永远不会开放。", "method")
    if method not in METHOD_CAPABILITY:
        raise ContractError("unknown_method", "未知宿主方法。", "method")
    params = dict(_require_mapping(message.get("params", {}), "params"))
    if "app_id" in params or "identity" in params:
        raise ContractError(
            "identity_spoofing",
            "应用身份由 Unix Socket 凭据绑定，不能由请求参数指定。",
            "params",
        )
    return {
        "protocol": HOST_PROTOCOL_VERSION,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }


def _grant_enabled(grants: Mapping[str, Any], capability: str) -> bool:
    raw = grants.get(capability)
    if raw is True:
        return True
    return isinstance(raw, Mapping) and raw.get("granted") is True


def _permission_constraints(
    permissions: Mapping[str, Mapping[str, Any]], capability: str
) -> dict[str, Any]:
    permission = permissions.get(capability, {})
    raw = permission.get("constraints", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _authorize_network(
    params: dict[str, Any], constraints: Mapping[str, Any]
) -> dict[str, Any]:
    url = _require_text(params.get("url"), "params.url", maximum=2048)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ContractError(
            "network_target_denied",
            "联网代理只接受不含凭据的 HTTPS URL。",
            "params.url",
        )
    hostname = parsed.hostname.lower().rstrip(".")
    allowed = [str(value).lower().rstrip(".") for value in constraints.get("domains", [])]
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed):
        raise ContractError(
            "network_domain_denied",
            "目标域名不在应用清单允许范围内。",
            "params.url",
        )
    method = str(params.get("httpMethod", "GET")).upper()
    if method not in constraints.get("methods", ["GET"]):
        raise ContractError(
            "network_method_denied",
            "HTTP 方法不在清单允许范围内。",
            "params.httpMethod",
        )
    sanitized = dict(params)
    sanitized["url"] = url
    sanitized["httpMethod"] = method
    return sanitized


def _authorize_motor(
    params: dict[str, Any], constraints: Mapping[str, Any]
) -> dict[str, Any]:
    sanitized = dict(params)
    for axis, field in (("pan", "panDegrees"), ("tilt", "tiltDegrees")):
        value = params.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError("invalid_motor_target", "云台目标必须是有限数字。", f"params.{axis}")
        lower, upper = constraints[field]
        if float(value) < lower or float(value) > upper:
            raise ContractError("motor_limit_exceeded", "云台目标超过清单限角。", f"params.{axis}")
        sanitized[axis] = float(value)
    speed = params.get("degreesPerSecond", constraints["maxDegreesPerSecond"])
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ContractError("invalid_motor_speed", "云台速度必须是数字。", "params.degreesPerSecond")
    speed = float(speed)
    if speed <= 0 or speed > float(constraints["maxDegreesPerSecond"]):
        raise ContractError("motor_speed_exceeded", "云台速度超过清单上限。", "params.degreesPerSecond")
    sanitized["degreesPerSecond"] = speed
    return sanitized


def authorize_request(
    manifest: Mapping[str, Any],
    grants: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    session: Mapping[str, Any],
) -> AuthorizationDecision:
    """Authorize one host request against declaration, grant, and session state."""

    try:
        normalized = validate_manifest(manifest)
        message = validate_host_request(request)
        expected_app_id = normalized["metadata"]["id"]
        if session.get("app_id") != expected_app_id:
            raise ContractError(
                "identity_mismatch",
                "运行时身份与应用清单不匹配。",
                "session.app_id",
            )
        capability = METHOD_CAPABILITY[message["method"]]
        permissions = {
            item["capability"]: item
            for item in normalized["spec"]["permissions"]
        }
        if capability not in permissions:
            raise ContractError(
                "capability_not_declared",
                "应用清单没有声明该能力。",
                "method",
            )
        if not _grant_enabled(grants, capability):
            raise ContractError(
                "capability_not_granted",
                "用户或设备策略尚未授予该能力。",
                "method",
            )
        spec = CAPABILITY_CATALOG[capability]
        if spec.grant_scope == "session" and not bool(session.get("foreground")):
            raise ContractError(
                "foreground_required",
                "该能力只能由前台可见应用使用。",
                "session.foreground",
            )
        if capability in {
            "camera.snapshot",
            "camera.stream",
            "microphone.stream",
            "motor.pan_tilt",
            "assistant.query",
        } and not bool(session.get("user_present")):
            raise ContractError(
                "user_presence_required",
                "该能力需要近期用户交互。",
                "session.user_present",
            )
        params = dict(message["params"])
        constraints = _permission_constraints(permissions, capability)
        if capability == "network.outbound":
            params = _authorize_network(params, constraints)
        elif capability == "storage.app":
            params["path"] = safe_package_path(
                _require_text(params.get("path"), "params.path", maximum=240),
                field="params.path",
            )
        elif capability == "motor.pan_tilt" and message["method"] == "motor.move":
            params = _authorize_motor(params, constraints)
        elif capability == "speaker.playback" and "volumePercent" in params:
            volume = _bounded_integer(
                params["volumePercent"], "params.volumePercent", 0, 100
            )
            if volume > int(constraints.get("maxVolumePercent", 80)):
                raise ContractError(
                    "volume_limit_exceeded",
                    "播放音量超过清单上限。",
                    "params.volumePercent",
                )
            params["volumePercent"] = volume
        return AuthorizationDecision(
            True,
            "ok",
            "authorized",
            capability=capability,
            sanitized_params=params,
        )
    except ContractError as exc:
        capability = None
        method = request.get("method") if isinstance(request, Mapping) else None
        if isinstance(method, str):
            capability = METHOD_CAPABILITY.get(method)
        return AuthorizationDecision(
            False,
            exc.code,
            exc.message,
            capability=capability,
            sanitized_params=None,
        )


def response_message(
    request_id: str,
    *,
    result: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(result is not None) == bool(error is not None):
        raise ValueError("exactly one of result or error is required")
    payload: dict[str, Any] = {
        "protocol": HOST_PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
    }
    if result is not None:
        payload["result"] = dict(result)
    else:
        payload["error"] = dict(error or {})
    return payload


def event_message(name: str, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": HOST_PROTOCOL_VERSION,
        "type": "event",
        "id": str(uuid.uuid4()),
        "event": _require_text(name, "event", maximum=96),
        "data": dict(data),
    }
