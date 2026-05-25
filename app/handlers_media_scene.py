"""Compatibility shim for legacy `app.handlers_media_scene` imports."""

from __future__ import annotations

from .handlers import handlers_media_scene as _impl
from .vertex_images import generate_image_vertex


async def _generate_image_with_lenient_fallback(
    prompt: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
) -> bytes:
    """
    Compatibility wrapper used by tests/legacy imports.

    Keeps monkeypatchability via `app.handlers_media_scene.generate_image_vertex`
    by syncing patched symbol into the implementation module before delegation.
    """
    _impl.generate_image_vertex = generate_image_vertex
    return await _impl._generate_image_with_lenient_fallback(
        prompt,
        reference_image=reference_image,
        reference_images=reference_images,
    )


__all__ = ["_generate_image_with_lenient_fallback", "generate_image_vertex"]
