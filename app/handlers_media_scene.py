"""Compatibility shim for legacy `app.handlers_media_scene` imports."""

from __future__ import annotations

from .handlers import handlers_media_scene as _impl
from .vertex_images import generate_image_vertex


class _CompatImageGenerationResult:
    def __init__(self, data: bytes, model: str = "patched", request_id: str = "-"):
        self.data = data
        self.model = model
        self.request_id = request_id


def generate_image_vertex_result(prompt, reference_image=None, *, reference_images=None, request_id=None, model=None):
    try:
        data = generate_image_vertex(
            prompt,
            reference_image,
            reference_images=reference_images,
            request_id=request_id,
            model=model,
        )
    except TypeError as e:
        msg = str(e)
        if "model" not in msg and "request_id" not in msg:
            raise
        try:
            data = generate_image_vertex(
                prompt,
                reference_image,
                reference_images=reference_images,
            )
        except TypeError as inner:
            if "reference_images" not in str(inner):
                raise
            data = generate_image_vertex(prompt, reference_image)
    return _CompatImageGenerationResult(data=data, model=model or "patched", request_id=request_id or "-")


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
    _impl.generate_image_vertex_result = generate_image_vertex_result
    return await _impl._generate_image_with_lenient_fallback(
        prompt,
        reference_image=reference_image,
        reference_images=reference_images,
    )


__all__ = ["_generate_image_with_lenient_fallback", "generate_image_vertex", "generate_image_vertex_result"]
