"""Normalize scene frames before sending them to Syntax (Telethon)."""

from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image


def _syntax_min_edge_px() -> int:
    raw = (os.getenv("SYNTAX_MIN_IMAGE_EDGE_PX") or "300").strip()
    try:
        n = int(raw)
    except ValueError:
        return 300
    return max(1, n)


def prepare_syntax_frame_bytes(data: bytes) -> bytes:
    """
    Ensure image dimensions are at least ``SYNTAX_MIN_IMAGE_EDGE_PX`` (default 300)
    on both edges, preserving aspect ratio. Output is PNG bytes.

    Raises:
        RuntimeError: if Pillow is missing or bytes are not a decodable image.
    """
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(
            "Pillow is required to resize frames for Syntax. "
            "Install with: python -m pip install pillow"
        ) from exc

    min_edge = _syntax_min_edge_px()
    buf = io.BytesIO(data)
    with Image.open(buf) as im:
        im.load()
        rgb = im.convert("RGB")
        w, h = rgb.size
        if w >= min_edge and h >= min_edge:
            out = io.BytesIO()
            rgb.save(out, format="PNG", optimize=True)
            return out.getvalue()
        scale = max(min_edge / float(w), min_edge / float(h))
        new_w = max(min_edge, int(round(w * scale)))
        new_h = max(min_edge, int(round(h * scale)))
        resized = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        resized.save(out, format="PNG", optimize=True)
        return out.getvalue()
