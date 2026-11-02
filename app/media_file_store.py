"""Filesystem-backed storage for generated scene media."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

SCENE_MEDIA_ARTIFACTS_DIR_ENV_NAME = "SCENE_MEDIA_ARTIFACTS_DIR"
DEFAULT_SCENE_MEDIA_ARTIFACTS_DIR = "artifacts/scene_media"


def scene_media_artifacts_dir() -> Path:
    raw = (os.getenv(SCENE_MEDIA_ARTIFACTS_DIR_ENV_NAME) or "").strip()
    base = Path(raw or DEFAULT_SCENE_MEDIA_ARTIFACTS_DIR)
    if not base.is_absolute():
        base = Path.cwd() / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_path_part(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    allowed = [ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text]
    cleaned = "".join(allowed).strip("._")
    return cleaned or fallback


def media_file_uri(path: str | Path) -> str:
    path_obj = Path(path)
    if not path_obj.is_absolute():
        path_obj = path_obj.resolve()
    return path_obj.as_uri()


def local_path_from_media_uri(uri: str | None) -> Path | None:
    raw = str(uri or "").strip()
    if not raw.startswith("file://"):
        return None
    parsed = urlparse(raw)
    if not parsed.path:
        return None
    return Path(unquote(parsed.path))


def read_media_uri_bytes(uri: str | None) -> bytes | None:
    path = local_path_from_media_uri(uri)
    if path is None or not path.is_file():
        return None
    return path.read_bytes()


def image_mime_type(data: bytes | bytearray | None) -> str:
    """Detect the MIME type from image bytes instead of trusting a file suffix."""
    payload = bytes(data or b"")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    # Keep compatibility with generated payloads and test doubles whose
    # container cannot be identified from a short prefix.
    return "image/png"


def image_file_extension(data: bytes | bytearray | None) -> str:
    return {
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(image_mime_type(data), "png")


def store_scene_candidate_media(
    *,
    run_id: object,
    scene_id: object,
    candidate_id: object,
    data: bytes,
    extension: str,
) -> str:
    ext = str(extension or "").strip().lower().lstrip(".") or "bin"
    run_part = _safe_path_part(run_id, "run")
    scene_part = _safe_path_part(scene_id, "scene")
    candidate_part = _safe_path_part(candidate_id, "candidate")
    target_dir = scene_media_artifacts_dir() / run_part / scene_part
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{candidate_part}.{ext}"
    target.write_bytes(bytes(data))
    return media_file_uri(target)


def store_project_artifact_media(
    *,
    project_id: object,
    category: object,
    artifact_id: object,
    data: bytes,
    extension: str,
) -> str:
    """Store non-scene guest media inside its isolated project directory."""
    ext = str(extension or "").strip().lower().lstrip(".") or "bin"
    project_part = _safe_path_part(project_id, "project")
    category_part = _safe_path_part(category, "artifacts")
    artifact_part = _safe_path_part(artifact_id, "artifact")
    target_dir = scene_media_artifacts_dir() / "projects" / project_part / category_part
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{artifact_part}.{ext}"
    target.write_bytes(bytes(data))
    return media_file_uri(target)
