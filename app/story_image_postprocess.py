"""Deterministic finishing for historical Story-mode watercolor frames."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageChops, ImageEnhance, ImageStat, UnidentifiedImageError


def is_historical_story_watercolor(scene: dict[str, Any]) -> bool:
    if str(scene.get("generation_mode") or "").strip().lower() != "story":
        return False
    if str(scene.get("time_period") or "").strip().lower() in {"past", "прошлое", "historical", "retro"}:
        return True
    blob = f"{scene.get('era_style') or ''} {scene.get('action') or ''}".lower()
    return any(token in blob for token in ("watercolor", "акварел", "sepia", "сепия", "retro", "ретро"))


def _paper_sheet_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    width, height = rgb.size
    corner = max(2, min(width, height) // 80)
    corners = [
        rgb.crop((0, 0, corner, corner)),
        rgb.crop((width - corner, 0, width, corner)),
        rgb.crop((0, height - corner, corner, height)),
        rgb.crop((width - corner, height - corner, width, height)),
    ]
    means = [ImageStat.Stat(item).mean for item in corners]
    background = tuple(int(round(sum(mean[channel] for mean in means) / len(means))) for channel in range(3))
    bg = Image.new("RGB", rgb.size, background)
    diff = ImageChops.difference(rgb, bg).convert("L")
    # The outside margin is usually near-pure white, while the watercolor
    # paper has a visible warm texture.  Ignore tiny JPEG/PNG noise.
    mask = diff.point(lambda value: 255 if value >= 7 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    left, top, right, bottom = bbox
    margin_ratio = min(left / width, top / height, (width - right) / width, (height - bottom) / height)
    if margin_ratio < 0.008:
        return None
    inset_x = max(1, int((right - left) * 0.008))
    inset_y = max(1, int((bottom - top) * 0.008))
    return (
        min(right - 2, left + inset_x),
        min(bottom - 2, top + inset_y),
        max(left + 2, right - inset_x),
        max(top + 2, bottom - inset_y),
    )


def _cover_crop(image: Image.Image, target_ratio: float = 9 / 16) -> Image.Image:
    width, height = image.size
    ratio = width / max(1, height)
    if abs(ratio - target_ratio) < 0.001:
        return image
    if ratio > target_ratio:
        target_width = max(1, int(round(height * target_ratio)))
        left = max(0, (width - target_width) // 2)
        return image.crop((left, 0, left + target_width, height))
    target_height = max(1, int(round(width / target_ratio)))
    top = max(0, (height - target_height) // 2)
    return image.crop((0, top, width, top + target_height))


def crop_story_reference_border(image_bytes: bytes) -> bytes:
    """Remove an outer photographed/scanned paper margin from a Story reference.

    The character artwork itself is not recolored or resized.  References that
    already reach the image edges are returned byte-for-byte unchanged.
    """
    if not image_bytes:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.load()
            image = source.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return image_bytes
    bbox = _paper_sheet_bbox(image)
    if bbox is None:
        return image_bytes
    image = image.crop(bbox)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def finish_story_historical_frame(image_bytes: bytes, scene: dict[str, Any]) -> bytes:
    """Remove generated paper-sheet margins and restore restrained watercolor color."""
    if not image_bytes or not is_historical_story_watercolor(scene):
        return image_bytes
    with Image.open(io.BytesIO(image_bytes)) as source:
        source.load()
        original_size = source.size
        image = source.convert("RGB")
    bbox = _paper_sheet_bbox(image)
    if bbox is not None:
        image = image.crop(bbox)
    image = _cover_crop(image)
    # Keep the requested muted editorial look, but avoid a flat gray/brown
    # pseudo-sepia result.  This does not invent new content.
    image = ImageEnhance.Color(image).enhance(1.18)
    image = ImageEnhance.Contrast(image).enhance(1.06)
    image = ImageEnhance.Brightness(image).enhance(0.99)
    target_width = original_size[0]
    target_height = max(1, int(round(target_width * 16 / 9)))
    image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
