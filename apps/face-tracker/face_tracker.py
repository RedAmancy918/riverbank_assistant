#!/usr/bin/env python3
"""Persistent Hailo SCRFD face tracker fed by the shared camera hub."""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import hailo  # noqa: E402
import hailo_apps.hailo_app_python.core.gstreamer.gstreamer_app as gst_app  # noqa: E402
from hailo_apps.hailo_app_python.core.common.buffer_utils import get_caps_from_pad  # noqa: E402
from hailo_apps.hailo_app_python.core.common.core import get_default_parser  # noqa: E402
from hailo_apps.hailo_app_python.core.gstreamer.gstreamer_app import (  # noqa: E402
    GStreamerApp,
    app_callback_class,
)
from hailo_apps.hailo_app_python.core.gstreamer.gstreamer_helper_pipelines import (  # noqa: E402
    DISPLAY_PIPELINE,
    INFERENCE_PIPELINE,
    INFERENCE_PIPELINE_WRAPPER,
    QUEUE,
    TRACKER_PIPELINE,
    USER_CALLBACK_PIPELINE,
)
from vision_leases import VisionLeaseManager


RUNTIME_DIR = Path(os.environ.get("RIVERBANK_FACE_RUNTIME", "/run/riverbank-face-tracker"))
STATE_PATH = RUNTIME_DIR / "state.json"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
EXPRESSION_SOCKET_PATH = Path("/run/riverbank-expression/control.sock")
MODEL_PATH = os.environ.get(
    "RIVERBANK_FACE_MODEL",
    "/mnt/nvme64/ai/models/face/scrfd_2.5g_hailo8_v2.14.hef",
)
POSTPROCESS_SO = os.environ.get(
    "RIVERBANK_FACE_POSTPROCESS",
    "/mnt/nvme64/ai/resources/so/libscrfd_v2_14.so",
)
POSTPROCESS_CONFIG = os.environ.get(
    "RIVERBANK_FACE_CONFIG",
    "/mnt/nvme64/ai/models/face/scrfd_640.json",
)
POSTPROCESS_FUNCTION = "scrfd_2_5g"
VISION_SOURCE = "hailo_face_tracker"
VISION_HEARTBEAT_SECONDS = 1.0
VISION_INDICATOR_TTL_SECONDS = 2.5


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_vision_activity(active: bool) -> None:
    payload = {
        "command": "vision_activity",
        "active": bool(active),
        "source": VISION_SOURCE,
        "ttl_seconds": VISION_INDICATOR_TTL_SECONDS,
    }
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                str(EXPRESSION_SOCKET_PATH),
            )
        finally:
            client.close()
    except OSError:
        pass


def track_id(detection: object) -> int | None:
    objects = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
    return int(objects[0].get_id()) if len(objects) == 1 else None


