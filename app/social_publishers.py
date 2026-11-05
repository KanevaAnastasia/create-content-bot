"""Direct official-API publishing for queued social videos.

Secrets are strictly project-scoped.  For example, project
``grandma_kitchen`` reads only
``PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from sqlalchemy import or_, select

from .db import SessionLocal
from .models import Project, PublicationJob
from .publication_config import project_env, project_env_name, shared_env
from .publish_secret_store import set_project_secrets
from . import zernio_publisher
from .publications import (
    PUBLICATION_STATUS_APPROVED,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_NEEDS_MANUAL_ACTION,
    PUBLICATION_STATUS_PUBLISHED,
    PUBLICATION_STATUS_PUBLISHING,
    PUBLICATION_STATUS_SCHEDULED,
)


DEFAULT_REQUEST_TIMEOUT = (20, 300)
INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS = 7 * 24 * 60 * 60
SUPPORTED_DIRECT_PLATFORMS = frozenset({"youtube", "instagram", "tiktok", "vk"})


class PublicationDeliveryError(RuntimeError):
    """A platform rejected or could not complete a publication request."""


@dataclass(frozen=True, slots=True)
class PlatformReadiness:
    platform: str
    language: str
    ready: bool
    missing: tuple[str, ...]
    note: str = ""


def _required_keys(platform: str, language: str) -> tuple[str, ...]:
    lang = str(language or "ru").strip().upper()
    return {
        "youtube": (
            f"YOUTUBE_CHANNEL_ID_{lang}",
        ),
        "instagram": (
            f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{lang}",
        ),
        "tiktok": ("TIKTOK_OPEN_ID",),
        "vk": (f"VK_GROUP_ID_{lang}",),
    }.get(str(platform or "").strip().lower(), ())


def platform_readiness(project_slug: str, platform: str, language: str) -> PlatformReadiness:
    normalized_platform = str(platform or "").strip().lower()
    normalized_language = str(language or "").strip().lower()
    if normalized_platform == "tiktok":
        provider = project_env(project_slug, "TIKTOK_PROVIDER", "direct")
        if provider == "zernio":
            missing = tuple(project_env_name(project_slug, key) for key in zernio_publisher.CONFIG_KEYS
                            if not project_env(project_slug, key))
            return PlatformReadiness(normalized_platform, normalized_language, not missing, missing,
                                     "Zernio · TikTok (free-only)" if not missing else "")
        if provider != "direct":
            return PlatformReadiness(normalized_platform, normalized_language, False, (), "Unknown TikTok provider")
    required = _required_keys(normalized_platform, normalized_language)
    missing = [project_env_name(project_slug, key) for key in required if not project_env(project_slug, key)]
    if normalized_platform == "youtube":
        lang = normalized_language.upper()
        for key in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
            if not (
                project_env(project_slug, f"{key}_{lang}")
                or project_env(project_slug, key)
            ):
                missing.append(project_env_name(project_slug, f"{key}_{lang}"))
    if normalized_platform == "instagram":
        lang = normalized_language.upper()
        if not (
            project_env(project_slug, f"INSTAGRAM_ACCESS_TOKEN_{lang}")
            or project_env(project_slug, "INSTAGRAM_ACCESS_TOKEN")
        ):
            missing.append(project_env_name(project_slug, f"INSTAGRAM_ACCESS_TOKEN_{lang}"))
    if normalized_platform == "tiktok" and not project_env(project_slug, "TIKTOK_ACCESS_TOKEN"):
        for key in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"):
            if not project_env(project_slug, key):
                missing.append(project_env_name(project_slug, key))
    if normalized_platform == "vk":
        if not (
            project_env(project_slug, "VK_USER_ID")
            or project_env(project_slug, "VK_USER_SCREEN_NAME")
        ):
            missing.append(project_env_name(project_slug, "VK_USER_ID"))
        if not project_env(project_slug, "VK_ACCESS_TOKEN"):
            oauth_keys = ("VK_CLIENT_ID", "VK_REFRESH_TOKEN", "VK_DEVICE_ID", "VK_REDIRECT_URI")
            missing.extend(
                project_env_name(project_slug, key)
                for key in oauth_keys
                if not project_env(project_slug, key)
            )
    supported = normalized_platform in SUPPORTED_DIRECT_PLATFORMS
    return PlatformReadiness(
        platform=normalized_platform,
        language=normalized_language,
        ready=supported and not missing,
        missing=tuple(missing),
        note="Official direct API" if supported and not missing else ("Unsupported platform" if not supported else ""),
    )


def _response_json(response: requests.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise PublicationDeliveryError(f"{context}: invalid JSON response ({response.status_code})") from exc
    if not response.ok:
        detail = (payload.get("error") or payload) if isinstance(payload, dict) else payload
        raise PublicationDeliveryError(f"{context}: HTTP {response.status_code}: {detail}")
    return payload if isinstance(payload, dict) else {}


def _youtube_env(project_slug: str, key: str, language: str) -> str:
    lang = str(language or "ru").strip().upper()
    return project_env(project_slug, f"{key}_{lang}") or project_env(project_slug, key)


def _youtube_access_token(project_slug: str, language: str) -> str:
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": _youtube_env(project_slug, "YOUTUBE_CLIENT_ID", language),
            "client_secret": _youtube_env(project_slug, "YOUTUBE_CLIENT_SECRET", language),
            "refresh_token": _youtube_env(project_slug, "YOUTUBE_REFRESH_TOKEN", language),
            "grant_type": "refresh_token",
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _response_json(response, "YouTube OAuth refresh")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise PublicationDeliveryError("YouTube OAuth refresh returned no access_token.")
    return token


def _set_youtube_thumbnail(access_token: str, video_id: str, cover_path: str | Path) -> str:
    """Apply a custom thumbnail after a YouTube video finishes processing."""
    path = Path(cover_path)
    if not path.is_file():
        return "cover_missing"
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status_response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "processingDetails", "id": video_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        status_payload = _response_json(status_response, "YouTube processing status")
        items = status_payload.get("items") if isinstance(status_payload.get("items"), list) else []
        details = items[0].get("processingDetails") if items and isinstance(items[0], dict) else {}
        status = str((details or {}).get("processingStatus") or "").lower()
        if status == "succeeded":
            break
        if status in {"failed", "terminated"}:
            return f"processing_{status}"
        time.sleep(5)
    else:
        return "processing_timeout"

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as handle:
        response = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
            params={"videoId": video_id},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": mime_type,
                "Content-Length": str(path.stat().st_size),
            },
            data=handle,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    _response_json(response, "YouTube custom thumbnail")
    return "applied"


def _publish_youtube(job: PublicationJob, project_slug: str) -> str:
    token = _youtube_access_token(project_slug, str(job.language or "ru"))
    headers = {"Authorization": f"Bearer {token}"}
    channel_response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id", "mine": "true"},
        headers=headers,
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    channel_payload = _response_json(channel_response, "YouTube channel check")
    actual_ids = {str(row.get("id") or "") for row in channel_payload.get("items", []) if isinstance(row, dict)}
    expected_id = project_env(project_slug, f"YOUTUBE_CHANNEL_ID_{job.language.upper()}")
    if expected_id not in actual_ids:
        raise PublicationDeliveryError(
            f"YouTube OAuth belongs to {sorted(actual_ids) or ['unknown channel']}, expected {expected_id}."
        )

    video_path = Path(job.video_path)
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    privacy = shared_env("YOUTUBE_PRIVACY_STATUS", "public").lower()
    if privacy not in {"private", "unlisted", "public"}:
        privacy = "public"
    metadata = {
        "snippet": {
            "title": str(job.title or job.description or video_path.stem)[:100],
            "description": str(job.description or "")[:5000],
            "categoryId": shared_env("YOUTUBE_CATEGORY_ID", "22"),
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    init_response = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
        headers={
            **headers,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(video_path.stat().st_size),
            "X-Upload-Content-Type": mime_type,
        },
        json=metadata,
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    if not init_response.ok:
        _response_json(init_response, "YouTube upload initialization")
    upload_url = str(init_response.headers.get("Location") or "").strip()
    if not upload_url:
        raise PublicationDeliveryError("YouTube did not return a resumable upload URL.")
    with video_path.open("rb") as handle:
        upload_response = requests.put(
            upload_url,
            headers={**headers, "Content-Type": mime_type, "Content-Length": str(video_path.stat().st_size)},
            data=handle,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    upload_payload = _response_json(upload_response, "YouTube video upload")
    video_id = str(upload_payload.get("id") or "").strip()
    if not video_id:
        raise PublicationDeliveryError("YouTube upload completed without a video id.")
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    cover_path = str(payload.get("cover_path") or "").strip()
    if cover_path:
        try:
            cover_status = _set_youtube_thumbnail(token, video_id, cover_path)
        except Exception as exc:
            cover_status = f"failed:{type(exc).__name__}"
        job.payload_json = {**payload, "cover_delivery": cover_status}
    return video_id


def _refresh_instagram_access_token(
    project_slug: str,
    language: str,
    access_token: str,
    *,
    language_scoped: bool,
) -> str:
    lang = str(language or "ru").strip().upper()
    app_id = (
        project_env(project_slug, f"INSTAGRAM_APP_ID_{lang}")
        or project_env(project_slug, "INSTAGRAM_APP_ID")
    )
    app_secret = (
        project_env(project_slug, f"INSTAGRAM_APP_SECRET_{lang}")
        or project_env(project_slug, "INSTAGRAM_APP_SECRET")
    )
    if not app_id or not app_secret:
        raise PublicationDeliveryError(
            f"Instagram {lang} token is near expiry, but its app id or app secret is not configured."
        )
    version = shared_env("META_GRAPH_API_VERSION", "v25.0")
    try:
        response = requests.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": access_token,
            },
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise PublicationDeliveryError(
            f"Instagram {lang} token refresh request failed ({type(exc).__name__})."
        ) from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise PublicationDeliveryError(
            f"Instagram {lang} token refresh returned invalid JSON ({response.status_code})."
        ) from exc
    refreshed_token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
    if not response.ok or not refreshed_token:
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        raise PublicationDeliveryError(
            f"Instagram {lang} token refresh failed with HTTP {response.status_code}"
            f"{f' (code {error_code})' if error_code else ''}."
        )

    suffix = f"_{lang}" if language_scoped else ""
    values = {f"INSTAGRAM_ACCESS_TOKEN{suffix}": refreshed_token}
    expires_in = payload.get("expires_in")
    try:
        expires_in_seconds = int(expires_in) if expires_in is not None else 0
    except (TypeError, ValueError):
        expires_in_seconds = 0
    if expires_in_seconds > 0:
        values[f"INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT{suffix}"] = str(
            int(time.time()) + expires_in_seconds
        )
    set_project_secrets(project_slug, values)
    return refreshed_token


def _instagram_access_token(project_slug: str, language: str) -> str:
    lang = str(language or "ru").strip().upper()
    scoped_token = project_env(project_slug, f"INSTAGRAM_ACCESS_TOKEN_{lang}")
    language_scoped = bool(scoped_token)
    access_token = scoped_token or project_env(project_slug, "INSTAGRAM_ACCESS_TOKEN")
    if not access_token:
        return ""
    expiry_key = (
        f"INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_{lang}"
        if language_scoped
        else "INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT"
    )
    expires_at_raw = project_env(project_slug, expiry_key)
    if not expires_at_raw:
        return access_token
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return access_token
    if expires_at > int(time.time()) + INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS:
        return access_token
    return _refresh_instagram_access_token(
        project_slug,
        lang,
        access_token,
        language_scoped=language_scoped,
    )


def refresh_expiring_instagram_tokens(
    project_slugs: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    """Refresh configured language-scoped Meta tokens before they expire."""
    if project_slugs is None:
        with SessionLocal() as db:
            project_slugs = tuple(
                db.scalars(select(Project.slug).where(Project.is_active.is_(True))).all()
            )
    results: list[dict[str, str]] = []
    for project_slug in project_slugs:
        normalized_slug = str(project_slug or "").strip()
        if not normalized_slug:
            continue
        for language in ("ru", "en"):
            lang = language.upper()
            token = project_env(normalized_slug, f"INSTAGRAM_ACCESS_TOKEN_{lang}")
            expires_at = project_env(
                normalized_slug,
                f"INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_{lang}",
            )
            if not token or not expires_at:
                continue
            try:
                refreshed = _instagram_access_token(normalized_slug, language)
            except Exception as exc:
                results.append(
                    {
                        "project": normalized_slug,
                        "language": language,
                        "status": "error",
                        "detail": type(exc).__name__,
                    }
                )
            else:
                results.append(
                    {
                        "project": normalized_slug,
                        "language": language,
                        "status": "refreshed" if refreshed != token else "valid",
                    }
                )
    return results


def _publish_instagram(job: PublicationJob, project_slug: str) -> str:
    version = shared_env("META_GRAPH_API_VERSION", "v25.0")
    account_id = project_env(project_slug, f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{job.language.upper()}")
    access_token = _instagram_access_token(project_slug, job.language)
    base_url = f"https://graph.facebook.com/{version}"
    video_path = Path(job.video_path)
    if not video_path.is_file():
        raise PublicationDeliveryError(f"Instagram video does not exist: {video_path}")
    video_path = _instagram_compatible_video(video_path)
    file_size = video_path.stat().st_size
    if file_size > 1_000_000_000:
        raise PublicationDeliveryError("Instagram Reels video exceeds the 1 GB API limit.")

    # Facebook Login for Business supports Meta's resumable upload flow.  It lets
    # us send the local MP4 directly instead of exposing the admin or media through
    # a public HTTPS URL.
    create_data = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": str(job.description or "")[:2200],
        "access_token": access_token,
    }
    is_trial_reel = (
        str(job.language or "").strip().lower() == "ru"
        and bool((job.payload_json or {}).get("instagram_trial"))
    )
    if is_trial_reel:
        create_data["trial_params"] = json.dumps(
            {"graduation_strategy": "MANUAL"},
            separators=(",", ":"),
        )
    else:
        create_data["share_to_feed"] = "true"
    create_response = requests.post(
        f"{base_url}/{account_id}/media",
        data=create_data,
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    create_payload = _response_json(create_response, "Instagram reel container")
    creation_id = str(create_payload.get("id") or "")
    upload_url = str(create_payload.get("uri") or "")
    if not creation_id or not upload_url:
        raise PublicationDeliveryError("Instagram did not return a creation id and upload URI.")
    parsed_upload_url = urlparse(upload_url)
    if parsed_upload_url.scheme != "https" or parsed_upload_url.hostname != "rupload.facebook.com":
        raise PublicationDeliveryError("Instagram returned an unsafe resumable upload URI.")

    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    with video_path.open("rb") as handle:
        upload_response = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": mime_type,
            },
            data=handle,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    _response_json(upload_response, "Instagram resumable video upload")

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        status_response = requests.get(
            f"{base_url}/{creation_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        status_payload = _response_json(status_response, "Instagram container status")
        status = str(status_payload.get("status_code") or "").upper()
        if status == "FINISHED":
            break
        if status in {"ERROR", "EXPIRED"}:
            raise PublicationDeliveryError(f"Instagram container failed: {status_payload.get('status') or status}.")
        time.sleep(5)
    else:
        raise PublicationDeliveryError("Instagram reel processing timed out.")
    publish_response = requests.post(
        f"{base_url}/{account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    media_id = str(_response_json(publish_response, "Instagram reel publish").get("id") or "")
    if not media_id:
        raise PublicationDeliveryError("Instagram publish completed without a media id.")
    # A permalink makes the admin monitor useful after delivery.  Do not turn a
    # successful publish into a failure if Meta delays or withholds the field.
    try:
        permalink_response = requests.get(
            f"{base_url}/{media_id}",
            params={"fields": "permalink", "access_token": access_token},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        permalink_payload = _response_json(permalink_response, "Instagram media permalink")
        permalink = str(permalink_payload.get("permalink") or "").strip()
        if permalink.startswith("https://"):
            job.payload_json = {**(job.payload_json or {}), "external_post_url": permalink}
    except Exception:
        pass
    return media_id


def _instagram_compatible_video(video_path: Path) -> Path:
    """Create and cache a conservative Instagram-compatible MP4.

    Final builds are intentionally 4K for YouTube, while Instagram's publishing
    API rejects video wider than 1920 px and video above its bitrate ceiling with
    an opaque ``ProcessingFailedError``.  Normalizing here also covers MP4 files
    uploaded manually through the publication page.
    """

    try:
        from .ai_approval_service import _resolve_ffmpeg
        from .post_production import _video_dimensions

        width, height = _video_dimensions(video_path)
        ffmpeg = _resolve_ffmpeg()
    except Exception:
        width = height = None
        ffmpeg = None
    # Invalid tiny fixtures and environments without ffmpeg retain the existing
    # behavior; real media with known dimensions is normalized exactly once.
    if not width or not height or not ffmpeg:
        return video_path

    output_path = video_path.with_name(f"{video_path.stem}.instagram.mp4")
    try:
        if (
            output_path.is_file()
            and output_path.stat().st_size > 0
            and output_path.stat().st_mtime_ns >= video_path.stat().st_mtime_ns
        ):
            return output_path
    except OSError:
        pass

    temp_path = output_path.with_name(
        f".{output_path.stem}.{uuid.uuid4().hex}.tmp.mp4"
    )
    scale_filter = "scale=w='min(1080,iw)':h=-2:flags=lanczos"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        scale_filter,
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-maxrate",
        "20M",
        "-bufsize",
        "40M",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(temp_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        os.replace(temp_path, output_path)
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        detail = str(getattr(exc, "stderr", "") or exc).strip()
        raise PublicationDeliveryError(
            f"Instagram video normalization failed: {detail[:1000]}"
        ) from exc
    return output_path


def _tiktok_access_token(project_slug: str) -> str:
    client_key = project_env(project_slug, "TIKTOK_CLIENT_KEY")
    client_secret = project_env(project_slug, "TIKTOK_CLIENT_SECRET")
    refresh_token = project_env(project_slug, "TIKTOK_REFRESH_TOKEN")
    if not all((client_key, client_secret, refresh_token)):
        token = project_env(project_slug, "TIKTOK_ACCESS_TOKEN")
        if not token:
            raise PublicationDeliveryError("TikTok OAuth credentials are not configured.")
        return token
    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _response_json(response, "TikTok OAuth refresh")
    if payload.get("error"):
        raise PublicationDeliveryError(
            f"TikTok OAuth refresh: {payload.get('error_description') or payload.get('error')}"
        )
    access_token = str(payload.get("access_token") or "").strip()
    rotated_refresh_token = str(payload.get("refresh_token") or "").strip()
    open_id = str(payload.get("open_id") or "").strip()
    expected_open_id = project_env(project_slug, "TIKTOK_OPEN_ID")
    if expected_open_id and open_id and open_id != expected_open_id:
        raise PublicationDeliveryError(
            f"TikTok token belongs to {open_id}, expected {expected_open_id}."
        )
    if not access_token or not rotated_refresh_token:
        raise PublicationDeliveryError("TikTok OAuth refresh returned incomplete credentials.")
    values = {
        "TIKTOK_ACCESS_TOKEN": access_token,
        "TIKTOK_REFRESH_TOKEN": rotated_refresh_token,
    }
    if open_id:
        values["TIKTOK_OPEN_ID"] = open_id
    set_project_secrets(project_slug, values)
    return access_token


def _tiktok_chunk_plan(total_size: int) -> tuple[int, int]:
    if total_size <= 0:
        raise PublicationDeliveryError("TikTok cannot upload an empty video file.")
    chunk_size = total_size if total_size <= 64 * 1024 * 1024 else 64 * 1024 * 1024
    return chunk_size, max(1, total_size // chunk_size)


def tiktok_creator_info(project_slug: str, *, access_token: str | None = None) -> dict[str, Any]:
    """Return the current creator posting capabilities for the connected account."""
    if project_env(project_slug, "TIKTOK_PROVIDER", "direct") == "zernio" and access_token is None:
        return zernio_publisher.creator_info(project_slug)
    access_token = access_token or _tiktok_access_token(project_slug)
    response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _response_json(response, "TikTok creator info")
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error.get("code") not in {None, "", "ok"}:
        raise PublicationDeliveryError(f"TikTok creator info: {error}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "creator_nickname": str(data.get("creator_nickname") or ""),
        "creator_username": str(data.get("creator_username") or ""),
        "creator_avatar_url": str(data.get("creator_avatar_url") or ""),
        "privacy_level_options": [
            str(value)
            for value in data.get("privacy_level_options", [])
            if str(value or "").strip()
        ],
        "comment_disabled": bool(data.get("comment_disabled")),
        "duet_disabled": bool(data.get("duet_disabled")),
        "stitch_disabled": bool(data.get("stitch_disabled")),
        "max_video_post_duration_sec": int(data.get("max_video_post_duration_sec") or 0),
    }


def _publish_tiktok(job: PublicationJob, project_slug: str) -> str:
    access_token = _tiktok_access_token(project_slug)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
    creator_data = tiktok_creator_info(project_slug, access_token=access_token)
    allowed_privacy = creator_data["privacy_level_options"]
    job_options = job.payload_json if isinstance(job.payload_json, dict) else {}
    requested_privacy = str(
        job_options.get("tiktok_privacy_level")
        or shared_env("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
    )
    if requested_privacy not in allowed_privacy:
        raise PublicationDeliveryError(
            "TikTok privacy level is unavailable for this creator. Refresh the creator settings and choose again."
        )
    music_confirmed = job_options.get("tiktok_music_usage_confirmed")
    if music_confirmed is False:
        raise PublicationDeliveryError("TikTok music usage must be confirmed before publishing.")
    video_path = Path(job.video_path)
    total_size = video_path.stat().st_size
    chunk_size, chunk_count = _tiktok_chunk_plan(total_size)
    # TikTok deliberately defines the count as floor(video_size/chunk_size).
    # Any trailing bytes are appended to the final chunk (which may be up to
    # 128 MB), rather than reported as an extra undersized chunk.
    init_response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=headers,
        json={
            "post_info": {
                "title": str(job.description or job.title or "")[:2200],
                "privacy_level": requested_privacy,
                "disable_duet": bool(creator_data["duet_disabled"]) or not bool(
                    job_options.get("tiktok_allow_duet", False)
                ),
                "disable_comment": bool(creator_data["comment_disabled"]) or not bool(
                    job_options.get("tiktok_allow_comment", False)
                ),
                "disable_stitch": bool(creator_data["stitch_disabled"]) or not bool(
                    job_options.get("tiktok_allow_stitch", False)
                ),
                "brand_content_toggle": bool(job_options.get("tiktok_brand_content", False)),
                "brand_organic_toggle": bool(job_options.get("tiktok_brand_organic", False)),
                "is_aigc": bool(
                    job_options.get("tiktok_is_aigc", shared_env("TIKTOK_IS_AIGC", "1") != "0")
                ),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": total_size,
                "chunk_size": chunk_size,
                "total_chunk_count": chunk_count,
            },
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    init_payload = _response_json(init_response, "TikTok publish initialization")
    init_error = init_payload.get("error") if isinstance(init_payload.get("error"), dict) else {}
    if init_error.get("code") not in {None, "", "ok"}:
        raise PublicationDeliveryError(f"TikTok publish initialization: {init_error}")
    data = init_payload.get("data") if isinstance(init_payload.get("data"), dict) else {}
    publish_id = str(data.get("publish_id") or "")
    upload_url = str(data.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise PublicationDeliveryError("TikTok did not return publish_id/upload_url.")
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    with video_path.open("rb") as handle:
        for index in range(chunk_count):
            start = index * chunk_size
            bytes_to_read = total_size - start if index == chunk_count - 1 else chunk_size
            payload = handle.read(bytes_to_read)
            end = start + len(payload) - 1
            upload_response = requests.put(
                upload_url,
                headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(payload)),
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                },
                data=payload,
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
            if not upload_response.ok:
                raise PublicationDeliveryError(
                    f"TikTok video upload chunk {index + 1}/{chunk_count}: HTTP {upload_response.status_code}."
                )
    return publish_id


def _vk_error_message(payload: dict[str, Any], context: str) -> None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return
    code = error.get("error_code")
    message = str(error.get("error_msg") or "VK API error")
    suffix = f" (code {code})" if code is not None else ""
    raise PublicationDeliveryError(f"{context}: {message}{suffix}")


def _vk_access_token(project_slug: str) -> str:
    client_id = project_env(project_slug, "VK_CLIENT_ID")
    refresh_token = project_env(project_slug, "VK_REFRESH_TOKEN")
    device_id = project_env(project_slug, "VK_DEVICE_ID")
    redirect_uri = project_env(project_slug, "VK_REDIRECT_URI")
    if not all((client_id, refresh_token, device_id, redirect_uri)):
        token = project_env(project_slug, "VK_ACCESS_TOKEN")
        if not token:
            raise PublicationDeliveryError("VK user OAuth credentials are not configured.")
        return token

    state = secrets.token_urlsafe(32)
    response = requests.post(
        "https://id.vk.ru/oauth2/auth",
        params={
            "grant_type": "refresh_token",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "device_id": device_id,
            "state": state,
        },
        data={"refresh_token": refresh_token},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _response_json(response, "VK ID OAuth refresh")
    if payload.get("error"):
        raise PublicationDeliveryError(
            f"VK ID OAuth refresh: {payload.get('error_description') or payload.get('error')}"
        )
    if str(payload.get("state") or "") != state:
        raise PublicationDeliveryError("VK ID OAuth refresh returned an invalid state.")
    access_token = str(payload.get("access_token") or "").strip()
    rotated_refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not rotated_refresh_token:
        raise PublicationDeliveryError("VK ID OAuth refresh returned incomplete credentials.")
    values = {"VK_ACCESS_TOKEN": access_token, "VK_REFRESH_TOKEN": rotated_refresh_token}
    returned_device_id = str(payload.get("device_id") or "").strip()
    if returned_device_id:
        values["VK_DEVICE_ID"] = returned_device_id
    set_project_secrets(project_slug, values)
    return access_token


def _verify_vk_identity(project_slug: str, *, token: str, group_id: str, api_version: str) -> None:
    expected_user_id = project_env(project_slug, "VK_USER_ID").strip()
    expected_screen_name = project_env(project_slug, "VK_USER_SCREEN_NAME").lstrip("@").lower()
    user_response = requests.post(
        "https://api.vk.ru/method/users.get",
        data={"access_token": token, "v": api_version, "fields": "screen_name"},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    user_payload = _response_json(user_response, "VK users.get")
    _vk_error_message(user_payload, "VK users.get")
    users = user_payload.get("response") if isinstance(user_payload.get("response"), list) else []
    user = users[0] if users and isinstance(users[0], dict) else {}
    actual_user_id = str(user.get("id") or "").strip()
    actual_screen_name = str(user.get("screen_name") or "").lstrip("@").lower()
    if expected_user_id and actual_user_id != expected_user_id:
        raise PublicationDeliveryError(
            f"VK token belongs to user {actual_user_id or 'unknown'}, expected {expected_user_id}."
        )
    if not expected_user_id and (
        not expected_screen_name or actual_screen_name != expected_screen_name
    ):
        raise PublicationDeliveryError(
            f"VK token belongs to @{actual_screen_name or 'unknown'}, expected @{expected_screen_name or 'unconfigured'}."
        )

    group_response = requests.post(
        "https://api.vk.ru/method/groups.getById",
        data={
            "access_token": token,
            "v": api_version,
            "group_id": group_id,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    group_payload = _response_json(group_response, "VK groups.getById")
    _vk_error_message(group_payload, "VK groups.getById")
    response = group_payload.get("response") if isinstance(group_payload.get("response"), dict) else {}
    groups = response.get("groups") if isinstance(response.get("groups"), list) else []
    group = next(
        (
            item
            for item in groups
            if isinstance(item, dict) and str(item.get("id") or "") == str(group_id)
        ),
        None,
    )
    if not isinstance(group, dict):
        raise PublicationDeliveryError(
            f"VK did not return configured group {group_id}."
        )


def _publish_vk(job: PublicationJob, project_slug: str) -> str:
    token = _vk_access_token(project_slug)
    language = str(job.language or "ru").upper()
    group_id = project_env(project_slug, f"VK_GROUP_ID_{language}")
    api_version = shared_env("VK_API_VERSION", "5.199")
    _verify_vk_identity(project_slug, token=token, group_id=group_id, api_version=api_version)
    save_response = requests.post(
        "https://api.vk.ru/method/video.save",
        data={
            "access_token": token,
            "v": api_version,
            "group_id": group_id,
            "name": str(job.title or job.description or "Video")[:128],
            "description": str(job.description or "")[:5000],
            # Upload first.  VK may allow an administrator to upload video while
            # rejecting video.save(wallpost=1) with code 15.  Posting to the wall
            # is therefore a separate best-effort operation below.
            "wallpost": 0,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    save_payload = _response_json(save_response, "VK video.save")
    _vk_error_message(save_payload, "VK video.save")
    data = save_payload.get("response") if isinstance(save_payload.get("response"), dict) else {}
    upload_url = str(data.get("upload_url") or "")
    if not upload_url:
        raise PublicationDeliveryError("VK video.save did not return upload_url.")
    # VK's upload host may time out while receiving the 4K/30+ Mbps master.
    # Reuse the cached 1080x1920 H.264 delivery copy prepared for Reels.
    video_path = _instagram_compatible_video(Path(job.video_path))
    with video_path.open("rb") as handle:
        upload_response = requests.post(
            upload_url,
            files={"video_file": (video_path.name, handle, mimetypes.guess_type(video_path.name)[0] or "video/mp4")},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    upload_payload = _response_json(upload_response, "VK video upload")
    video_id = upload_payload.get("video_id") or data.get("video_id")
    owner_id = upload_payload.get("owner_id") or data.get("owner_id")
    external_id = (
        f"{owner_id}_{video_id}"
        if owner_id is not None and video_id is not None
        else str(video_id or "uploaded")
    )
    if owner_id is not None and video_id is not None:
        payload = {**(job.payload_json or {})}
        payload["external_post_url"] = f"https://vk.com/video{external_id}"
        try:
            wall_response = requests.post(
                "https://api.vk.ru/method/wall.post",
                data={
                    "access_token": token,
                    "v": api_version,
                    "owner_id": f"-{group_id}",
                    "from_group": 1,
                    "message": str(job.description or "")[:5000],
                    "attachments": f"video{external_id}",
                },
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
            wall_payload = _response_json(wall_response, "VK wall.post")
            _vk_error_message(wall_payload, "VK wall.post")
            wall_data = wall_payload.get("response") if isinstance(wall_payload.get("response"), dict) else {}
            post_id = wall_data.get("post_id")
            if post_id is not None:
                payload["vk_wall_post_id"] = str(post_id)
                payload["external_post_url"] = f"https://vk.com/wall-{group_id}_{post_id}"
        except PublicationDeliveryError as exc:
            # The video is already safely published in the community.  Surface
            # the partial wall-post failure without turning the upload into a
            # misleading full failure.
            payload["vk_wall_post_error"] = str(exc)
        job.payload_json = payload
    return external_id


def publish_publication_job(job_id: str | uuid.UUID) -> dict[str, Any]:
    with SessionLocal() as session:
        job = session.scalar(
            select(PublicationJob)
            .where(PublicationJob.id == uuid.UUID(str(job_id)))
            .with_for_update()
        )
        if job is None:
            raise PublicationDeliveryError(f"Publication job not found: {job_id}")
        if job.status == PUBLICATION_STATUS_PUBLISHED:
            return {"job_id": str(job.id), "status": job.status, "external_post_id": job.external_post_id}
        if job.status == PUBLICATION_STATUS_PUBLISHING:
            return {"job_id": str(job.id), "status": job.status}
        predecessor_id = str((job.payload_json or {}).get("publish_after_job_id") or "").strip()
        if predecessor_id:
            try:
                predecessor_uuid = uuid.UUID(predecessor_id)
            except ValueError:
                predecessor_uuid = None
            predecessor = (
                session.scalar(select(PublicationJob).where(PublicationJob.id == predecessor_uuid))
                if predecessor_uuid is not None
                else None
            )
            if predecessor is None:
                job.status = PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
                job.publish_error = "Previous publication in the sequence was not found."
                session.commit()
                return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
            if predecessor.status != PUBLICATION_STATUS_PUBLISHED:
                if predecessor.status in {
                    PUBLICATION_STATUS_FAILED,
                    PUBLICATION_STATUS_NEEDS_MANUAL_ACTION,
                }:
                    job.status = PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
                job.publish_error = (
                    f"Waiting for previous publication {predecessor.id}: {predecessor.status}."
                )
                session.commit()
                return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        now = datetime.now(timezone.utc)
        scheduled_at = job.scheduled_at
        if scheduled_at is not None and scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        if scheduled_at is not None and scheduled_at > now:
            return {"job_id": str(job.id), "status": PUBLICATION_STATUS_SCHEDULED, "scheduled_at": scheduled_at.isoformat()}
        if job.platform == "tiktok":
            pinned = (job.payload_json or {}).get("tiktok_provider")
            configured = project_env(job.project.slug, "TIKTOK_PROVIDER", "direct")
            if pinned and pinned != configured:
                job.status = PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
                job.publish_error = "TikTok provider changed after submission. Reconcile the old publication before retrying."
                session.commit()
                return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        readiness = platform_readiness(job.project.slug, job.platform, job.language)
        if not readiness.ready:
            job.status = PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
            job.publish_error = readiness.note or f"Missing configuration: {', '.join(readiness.missing)}"
            session.commit()
            return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        video_path = Path(job.video_path)
        if not video_path.is_file():
            job.status = PUBLICATION_STATUS_FAILED
            job.publish_error = f"Video file not found: {video_path}"
            session.commit()
            return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        job.status = PUBLICATION_STATUS_PUBLISHING
        job.publish_error = None
        job.attempt_count = int(job.attempt_count or 0) + 1
        if job.platform == "tiktok":
            payload = job.payload_json or {}
            # Pin the transport for retries even if project settings later change.
            job.payload_json = {**payload, "tiktok_provider": payload.get("tiktok_provider")
                                or project_env(job.project.slug, "TIKTOK_PROVIDER", "direct")}
        session.commit()
        try:
            if job.platform == "tiktok" and job.payload_json.get("tiktok_provider") == "zernio":
                external_post_id = zernio_publisher.publish(job, job.project.slug, session.commit)
            else:
                external_post_id = {
                    "youtube": _publish_youtube,
                    "instagram": _publish_instagram,
                    "tiktok": _publish_tiktok,
                    "vk": _publish_vk,
                }[job.platform](job, job.project.slug)
        except (zernio_publisher.ZernioPending, zernio_publisher.ZernioNeedsReview) as exc:
            job.status = (PUBLICATION_STATUS_PUBLISHING if isinstance(exc, zernio_publisher.ZernioPending)
                          else PUBLICATION_STATUS_NEEDS_MANUAL_ACTION)
            job.publish_error = str(exc)[:4000]
            session.commit()
            return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        except Exception as exc:
            job.status = PUBLICATION_STATUS_FAILED
            job.publish_error = f"{type(exc).__name__}: {exc}"[:4000]
            session.commit()
            return {"job_id": str(job.id), "status": job.status, "error": job.publish_error}
        job.status = PUBLICATION_STATUS_PUBLISHED
        job.external_post_id = str(external_post_id or "") or None
        job.published_at = datetime.now(timezone.utc)
        job.publish_error = None
        session.commit()
        return {"job_id": str(job.id), "status": job.status, "external_post_id": job.external_post_id}


def publish_publication_jobs(job_ids: Iterable[str | uuid.UUID]) -> list[dict[str, Any]]:
    return [publish_publication_job(job_id) for job_id in job_ids]


def publish_due_publication_jobs(limit: int = 20) -> list[dict[str, Any]]:
    refresh_zernio_publications(limit=limit)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        rows = session.scalars(
            select(PublicationJob)
            .where(PublicationJob.status.in_((PUBLICATION_STATUS_APPROVED, PUBLICATION_STATUS_SCHEDULED)))
            .order_by(PublicationJob.created_at.asc())
            .limit(max(1, int(limit or 20)))
        ).all()
        ids = [row.id for row in rows if row.scheduled_at is None or row.scheduled_at <= now]
    return publish_publication_jobs(ids)


def refresh_zernio_publications(limit: int = 20) -> None:
    """Reconcile asynchronous posts and late TikTok permalinks without publishing."""
    with SessionLocal() as session:
        jobs = session.scalars(
            select(PublicationJob).where(
                PublicationJob.platform == "tiktok",
                PublicationJob.status.in_((PUBLICATION_STATUS_PUBLISHING, PUBLICATION_STATUS_PUBLISHED)),
                PublicationJob.payload_json["tiktok_provider"].as_string() == "zernio",
                or_(
                    PublicationJob.payload_json["zernio_post_id"].as_string().is_not(None),
                    PublicationJob.updated_at < datetime.now(timezone.utc) - timedelta(minutes=15),
                ),
                PublicationJob.payload_json["external_post_url"].as_string().is_(None),
            ).order_by(PublicationJob.updated_at.asc()).limit(max(1, limit))
        ).all()
        for job in jobs:
            try:
                external_id = zernio_publisher.refresh(job, job.project.slug, session.commit)
            except zernio_publisher.ZernioPending as exc:
                job.publish_error = str(exc)
            except zernio_publisher.ZernioNeedsReview as exc:
                job.status = PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
                job.publish_error = str(exc)
            except zernio_publisher.ZernioError as exc:
                if (job.payload_json or {}).get("zernio_status") in {"failed", "error", "cancelled", "canceled"}:
                    job.status = PUBLICATION_STATUS_FAILED
                job.publish_error = str(exc)
            else:
                job.status = PUBLICATION_STATUS_PUBLISHED
                job.external_post_id = external_id
                job.published_at = job.published_at or datetime.now(timezone.utc)
                job.publish_error = None
            job.updated_at = datetime.now(timezone.utc)
            session.commit()
