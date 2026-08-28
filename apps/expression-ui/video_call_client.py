#!/usr/bin/env python3
"""Small dependency-light client for the local RiverBank video-call service."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:19734"


def request_json(
    path: str,
    *,
    method: str = "GET",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 1.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=b"{}" if method.upper() == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method.upper(),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_status(
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 0.8,
) -> dict[str, Any]:
    return request_json(
        "/api/v1/status",
        base_url=base_url,
        timeout=timeout,
    )


def activate_call(
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 1.5,
) -> dict[str, Any]:
    return request_json(
        "/api/v1/activate",
        method="POST",
        base_url=base_url,
        timeout=timeout,
    )


def hangup_call(
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 1.5,
) -> dict[str, Any]:
    return request_json(
        "/api/v1/hangup",
        method="POST",
        base_url=base_url,
        timeout=timeout,
    )


def fetch_remote_frame(
    target_size: tuple[int, int],
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 0.8,
) -> bytes:
    from PIL import Image, ImageOps

    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/remote/snapshot.jpg",
        headers={"Accept": "image/jpeg"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            raise RuntimeError("waiting for remote video") from exc
        raise
    if not payload:
        raise RuntimeError("waiting for remote video")
    with Image.open(io.BytesIO(payload)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        fitted = ImageOps.fit(
            image,
            target_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        return fitted.tobytes()
