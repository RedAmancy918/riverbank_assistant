#!/usr/bin/env python3
"""Regression tests for expression animation asset preparation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image

from animation_assets import (
    apply_background_mode,
    decode_animation,
    dominant_edge_color,
    fitted_size,
    render_viewport,
)


def main() -> None:
    assert fitted_size((1600, 900), (800, 800)) == (800, 450)
    assert fitted_size((400, 800), (800, 800)) == (400, 800)

    source = Image.new("RGB", (20, 10), (10, 20, 30))
    for x in range(8, 12):
        for y in range(3, 7):
            source.putpixel((x, y), (240, 240, 240))
    assert dominant_edge_color(source) == (10, 20, 30)
    matte, background = apply_background_mode(source, (10, 20, 30), "black_chroma", 24)
    assert background == (0, 0, 0)
    assert matte.getpixel((0, 0)) == (0, 0, 0)
    assert max(matte.getpixel((9, 4))) > 150

    viewport = render_viewport(source, (40, 40), (1, 2, 3), 1.0)
    assert viewport.size == (40, 40)
    assert viewport.getpixel((0, 0)) == (1, 2, 3)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.gif"
        frame_a = Image.new("RGB", (20, 10), (10, 20, 30))
        frame_b = Image.new("RGB", (20, 10), (220, 220, 220))
        frame_a.save(
            path,
            save_all=True,
            append_images=[frame_b],
            duration=[100, 100],
            loop=0,
        )
        decoded = decode_animation(
            "test",
            path,
            (40, 40),
            "original",
            24,
            1.0,
            20.0,
        )
        assert decoded.state == "test"
        assert decoded.size == (40, 40)
        assert len(decoded.frames) == 4
        assert decoded.durations == [0.05] * 4
        assert all(len(frame) == 40 * 40 * 3 for frame in decoded.frames)
    print("animation assets module: regression checks passed")


if __name__ == "__main__":
    main()
