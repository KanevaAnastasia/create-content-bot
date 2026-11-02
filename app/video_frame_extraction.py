"""Small ffmpeg helpers used by continuity-aware guest workflows."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .ai_approval_service import _resolve_ffmpeg


def extract_last_video_frame(
    *,
    video_bytes: bytes | None = None,
    video_path: str | os.PathLike[str] | None = None,
) -> bytes:
    """Return the final decoded video frame as PNG bytes."""
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    with tempfile.TemporaryDirectory(prefix="guest_video_last_frame_") as tmp:
        tmp_path = Path(tmp)
        source = Path(video_path) if video_path is not None else tmp_path / "source.mp4"
        if video_path is None:
            if not video_bytes:
                raise RuntimeError("video bytes are empty")
            source.write_bytes(video_bytes)
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError("video file is empty or missing")
        output = tmp_path / "last_frame.png"
        # Decode the final second and repeatedly overwrite one PNG. The file
        # left on disk is therefore the last decodable frame, not a midpoint.
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-sseof",
                "-1",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "scale='min(1080,iw)':-2",
                "-fps_mode",
                "passthrough",
                "-update",
                "1",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError("ffmpeg did not extract the final frame")
        return output.read_bytes()
