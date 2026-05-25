"""Download helper for Syntax ``getvideo.syntxai.net`` player pages."""

from __future__ import annotations

import re

import requests


def _fetch_html(url: str, *, timeout_sec: int = 45) -> str:
    """
    Load player HTML.

    Uses ``requests`` (truststore-friendly) because some Python installs lack a
    usable default CA bundle while ``curl``/``requests`` still verify correctly.
    """
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 content-fabric-bot"},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    return resp.text


_PLACEHOLDER_HOSTS = ("example.com", "example.org", "test.com")


def _normalize_js_escaped_url(raw: str) -> str:
    return raw.replace(r"\/", "/")


def _is_placeholder_mp4(url: str) -> bool:
    low = url.lower()
    if not low.endswith(".mp4") and ".mp4?" not in low:
        return True
    return any(h in low for h in _PLACEHOLDER_HOSTS)


def _first_mp4_url(html: str) -> str | None:
    """
    Pick the best direct ``.mp4`` URL from player HTML.

    Pages often include a documentation snippet with ``example.com`` *before* the
    real ``videoUrl`` assignment; we skip placeholder hosts and prefer ``r2`` CDN URLs.
    """
    candidates: list[str] = []

    for m in re.finditer(
        r'videoUrl\s*=\s*"(https?:\\/\\/[^"]+\.mp4[^"]*)";',
        html,
        flags=re.IGNORECASE,
    ):
        candidates.append(_normalize_js_escaped_url(m.group(1)))

    patterns = [
        r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',
        r'"url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r"src=\"(https?://[^\"]+\.mp4[^\"]*)\"",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.IGNORECASE):
            candidates.append(m.group(1) if m.lastindex else m.group(0))

    cleaned: list[str] = []
    seen: set[str] = set()
    for u in candidates:
        u = u.strip().rstrip("</code>'\"")
        u = _normalize_js_escaped_url(u)
        if not u or u in seen:
            continue
        seen.add(u)
        cleaned.append(u)

    for u in cleaned:
        if _is_placeholder_mp4(u):
            continue
        if "r2.syntx.ai" in u.lower():
            return u

    for u in cleaned:
        if not _is_placeholder_mp4(u):
            return u
    return None


def fetch_mp4_bytes_from_getvideo_page(page_url: str, *, timeout_sec: int = 120) -> bytes:
    """
    Fetch an MP4 by loading a ``getvideo.syntxai.net/...`` HTML page and locating a direct ``.mp4`` URL.

    Raises:
        RuntimeError: If the page cannot be loaded or no MP4 URL is found.
    """
    try:
        html = _fetch_html(page_url, timeout_sec=min(45, timeout_sec))
    except requests.RequestException as e:
        raise RuntimeError(f"getvideo page network error: {e}") from e
    mp4 = _first_mp4_url(html)
    if not mp4:
        raise RuntimeError("getvideo page did not expose a direct .mp4 URL in HTML.")
    try:
        vresp = requests.get(
            mp4,
            headers={"User-Agent": "Mozilla/5.0 content-fabric-bot"},
            timeout=timeout_sec,
        )
        vresp.raise_for_status()
        data = vresp.content
    except requests.RequestException as e:
        raise RuntimeError(f"getvideo mp4 download error: {e}") from e
    if not data or len(data) < 1024:
        raise RuntimeError("Downloaded video payload is empty or too small.")
    return data
