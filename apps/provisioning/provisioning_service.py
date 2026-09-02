#!/usr/bin/env python3
"""Offline-first phone provisioning for RiverBank devices."""

from __future__ import annotations

import argparse
import html
import json
import os
import pwd
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


DEFAULT_CONFIG = Path("/etc/riverbank/provisioning.json")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ALLOWED_SECRET_KEYS = {
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
}
DEFAULT_HOTSPOT_HOST = ".".join(str(part) for part in (10, 42, 0, 1))


def atomic_write(path: Path, payload: str, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, mode)
    os.chown(temporary, uid, gid)
    os.replace(temporary, path)


def atomic_json(path: Path, payload: dict[str, Any], mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provisioning config must be a JSON object")
    for key in ("user", "home", "runtime_dir", "state_path", "device_config_path"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"config.{key} must be a non-empty string")
    payload.setdefault("port", 19735)
    payload.setdefault("captive_port", 80)
    payload.setdefault("check_interval_seconds", 15)
    payload.setdefault("offline_grace_seconds", 60)
    payload.setdefault("probe_timeout_seconds", 3)
    payload.setdefault("hotspot_prefix", "RiverBank-Setup")
    payload.setdefault("hotspot_connection", "riverbank-provisioning")
    payload.setdefault("probe_urls", ["https://connectivitycheck.gstatic.com/generate_204"])
    payload.setdefault("hermes_env_path", str(Path(payload["home"]) / ".hermes/.env"))
    return payload


def update_env_file(
    path: Path,
    updates: dict[str, str],
    *,
    uid: int,
    gid: int,
) -> None:
    """Atomically update only explicitly allowed Hermes provider variables."""
    filtered = {
        key: value.strip()
        for key, value in updates.items()
        if key in ALLOWED_SECRET_KEYS and ENV_KEY_RE.fullmatch(key) and value.strip()
    }
    if not filtered:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    output: list[str] = []
    consumed: set[str] = set()
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in filtered:
            output.append(f"{key}={filtered[key]}")
            consumed.add(key)
        else:
            output.append(line)
    for key in sorted(filtered):
        if key not in consumed:
            output.append(f"{key}={filtered[key]}")
    atomic_write(path, "\n".join(output).rstrip() + "\n", 0o600, uid, gid)


def wifi_qr_payload(ssid: str, password: str) -> str:
    escape = lambda value: value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace(":", "\\:")
    return f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};H:false;;"


