#!/usr/bin/env python3
"""Single-peer RiverBank WebRTC endpoint for LAN and Tailscale calls."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote

import av
from aiohttp import web
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaPlayer

from attachment_store import (
    DEFAULT_ATTACHMENT_ROOT,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_MESSAGE_ATTACHMENT_BYTES,
    AttachmentStore,
    AttachmentValidationError,
)
from chat_store import DEFAULT_CHAT_DB, ChatStore
from report_library import DEFAULT_REPORTS_DIR, REPORT_EXTENSIONS, ReportLibrary
from task_store import TaskStore


LOGGER = logging.getLogger("riverbank-video-call")
SCHEMA = "riverbank.video-call/v1"
EXPRESSION_SOCKET = Path("/run/riverbank-expression/control.sock")
DEFAULT_TASK_DB = Path("/var/lib/riverbank-tasks/tasks.db")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def is_loopback_request(request: web.Request) -> bool:
    # A local reverse proxy (including Tailscale Serve) also connects from
    # loopback. Forwarding metadata must never inherit local-only privileges.
    if any(
        request.headers.get(name)
        for name in (
            "Forwarded",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "Tailscale-User-Login",
        )
    ):
        return False
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not peer:
        return False
    try:
        return ipaddress.ip_address(str(peer[0])).is_loopback
    except ValueError:
        return False


def send_expression_command(command: str, **payload: Any) -> bool:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.sendto(
            json.dumps(
                {"command": command, **payload},
                ensure_ascii=False,
            ).encode("utf-8"),
            str(EXPRESSION_SOCKET),
        )
        client.close()
        return True
    except OSError as exc:
        LOGGER.warning("expression command failed command=%s error=%s", command, exc)
        return False


class ResizedVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source: MediaStreamTrack, width: int, height: int) -> None:
        super().__init__()
        self.source = source
        self.width = width
        self.height = height

    async def recv(self) -> av.VideoFrame:
        frame = await self.source.recv()
        if not isinstance(frame, av.VideoFrame):
            return frame
        if frame.width == self.width and frame.height == self.height:
            return frame
        resized = frame.reformat(
            width=self.width,
            height=self.height,
            format="yuv420p",
        )
        resized.pts = frame.pts
        resized.time_base = frame.time_base
        return resized

    def stop(self) -> None:
        super().stop()
        self.source.stop()


class PipeWireAudioTrack(MediaStreamTrack):
    """Read shared microphone PCM from PipeWire without opening ALSA directly."""

    kind = "audio"

    def __init__(
        self,
        source: str,
        *,
        sample_rate: int = 48000,
        samples_per_frame: int = 960,
    ) -> None:
        super().__init__()
        self.source = source
        self.sample_rate = sample_rate
        self.samples_per_frame = samples_per_frame
        self.process: asyncio.subprocess.Process | None = None
        self.timestamp = 0

    async def ensure_process(self) -> asyncio.subprocess.Process:
        if self.process is not None:
            return self.process
        command = [
            "/usr/bin/pw-cat",
            "--record",
            "--target",
            self.source,
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
        ]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self.process

    async def recv(self) -> av.AudioFrame:
        process = await self.ensure_process()
        if process.stdout is None:
            raise RuntimeError("PipeWire microphone stdout is unavailable")
        byte_count = self.samples_per_frame * 2
        try:
            payload = await process.stdout.readexactly(byte_count)
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("PipeWire microphone stream ended") from exc
        frame = av.AudioFrame(
            format="s16",
            layout="mono",
            samples=self.samples_per_frame,
        )
        frame.planes[0].update(payload)
        frame.sample_rate = self.sample_rate
        frame.pts = self.timestamp
        frame.time_base = Fraction(1, self.sample_rate)
        self.timestamp += self.samples_per_frame
        return frame

    async def aclose(self) -> None:
        process = self.process
        self.process = None
        super().stop()
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
        super().stop()


def prefer_codecs(pc: RTCPeerConnection, sender: RTCRtpSender) -> None:
    kind = sender.track.kind if sender.track is not None else ""
    if kind not in {"audio", "video"}:
        return
    capabilities = RTCRtpSender.getCapabilities(kind).codecs
    preferred_mime = "video/H264" if kind == "video" else "audio/opus"
    preferred = [codec for codec in capabilities if codec.mimeType == preferred_mime]
    remaining = [codec for codec in capabilities if codec.mimeType != preferred_mime]
    transceiver = next(
        item for item in pc.getTransceivers() if item.sender is sender
    )
    if preferred:
        transceiver.setCodecPreferences(preferred + remaining)


async def wait_for_ice_complete(
    pc: RTCPeerConnection,
    timeout_seconds: float = 8.0,
) -> None:
    if pc.iceGatheringState == "complete":
        return
    event = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_gathering_state_change() -> None:
        if pc.iceGatheringState == "complete":
            event.set()

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        LOGGER.warning("ICE gathering timed out state=%s", pc.iceGatheringState)


class RemoteVideoSink:
    def __init__(self, max_fps: float = 20.0, jpeg_quality: int = 82) -> None:
        self.max_fps = max(1.0, max_fps)
        self.jpeg_quality = max(50, min(jpeg_quality, 95))
        self.latest_jpeg: bytes | None = None
        self.latest_at = 0.0
        self.frame_count = 0
        self.width = 0
        self.height = 0
        self.last_error: str | None = None

    @staticmethod
    def encode_jpeg(frame: av.VideoFrame, quality: int) -> tuple[bytes, int, int]:
        image = frame.to_image()
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=False)
        return output.getvalue(), image.width, image.height

    async def consume(self, track: MediaStreamTrack) -> None:
        minimum_interval = 1.0 / self.max_fps
        while True:
            try:
                frame = await track.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                return
            now = time.monotonic()
            if now - self.latest_at < minimum_interval:
                continue
            try:
                payload, width, height = await asyncio.to_thread(
                    self.encode_jpeg,
                    frame,
                    self.jpeg_quality,
                )
            except Exception as exc:
                self.last_error = str(exc)
                continue
            self.latest_jpeg = payload
            self.latest_at = now
            self.frame_count += 1
            self.width = width
            self.height = height
            self.last_error = None


class PipeWireAudioSink:
    def __init__(self, target: str = "") -> None:
        self.target = target
        self.process: asyncio.subprocess.Process | None = None
        self.frames = 0
        self.last_error: str | None = None

    async def consume(self, track: MediaStreamTrack) -> None:
        command = [
            "/usr/bin/pw-cat",
            "--playback",
            "--format",
            "s16",
            "--rate",
            "48000",
            "--channels",
            "2",
            "--channel-map",
            "stereo",
            "--latency",
            "40ms",
            "--media-role",
            "Communication",
        ]
        if self.target:
            command.extend(["--target", self.target])
        command.append("-")
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
        try:
            while True:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    if self.process.stdin is None:
                        return
                    self.process.stdin.write(bytes(converted.planes[0]))
                    await self.process.stdin.drain()
                    self.frames += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            await self.stop()

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


@dataclass
class CallSession:
    session_id: str
    device_name: str
    pc: RTCPeerConnection
    created_at: float
    camera_player: MediaPlayer | None = None
    microphone_track: PipeWireAudioTrack | None = None
    local_video: MediaStreamTrack | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    audio_sink: PipeWireAudioSink | None = None
    connection_state: str = "new"
    remote_audio: bool = False
    remote_video: bool = False


class RiverBankVideoCallServer:
    def __init__(
        self,
        *,
        token: str,
        state_path: Path,
        camera_url: str,
        audio_source: str,
        audio_sink: str,
        video_width: int,
        video_height: int,
        reports_dir: Path = DEFAULT_REPORTS_DIR,
        task_db: Path = DEFAULT_TASK_DB,
        chat_db: Path = DEFAULT_CHAT_DB,
        attachment_root: Path = DEFAULT_ATTACHMENT_ROOT,
    ) -> None:
        if len(token) < 16:
            raise ValueError("pairing token must contain at least 16 characters")
        self.token = token
        self.state_path = state_path
        self.camera_url = camera_url
        self.audio_source = audio_source
        self.audio_sink = audio_sink
        self.video_width = video_width
        self.video_height = video_height
        self.report_library = ReportLibrary(reports_dir)
        self.task_store = TaskStore(task_db)
        self.chat_store = ChatStore(chat_db)
        self.attachment_store = AttachmentStore(attachment_root)
        self.started_at = time.time()
        self.waiting = False
        self.session: CallSession | None = None
        self.remote_video_sink = RemoteVideoSink()
        self.last_error: str | None = None
        self._closing = False
        self.write_state()

    def authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def state(self) -> dict[str, Any]:
        session = self.session
        latest_age = (
            max(0.0, time.monotonic() - self.remote_video_sink.latest_at)
            if self.remote_video_sink.latest_at
            else None
        )
        return {
            "schema": SCHEMA,
            "started_at": self.started_at,
            "active": session is not None,
            "waiting": self.waiting,
            "session_id": session.session_id if session else None,
            "device_name": session.device_name if session else None,
            "connection_state": session.connection_state if session else "idle",
            "remote_audio": session.remote_audio if session else False,
            "remote_video": session.remote_video if session else False,
            "remote_frame": {
                "available": self.remote_video_sink.latest_jpeg is not None,
                "age_seconds": round(latest_age, 3) if latest_age is not None else None,
                "frames": self.remote_video_sink.frame_count,
                "width": self.remote_video_sink.width,
                "height": self.remote_video_sink.height,
                "last_error": self.remote_video_sink.last_error,
            },
            "media": {
                "camera_url": self.camera_url,
                "audio_source": self.audio_source,
                "audio_sink": self.audio_sink or "default",
                "outgoing_video_size": [self.video_width, self.video_height],
                "transport": "WebRTC DTLS-SRTP",
                "scope": "LAN-or-tailnet",
                "stun": False,
                "turn": False,
            },
            "reports": {
                "available": self.report_library.root.is_dir(),
                "read_only": True,
                "formats": sorted(REPORT_EXTENSIONS),
            },
            "tasks": {
                "available": True,
                "persistent": True,
                "counts": self.task_store.counts(),
            },
            "chat": {
                "available": True,
                "persistent": True,
                "attachments": True,
                "conversation_count": len(self.chat_store.list_conversations(limit=200)),
            },
            "last_error": self.last_error,
            "updated_at": time.time(),
        }

    def write_state(self) -> None:
        atomic_write_json(self.state_path, self.state())

    async def activate(self) -> None:
        self.waiting = True
        self.last_error = None
        send_expression_command("music_control", action="pause")
        send_expression_command("video_call_view", active=True)
        self.write_state()

    async def close_session(self, *, close_view: bool, reason: str) -> None:
        if self._closing:
            return
        self._closing = True
        session = self.session
        self.session = None
        self.waiting = False
        try:
            if session is not None:
                tasks = list(session.tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if session.audio_sink is not None:
                    await session.audio_sink.stop()
                if session.local_video is not None:
                    session.local_video.stop()
                if session.camera_player is not None:
                    if session.camera_player.audio is not None:
                        session.camera_player.audio.stop()
                    if session.camera_player.video is not None:
                        session.camera_player.video.stop()
                if session.microphone_track is not None:
                    await session.microphone_track.aclose()
                await session.pc.close()
            self.remote_video_sink = RemoteVideoSink()
            if close_view:
                send_expression_command("video_call_view", active=False, hangup=False)
            LOGGER.info("call closed reason=%s", reason)
        finally:
            self._closing = False
            self.write_state()

    async def accept_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        sdp = str(payload.get("sdp", ""))
        description_type = str(payload.get("type", "offer"))
        device_name = str(payload.get("device_name", "Windows device")).strip()[:80]
        if not sdp or description_type != "offer":
            raise web.HTTPBadRequest(text="invalid WebRTC offer")
        await self.close_session(close_view=False, reason="replaced")
        self.waiting = False
        self.remote_video_sink = RemoteVideoSink()
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        session = CallSession(
            session_id=uuid.uuid4().hex,
            device_name=device_name or "Windows device",
            pc=pc,
            created_at=time.time(),
        )
        self.session = session

        @pc.on("connectionstatechange")
        def on_connection_state_change() -> None:
            session.connection_state = pc.connectionState
            self.write_state()
            LOGGER.info(
                "connection state session=%s state=%s",
                session.session_id,
                pc.connectionState,
            )
            if pc.connectionState in {"failed", "closed"}:
                asyncio.create_task(
                    self.close_session(close_view=True, reason=pc.connectionState)
                )

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            LOGGER.info("remote track session=%s kind=%s", session.session_id, track.kind)
            if track.kind == "video":
                session.remote_video = True
                task = asyncio.create_task(self.remote_video_sink.consume(track))
            elif track.kind == "audio":
                session.remote_audio = True
                session.audio_sink = PipeWireAudioSink(self.audio_sink)
                task = asyncio.create_task(session.audio_sink.consume(track))
            else:
                return
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
            self.write_state()

        try:
            camera = MediaPlayer(self.camera_url, format="mpjpeg")
            if camera.video is None:
                raise RuntimeError("Camera Hub did not expose a video track")
            session.camera_player = camera
            session.local_video = ResizedVideoTrack(
                camera.video,
                self.video_width,
                self.video_height,
            )
            video_sender = pc.addTrack(session.local_video)
            prefer_codecs(pc, video_sender)

            session.microphone_track = PipeWireAudioTrack(self.audio_source)
            audio_sender = pc.addTrack(session.microphone_track)
            prefer_codecs(pc, audio_sender)

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=description_type)
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await wait_for_ice_complete(pc)
            session.connection_state = pc.connectionState
            send_expression_command("music_control", action="pause")
            send_expression_command("video_call_view", active=True)
            self.last_error = None
            self.write_state()
            local_description = pc.localDescription
            if local_description is None:
                raise RuntimeError("local WebRTC answer was not created")
            return {
                "sdp": local_description.sdp,
                "type": local_description.type,
                "session_id": session.session_id,
                "scope": "LAN-or-tailnet",
            }
        except Exception as exc:
            self.last_error = str(exc)
            LOGGER.exception("failed to accept offer")
            await self.close_session(close_view=True, reason="offer-error")
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    async def shutdown(self) -> None:
        await self.close_session(close_view=True, reason="service-shutdown")


def create_app(server: RiverBankVideoCallServer) -> web.Application:
    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        try:
            if request.method == "OPTIONS":
                response: web.StreamResponse = web.Response(status=204)
            else:
                response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, Idempotency-Key"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Expose-Headers"] = (
            "Content-Disposition, Content-Length, Content-Type"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(
        middlewares=[middleware],
        client_max_size=32 * 1024 * 1024,
    )

    async def healthz(_request: web.Request) -> web.Response:
        state = server.state()
        return web.json_response(
            {
                "ok": True,
                "schema": SCHEMA,
                "active": state["active"],
                "waiting": state["waiting"],
                "connection_state": state["connection_state"],
                "scope": "LAN-or-tailnet",
                "reports": True,
                "reports_available": server.report_library.root.is_dir(),
                "tasks": True,
                "task_counts": await asyncio.to_thread(server.task_store.counts),
                "chat": True,
                "chat_attachments": True,
                "chat_conversation_count": len(
                    await asyncio.to_thread(
                        server.chat_store.list_conversations,
                        limit=200,
                    )
                ),
            }
        )

    async def status(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        return web.json_response(server.state())

    async def activate(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        await server.activate()
        return web.json_response({"ok": True, "state": server.state()})

    async def offer(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        payload = await request.json()
        return web.json_response(await server.accept_offer(payload))

    async def hangup(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        await server.close_session(close_view=True, reason="requested")
        return web.json_response({"ok": True})

    async def snapshot(request: web.Request) -> web.Response:
        if not is_loopback_request(request):
            raise web.HTTPForbidden(text="remote snapshot is local-only")
        payload = server.remote_video_sink.latest_jpeg
        if payload is None:
            return web.Response(status=204)
        return web.Response(body=payload, content_type="image/jpeg")

    async def reports(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid report limit")
        records = await asyncio.to_thread(server.report_library.list_reports, limit)
        return web.json_response(
            {
                "schema": "riverbank.reports/v1",
                "read_only": True,
                "count": len(records),
                "reports": records,
                "updated_at": time.time(),
            }
        )

    async def report_content(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            path, content = await asyncio.to_thread(
                server.report_library.read_report,
                request.match_info["report_id"],
            )
        except FileNotFoundError:
            raise web.HTTPNotFound(text="report not found")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        stat = path.stat()
        return web.json_response(
            {
                "schema": "riverbank.report/v1",
                "id": request.match_info["report_id"],
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                "content": content,
            }
        )

    async def report_download(request: web.Request) -> web.StreamResponse:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            path = server.report_library.resolve(request.match_info["report_id"])
        except FileNotFoundError:
            raise web.HTTPNotFound(text="report not found")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        response = web.FileResponse(path)
        response.content_type = mimetypes.guess_type(path.name)[0] or "text/markdown"
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(path.name)}"
        )
        return response

    async def tasks(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            limit = int(request.query.get("limit", "100"))
            status_filter = request.query.get("status", "").strip()
            records = await asyncio.to_thread(
                server.task_store.list_tasks,
                limit=limit,
                status=status_filter,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {
                "schema": "riverbank.tasks/v1",
                "count": len(records),
                "tasks": records,
                "counts": await asyncio.to_thread(server.task_store.counts),
                "updated_at": time.time(),
            }
        )

    async def create_task(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            idempotency_key = (
                request.headers.get("Idempotency-Key", "").strip()
                or str(payload.get("idempotency_key") or "").strip()
            )
            record, created = await asyncio.to_thread(
                server.task_store.create_task,
                prompt=payload.get("prompt"),
                title=payload.get("title", ""),
                kind=payload.get("kind", "research"),
                source=payload.get("source", "api"),
                device_name=payload.get("device_name", ""),
                idempotency_key=idempotency_key,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {
                "schema": "riverbank.task/v1",
                "created": created,
                "task": record,
            },
            status=201 if created else 200,
        )

    async def task_detail(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            record = await asyncio.to_thread(
                server.task_store.get_task,
                request.match_info["task_id"],
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if record is None:
            raise web.HTTPNotFound(text="task not found")
        return web.json_response({"schema": "riverbank.task/v1", "task": record})

    async def answer_task(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            record = await asyncio.to_thread(
                server.task_store.answer,
                request.match_info["task_id"],
                payload.get("answer"),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if record is None:
            raise web.HTTPNotFound(text="task not found")
        return web.json_response({"schema": "riverbank.task/v1", "task": record})

    async def cancel_task(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        record = await asyncio.to_thread(
            server.task_store.request_cancel,
            request.match_info["task_id"],
        )
        if record is None:
            raise web.HTTPNotFound(text="task not found")
        return web.json_response({"schema": "riverbank.task/v1", "task": record})

    async def chat_conversations(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            limit = int(request.query.get("limit", "100"))
            records = await asyncio.to_thread(
                server.chat_store.list_conversations,
                limit=limit,
                include_internal=request.query.get("include_internal", "") == "1",
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {
                "schema": "riverbank.chat-conversations/v1",
                "count": len(records),
                "conversations": records,
                "updated_at": time.time(),
            }
        )

    async def create_chat_conversation(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            conversation = await asyncio.to_thread(
                server.chat_store.create_conversation,
                title=payload.get("title", ""),
                source=payload.get("source", "api"),
                device_name=payload.get("device_name", ""),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {"schema": "riverbank.chat-conversation/v1", "conversation": conversation},
            status=201,
        )

    async def delete_chat_conversation(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        conversation_id = request.match_info["conversation_id"]
        try:
            messages = await asyncio.to_thread(
                server.chat_store.list_messages,
                conversation_id,
            )
            if any(item["state"] in {"queued", "running"} for item in messages):
                raise web.HTTPConflict(text="stop the active response before deleting")
            deleted = await asyncio.to_thread(
                server.chat_store.delete_conversation,
                conversation_id,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if not deleted:
            raise web.HTTPNotFound(text="conversation not found")
        await asyncio.to_thread(
            server.attachment_store.delete_conversation,
            conversation_id,
        )
        return web.json_response({"ok": True, "deleted": conversation_id})

    async def chat_messages(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        conversation_id = request.match_info["conversation_id"]
        try:
            conversation = await asyncio.to_thread(
                server.chat_store.get_conversation,
                conversation_id,
            )
            if conversation is None:
                raise web.HTTPNotFound(text="conversation not found")
            records = await asyncio.to_thread(
                server.chat_store.list_messages,
                conversation_id,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {
                "schema": "riverbank.chat-messages/v1",
                "conversation": conversation,
                "count": len(records),
                "messages": records,
                "updated_at": time.time(),
            }
        )

    async def create_chat_message(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        conversation_id = request.match_info["conversation_id"]
        saved_attachments: list[dict[str, Any]] = []
        try:
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                content = ""
                total_bytes = 0
                image_count = 0
                async for part in reader:
                    if part.name == "content":
                        content = await part.text()
                        continue
                    if part.name not in {"file", "files"} or not part.filename:
                        await part.read(decode=False)
                        continue
                    if len(saved_attachments) >= MAX_ATTACHMENTS_PER_MESSAGE:
                        raise AttachmentValidationError("单条消息最多上传 4 个附件")
                    payload_bytes = bytearray()
                    while True:
                        chunk = await part.read_chunk(size=256 * 1024)
                        if not chunk:
                            break
                        payload_bytes.extend(chunk)
                        total_bytes += len(chunk)
                        if len(payload_bytes) > MAX_ATTACHMENT_BYTES:
                            raise AttachmentValidationError("单个附件不能超过 15 MB")
                        if total_bytes > MAX_MESSAGE_ATTACHMENT_BYTES:
                            raise AttachmentValidationError("单条消息附件总计不能超过 30 MB")
                    attachment = await asyncio.to_thread(
                        server.attachment_store.save,
                        conversation_id,
                        part.filename,
                        bytes(payload_bytes),
                    )
                    saved_attachments.append(attachment)
                    if attachment["kind"] == "image":
                        image_count += 1
                        if image_count > 1:
                            raise AttachmentValidationError("单条消息最多上传 1 张图片")
            else:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
                content = payload.get("content")
            user_message, assistant_message = await asyncio.to_thread(
                server.chat_store.create_turn,
                conversation_id,
                content=content,
                attachments=saved_attachments,
            )
        except KeyError:
            await asyncio.to_thread(
                server.attachment_store.delete_records,
                saved_attachments,
            )
            raise web.HTTPNotFound(text="conversation not found")
        except RuntimeError as exc:
            await asyncio.to_thread(
                server.attachment_store.delete_records,
                saved_attachments,
            )
            raise web.HTTPConflict(text=str(exc))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            await asyncio.to_thread(
                server.attachment_store.delete_records,
                saved_attachments,
            )
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(
            {
                "schema": "riverbank.chat-turn/v1",
                "user_message": user_message,
                "assistant_message": assistant_message,
            },
            status=202,
        )

    async def chat_attachment(request: web.Request) -> web.StreamResponse:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            attachment = await asyncio.to_thread(
                server.chat_store.get_attachment,
                request.match_info["conversation_id"],
                request.match_info["attachment_id"],
                include_private=True,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if attachment is None:
            raise web.HTTPNotFound(text="attachment not found")
        try:
            path = await asyncio.to_thread(
                server.attachment_store.resolve,
                attachment["storage_path"],
            )
        except FileNotFoundError:
            raise web.HTTPNotFound(text="attachment file not found")
        response = web.FileResponse(path)
        response.content_type = str(attachment["media_type"])
        disposition = "inline" if attachment["kind"] == "image" else "attachment"
        response.headers["Content-Disposition"] = (
            f"{disposition}; filename*=UTF-8''{quote(str(attachment['original_name']))}"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def cancel_chat_message(request: web.Request) -> web.Response:
        if not server.authorized(request):
            raise web.HTTPUnauthorized(text="pairing token required")
        try:
            message = await asyncio.to_thread(
                server.chat_store.request_cancel,
                request.match_info["message_id"],
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if message is None:
            raise web.HTTPNotFound(text="message not found")
        return web.json_response(
            {"schema": "riverbank.chat-message/v1", "message": message}
        )

    async def on_shutdown(_app: web.Application) -> None:
        await server.shutdown()

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/v1/status", status)
    app.router.add_post("/api/v1/activate", activate)
    app.router.add_post("/api/v1/offer", offer)
    app.router.add_post("/api/v1/hangup", hangup)
    app.router.add_get("/api/v1/remote/snapshot.jpg", snapshot)
    app.router.add_get("/api/v1/reports", reports)
    app.router.add_get("/api/v1/reports/{report_id}", report_content)
    app.router.add_get("/api/v1/reports/{report_id}/download", report_download)
    app.router.add_get("/api/v1/tasks", tasks)
    app.router.add_post("/api/v1/tasks", create_task)
    app.router.add_get("/api/v1/tasks/{task_id}", task_detail)
    app.router.add_post("/api/v1/tasks/{task_id}/answer", answer_task)
    app.router.add_post("/api/v1/tasks/{task_id}/cancel", cancel_task)
    app.router.add_get("/api/v1/chats", chat_conversations)
    app.router.add_post("/api/v1/chats", create_chat_conversation)
    app.router.add_delete(
        "/api/v1/chats/{conversation_id}", delete_chat_conversation
    )
    app.router.add_get(
        "/api/v1/chats/{conversation_id}/messages", chat_messages
    )
    app.router.add_post(
        "/api/v1/chats/{conversation_id}/messages", create_chat_message
    )
    app.router.add_get(
        "/api/v1/chats/{conversation_id}/attachments/{attachment_id}",
        chat_attachment,
    )
    app.router.add_post(
        "/api/v1/chats/{conversation_id}/messages/{message_id}/cancel",
        cancel_chat_message,
    )
    app.on_shutdown.append(on_shutdown)
    return app


def read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read pairing token: {path}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19734)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/home/geo/.config/riverbank-video-call/token"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("/run/riverbank-video-call/state.json"),
    )
    parser.add_argument(
        "--camera-url",
        default="http://127.0.0.1:19733/stream",
    )
    parser.add_argument(
        "--audio-source",
        default=(
            "alsa_input.usb-EII_ListenGo_Circular_6-Microphone_"
            "2c000c70f5c2c6e1a11-00.mono-fallback"
        ),
    )
    parser.add_argument("--audio-sink", default="")
    parser.add_argument("--video-width", type=int, default=640)
    parser.add_argument("--video-height", type=int, default=360)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
    )
    parser.add_argument(
        "--task-db",
        type=Path,
        default=DEFAULT_TASK_DB,
    )
    parser.add_argument(
        "--chat-db",
        type=Path,
        default=DEFAULT_CHAT_DB,
    )
    parser.add_argument(
        "--attachment-root",
        type=Path,
        default=DEFAULT_ATTACHMENT_ROOT,
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.verbose:
        logging.getLogger("aioice").setLevel(logging.WARNING)
    if args.self_test:
        print(
            json.dumps(
                {
                    "ok": True,
                    "schema": SCHEMA,
                    "aiortc": __import__("aiortc").__version__,
                    "av": av.__version__,
                    "camera_url": args.camera_url,
                    "audio_source": args.audio_source,
                    "video_size": [args.video_width, args.video_height],
                    "scope": "LAN-or-tailnet",
                    "reports_dir": str(args.reports_dir),
                    "task_db": str(args.task_db),
                    "chat_db": str(args.chat_db),
                    "chat": True,
                    "attachment_root": str(args.attachment_root),
                    "chat_attachments": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    token = read_token(args.token_file)
    server = RiverBankVideoCallServer(
        token=token,
        state_path=args.state_path,
        camera_url=args.camera_url,
        audio_source=args.audio_source,
        audio_sink=args.audio_sink,
        video_width=max(320, min(args.video_width, 1280)),
        video_height=max(180, min(args.video_height, 720)),
        reports_dir=args.reports_dir,
        task_db=args.task_db,
        chat_db=args.chat_db,
        attachment_root=args.attachment_root,
    )
    app = create_app(server)
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        access_log=None,
        handle_signals=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
