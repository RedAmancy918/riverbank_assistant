#!/usr/bin/env python3
"""ListenGo control-port daemon for handshake, wake events, and DOA state."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socketserver
import struct
import threading
import time
from pathlib import Path
from typing import Any

import serial


SYNC = 0xA5
USER_ID = 0x01
TYPE_HANDSHAKE = 0x01
TYPE_WAKE = 0x04
TYPE_CONTROL = 0x05
TYPE_AUDIO = 0x06
TYPE_HANDSHAKE_ACK = 0xFF
HEADER_SIZE = 7
MAX_PAYLOAD = 10232


def checksum(data: bytes) -> int:
    return (-sum(data)) & 0xFF


def encode_message(message_type: int, payload: bytes, message_id: int) -> bytes:
    header = bytes((SYNC, USER_ID, message_type)) + struct.pack(
        "<HH", len(payload), message_id
    )
    body = header + payload
    return body + bytes((checksum(body),))


def nested_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def extract_wake_fields(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    candidates: list[dict[str, Any]] = [message]
    content = nested_json(message.get("content"))
    if isinstance(content, dict):
        candidates.append(content)
        info = nested_json(content.get("info"))
        if isinstance(info, dict):
            candidates.append(info)
            ivw = nested_json(info.get("ivw"))
            if isinstance(ivw, dict):
                candidates.append(ivw)
    result: dict[str, Any] = {}
    aliases = {
        "angle": ("angle",),
        "beam": ("beam", "physical"),
        "keyword": ("keyword", "text"),
    }
    for output_key, keys in aliases.items():
        for candidate in candidates:
            for key in keys:
                if key in candidate:
                    result[output_key] = candidate[key]
                    break
            if output_key in result:
                break
    return result


class ListenGoDaemon:
    def __init__(self, control_device: str, audio_card_name: str, runtime_dir: str):
        self.control_device = control_device
        self.audio_card_name = audio_card_name
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / "state.json"
        self.socket_path = self.runtime_dir / "control.sock"
        self.stop_event = threading.Event()
        self.serial_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.serial_port: serial.Serial | None = None
        self.message_id = 0
        self.pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.state: dict[str, Any] = {
            "service": "starting",
            "control_device": control_device,
            "control_connected": False,
            "audio_card_name": audio_card_name,
            "audio_device_present": False,
            "firmware": None,
            "last_wake": None,
            "last_message_at": None,
            "last_error": None,
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    def audio_present(self) -> bool:
        try:
            return self.audio_card_name in Path("/proc/asound/cards").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return False

    def update_state(self, **changes: Any) -> None:
        with self.state_lock:
            self.state.update(changes)
            self.state["audio_device_present"] = self.audio_present()
            self.state["updated_at"] = time.time()
            snapshot = dict(self.state)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, self.state_path)

    def next_message_id(self) -> int:
        with self.serial_lock:
            value = self.message_id
            self.message_id = (self.message_id + 1) % 65536
            return value

    def write_message(
        self, message_type: int, payload: bytes, message_id: int | None = None
    ) -> int:
        if message_id is None:
            message_id = self.next_message_id()
        with self.serial_lock:
            if not self.serial_port or not self.serial_port.is_open:
                raise RuntimeError("ListenGo control port is disconnected")
            self.serial_port.write(encode_message(message_type, payload, message_id))
            self.serial_port.flush()
        return message_id

    def send_handshake_ack(self, message_id: int) -> None:
        self.write_message(
            TYPE_HANDSHAKE_ACK, bytes((0xA5, 0x00, 0x00, 0x00)), message_id
        )

    def send_control(
        self, request: dict[str, Any], timeout: float = 3.0
    ) -> dict[str, Any]:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        message_id = self.next_message_id()
        event = threading.Event()
        holder: dict[str, Any] = {}
        with self.pending_lock:
            self.pending[message_id] = (event, holder)
        try:
            self.write_message(TYPE_CONTROL, payload, message_id)
            if not event.wait(timeout):
                raise TimeoutError(f"ListenGo command timed out (id={message_id})")
            return holder.get("response", {})
        finally:
            with self.pending_lock:
                self.pending.pop(message_id, None)

    def handle_message(self, message_type: int, message_id: int, payload: bytes) -> None:
        now = time.time()
        self.update_state(last_message_at=now, last_error=None)
        if message_type == TYPE_HANDSHAKE:
            self.send_handshake_ack(message_id)
            logging.info("Handshake acknowledged (id=%s)", message_id)
            try:
                payload = json.dumps(
                    {"type": "version"}, separators=(",", ":")
                ).encode("utf-8")
                self.write_message(TYPE_CONTROL, payload)
            except Exception:
                logging.debug("Version query after handshake failed", exc_info=True)
            return
        if message_type == TYPE_AUDIO:
            return
        try:
            decoded: Any = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            decoded = {"raw": payload.decode("utf-8", errors="replace")}

        if message_type == TYPE_WAKE:
            wake = extract_wake_fields(decoded)
            wake.update({"received_at": now, "message_id": message_id, "raw": decoded})
            self.update_state(last_wake=wake)
            logging.info(
                "Wake event: angle=%s beam=%s keyword=%s",
                wake.get("angle"),
                wake.get("beam"),
                wake.get("keyword"),
            )

        if message_type == TYPE_CONTROL:
            content = decoded.get("content") if isinstance(decoded, dict) else None
            if isinstance(content, str) and "version:" in content:
                self.update_state(firmware=content)
            with self.pending_lock:
                pending = self.pending.get(message_id)
            if pending:
                event, holder = pending
                holder["response"] = decoded
                event.set()

    def serial_loop(self) -> None:
        buffer = bytearray()
        while not self.stop_event.is_set():
            try:
                port = serial.Serial(
                    self.control_device,
                    115200,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.2,
                )
                with self.serial_lock:
                    self.serial_port = port
                self.update_state(
                    service="running", control_connected=True, last_error=None
                )
                logging.info("Connected to %s", self.control_device)
                buffer.clear()
                time.sleep(0.2)
                try:
                    payload = json.dumps(
                        {"type": "version"}, separators=(",", ":")
                    ).encode("utf-8")
                    self.write_message(TYPE_CONTROL, payload)
                except Exception:
                    logging.debug("Initial version query failed", exc_info=True)

                while not self.stop_event.is_set() and port.is_open:
                    chunk = port.read(max(1, port.in_waiting))
                    if chunk:
                        buffer.extend(chunk)
                    while len(buffer) >= HEADER_SIZE:
                        sync_position = buffer.find(bytes((SYNC, USER_ID)))
                        if sync_position < 0:
                            buffer.clear()
                            break
                        if sync_position:
                            del buffer[:sync_position]
                        if len(buffer) < HEADER_SIZE:
                            break
                        message_type = buffer[2]
                        payload_length, message_id = struct.unpack("<HH", buffer[3:7])
                        if payload_length > MAX_PAYLOAD:
                            del buffer[0]
                            continue
                        total_length = HEADER_SIZE + payload_length + 1
                        if len(buffer) < total_length:
                            break
                        message = bytes(buffer[:total_length])
                        del buffer[:total_length]
                        if checksum(message[:-1]) != message[-1]:
                            logging.warning("Dropped message with bad checksum")
                            continue
                        self.handle_message(
                            message_type, message_id, message[HEADER_SIZE:-1]
                        )
            except Exception as exc:
                logging.warning("Control-port connection lost: %s", exc)
                self.update_state(
                    service="degraded",
                    control_connected=False,
                    last_error=str(exc),
                )
            finally:
                with self.serial_lock:
                    if self.serial_port:
                        try:
                            self.serial_port.close()
                        except Exception:
                            pass
                    self.serial_port = None
                with self.pending_lock:
                    for event, holder in self.pending.values():
                        holder["response"] = {"ok": False, "error": "disconnected"}
                        event.set()
                if not self.stop_event.wait(2.0):
                    continue

    def status_snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            snapshot = dict(self.state)
        snapshot["audio_device_present"] = self.audio_present()
        return snapshot

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command", "status")
        if command == "status":
            return {"ok": True, "state": self.status_snapshot()}
        if command == "version":
            response = self.send_control({"type": "version"})
            return {"ok": True, "response": response, "state": self.status_snapshot()}
        if command in {"manual_wakeup", "set_beam"}:
            beam = int(request.get("beam", 0))
            if not 0 <= beam <= 5:
                raise ValueError("beam must be between 0 and 5")
            response = self.send_control(
                {"type": "manual_wakeup", "content": {"beam": beam}}
            )
            return {"ok": True, "response": response}
        if command == "set_keyword":
            keyword = str(request.get("keyword", "")).strip()
            threshold = int(request.get("threshold", 700))
            if not keyword:
                raise ValueError("keyword is required")
            if not 500 <= threshold <= 1500:
                raise ValueError("threshold must be between 500 and 1500")
            response = self.send_control(
                {
                    "type": "wakeup_keywords",
                    "content": {"keyword": keyword, "threshold": str(threshold)},
                }
            )
            return {
                "ok": True,
                "response": response,
                "notice": "Power-cycle the ListenGo module before the new keyword is active.",
            }
        raise ValueError(f"unknown command: {command}")


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(65536)
        try:
            request = json.loads(raw.decode("utf-8")) if raw else {"command": "status"}
            response = self.server.daemon.handle_request(request)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write(
            (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
        )


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-device", required=True)
    parser.add_argument("--audio-card-name", default="L6Microphone")
    parser.add_argument("--runtime-dir", default="/run/listengo-mic")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    daemon = ListenGoDaemon(
        args.control_device, args.audio_card_name, args.runtime_dir
    )
    daemon.runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        daemon.socket_path.unlink()
    except FileNotFoundError:
        pass
    daemon.update_state(service="starting")
    serial_thread = threading.Thread(target=daemon.serial_loop, daemon=True)
    serial_thread.start()
    server = ThreadedUnixServer(str(daemon.socket_path), ControlHandler)
    server.daemon = daemon  # type: ignore[attr-defined]
    os.chmod(daemon.socket_path, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        daemon.stop_event.set()
        server.shutdown()
        server.server_close()
        serial_thread.join(timeout=3)
        daemon.update_state(service="stopped", control_connected=False)


if __name__ == "__main__":
    main()
