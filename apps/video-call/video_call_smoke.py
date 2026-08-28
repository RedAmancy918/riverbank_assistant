#!/usr/bin/env python3
"""End-to-end silent WebRTC smoke test for the local RiverBank endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from aiohttp import ClientSession
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack


async def wait_for_ice_complete(
    peer: RTCPeerConnection,
    timeout_seconds: float = 8.0,
) -> None:
    if peer.iceGatheringState == "complete":
        return
    event = asyncio.Event()

    @peer.on("icegatheringstatechange")
    def on_state() -> None:
        if peer.iceGatheringState == "complete":
            event.set()

    await asyncio.wait_for(event.wait(), timeout=timeout_seconds)


async def wait_for_connection(
    peer: RTCPeerConnection,
    timeout_seconds: float = 12.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while peer.connectionState not in {"connected", "failed", "closed"}:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"connection timeout state={peer.connectionState}")
        await asyncio.sleep(0.1)
    if peer.connectionState != "connected":
        raise RuntimeError(f"connection failed state={peer.connectionState}")


async def run(base_url: str, token: str) -> dict[str, object]:
    peer = RTCPeerConnection()
    peer.addTrack(AudioStreamTrack())
    peer.addTrack(VideoStreamTrack())
    received = {"audio": 0, "video": 0}
    receiver_tasks: list[asyncio.Task[None]] = []

    @peer.on("track")
    def on_track(track: object) -> None:
        async def receive_one() -> None:
            await track.recv()
            received[track.kind] = received.get(track.kind, 0) + 1

        receiver_tasks.append(asyncio.create_task(receive_one()))

    headers = {"Authorization": f"Bearer {token}"}
    try:
        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        await wait_for_ice_complete(peer)
        local = peer.localDescription
        if local is None:
            raise RuntimeError("client offer missing")
        async with ClientSession(headers=headers) as client:
            async with client.post(
                f"{base_url.rstrip('/')}/api/v1/offer",
                json={
                    "sdp": local.sdp,
                    "type": local.type,
                    "device_name": "RiverBank smoke test",
                },
            ) as response:
                response.raise_for_status()
                answer = await response.json()
            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )
            await wait_for_connection(peer)
            was_connected = True
            for _ in range(30):
                if len(receiver_tasks) >= 2:
                    break
                await asyncio.sleep(0.1)
            if len(receiver_tasks) < 2:
                raise RuntimeError("server audio/video tracks were not received")
            await asyncio.wait_for(asyncio.gather(*receiver_tasks), timeout=8.0)

            remote_frame = False
            status: dict[str, object] = {}
            for _ in range(40):
                async with client.get(
                    f"{base_url.rstrip('/')}/api/v1/status"
                ) as response:
                    response.raise_for_status()
                    status = await response.json()
                remote_frame = bool(
                    (status.get("remote_frame") or {}).get("available")
                )
                if remote_frame:
                    break
                await asyncio.sleep(0.1)
            async with client.post(
                f"{base_url.rstrip('/')}/api/v1/hangup",
                json={},
            ) as response:
                response.raise_for_status()
        return {
            "ok": was_connected and remote_frame and all(received.values()),
            "received": received,
            "remote_frame": remote_frame,
            "server_connection": status.get("connection_state"),
        }
    finally:
        for task in receiver_tasks:
            if not task.done():
                task.cancel()
        await peer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19734")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/home/geo/.config/riverbank-video-call/token"),
    )
    args = parser.parse_args()
    result = asyncio.run(
        run(args.base_url, args.token_file.read_text(encoding="utf-8").strip())
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
