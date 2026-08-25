#!/usr/bin/env python3
"""Expression animation decoding and matte/viewport preparation."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageSequence


def dominant_edge_color(frame: Image.Image) -> tuple[int, int, int]:
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    band = max(4, min(width, height) // 40)
    boxes = (
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, band, band, height - band),
        (width - band, band, width, height - band),
    )
    colors: Counter[tuple[int, int, int]] = Counter()
    for box in boxes:
        for red, green, blue, alpha in rgba.crop(box).getdata():
            if alpha >= 128:
                colors[(red, green, blue)] += 1
    return colors.most_common(1)[0][0] if colors else (0, 0, 0)


def black_chroma_matte(
    image: Image.Image,
    background: tuple[int, int, int],
    soft_distance: int,
) -> Image.Image:
    """Replace a uniform matte with black and decontaminate antialiased edges."""
    rgb = image.convert("RGB")
    matte = Image.new("RGB", rgb.size, background)
    red_delta, green_delta, blue_delta = ImageChops.difference(rgb, matte).split()
    distance = ImageChops.lighter(ImageChops.lighter(red_delta, green_delta), blue_delta)
    threshold = max(4, min(int(soft_distance), 96))
    alpha_lut = [
        0 if value <= 2 else 255 if value >= threshold else round(255 * value / threshold)
        for value in range(256)
    ]
    alpha = distance.point(alpha_lut)
    channels = []
    for channel, matte_value in zip(rgb.split(), background):
        residual_lut = [round((255 - value) * matte_value / 255) for value in range(256)]
        channels.append(ImageChops.subtract(channel, alpha.point(residual_lut)))
    return Image.merge("RGB", channels)


def apply_background_mode(
    image: Image.Image,
    background: tuple[int, int, int],
    mode: str,
    soft_distance: int,
) -> tuple[Image.Image, tuple[int, int, int]]:
    if mode == "original":
        return image.convert("RGB"), background
    if mode == "black_chroma":
        return black_chroma_matte(image, background, soft_distance), (0, 0, 0)
    raise ValueError(f"unsupported background_mode: {mode}")


def fitted_size(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    source_width, source_height = source
    target_width, target_height = target
    scale = min(target_width / source_width, target_height / source_height)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))


def render_viewport(
    image: Image.Image,
    target_size: tuple[int, int],
    background: tuple[int, int, int],
    display_scale: float,
) -> Image.Image:
    """Pre-render the exact centered/cropped viewport shown on the DSI panel."""
    rendered_size = (
        max(1, round(image.size[0] * display_scale)),
        max(1, round(image.size[1] * display_scale)),
    )
    if image.size != rendered_size:
        image = image.resize(rendered_size, Image.Resampling.LANCZOS)
    viewport = Image.new("RGB", target_size, background)
    left = (target_size[0] - rendered_size[0]) // 2
    top = (target_size[1] - rendered_size[1]) // 2
    viewport.paste(image, (left, top))
    return viewport


@dataclass
class RawAnimation:
    state: str
    size: tuple[int, int]
    frames: list[bytes]
    durations: list[float]
    background: tuple[int, int, int]


@dataclass
class Animation:
    state: str
    frames: list[object]
    durations: list[float]
    background: tuple[int, int, int]
    memory_bytes: int


def decode_animation(
    state: str,
    path: Path,
    target_size: tuple[int, int],
    background_mode: str,
    chroma_soft_distance: int,
    display_scale: float,
    animation_fps: float,
) -> RawAnimation:
    source_frames: list[Image.Image] = []
    source_times: list[float] = []
    source_durations: list[float] = []
    source_time = 0.0
    sample_interval = 1.0 / animation_fps
    with Image.open(path) as source:
        default_duration = max(0.001, float(source.info.get("duration", 100)) / 1000.0)
        background = dominant_edge_color(source.copy())
        fitted = fitted_size(source.size, target_size)
        for frame in ImageSequence.Iterator(source):
            duration = max(
                0.001,
                float(frame.info.get("duration", default_duration * 1000)) / 1000.0,
            )
            image = frame.convert("RGB")
            if image.size != fitted:
                image = image.resize(fitted, Image.Resampling.LANCZOS)
            image, output_background = apply_background_mode(
                image,
                background,
                background_mode,
                chroma_soft_distance,
            )
            image = render_viewport(
                image,
                target_size,
                output_background,
                display_scale,
            )
            source_frames.append(image)
            source_times.append(source_time)
            source_durations.append(duration)
            source_time += duration
    if not source_frames:
        raise ValueError(f"no frames decoded from {path}")
    target_frame_count = max(1, round(source_time * animation_fps))
    frames: list[bytes] = []
    for target_index in range(target_frame_count):
        target_time = min(target_index * sample_interval, source_time - 1e-9)
        source_index = max(0, bisect_right(source_times, target_time) - 1)
        next_index = (source_index + 1) % len(source_frames)
        frame_duration = max(source_durations[source_index], 0.001)
        blend = max(
            0.0,
            min((target_time - source_times[source_index]) / frame_duration, 1.0),
        )
        if blend <= 0.001:
            rendered = source_frames[source_index]
        else:
            rendered = Image.blend(
                source_frames[source_index],
                source_frames[next_index],
                blend,
            )
        frames.append(rendered.tobytes())
    durations = [sample_interval] * target_frame_count
    return RawAnimation(state, target_size, frames, durations, output_background)
