#!/usr/bin/env python3
"""Trusted Host Broker for declarative Workshop applications."""

from __future__ import annotations

import array
import json
import math
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from workshop_contract import authorize_request, response_message
from workshop_manager import WorkshopManager, atomic_write_json
from workshop_store import WorkshopStore


EXPRESSION_SOCKET = Path("/run/riverbank-expression/control.sock")
FACE_TRACKER_SOCKET = Path("/run/riverbank-face-tracker/control.sock")
RIVERBANK_HOME = Path(os.environ.get("RIVERBANK_HOME", Path.home()))


def send_expression(payload: dict[str, Any]) -> None:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                str(EXPRESSION_SOCKET),
            )
        finally:
            client.close()
    except OSError:
        pass


class PipeWireMicrophoneCapture:
    """Read one shared PipeWire source without opening ALSA or persisting PCM."""

    def __init__(
        self,
        source: str,
        sample_rate: int,
        *,
        command: Path = Path("/usr/bin/pw-cat"),
    ) -> None:
        self.source = str(source).strip()
        self.sample_rate = int(sample_rate)
        self.command = Path(command)
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if not self.command.is_file():
                raise RuntimeError("PipeWire 录音工具未安装")
            command = [str(self.command), "--record"]
            if self.source:
                command.extend(("--target", self.source))
            command.extend(
                (
                    "--format",
                    "s16",
                    "--rate",
                    str(self.sample_rate),
                    "--channels",
                    "1",
                    "--channel-map",
                    "mono",
                    "--latency",
                    "20ms",
                    "--media-role",
                    "Communication",
                    "-",
                )
            )
            self.buffer.clear()
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=True,
            )
            if self.process.stdout is None:
                self.close()
                raise RuntimeError("PipeWire 麦克风输出不可用")
            os.set_blocking(self.process.stdout.fileno(), False)

    def read_level(self, frame_milliseconds: int) -> dict[str, Any]:
        frame_milliseconds = max(20, min(int(frame_milliseconds), 500))
        expected = max(
            2,
            round(self.sample_rate * frame_milliseconds / 1000.0) * 2,
        )
        deadline = time.monotonic() + max(1.0, frame_milliseconds / 1000.0 * 4.0)
        with self.lock:
            process = self.process
            if process is None or process.stdout is None:
                raise RuntimeError("PipeWire 麦克风尚未启动")
            file_descriptor = process.stdout.fileno()
            while len(self.buffer) < expected:
                if process.poll() is not None:
                    raise RuntimeError("PipeWire 麦克风流意外结束")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("PipeWire 麦克风读取超时")
                readable, _, _ = select.select(
                    [file_descriptor], [], [], min(remaining, 0.25)
                )
                if not readable:
                    continue
                payload = os.read(file_descriptor, max(4096, expected - len(self.buffer)))
                if not payload:
                    raise RuntimeError("PipeWire 麦克风流已经关闭")
                self.buffer.extend(payload)
            payload = bytes(self.buffer[:expected])
            del self.buffer[:expected]

        return self._level_metrics(payload, self.sample_rate, frame_milliseconds)

    @staticmethod
    def _level_metrics(
        payload: bytes,
        sample_rate: int,
        frame_milliseconds: int,
    ) -> dict[str, Any]:
        samples = array.array("h")
        samples.frombytes(payload)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return {
                "rms": 0.0,
                "peak": 0.0,
                "dbfs": -96.0,
                "sampleRate": sample_rate,
                "frameMilliseconds": frame_milliseconds,
                "samplesAnalyzed": 0,
            }
        square_sum = sum(float(sample) * float(sample) for sample in samples)
        rms = math.sqrt(square_sum / len(samples))
        peak = max(abs(int(sample)) for sample in samples)
        dbfs = max(-96.0, 20.0 * math.log10(max(rms, 1.0) / 32768.0))
        return {
            "rms": round(rms / 32768.0, 6),
            "peak": round(min(1.0, peak / 32768.0), 6),
            "dbfs": round(dbfs, 2),
            "sampleRate": sample_rate,
            "frameMilliseconds": frame_milliseconds,
            "samplesAnalyzed": len(samples),
        }

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            self.buffer.clear()
        if process is None:
            return
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)


