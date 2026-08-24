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


RUNTIME_DIR = Path(os.environ.get("RIVERBANK_FACE_RUNTIME", "/run/riverbank-face-tracker"))
STATE_PATH = RUNTIME_DIR / "state.json"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
RIVERBANK_DATA = Path(
    os.environ.get("RIVERBANK_DATA", Path.home() / ".local/share/riverbank")
)
MODEL_PATH = os.environ.get(
    "RIVERBANK_FACE_MODEL",
    str(RIVERBANK_DATA / "ai/models/face/scrfd_2.5g_hailo8_v2.14.hef"),
)
POSTPROCESS_SO = os.environ.get(
    "RIVERBANK_FACE_POSTPROCESS",
    str(RIVERBANK_DATA / "ai/resources/so/libscrfd_v2_14.so"),
)
POSTPROCESS_CONFIG = os.environ.get(
    "RIVERBANK_FACE_CONFIG",
    str(RIVERBANK_DATA / "ai/models/face/scrfd_640.json"),
)
POSTPROCESS_FUNCTION = "scrfd_2_5g"


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def track_id(detection: object) -> int | None:
    objects = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
    return int(objects[0].get_id()) if len(objects) == 1 else None


class FaceState(app_callback_class):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
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
            "model": Path(MODEL_PATH).name,
            "postprocess": POSTPROCESS_FUNCTION,
            "resolution": [width, height],
            "frame": self.get_count(),
            "average_pipeline_fps": round(self.get_count() / elapsed_total, 3),
            "visible": visible,
            "face_count": len(faces),
            "target": target,
            "faces": faces,
        }
        with self.lock:
            self.latest = payload
            self.last_frame_monotonic = now
            atomic_json(STATE_PATH, payload)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.latest))

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
                        command = raw_request
                        if raw_request.startswith("{"):
                            try:
                                command = str(json.loads(raw_request).get("command", ""))
                            except (json.JSONDecodeError, AttributeError):
                                command = "invalid"
                        if command not in {"", "status"}:
                            response = {"ok": False, "error": "supported command: status"}
                        else:
                            response = self.snapshot()
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
        self.server_thread = threading.Thread(
            target=self.serve,
            name="face-tracker-control",
            daemon=True,
        )
        self.server_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.server_thread is not None:
            self.server_thread.join(timeout=2)


def callback(pad: Gst.Pad, info: Gst.PadProbeInfo, state: FaceState):
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
        str(RIVERBANK_DATA / "ai/apps/hailo-rpi5-examples/.env"),
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
