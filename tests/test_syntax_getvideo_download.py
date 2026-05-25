"""Regression tests for Syntax getvideo HTML → mp4 resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.syntax_getvideo_download import fetch_mp4_bytes_from_getvideo_page


def test_fetch_mp4_from_getvideo_page_parses_html_and_downloads() -> None:
    page_html = '<html><video src="https://cdn.real.test/v/file.mp4?x=1"></html>'
    mp4_bytes = b"x" * 2048

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        if "getvideo" in url:
            resp.text = page_html
            resp.raise_for_status = MagicMock()
            return resp
        if url.startswith("https://cdn.real.test"):
            resp.content = mp4_bytes
            resp.raise_for_status = MagicMock()
            return resp
        raise AssertionError(f"unexpected url {url!r}")

    with patch("app.syntax_getvideo_download.requests.get", side_effect=fake_get):
        out = fetch_mp4_bytes_from_getvideo_page("https://getvideo.syntxai.net/abc", timeout_sec=30)

    assert out == mp4_bytes


def test_fetch_mp4_prefers_video_url_over_example_placeholder() -> None:
    page_html = (
        "Example: <code>?url=https://example.com/video.mp4</code>"
        'const videoUrl = "https:\\/\\/r2.syntx.ai\\/veo3\\/file.mp4";'
    )
    mp4_bytes = b"y" * 2048

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        if "getvideo" in url:
            resp.text = page_html
            resp.raise_for_status = MagicMock()
            return resp
        if url.startswith("https://r2.syntx.ai"):
            resp.content = mp4_bytes
            resp.raise_for_status = MagicMock()
            return resp
        raise AssertionError(f"unexpected url {url!r}")

    with patch("app.syntax_getvideo_download.requests.get", side_effect=fake_get):
        out = fetch_mp4_bytes_from_getvideo_page("https://getvideo.syntxai.net/abc", timeout_sec=30)

    assert out == mp4_bytes


def test_fetch_mp4_raises_when_no_url_in_html() -> None:
    with patch("app.syntax_getvideo_download.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "<html>no mp4 here</html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="did not expose"):
            fetch_mp4_bytes_from_getvideo_page("https://getvideo.syntxai.net/abc")