class HailoResourceCoordinator:
    """Reserve the single Hailo device through the trusted face broker."""

    def __init__(self, socket_path: Path = FACE_TRACKER_SOCKET) -> None:
        self.socket_path = Path(socket_path)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3.0)
        try:
            client.connect(str(self.socket_path))
            client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = client.recv(65535)
                if not chunk:
                    break
                total += len(chunk)
                if total > 512 * 1024:
                    raise RuntimeError("Hailo 资源协调响应过大")
                chunks.append(chunk)
        finally:
            client.close()
        result = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("Hailo 资源协调响应无效")
        return result

    def reserve(self, source: str, ttl_seconds: float = 8.0) -> str:
        result = self.request(
            {
                "command": "reserve_external",
                "source": source,
                "ttl_seconds": ttl_seconds,
            }
        )
        reservation = result.get("reservation")
        if not result.get("ok") or not isinstance(reservation, dict):
            raise RuntimeError(str(result.get("error") or "Hailo 设备当前正忙"))
        reservation_id = str(reservation.get("lease_id") or "")
        if not reservation_id:
            raise RuntimeError("Hailo 资源协调器没有返回预约标识")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            state = self.request({"command": "status"})
            inference = state.get("inference") if isinstance(state, dict) else None
            if (
                isinstance(inference, dict)
                and not inference.get("active")
                and inference.get("pipeline_state") == "unloaded"
            ):
                return reservation_id
            time.sleep(0.1)
        self.release(reservation_id)
        raise RuntimeError("等待人脸管线释放 Hailo 设备超时")

    def renew(self, reservation_id: str, ttl_seconds: float = 8.0) -> None:
        result = self.request(
            {
                "command": "renew_external",
                "reservation_id": reservation_id,
                "ttl_seconds": ttl_seconds,
            }
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Hailo 预约续期失败"))

    def release(self, reservation_id: str) -> None:
        try:
            self.request(
                {
                    "command": "release_external",
                    "reservation_id": reservation_id,
                }
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            pass


class HailoObjectDetector:
    """Lease one on-demand YOLO pipeline and expose only filtered detections."""

    def __init__(
        self,
        *,
        python: Path | None = None,
        script: Path | None = None,
        model: Path | None = None,
        output_dir: Path | None = None,
        camera_stream: str | None = None,
    ) -> None:
        self.python = Path(
            python
            or os.environ.get(
                "RIVERBANK_HAILO_YOLO_PYTHON",
                "/mnt/nvme64/ai/apps/hailo-rpi5-examples/venv_hailo_rpi_examples/bin/python",
            )
        )
        self.script = Path(
            script
            or os.environ.get(
                "RIVERBANK_HAILO_YOLO_SCRIPT",
                "/mnt/nvme64/ai/apps/hailo-rpi5-examples/hailo_yolo_camera.py",
            )
        )
        self.model = Path(
            model
            or os.environ.get(
                "RIVERBANK_HAILO_YOLO_MODEL",
                "/mnt/nvme64/ai/models/yolo/yolov11x_hailo8_v2.14.hef",
            )
        )
        self.output_dir = Path(
            output_dir
            or os.environ.get(
                "RIVERBANK_HAILO_YOLO_OUTPUT",
                "/mnt/nvme64/ai/output/workshop-yolo",
            )
        )
        self.camera_stream = camera_stream or os.environ.get(
            "RIVERBANK_CAMERA_STREAM_URL",
            "http://127.0.0.1:19733/stream",
        )
        self.process: subprocess.Popen[str] | None = None
        self.process_group_id: int | None = None
        self.lock = threading.Lock()

    @property
    def result_path(self) -> Path:
        return self.output_dir / "latest.json"

    def start(self, maximum_fps: float = 8.0) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if self.process_group_id is not None:
                self._terminate_group(self.process_group_id, self.process)
                self.process = None
                self.process_group_id = None
            if (
                not self.python.is_file()
                or not self.script.is_file()
                or not self.model.is_file()
            ):
                raise RuntimeError("Hailo YOLO 运行环境尚未安装")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.result_path.unlink()
            except FileNotFoundError:
                pass
            environment = os.environ.copy()
            environment["HAILO_YOLO_OUTPUT"] = str(self.output_dir)
            environment["HAILO_YOLO_MODEL"] = str(self.model)
            environment["HAILO_YOLO_SAVE_INTERVAL"] = str(
                max(0.1, min(2.0, 1.0 / max(0.5, float(maximum_fps))))
            )
            with (self.output_dir / "pipeline.log").open("w", encoding="utf-8") as output:
                self.process = subprocess.Popen(
                    [
                        str(self.python),
                        str(self.script),
                        "--input",
                        self.camera_stream,
                        "--use-frame",
                        "--arch",
                        "hailo8",
                        "--hef-path",
                        str(self.model),
                        "--frame-rate",
                        str(max(1, min(12, round(float(maximum_fps))))),
                    ],
                    env=environment,
                    cwd=self.script.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                self.process_group_id = self.process.pid

    def read(self, classes: list[str], minimum_confidence: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.poll() is not None:
            detail = ""
            try:
                detail = (self.output_dir / "pipeline.log").read_text(
                    encoding="utf-8", errors="replace"
                )[-600:].strip()
            except OSError:
                pass
            message = "Hailo YOLO 推理进程未运行"
            if detail:
                message += f"：{detail}"
            raise RuntimeError(message)
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"ready": False, "detections": [], "detectionCount": 0}
        normalize = lambda value: re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
        allowed = {normalize(value) for value in classes}
        detections = []
        for raw in payload.get("detections", []):
            if not isinstance(raw, dict):
                continue
            label = normalize(raw.get("label") or "")
            confidence = float(raw.get("confidence") or 0.0)
            if label in allowed and confidence >= minimum_confidence:
                detections.append(
                    {
                        "class": label,
                        "confidence": round(confidence, 4),
                        "bbox": list(raw.get("bbox_xyxy") or [])[:4],
                        "trackId": raw.get("track_id"),
                    }
                )
        return {
            "ready": True,
            "timestamp": payload.get("timestamp"),
            "model": payload.get("model"),
            "detections": detections[:64],
            "detectionCount": len(detections),
        }

    def stop(self) -> None:
        with self.lock:
            process = self.process
            process_group_id = self.process_group_id
            self.process = None
            self.process_group_id = None
        if process_group_id is None:
            return
        self._terminate_group(process_group_id, process)

    @staticmethod
    def _terminate_group(
        process_group_id: int,
        process: subprocess.Popen[str] | None,
    ) -> None:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


class WorkshopHostBroker:
    def __init__(
        self,
        manager: WorkshopManager,
        store: WorkshopStore,
        *,
        runtime_root: Path = Path("/run/riverbank-workshop"),
        camera_snapshot_url: str = "http://127.0.0.1:19733/snapshot",
        task_api_url: str | None = None,
        task_token_file: Path | None = None,
        reports_dir: Path | None = None,
        ui_callback: Callable[[str, dict[str, Any]], None] | None = None,
        notification_callback: Callable[[str, dict[str, Any]], None] | None = None,
        microphone_factory: Callable[[str, int], Any] | None = None,
    ) -> None:
        self.manager = manager
        self.store = store
        self.runtime_root = Path(runtime_root)
        self.camera_snapshot_url = camera_snapshot_url
        self.task_api_url = (
            task_api_url
            or os.environ.get(
                "RIVERBANK_BACKGROUND_TASK_API_URL",
                "http://127.0.0.1:19734/api/v1/tasks",
            )
        ).rstrip("/")
        self.task_token_file = Path(
            task_token_file
            or os.environ.get(
                "RIVERBANK_BACKGROUND_TASK_TOKEN_FILE",
                RIVERBANK_HOME / ".config/riverbank-video-call/token",
            )
        )
        self.reports_dir = Path(
            reports_dir
            or os.environ.get(
                "RIVERBANK_REPORTS_DIR",
                RIVERBANK_HOME / ".hermes/profiles/daily/workspace/reports",
            )
        )
        self.ui_callback = ui_callback
        self.notification_callback = notification_callback
        self.microphone_source = os.environ.get("RIVERBANK_AUDIO_SOURCE", "").strip()
        self.microphone_factory = microphone_factory or (
            lambda source, sample_rate: PipeWireMicrophoneCapture(source, sample_rate)
        )
        self.microphone_lock = threading.RLock()
        self.microphone_leases: dict[str, dict[str, Any]] = {}
        self.detector = HailoObjectDetector()
        self.hailo_coordinator = HailoResourceCoordinator()
        self.hailo_reservation_id: str | None = None
        self.hailo_reservation_app_id: str | None = None
        self.hailo_reservation_renewed_at = 0.0
        self.event_subscriptions: dict[str, set[str]] = {}
        self.notification_times: dict[tuple[str, str], float] = {}
        self.stream_leases: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _microphone_indicator(app_id: str, active: bool, ttl_seconds: float = 2.0) -> None:
        send_expression(
            {
                "command": "audio_activity",
                "active": bool(active),
                "source": f"workshop:{app_id}",
                "ttl_seconds": ttl_seconds,
            }
        )

    def _close_microphone_lease(
        self,
        lease_id: str,
        *,
        expected_app_id: str | None = None,
        expired: bool = False,
    ) -> bool:
        with self.microphone_lock:
            lease = self.microphone_leases.get(lease_id)
            if lease is None:
                return False
            app_id = str(lease.get("appId") or "")
            if expected_app_id is not None and app_id != expected_app_id:
                return False
            self.microphone_leases.pop(lease_id, None)
            timer = lease.get("timer")
            capture = lease.get("capture")
        if isinstance(timer, threading.Timer):
            timer.cancel()
        if capture is not None:
            capture.close()
        self._microphone_indicator(app_id, False)
        if expired:
            self.store.audit(
                "microphone.lease_expired",
                app_id=app_id,
                lease_id=lease_id,
            )
        return True

    def _expire_microphone_lease(self, lease_id: str) -> None:
        self._close_microphone_lease(lease_id, expired=True)

    def stop_microphone(self, app_id: str) -> None:
        with self.microphone_lock:
            lease_ids = [
                lease_id
                for lease_id, lease in self.microphone_leases.items()
                if lease.get("appId") == app_id
            ]
        for lease_id in lease_ids:
            self._close_microphone_lease(lease_id, expected_app_id=app_id)

    def close(self) -> None:
        with self.microphone_lock:
            lease_ids = list(self.microphone_leases)
        for lease_id in lease_ids:
            self._close_microphone_lease(lease_id)

    def microphone_status(self) -> dict[str, Any]:
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        pipewire_socket = runtime_dir / "pipewire-0"
        with self.microphone_lock:
            active_leases = len(self.microphone_leases)
        return {
            "ready": Path("/usr/bin/pw-cat").is_file() and pipewire_socket.exists(),
            "transport": "pipewire-shared",
            "source": self.microphone_source or "default-input",
            "pipewireSocket": str(pipewire_socket),
            "activeLeases": active_leases,
            "rawAudioPersisted": False,
        }

    def _task_api(
        self,
        method: str,
        path: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            token = self.task_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("后台任务配对令牌不可用") from exc
        if not token:
            raise RuntimeError("后台任务配对令牌为空")
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RiverBank-Workshop/1",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = f"workshop-{uuid.uuid4().hex}"
        request = urllib.request.Request(
            self.task_api_url + path,
            data=body,
            method=method,
            headers=headers,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5.0) as response:
            content = response.read(512 * 1024 + 1)
        if len(content) > 512 * 1024:
            raise RuntimeError("后台任务响应超过大小限制")
        result = json.loads(content.decode("utf-8")) if content else {}
        if not isinstance(result, dict):
            raise RuntimeError("后台任务服务返回格式无效")
        return result

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol": "riverbank.app-host/v1",
            "type": "request",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }

    def dispatch(
        self,
        app_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        foreground: bool,
        user_present: bool,
    ) -> dict[str, Any]:
        request = self._request(method, dict(params or {}))
        record = self.manager.get_app(app_id)
        if record is None or record.get("status") != "enabled":
            return response_message(
                request["id"],
                error={"code": "app_not_enabled", "message": "应用未启用。"},
            )
        manifest = self.manager.installed_manifest(app_id)
        grants = {
            str(item.get("capability")): {"granted": bool(item.get("granted"))}
            for item in record.get("permissions", [])
            if isinstance(item, dict)
        }
        decision = authorize_request(
            manifest,
            grants,
            request,
            session={
                "app_id": app_id,
                "foreground": bool(foreground),
                "user_present": bool(user_present),
            },
        )
        if not decision.allowed:
            self.store.audit(
                "host.denied",
                app_id=app_id,
                method=method,
                code=decision.code,
            )
            return response_message(
                request["id"],
                error={"code": decision.code, "message": decision.reason},
            )
        try:
            result = self._execute(
                app_id,
                method,
                decision.sanitized_params or {},
            )
            self.store.audit("host.allowed", app_id=app_id, method=method)
            return response_message(request["id"], result=result)
        except Exception as exc:
            self.store.audit(
                "host.failed",
                app_id=app_id,
                method=method,
                error=str(exc)[:500],
            )
            return response_message(
                request["id"],
                error={"code": "host_backend_error", "message": str(exc)[:500]},
            )

    def _app_storage(self, app_id: str) -> Path:
        root = self.manager.data_root / "app-data" / app_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _storage_path(self, app_id: str, relative: str) -> Path:
        root = self._app_storage(app_id).resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ValueError("应用私有存储路径越界")
        return target

    def _execute(self, app_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ui.present":
            payload = {
                "view": str(params.get("view") or "status")[:48],
                "title": str(params.get("title") or "")[:40],
                "presentation": (
                    params.get("presentation")
                    if isinstance(params.get("presentation"), dict)
                    else None
                ),
                "data": params.get("data") if isinstance(params.get("data"), dict) else {},
            }
            if self.ui_callback is not None:
                self.ui_callback(app_id, payload)
            return {"presented": True}
        if method == "ui.dismiss":
            if self.ui_callback is not None:
                self.ui_callback(app_id, {"dismissed": True})
            return {"dismissed": True}
        if method.startswith("storage."):
            target = self._storage_path(app_id, str(params["path"]))
            if method == "storage.get":
                if not target.is_file():
                    return {"found": False}
                return {"found": True, "value": json.loads(target.read_text(encoding="utf-8"))}
            if method == "storage.put":
                target.parent.mkdir(parents=True, exist_ok=True)
                encoded = json.dumps(params.get("value"), ensure_ascii=False)
                if len(encoded.encode("utf-8")) > 256 * 1024:
                    raise ValueError("单次私有存储写入超过 256 KiB")
                atomic_write_json(target, params.get("value"))
                return {"stored": True}
            if method == "storage.list":
                directory = target if target.is_dir() else target.parent
                root = self._app_storage(app_id).resolve()
                items = [
                    path.resolve().relative_to(root).as_posix()
                    for path in sorted(directory.iterdir())[:200]
                    if path.is_file() and not path.is_symlink()
                ] if directory.is_dir() else []
                return {"items": items}
            if method == "storage.delete":
                if target.is_file() and not target.is_symlink():
                    target.unlink()
                    return {"deleted": True}
                return {"deleted": False}
        if method == "events.subscribe":
            event = str(params.get("event") or "").strip()[:96]
            if not event:
                raise ValueError("事件名不能为空")
            self.event_subscriptions.setdefault(app_id, set()).add(event)
            return {"subscribed": event}
        if method == "events.unsubscribe":
            event = str(params.get("event") or "").strip()[:96]
            self.event_subscriptions.setdefault(app_id, set()).discard(event)
            return {"unsubscribed": event}
        if method == "notifications.show":
            title = str(params.get("title") or "应用提醒")[:40]
            body = str(params.get("body") or "")[:160]
            cooldown = max(5.0, min(float(params.get("cooldownSeconds", 30)), 86400.0))
            key = (app_id, title)
            now = time.monotonic()
            if now - self.notification_times.get(key, 0.0) < cooldown:
                return {"shown": False, "reason": "rate_limited"}
            self.notification_times[key] = now
            payload = {"title": title, "body": body, "appId": app_id}
            if self.notification_callback is not None:
                self.notification_callback(app_id, payload)
            return {"shown": True}
        if method == "camera.snapshot":
            request = urllib.request.Request(self.camera_snapshot_url, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3.0) as response:
                content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("摄像头单帧超过大小限制")
            handle = uuid.uuid4().hex
            frame_dir = self.runtime_root / "frames"
            frame_dir.mkdir(parents=True, exist_ok=True)
            (frame_dir / f"{handle}.jpg").write_bytes(content)
            send_expression(
                {"command": "vision_activity", "active": True, "source": f"workshop:{app_id}", "ttl_seconds": 2.5}
            )
            return {"handle": handle, "bytes": len(content)}
        if method == "camera.stream.open":
            lease_id = uuid.uuid4().hex
            ttl = max(1.0, min(float(params.get("leaseSeconds", 30)), 300.0))
            self.stream_leases[lease_id] = {
                "appId": app_id,
                "expires": time.monotonic() + ttl,
            }
            send_expression(
                {"command": "vision_activity", "active": True, "source": f"workshop:{app_id}", "ttl_seconds": 2.5}
            )
            return {"leaseId": lease_id, "ttlSeconds": ttl}
        if method == "camera.stream.close":
            lease_id = str(params.get("leaseId") or "")
            lease = self.stream_leases.get(lease_id)
            if lease and lease.get("appId") == app_id:
                self.stream_leases.pop(lease_id, None)
            send_expression(
                {"command": "vision_activity", "active": False, "source": f"workshop:{app_id}"}
            )
            return {"closed": True}
        if method == "microphone.stream.open":
            ttl = float(params["leaseSeconds"])
            sample_rate = int(params["sampleRate"])
            frame_milliseconds = int(params["frameMilliseconds"])
            with self.microphone_lock:
                if self.microphone_leases:
                    raise RuntimeError("另一个工坊麦克风会话仍在运行")
            capture = self.microphone_factory(self.microphone_source, sample_rate)
            try:
                capture.start()
            except Exception:
                capture.close()
                raise
            lease_id = uuid.uuid4().hex
            timer = threading.Timer(ttl, self._expire_microphone_lease, (lease_id,))
            timer.daemon = True
            with self.microphone_lock:
                self.microphone_leases[lease_id] = {
                    "appId": app_id,
                    "capture": capture,
                    "expires": time.monotonic() + ttl,
                    "frameMilliseconds": frame_milliseconds,
                    "sampleRate": sample_rate,
                    "sequence": 0,
                    "timer": timer,
                }
            timer.start()
            self._microphone_indicator(app_id, True, min(ttl, 2.0))
            return {
                "leaseId": lease_id,
                "ttlSeconds": ttl,
                "sampleRate": sample_rate,
                "frameMilliseconds": frame_milliseconds,
                "format": "level-metrics-v1",
                "rawAudioPersisted": False,
            }
        if method == "microphone.stream.read":
            lease_id = str(params["leaseId"])
            with self.microphone_lock:
                lease = self.microphone_leases.get(lease_id)
                if lease is None or lease.get("appId") != app_id:
                    raise RuntimeError("麦克风租约不存在或已经过期")
                if time.monotonic() >= float(lease["expires"]):
                    expired = True
                    capture = None
                else:
                    expired = False
                    capture = lease["capture"]
            if expired:
                self._close_microphone_lease(
                    lease_id, expected_app_id=app_id, expired=True
                )
                raise RuntimeError("麦克风租约已经过期")
            result = capture.read_level(int(params["frameMilliseconds"]))
            with self.microphone_lock:
                current = self.microphone_leases.get(lease_id)
                if current is None or current.get("appId") != app_id:
                    raise RuntimeError("麦克风租约已经过期")
                current["sequence"] = int(current.get("sequence") or 0) + 1
                sequence = int(current["sequence"])
                remaining = max(0.0, float(current["expires"]) - time.monotonic())
            self._microphone_indicator(app_id, True, min(2.0, max(0.5, remaining)))
            return {
                **result,
                "sequence": sequence,
                "leaseRemainingSeconds": round(remaining, 3),
            }
        if method == "microphone.stream.close":
            closed = self._close_microphone_lease(
                str(params["leaseId"]), expected_app_id=app_id
            )
            return {"closed": closed}
        if method == "vision.detect":
            classes = [str(value).lower() for value in params.get("classes", [])][:12]
            confidence = max(0.1, min(float(params.get("minimumConfidence", 0.55)), 0.99))
            return self.detector.read(classes, confidence)
        if method in {"tasks.create", "assistant.query"}:
            prompt = str(params.get("prompt") or "").strip()
            if not 1 <= len(prompt) <= 1200:
                raise ValueError("后台任务内容长度必须在 1 到 1200 字之间")
            kind = str(params.get("kind") or "general").strip().lower()
            if kind not in {"general", "research", "file"}:
                raise ValueError("后台任务类型无效")
            result = self._task_api(
                "POST",
                payload={
                    "prompt": prompt,
                    "kind": kind,
                    "source": "workshop",
                    "device_name": "riverbank-tech",
                },
            )
            task = result.get("task")
            if not isinstance(task, dict):
                raise RuntimeError("后台任务服务没有返回任务记录")
            return {
                "id": str(task.get("id") or ""),
                "status": str(task.get("status") or "queued"),
                "title": str(task.get("title") or "")[:120],
            }
        if method == "tasks.status":
            task_id = str(params.get("id") or "").strip()
            if not task_id or len(task_id) > 128 or not all(
                character.isalnum() or character in "-_" for character in task_id
            ):
                raise ValueError("后台任务标识无效")
            result = self._task_api("GET", f"/{task_id}")
            task = result.get("task")
            if not isinstance(task, dict):
                raise RuntimeError("后台任务服务没有返回任务记录")
            return {
                "id": str(task.get("id") or task_id),
                "status": str(task.get("status") or "unknown"),
                "progress": float(task.get("progress") or 0.0),
                "statusMessage": str(task.get("status_message") or "")[:160],
                "reportId": str(task.get("report_filename") or "")[:180],
            }
        if method == "reports.list":
            files = [path for path in sorted(self.reports_dir.glob("*.md"), reverse=True)[:100] if path.is_file()]
            return {"reports": [{"id": path.name, "name": path.stem} for path in files]}
        if method == "reports.read":
            name = str(params.get("id") or "")
            if Path(name).name != name or not name.endswith(".md"):
                raise ValueError("报告标识无效")
            target = (self.reports_dir / name).resolve()
            root = self.reports_dir.resolve()
            if root not in target.parents or not target.is_file() or target.is_symlink():
                raise FileNotFoundError("报告不存在")
            return {"id": name, "content": target.read_text(encoding="utf-8")[:512_000]}
        if method == "network.fetch":
            request = urllib.request.Request(
                str(params["url"]),
                method=str(params.get("httpMethod", "GET")),
                headers={"User-Agent": "RiverBank-Workshop/1"},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler())
            with opener.open(request, timeout=10.0) as response:
                content = response.read(1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "")
            if len(content) > 1024 * 1024:
                raise ValueError("联网响应超过 1 MiB")
            return {
                "status": int(getattr(response, "status", 200)),
                "contentType": content_type[:120],
                "text": content.decode("utf-8", errors="replace"),
            }
        if method in {
            "speaker.play",
            "speaker.stop",
            "motor.move",
            "motor.stop",
        }:
            raise RuntimeError("该硬件 Host 适配器尚未启用")
        raise RuntimeError("Host 方法尚未实现")

    def vision_heartbeat(self, app_id: str) -> None:
        if self.hailo_reservation_app_id == app_id and self.hailo_reservation_id:
            now = time.monotonic()
            if now - self.hailo_reservation_renewed_at >= 2.0:
                self.hailo_coordinator.renew(self.hailo_reservation_id, 8.0)
                self.hailo_reservation_renewed_at = now
        send_expression(
            {"command": "vision_activity", "active": True, "source": f"workshop:{app_id}", "ttl_seconds": 2.5}
        )

    def start_vision(self, app_id: str, maximum_fps: float) -> None:
        if self.hailo_reservation_id is not None:
            raise RuntimeError("另一个工坊视觉会话仍持有 Hailo 预约")
        reservation_id = self.hailo_coordinator.reserve(
            f"workshop:{app_id}",
            8.0,
        )
        self.hailo_reservation_id = reservation_id
        self.hailo_reservation_app_id = app_id
        self.hailo_reservation_renewed_at = time.monotonic()
        try:
            self.detector.start(maximum_fps)
        except Exception:
            self.hailo_coordinator.release(reservation_id)
            self.hailo_reservation_id = None
            self.hailo_reservation_app_id = None
            raise

    def stop_vision(self, app_id: str) -> None:
        self.detector.stop()
        if self.hailo_reservation_app_id == app_id and self.hailo_reservation_id:
            self.hailo_coordinator.release(self.hailo_reservation_id)
            self.hailo_reservation_id = None
            self.hailo_reservation_app_id = None
            self.hailo_reservation_renewed_at = 0.0
        self.stream_leases = {
            key: value for key, value in self.stream_leases.items() if value.get("appId") != app_id
        }
        send_expression(
            {"command": "vision_activity", "active": False, "source": f"workshop:{app_id}"}
        )