def generate_qr(payload: str, path: Path) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError as exc:
        raise RuntimeError("python3-qrcode is required for provisioning QR output") from exc
    image = qrcode.make(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.png")
    image.save(temporary)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


class NetworkBackend:
    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout

    def run(self, command: list[str], timeout: float | None = None) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        detail = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, detail

    def internet_reachable(self, urls: list[str], timeout: float) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in urls:
            request = urllib.request.Request(url, headers={"User-Agent": "riverbank-provisioning/1"})
            try:
                with opener.open(request, timeout=timeout) as response:
                    if 200 <= int(getattr(response, "status", 200)) < 500:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
        return False

    def default_ipv4(self) -> str:
        ok, output = self.run(["/usr/bin/hostname", "-I"], timeout=2)
        if not ok:
            return ""
        return next(
            (
                value
                for value in output.split()
                if not value.startswith("127.") and ":" not in value and not value.startswith("100.")
            ),
            "",
        )

    def has_default_route(self) -> bool:
        ok, output = self.run(["/usr/sbin/ip", "-4", "route", "show", "default"], timeout=2)
        return ok and bool(output.strip())

    def wifi_device(self) -> str:
        ok, output = self.run(
            ["/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
            timeout=4,
        )
        if not ok:
            return "wlan0"
        for line in output.splitlines():
            device, separator, kind = line.partition(":")
            if separator and kind == "wifi":
                return device
        return "wlan0"

    def start_hotspot(self, connection: str, ssid: str, password: str) -> tuple[bool, str]:
        self.run(["/usr/bin/nmcli", "connection", "delete", connection], timeout=6)
        return self.run(
            [
                "/usr/bin/nmcli",
                "device",
                "wifi",
                "hotspot",
                "ifname",
                self.wifi_device(),
                "con-name",
                connection,
                "ssid",
                ssid,
                "password",
                password,
            ],
            timeout=20,
        )

    def stop_hotspot(self, connection: str) -> None:
        self.run(["/usr/bin/nmcli", "connection", "down", connection], timeout=10)
        self.run(["/usr/bin/nmcli", "connection", "delete", connection], timeout=10)

    def connect_wifi(self, ssid: str, password: str) -> tuple[bool, str]:
        command = ["/usr/bin/nmcli", "device", "wifi", "connect", ssid]
        if password:
            command.extend(["password", password])
        return self.run(command, timeout=35)

    def scan_wifi(self) -> list[dict[str, Any]]:
        ok, output = self.run(
            [
                "/usr/bin/nmcli",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "--rescan",
                "auto",
            ],
            timeout=12,
        )
        if not ok:
            return []
        networks: dict[str, dict[str, Any]] = {}
        for line in output.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) != 3 or not parts[0]:
                continue
            ssid, signal, security = parts
            try:
                strength = max(0, min(int(signal), 100))
            except ValueError:
                strength = 0
            previous = networks.get(ssid)
            if previous is None or strength > int(previous["signal"]):
                networks[ssid] = {
                    "ssid": ssid,
                    "signal": strength,
                    "secure": bool(security and security != "--"),
                }
        return sorted(networks.values(), key=lambda item: int(item["signal"]), reverse=True)[:20]


class ProvisioningService:
    def __init__(self, config: dict[str, Any], backend: NetworkBackend | None = None) -> None:
        self.config = config
        self.backend = backend or NetworkBackend()
        self.runtime_dir = Path(config["runtime_dir"])
        self.state_path = Path(config["state_path"])
        self.qr_path = self.runtime_dir / "setup-qr.png"
        self.device_path = Path(config["device_config_path"])
        self.env_path = Path(config["hermes_env_path"])
        record = pwd.getpwnam(config["user"])
        self.user_uid = record.pw_uid
        self.user_gid = record.pw_gid
        self.token = secrets.token_urlsafe(24)
        self.hotspot_password = secrets.token_urlsafe(12).replace("-", "A").replace("_", "b")[:16]
        suffix = socket.gethostname().replace("riverbank-", "")[-4:].upper()
        self.hotspot_ssid = f"{config['hotspot_prefix']}-{suffix}"
        self.mode = "inactive"
        self.phase = "idle"
        self.message = "网络正常"
        self.offline_since = 0.0
        self.hotspot_active = False
        self.stop_event = threading.Event()
        self.apply_lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.captive_httpd: ThreadingHTTPServer | None = None
        self.visible_networks: list[dict[str, Any]] = []
        self.write_state(False)

    def setup_url(self) -> str:
        host = DEFAULT_HOTSPOT_HOST if self.hotspot_active else self.backend.default_ipv4()
        if not host:
            host = DEFAULT_HOTSPOT_HOST
        return f"http://{host}:{int(self.config['port'])}/?setup={quote(self.token)}"

    def write_state(self, active: bool) -> None:
        payload = {
            "version": 1,
            "active": bool(active),
            "mode": self.mode,
            "phase": self.phase,
            "message": self.message,
            "ssid": self.hotspot_ssid if self.hotspot_active else None,
            "setup_url": self.setup_url() if active else None,
            "qr_path": str(self.qr_path) if active and self.qr_path.exists() else None,
            "updated_at": time.time(),
            "secrets_in_state": False,
        }
        atomic_json(self.state_path, payload, mode=0o644)

    def activate(self, use_hotspot: bool) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.visible_networks = self.backend.scan_wifi()
        if use_hotspot and not self.hotspot_active:
            ok, detail = self.backend.start_hotspot(
                self.config["hotspot_connection"],
                self.hotspot_ssid,
                self.hotspot_password,
            )
            if not ok:
                self.mode = "error"
                self.phase = "failed"
                self.message = f"临时配网热点启动失败：{detail[:120]}"
                self.write_state(True)
                return
            self.hotspot_active = True
            self.start_captive_http()
        self.mode = "hotspot" if self.hotspot_active else "lan"
        self.phase = "waiting"
        if self.hotspot_active and self.captive_httpd is None:
            self.message = f"扫码连接临时网络，再访问 {DEFAULT_HOTSPOT_HOST}:{self.config['port']}"
        else:
            self.message = "扫码连接临时网络后完成设备设置" if self.hotspot_active else "扫码打开设备设置"
        qr_payload = (
            wifi_qr_payload(self.hotspot_ssid, self.hotspot_password)
            if self.hotspot_active
            else self.setup_url()
        )
        try:
            generate_qr(qr_payload, self.qr_path)
        except (OSError, RuntimeError) as exc:
            self.phase = "failed"
            self.message = str(exc)
        self.write_state(True)

    def deactivate(self) -> None:
        if self.hotspot_active:
            self.stop_captive_http()
            self.backend.stop_hotspot(self.config["hotspot_connection"])
            self.hotspot_active = False
        self.mode = "inactive"
        self.phase = "idle"
        self.message = "网络正常"
        self.offline_since = 0.0
        self.write_state(False)

    def authorized(self, value: str) -> bool:
        return bool(value) and secrets.compare_digest(value, self.token)

    def apply_configuration(self, values: dict[str, str]) -> tuple[bool, str]:
        if not self.apply_lock.acquire(blocking=False):
            return False, "另一项配置正在应用"
        try:
            hostname = values.get("device_name", "").strip().lower()
            account = values.get("account", "").strip()
            ssid = values.get("wifi_ssid", "").strip()
            password = values.get("wifi_password", "")
            if not HOSTNAME_RE.fullmatch(hostname):
                return False, "设备名只能使用小写字母、数字和中划线"
            if not (1 <= len(account) <= 64):
                return False, "账号名称长度应为 1–64 个字符"
            if not (1 <= len(ssid.encode("utf-8")) <= 32):
                return False, "Wi-Fi 名称长度不正确"
            if password and not (8 <= len(password) <= 63):
                return False, "Wi-Fi 密码应为 8–63 个字符"
            self.phase = "applying"
            self.message = "正在保存配置并连接 Wi-Fi"
            self.write_state(True)

            metadata = {
                "version": 1,
                "device_name": hostname,
                "owner_account": account,
                "configured_at": time.time(),
            }
            atomic_json(self.device_path, metadata, mode=0o644)
            updates = {
                "DEEPSEEK_API_KEY": values.get("deepseek_key", ""),
                "DASHSCOPE_API_KEY": values.get("qwen_key", ""),
                "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            }
            update_env_file(
                self.env_path,
                updates,
                uid=self.user_uid,
                gid=self.user_gid,
            )
            subprocess.run(
                ["/usr/bin/hostnamectl", "set-hostname", hostname],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if self.hotspot_active:
                self.stop_captive_http()
                self.backend.stop_hotspot(self.config["hotspot_connection"])
                self.hotspot_active = False
            ok, detail = self.backend.connect_wifi(ssid, password)
            if not ok:
                self.phase = "failed"
                self.message = f"Wi-Fi 连接失败：{detail[:120]}"
                self.write_state(True)
                return False, self.message
            subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "restart",
                    "--no-block",
                    "hermes-gateway.service",
                    "hermes-voice.service",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            self.mode = "lan"
            self.phase = "complete"
            self.message = "配置已保存，正在确认网络"
            self.write_state(True)
            self.offline_since = 0.0
            return True, "配置已保存，设备正在连接新网络"
        finally:
            self.apply_lock.release()

    def page(self, message: str = "") -> str:
        notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
        hotspot_hint = (
            f"已连接临时网络 <strong>{html.escape(self.hotspot_ssid)}</strong>"
            if self.hotspot_active
            else "手机需与设备位于同一局域网"
        )
        wifi_options = "".join(
            f'<option value="{html.escape(str(network.get("ssid") or ""), quote=True)}"></option>'
            for network in self.visible_networks
            if network.get("ssid")
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RiverBank 设备设置</title><style>
:root{{--bg:#061119;--card:#0b202b;--line:#1d5264;--text:#e3f5f8;--muted:#8ab6c3;--blue:#35c4e8}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 50% 0,#11303f,var(--bg) 50%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}
main{{width:min(680px,calc(100% - 28px));margin:28px auto 60px}}h1{{font-size:28px;margin:0 0 8px}}p{{color:var(--muted);line-height:1.6}}form{{background:rgba(11,32,43,.94);border:1px solid var(--line);border-radius:24px;padding:22px;box-shadow:0 24px 70px #0008}}
label{{display:block;margin:17px 0 7px;font-size:14px;color:#b8d8df}}input{{width:100%;padding:14px 15px;border-radius:13px;border:1px solid #286175;background:#06141c;color:white;font-size:16px;outline:none}}input:focus{{border-color:var(--blue);box-shadow:0 0 0 3px #35c4e826}}
button{{width:100%;margin-top:24px;padding:15px;border:0;border-radius:14px;background:linear-gradient(135deg,#37d5f1,#2488dc);color:#031117;font-size:17px;font-weight:700}}.notice{{padding:12px 14px;border-radius:12px;background:#123b45;color:#bdf5e1;margin:14px 0}}small{{display:block;margin-top:14px;color:#7099a5;line-height:1.5}}
</style></head><body><main><h1>RiverBank 设备设置</h1><p>{hotspot_hint}。模型密钥不会出现在二维码、页面回显或日志中。</p>{notice}
<form method="post" action="/configure">
<input type="hidden" name="setup" value="{html.escape(self.token)}">
<label>设备名称</label><input name="device_name" value="riverbank-tech" autocomplete="off" required>
<label>账号 / 使用者名称</label><input name="account" value="Geo" autocomplete="name" required>
<label>Wi-Fi 名称</label><input name="wifi_ssid" list="wifi-networks" autocomplete="off" required><datalist id="wifi-networks">{wifi_options}</datalist>
<label>Wi-Fi 密码</label><input type="password" name="wifi_password" autocomplete="new-password">
<label>DeepSeek API Key（留空则保持原值）</label><input type="password" name="deepseek_key" autocomplete="off">
<label>Qwen / DashScope 中国区 Key（留空则保持原值）</label><input type="password" name="qwen_key" autocomplete="off">
<button type="submit">保存并连接</button><small>“账号”是设备所有者标识，不会修改 Linux 用户名。密钥只保存到本机 Hermes 的权限受限配置文件。</small>
</form></main></body></html>"""

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RiverBankProvisioning/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def send_html(self, body: str, status: int = 200) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def token_from_request(self) -> str:
                return parse_qs(urlparse(self.path).query).get("setup", [""])[0]

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"/hotspot-detect.html", "/generate_204", "/ncsi.txt", "/connecttest.txt"}:
                    self.send_response(302)
                    self.send_header("Location", f"/?setup={quote(service.token)}")
                    self.end_headers()
                    return
                if parsed.path == "/api/status":
                    payload = json.dumps(
                        {"active": service.mode != "inactive", "phase": service.phase},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if parsed.path != "/":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                token = self.token_from_request()
                if not service.authorized(token) and not service.hotspot_active:
                    self.send_html("<h1>设置链接无效或已过期</h1>", 403)
                    return
                self.send_html(service.page())

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/configure":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 16384)
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                values = {
                    key: entries[-1]
                    for key, entries in parse_qs(
                        self.rfile.read(length).decode("utf-8"),
                        keep_blank_values=True,
                    ).items()
                }
                if not service.authorized(values.get("setup", "")):
                    self.send_html(service.page("设置会话已过期，请重新扫码"), 403)
                    return
                ok, message = service.apply_configuration(values)
                self.send_html(service.page(message), 200 if ok else 400)

        return Handler

    def start_http(self) -> None:
        self.httpd = ThreadingHTTPServer(("0.0.0.0", int(self.config["port"])), self.make_handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="provisioning-http", daemon=True).start()

    def start_captive_http(self) -> None:
        if self.captive_httpd is not None:
            return
        try:
            self.captive_httpd = ThreadingHTTPServer(
                ("0.0.0.0", int(self.config["captive_port"])),
                self.make_handler(),
            )
        except OSError as exc:
            self.captive_httpd = None
            self.message = f"临时网络已启动，请访问 {DEFAULT_HOTSPOT_HOST}:{self.config['port']}"
            return
        self.captive_httpd.daemon_threads = True
        threading.Thread(
            target=self.captive_httpd.serve_forever,
            name="provisioning-captive-http",
            daemon=True,
        ).start()

    def stop_captive_http(self) -> None:
        server = self.captive_httpd
        self.captive_httpd = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.stop_captive_http()

    def step(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        reachable = self.backend.internet_reachable(
            list(self.config["probe_urls"]),
            float(self.config["probe_timeout_seconds"]),
        )
        if reachable:
            if self.mode != "inactive":
                self.deactivate()
            self.offline_since = 0.0
            return
        if self.offline_since <= 0:
            self.offline_since = current
            return
        if current - self.offline_since < float(self.config["offline_grace_seconds"]):
            return
        if self.mode == "inactive":
            self.activate(use_hotspot=not self.backend.has_default_route())

    def run(self) -> None:
        self.start_http()
        interval = max(5.0, float(self.config["check_interval_seconds"]))
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as exc:
                self.mode = "error"
                self.phase = "failed"
                self.message = f"离线设置服务异常：{type(exc).__name__}"
                self.write_state(True)
            self.stop_event.wait(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--validate-config", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        config = load_config(arguments.config)
        pwd.getpwnam(config["user"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"configuration error: {exc}")
        return 2
    if arguments.validate_config:
        print("configuration valid")
        return 0
    service = ProvisioningService(config)
    signal.signal(signal.SIGTERM, service.request_stop)
    signal.signal(signal.SIGINT, service.request_stop)
    service.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
