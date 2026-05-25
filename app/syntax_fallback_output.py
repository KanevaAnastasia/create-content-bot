"""Resolve :class:`FallbackResult` into raw MP4 bytes (in-memory download if needed)."""

from __future__ import annotations

import requests

from .syntax_getvideo_download import fetch_mp4_bytes_from_getvideo_page
from .syntax_video_fallback import FallbackResult


def video_bytes_from_fallback_result(
    result: FallbackResult,
    *,
    download_timeout_sec: int = 300,
) -> bytes:
    """
    Return video bytes from a successful Syntax fallback result.

    If the provider returned only a URI (direct ``.mp4`` or ``getvideo`` page), downloads it.
    """
    if result.video_bytes:
        return result.video_bytes
    uri = (result.video_uri or "").strip()
    if not uri:
        raise RuntimeError("Fallback has no video bytes and no video URI.")
    low = uri.lower()
    if "getvideo.syntxai.net/" in low:
        return fetch_mp4_bytes_from_getvideo_page(uri, timeout_sec=download_timeout_sec)
    resp = requests.get(
        uri,
        headers={"User-Agent": "Mozilla/5.0 content-fabric-syntax-fallback"},
        timeout=download_timeout_sec,
    )
    resp.raise_for_status()
    data = resp.content
    if not data or len(data) < 1024:
        raise RuntimeError("Downloaded video is empty or too small.")
    return data
