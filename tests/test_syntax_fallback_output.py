"""Tests for :mod:`app.syntax_fallback_output`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.syntax_fallback_output import video_bytes_from_fallback_result
from app.syntax_video_fallback import FallbackResult


def test_returns_embedded_bytes_without_network() -> None:
    r = FallbackResult(
        success=True,
        video_bytes=b"\x00" * 2000,
        video_uri=None,
        provider_message="ok",
        attempt_id="a1",
    )
    assert video_bytes_from_fallback_result(r) == b"\x00" * 2000


def test_downloads_direct_mp4_uri() -> None:
    r = FallbackResult(
        success=True,
        video_bytes=None,
        video_uri="https://cdn.example.com/v.mp4",
        provider_message="ok",
        attempt_id="a2",
    )
    mock_resp = MagicMock()
    mock_resp.content = b"y" * 2000
    mock_resp.raise_for_status = MagicMock()
    with patch("app.syntax_fallback_output.requests.get", return_value=mock_resp) as mock_get:
        out = video_bytes_from_fallback_result(r, download_timeout_sec=30)
    mock_get.assert_called_once()
    assert out == b"y" * 2000


def test_downloads_getvideo_via_helper() -> None:
    r = FallbackResult(
        success=True,
        video_bytes=None,
        video_uri="https://getvideo.syntxai.net/abc",
        provider_message="ok",
        attempt_id="a3",
    )
    with patch(
        "app.syntax_fallback_output.fetch_mp4_bytes_from_getvideo_page",
        return_value=b"z" * 2000,
    ) as mock_fetch:
        out = video_bytes_from_fallback_result(r)
    mock_fetch.assert_called_once()
    assert out == b"z" * 2000


def test_raises_when_no_payload() -> None:
    r = FallbackResult(
        success=True,
        video_bytes=None,
        video_uri=None,
        provider_message="ok",
        attempt_id="a4",
    )
    with pytest.raises(RuntimeError, match="no video"):
        video_bytes_from_fallback_result(r)