class FaceState(app_callback_class):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.state_write_lock = threading.Lock()
        self.leases = VisionLeaseManager()
        self.started_monotonic = time.monotonic()
        self.last_frame_monotonic = 0.0
        self.last_target_monotonic = 0.0
        self.last_detection_monotonic = 0.0
        self.target_center: tuple[float, float] | None = None
        self.target_velocity = (0.0, 0.0)
        self.target_track_id: int | None = None
        self.latest: dict = {
            "ok": False,
            "service": "starting",
            "model": Path(MODEL_PATH).name,
            "visible": False,
            "faces": [],
        }
        self.running = True
        self.server_thread: threading.Thread | None = None
        self.maintenance_thread: threading.Thread | None = None
        self.pipeline_controller: Callable[[bool], None] | None = None
        self.pipeline_requested = False
        self.pipeline_active = False
        self.pipeline_request_changed_at = time.time()
        self.pipeline_transition_at: float | None = None
        self.last_indicator_heartbeat = 0.0
        self.last_runtime_write_monotonic = 0.0

    def attach_pipeline_controller(self, controller: Callable[[bool], None]) -> None:
        self.pipeline_controller = controller
        self.reconcile_inference(force=True)

    def pipeline_state_changed(self, active: bool, success: bool) -> None:
        with self.lock:
            if success:
                self.pipeline_active = bool(active)
            self.pipeline_transition_at = time.time()
        self.write_runtime_state()

    def inference_state(self) -> dict:
        lease_state = self.leases.snapshot()
        with self.lock:
            requested = self.pipeline_requested
            pipeline_active = self.pipeline_active
            request_changed_at = self.pipeline_request_changed_at
            transition_at = self.pipeline_transition_at
        return {
            "mode": "lease-controlled",
            "requested": requested,
            "active": pipeline_active,
            "pipeline_state": "playing" if pipeline_active else "paused",
            "lease_count": lease_state["lease_count"],
            "leases": lease_state["leases"],
            "next_expiry_seconds": lease_state["next_expiry_seconds"],
            "request_changed_at": request_changed_at,
            "last_transition_at": transition_at,
        }

    def clear_tracking(self) -> None:
        with self.lock:
            self.target_center = None
            self.target_velocity = (0.0, 0.0)
            self.target_track_id = None
            self.latest.update(
                {
                    "visible": False,
                    "face_count": 0,
                    "target": None,
                    "faces": [],
                }
            )

    def reconcile_inference(self, *, force: bool = False) -> None:
        desired = self.leases.active()
        with self.lock:
            changed = force or desired != self.pipeline_requested
            if desired != self.pipeline_requested:
                self.pipeline_request_changed_at = time.time()
            self.pipeline_requested = desired
            controller = self.pipeline_controller
        if changed:
            if not desired:
                self.clear_tracking()
            if controller is not None:
                controller(desired)
            publish_vision_activity(desired)
            self.last_indicator_heartbeat = time.monotonic()
        self.write_runtime_state()

    def write_runtime_state(self) -> None:
        payload = self.snapshot()
        with self.state_write_lock:
            atomic_json(STATE_PATH, payload)
        self.last_runtime_write_monotonic = time.monotonic()

    def maintain(self) -> None:
        while self.running:
            expired = self.leases.expire()
            if expired:
                self.reconcile_inference()
            else:
                desired = self.leases.active()
                with self.lock:
                    requested = self.pipeline_requested
                if desired != requested:
                    self.reconcile_inference()
                elif time.monotonic() - self.last_runtime_write_monotonic >= 1.0:
                    self.write_runtime_state()
            now = time.monotonic()
            if self.leases.active() and (
                now - self.last_indicator_heartbeat >= VISION_HEARTBEAT_SECONDS
            ):
                publish_vision_activity(True)
                self.last_indicator_heartbeat = now
            time.sleep(0.25)

    def choose_target(self, faces: list[dict], now: float) -> dict | None:
        if not faces:
            return None
        if self.target_track_id is not None:
            for face in faces:
                if face["track_id"] == self.target_track_id:
                    return face
        if self.target_center is None:
            return max(faces, key=lambda item: item["area"])
        elapsed = max(0.0, min(now - self.last_target_monotonic, 1.0))
        predicted = (
            self.target_center[0] + self.target_velocity[0] * elapsed,
            self.target_center[1] + self.target_velocity[1] * elapsed,
        )
        return min(
            faces,
            key=lambda item: (
                math.dist(predicted, item["center"]) - min(item["area"], 0.25) * 0.35
            ),
        )

    def update(self, faces: list[dict], width: int, height: int) -> None:
        now = time.monotonic()
        chosen = self.choose_target(faces, now)
        visible = chosen is not None
        if chosen is not None:
            measured = tuple(chosen["center"])
            if self.target_center is None:
                smoothed = measured
                velocity = (0.0, 0.0)
            else:
                elapsed = max(0.02, min(now - self.last_target_monotonic, 1.0))
                predicted = (
                    self.target_center[0] + self.target_velocity[0] * elapsed,
                    self.target_center[1] + self.target_velocity[1] * elapsed,
                )
                alpha = 0.68
                smoothed = (
                    predicted[0] * (1.0 - alpha) + measured[0] * alpha,
                    predicted[1] * (1.0 - alpha) + measured[1] * alpha,
                )
                measured_velocity = (
                    (smoothed[0] - self.target_center[0]) / elapsed,
                    (smoothed[1] - self.target_center[1]) / elapsed,
                )
                velocity = (
                    self.target_velocity[0] * 0.65 + measured_velocity[0] * 0.35,
                    self.target_velocity[1] * 0.65 + measured_velocity[1] * 0.35,
                )
            self.target_center = smoothed
            self.target_velocity = velocity
            self.target_track_id = chosen["track_id"]
            self.last_target_monotonic = now
            self.last_detection_monotonic = now
        elif self.target_center is not None and now - self.last_detection_monotonic <= 0.6:
            elapsed = now - self.last_target_monotonic
            self.target_center = (
                max(0.0, min(1.0, self.target_center[0] + self.target_velocity[0] * elapsed)),
                max(0.0, min(1.0, self.target_center[1] + self.target_velocity[1] * elapsed)),
            )
            self.last_target_monotonic = now
        else:
            self.target_center = None
            self.target_velocity = (0.0, 0.0)
            self.target_track_id = None

        elapsed_total = max(now - self.started_monotonic, 1e-6)
        target = None
        if self.target_center is not None:
            target = {
                "track_id": self.target_track_id,
                "center_normalized": [round(value, 5) for value in self.target_center],
                "error_from_center": [
                    round((self.target_center[0] - 0.5) * 2.0, 5),
                    round((self.target_center[1] - 0.5) * 2.0, 5),
                ],
                "velocity_normalized_per_second": [
                    round(value, 5) for value in self.target_velocity
                ],
                "last_seen_seconds_ago": round(now - self.last_detection_monotonic, 3),
            }
        payload = {
            "ok": True,
            "service": "running",
            "timestamp": time.time(),
            "last_frame_at": time.time(),
            "model": Path(MODEL_PATH).name,
            "postprocess": POSTPROCESS_FUNCTION,
            "resolution": [width, height],
            "frame": self.get_count(),
            "average_pipeline_fps": round(self.get_count() / elapsed_total, 3),
            "visible": visible,
            "face_count": len(faces),
            "target": target,
            "faces": faces,
            "inference": self.inference_state(),
        }
        with self.lock:
            self.latest = payload
            self.last_frame_monotonic = now
        with self.state_write_lock:
            atomic_json(STATE_PATH, payload)
        self.last_runtime_write_monotonic = time.monotonic()

    def snapshot(self) -> dict:
        inference = self.inference_state()
        with self.lock:
            payload = json.loads(json.dumps(self.latest))
        payload.update(
            {
                "ok": True,
                "service": "running",
                "timestamp": time.time(),
                "inference": inference,
            }
        )
        if not inference["active"]:
            payload.update(
                {
                    "visible": False,
                    "face_count": 0,
                    "target": None,
                    "faces": [],
                }
            )
        return payload

    def handle_request(self, raw_request: str) -> dict:
        request: dict = {}
        command = raw_request.strip()
        if command.startswith("{"):
            try:
                parsed = json.loads(command)
            except json.JSONDecodeError:
                return {"ok": False, "error": "invalid JSON request"}
            if not isinstance(parsed, dict):
                return {"ok": False, "error": "request must be a JSON object"}
            request = parsed
            command = str(request.get("command", "status"))
        command = command or "status"
        try:
            if command == "status":
                return self.snapshot()
            if command == "acquire":
                lease = self.leases.acquire(
                    str(request.get("source", "unknown")),
                    request.get("ttl_seconds", 30.0),
                )
                self.reconcile_inference()
                return {"ok": True, "lease": lease, "state": self.snapshot()}
            if command == "renew":
                lease_id = str(request.get("lease_id", "")).strip()
                if not lease_id:
                    raise ValueError("lease_id is required")
                lease = self.leases.renew(
                    lease_id,
                    request.get("ttl_seconds", 30.0),
                )
                if lease is None:
                    return {"ok": False, "error": "lease not found or expired"}
                self.reconcile_inference()
                return {"ok": True, "lease": lease, "state": self.snapshot()}
            if command == "release":
                lease_id = str(request.get("lease_id", "")).strip()
                if not lease_id:
                    raise ValueError("lease_id is required")
                released = self.leases.release(lease_id)
                self.reconcile_inference()
                return {
                    "ok": released,
                    "released": released,
                    "error": None if released else "lease not found or expired",
                    "state": self.snapshot(),
                }
            return {
                "ok": False,
                "error": "supported commands: status, acquire, renew, release",
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def serve(self) -> None:
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o660)
        server.listen(4)
        server.settimeout(0.5)
        try:
            while self.running:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    try:
                        raw_request = connection.recv(4096).decode("utf-8", "replace").strip()
                        response = self.handle_request(raw_request)
                        connection.sendall(
                            (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                        )
                    except OSError:
                        pass
        finally:
            server.close()
            try:
                SOCKET_PATH.unlink()
            except FileNotFoundError:
                pass

    def start_server(self) -> None:
        self.write_runtime_state()
        self.server_thread = threading.Thread(
            target=self.serve,
            name="face-tracker-control",
            daemon=True,
        )
        self.server_thread.start()
        self.maintenance_thread = threading.Thread(
            target=self.maintain,
            name="face-tracker-lease-maintenance",
            daemon=True,
        )
        self.maintenance_thread.start()

    def stop(self) -> None:
        self.running = False
        publish_vision_activity(False)
        if self.server_thread is not None:
            self.server_thread.join(timeout=2)
        if self.maintenance_thread is not None:
            self.maintenance_thread.join(timeout=2)


def callback(pad: Gst.Pad, info: Gst.PadProbeInfo, state: FaceState):
    if not state.leases.active():
        return Gst.PadProbeReturn.OK
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK
    state.increment()
    _format, width, height = get_caps_from_pad(pad)
    if width is None or height is None:
        return Gst.PadProbeReturn.OK
    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    faces: list[dict] = []
    for detection in detections:
        bbox = detection.get_bbox()
        center = (
            float(bbox.xmin() + bbox.width() / 2.0),
            float(bbox.ymin() + bbox.height() / 2.0),
        )
        faces.append(
            {
                "track_id": track_id(detection),
                "confidence": round(float(detection.get_confidence()), 5),
                "center": [round(center[0], 5), round(center[1], 5)],
                "bbox_normalized": [
                    round(float(bbox.xmin()), 5),
                    round(float(bbox.ymin()), 5),
                    round(float(bbox.width()), 5),
                    round(float(bbox.height()), 5),
                ],
                "area": round(float(bbox.width() * bbox.height()), 6),
            }
        )
    state.update(faces, int(width), int(height))
    return Gst.PadProbeReturn.OK


class FaceTrackerApp(GStreamerApp):
    def __init__(self, state: FaceState) -> None:
        parser = get_default_parser()
        super().__init__(parser, state)
        self.hef_path = self.options_menu.hef_path or MODEL_PATH
        self.video_width = 640
        self.video_height = 640
        self.batch_size = 1
        self.app_callback = callback
        self.create_pipeline()
        state.attach_pipeline_controller(self.request_inference_state)

    def request_inference_state(self, active: bool) -> None:
        GLib.idle_add(self.apply_inference_state, bool(active))

    def apply_inference_state(self, active: bool) -> bool:
        target = Gst.State.PLAYING if active else Gst.State.PAUSED
        result = self.pipeline.set_state(target)
        success = result != Gst.StateChangeReturn.FAILURE
        self.user_data.pipeline_state_changed(active, success)
        print(
            "Hailo inference transition "
            f"target={'playing' if active else 'paused'} "
            f"result={getattr(result, 'value_nick', str(result))}",
            flush=True,
        )
        return False

    def get_pipeline_string(self) -> str:
        if not self.video_source.startswith(("http://", "https://")):
            raise ValueError("face tracker requires the camera-hub HTTP stream")
        source = (
            f'souphttpsrc location="{self.video_source}" is-live=true '
            'do-timestamp=true timeout=2 keep-alive=false ! '
            'multipartdemux ! image/jpeg ! '
            f'{QUEUE(name="face_jpeg_q", max_size_buffers=2, leaky="downstream")} ! '
            'jpegparse ! jpegdec ! videoflip video-direction=horiz ! '
            f'{QUEUE(name="face_scale_q", max_size_buffers=2, leaky="downstream")} ! '
            'videoscale n-threads=2 ! videoconvert n-threads=2 qos=false ! '
            'video/x-raw,format=RGB,width=640,height=640,pixel-aspect-ratio=1/1'
        )
        inference = INFERENCE_PIPELINE(
            hef_path=self.hef_path,
            post_process_so=POSTPROCESS_SO,
            post_function_name=POSTPROCESS_FUNCTION,
            batch_size=1,
            config_json=POSTPROCESS_CONFIG,
            additional_params="",
        )
        return (
            f"{source} ! "
            f"{INFERENCE_PIPELINE_WRAPPER(inference)} ! "
            f"{TRACKER_PIPELINE(class_id=-1, kalman_dist_thr=0.7, iou_thr=0.75, init_iou_thr=0.85, keep_new_frames=2, keep_tracked_frames=6, keep_lost_frames=8, keep_past_metadata=True, name='riverbank_face_tracker')} ! "
            f"{USER_CALLBACK_PIPELINE()} ! "
            f"{DISPLAY_PIPELINE(video_sink='fakesink', sync='false', show_fps=False)}"
        )

    def shutdown(self, signum=None, frame=None) -> None:
        self.user_data.stop()
        super().shutdown(signum=signum, frame=frame)


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(
        "HAILO_ENV_FILE",
        "/mnt/nvme64/ai/apps/hailo-rpi5-examples/.env",
    )
    gst_app.GST_VIDEO_SINK = "fakesink"
    state = FaceState()
    state.start_server()
    app = FaceTrackerApp(state)
    signal.signal(signal.SIGTERM, app.shutdown)
    try:
        app.run()
    finally:
        state.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
