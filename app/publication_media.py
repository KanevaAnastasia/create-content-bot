"""Signed public media URLs used by APIs that pull a video from HTTPS."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from .env_utils import load_env_file


def _signing_key() -> bytes:
    load_env_file()
    value = (os.getenv("PUBLICATION_MEDIA_SIGNING_KEY") or "").strip()
    if not value:
        raise RuntimeError("PUBLICATION_MEDIA_SIGNING_KEY is required for public publication media URLs.")
    return value.encode("utf-8")


def publication_media_token(job_id: object, video_path: str | Path) -> str:
    message = f"{job_id}:{Path(video_path).resolve()}".encode("utf-8")
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()


def publication_media_token_is_valid(job_id: object, video_path: str | Path, token: str) -> bool:
    return hmac.compare_digest(publication_media_token(job_id, video_path), str(token or ""))
