#!/usr/bin/env python3
"""Single-owner supervisor for the RiverBank Hailo-8 accelerator.

The supervisor itself never opens Hailo. It owns the public control socket and
starts the SCRFD worker only while a face-tracking lease exists. An external
trusted workload can reserve the accelerator only after that worker has fully
exited, which is the only reliable way to release the GStreamer Hailo vdevice.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from vision_leases import VisionLeaseManager


RUNTIME_DIR = Path(
    os.environ.get("RIVERBANK_FACE_RUNTIME", "/run/riverbank-face-tracker")
)
STATE_PATH = RUNTIME_DIR / "state.json"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
WORKER_RUNTIME = RUNTIME_DIR / "worker"
WORKER_STATE_PATH = WORKER_RUNTIME / "state.json"
WORKER_LOG_PATH = RUNTIME_DIR / "worker.log"
EXPRESSION_SOCKET_PATH = Path("/run/riverbank-expression/control.sock")
WORKER_PYTHON = Path(os.environ.get("RIVERBANK_FACE_WORKER_PYTHON", sys.executable))
WORKER_SCRIPT = Path(
    os.environ.get(
        "RIVERBANK_FACE_WORKER_SCRIPT",
        Path(__file__).with_name("face_tracker.py"),
    )
)
CAMERA_STREAM = os.environ.get(
    "RIVERBANK_CAMERA_STREAM_URL",
    "http://127.0.0.1:19733/stream",
)
MODEL_PATH = os.environ.get(
    "RIVERBANK_FACE_MODEL",
    "/mnt/nvme64/ai/models/face/scrfd_2.5g_hailo8_v2.14.hef",
)
STOP_TIMEOUT_SECONDS = 10.0
RESTART_BACKOFF_SECONDS = 2.0


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_vision_inactive() -> None:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(
                json.dumps(
                    {
                        "command": "vision_activity",
                        "active": False,
                        "source": "hailo_face_tracker",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                str(EXPRESSION_SOCKET_PATH),
            )
        finally:
            client.close()
    except OSError:
        pass


class FaceTrackerSupervisor:
    def __init__(self) -> None:
        self.leases = VisionLeaseManager()
        self.external_reservations = VisionLeaseManager(
            minimum_ttl_seconds=1.0,
            maximum_ttl_seconds=30.0,
        )
        self.lock = threading.RLock()
        self.state_write_lock = threading.Lock()
        self.worker: subprocess.Popen[str] | None = None
        self.worker_started_at: float | None = None
        self.last_worker_exit: int | None = None
        self.last_worker_exit_at: float | None = None
        self.next_restart_monotonic = 0.0
        self.requested = False
        self.request_changed_at = time.time()
        self.last_transition_at: float | None = None
        self.running = True

    def worker_running(self) -> bool:
        return self.worker is not None and self.worker.poll() is None

    def desired_face_worker(self) -> bool:
        return self.leases.active() and not self.external_reservations.active()

    def start_worker(self) -> None:
        if self.worker_running():
            return
        if not WORKER_PYTHON.is_file() or not WORKER_SCRIPT.is_file():
            raise RuntimeError("face worker runtime is missing")
        WORKER_RUNTIME.mkdir(parents=True, exist_ok=True)
        try:
            WORKER_STATE_PATH.unlink()
        except FileNotFoundError:
            pass
        environment = os.environ.copy()
        environment.update(
            {
                "RIVERBANK_FACE_RUNTIME": str(WORKER_RUNTIME),
                "RIVERBANK_FACE_FORCE_INFERENCE": "1",
                "RIVERBANK_FACE_DISABLE_CONTROL": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        with WORKER_LOG_PATH.open("a", encoding="utf-8") as output:
            self.worker = subprocess.Popen(
                [
                    str(WORKER_PYTHON),
                    str(WORKER_SCRIPT),
                    "--input",
                    CAMERA_STREAM,
                    "--hef-path",
                    MODEL_PATH,
                    "--arch",
                    "hailo8",
                    "--disable-sync",
                    "--frame-rate",
                    "30",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        self.worker_started_at = time.time()
        self.last_transition_at = self.worker_started_at
        self.last_worker_exit = None

    def stop_worker(self) -> None:
        worker = self.worker
        self.worker = None
        if worker is None:
            publish_vision_inactive()
            return
        if worker.poll() is None:
            try:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=STOP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self.last_worker_exit = worker.poll()
        self.last_worker_exit_at = time.time()
        self.last_transition_at = self.last_worker_exit_at
        publish_vision_inactive()

    def reconcile(self) -> None:
        desired = self.desired_face_worker()
        if desired != self.requested:
            self.requested = desired
            self.request_changed_at = time.time()
        if desired:
            if not self.worker_running() and time.monotonic() >= self.next_restart_monotonic:
                self.start_worker()
        elif self.worker is not None:
            self.stop_worker()

    def worker_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(WORKER_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            face = self.leases.snapshot()
            external = self.external_reservations.snapshot()
            worker_running = self.worker_running()
            worker_payload = self.worker_state() if worker_running else {}
            worker_inference = worker_payload.get("inference")
            worker_ready = bool(
                isinstance(worker_inference, dict)
                and worker_inference.get("active")
            )
            pipeline_state = (
                "playing" if worker_ready else "starting" if worker_running else "unloaded"
            )
            payload: dict[str, Any] = {
                "ok": True,
                "service": "running",
                "model": Path(MODEL_PATH).name,
                "visible": False,
                "faces": [],
                "face_count": 0,
                "target": None,
                "timestamp": time.time(),
            }
            if worker_payload:
                payload.update(worker_payload)
                payload["ok"] = True
                payload["service"] = "running"
                payload["timestamp"] = time.time()
            if not worker_running:
                payload.update(
                    {
                        "visible": False,
                        "faces": [],
                        "face_count": 0,
                        "target": None,
                    }
                )
            payload["inference"] = {
                "mode": "process-supervised-lease-controlled",
                "requested": face["active"] and not external["active"],
                "active": worker_ready,
                "pipeline_state": pipeline_state,
                "lease_count": face["lease_count"],
                "leases": face["leases"],
                "next_expiry_seconds": face["next_expiry_seconds"],
                "external_reserved": external["active"],
                "external_reservation_count": external["lease_count"],
                "external_reservations": external["leases"],
                "worker_pid": self.worker.pid if worker_running and self.worker else None,
                "worker_started_at": self.worker_started_at,
                "last_worker_exit": self.last_worker_exit,
                "last_worker_exit_at": self.last_worker_exit_at,
                "request_changed_at": self.request_changed_at,
                "last_transition_at": self.last_transition_at,
            }
            return payload

    def write_state(self) -> None:
        payload = self.snapshot()
        with self.state_write_lock:
            atomic_json(STATE_PATH, payload)

    def handle_request(self, raw_request: str) -> dict[str, Any]:
        request: dict[str, Any] = {}
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
        with self.lock:
            try:
                if command == "status":
                    return self.snapshot()
                if command == "acquire":
                    if self.external_reservations.active():
                        return {
                            "ok": False,
                            "error": "Hailo device is reserved by another trusted vision workload",
                            "state": self.snapshot(),
                        }
                    lease = self.leases.acquire(
                        str(request.get("source", "unknown")),
                        request.get("ttl_seconds", 30.0),
                    )
                    self.reconcile()
                    self.write_state()
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
                    self.reconcile()
                    self.write_state()
                    return {"ok": True, "lease": lease, "state": self.snapshot()}
                if command == "release":
                    lease_id = str(request.get("lease_id", "")).strip()
                    if not lease_id:
                        raise ValueError("lease_id is required")
                    released = self.leases.release(lease_id)
                    self.reconcile()
                    self.write_state()
                    return {
                        "ok": released,
                        "released": released,
                        "error": None if released else "lease not found or expired",
                        "state": self.snapshot(),
                    }
                if command == "reserve_external":
                    if self.leases.active():
                        return {
                            "ok": False,
                            "error": "face tracker currently has an active lease",
                            "state": self.snapshot(),
                        }
                    reservation = self.external_reservations.acquire(
                        str(request.get("source", "external-vision")),
                        request.get("ttl_seconds", 8.0),
                    )
                    self.reconcile()
                    self.write_state()
                    return {
                        "ok": True,
                        "reservation": reservation,
                        "state": self.snapshot(),
                    }
                if command == "renew_external":
                    reservation_id = str(request.get("reservation_id", "")).strip()
                    if not reservation_id:
                        raise ValueError("reservation_id is required")
                    reservation = self.external_reservations.renew(
                        reservation_id,
                        request.get("ttl_seconds", 8.0),
                    )
                    if reservation is None:
                        return {"ok": False, "error": "external reservation not found or expired"}
                    self.reconcile()
                    self.write_state()
                    return {
                        "ok": True,
                        "reservation": reservation,
                        "state": self.snapshot(),
                    }
                if command == "release_external":
                    reservation_id = str(request.get("reservation_id", "")).strip()
                    if not reservation_id:
                        raise ValueError("reservation_id is required")
                    released = self.external_reservations.release(reservation_id)
                    self.reconcile()
                    self.write_state()
                    return {
                        "ok": released,
                        "released": released,
                        "error": None if released else "external reservation not found or expired",
                        "state": self.snapshot(),
                    }
                return {
                    "ok": False,
                    "error": (
                        "supported commands: status, acquire, renew, release, "
                        "reserve_external, renew_external, release_external"
                    ),
                }
            except (OSError, ValueError, RuntimeError) as exc:
                return {"ok": False, "error": str(exc), "state": self.snapshot()}

    def serve(self) -> None:
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o660)
        server.listen(8)
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

    def maintain(self) -> None:
        while self.running:
            with self.lock:
                self.leases.expire()
                self.external_reservations.expire()
                if self.worker is not None and self.worker.poll() is not None:
                    self.last_worker_exit = self.worker.returncode
                    self.last_worker_exit_at = time.time()
                    self.worker = None
                    self.next_restart_monotonic = (
                        time.monotonic() + RESTART_BACKOFF_SECONDS
                    )
                    publish_vision_inactive()
                try:
                    self.reconcile()
                except (OSError, RuntimeError):
                    self.next_restart_monotonic = (
                        time.monotonic() + RESTART_BACKOFF_SECONDS
                    )
                self.write_state()
            time.sleep(0.25)

    def run(self) -> int:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        WORKER_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.write_state()
        maintenance = threading.Thread(
            target=self.maintain,
            name="face-tracker-supervisor-maintenance",
            daemon=True,
        )
        maintenance.start()
        try:
            self.serve()
        finally:
            self.running = False
            with self.lock:
                self.stop_worker()
            maintenance.join(timeout=2)
        return 0

    def stop(self, *_args: object) -> None:
        self.running = False
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(SOCKET_PATH))
                probe.sendall(b'{"command":"status"}')
            finally:
                probe.close()
        except OSError:
            pass


def main() -> int:
    supervisor = FaceTrackerSupervisor()
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
