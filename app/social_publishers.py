"""Direct official-API publishing for queued social videos.

Secrets are strictly project-scoped.  For example, project
``grandma_kitchen`` reads only
``PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN``.
"""

from __future__ import annotations

import math
import mimetypes
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from sqlalchemy import select

from .db import SessionLocal
from .models import PublicationJob
from .publication_config import project_env, project_env_name, shared_env
from .publish_secret_store import set_project_secrets
from .publications import (
    PUBLICATION_STATUS_APPROVED,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_NEEDS_MANUAL_ACTION,
    PUBLICATION_STATUS_PUBLISHED,
    PUBLICATION_STATUS_PUBLISHING,
    PUBLICATION_STATUS_SCHEDULED,
)


DEFAULT_REQUEST_TIMEOUT = (20, 300)
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
            "YOUTUBE_CLIENT_ID",
            "YOUTUBE_CLIENT_SECRET",
            "YOUTUBE_REFRESH_TOKEN",
            f"YOUTUBE_CHANNEL_ID_{lang}",
        ),
        "instagram": (
            "INSTAGRAM_ACCESS_TOKEN",
            f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{lang}",
        ),
        "tiktok": ("TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID"),
        "vk": ("VK_USER_SCREEN_NAME", f"VK_GROUP_ID_{lang}"),
    }.get(str(platform or "").strip().lower(), ())


def platform_readiness(project_slug: str, platform: str, language: str) -> PlatformReadiness:
    normalized_platform = str(platform or "").strip().lower()
    normalized_language = str(language or "").strip().lower()
    required = _required_keys(normalized_platform, normalized_language)
    missing = [project_env_name(project_slug, key) for key in required if not project_env(project_slug, key)]
    if normalized_platform == "vk" and not project_env(project_slug, "VK_ACCESS_TOKEN"):
        oauth_keys = ("VK_CLIENT_ID", "VK_REFRESH_TOKEN", "VK_DEVICE_ID", "VK_REDIRECT_URI")
        missing.extend(
            project_env_name(project_slug, key)
            for key in oauth_keys
            if not project_env(project_slug, key)
        )
    if normalized_platform == "instagram":
        for shared_key in ("CONTENT_FABRIC_PUBLIC_BASE_URL", "PUBLICATION_MEDIA_SIGNING_KEY"):
            if not shared_env(shared_key):
                missing.append(shared_key)
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
        detail = payload.get("error") if isinstance(payload, dict) else payload
        raise PublicationDeliveryError(f"{context}: HTTP {response.status_code}: {detail}")
    return payload if isinstance(payload, dict) else {}


def _youtube_access_token(project_slug: str) -> str:
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": project_env(project_slug, "YOUTUBE_CLIENT_ID"),
            "client_secret": project_env(project_slug, "YOUTUBE_CLIENT_SECRET"),
            "refresh_token": project_env(project_slug, "YOUTUBE_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _response_json(response, "YouTube OAuth refresh")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise PublicationDeliveryError("YouTube OAuth refresh returned no access_token.")
    return token


def _publish_youtube(job: PublicationJob, project_slug: str) -> str:
    token = _youtube_access_token(project_slug)
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
    privacy = shared_env("YOUTUBE_PRIVACY_STATUS", "private").lower()
    if privacy not in {"private", "unlisted", "public"}:
        privacy = "private"
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
    return video_id


def _publication_media_url(job: PublicationJob) -> str:
    from .publication_media import publication_media_token

    base = shared_env("CONTENT_FABRIC_PUBLIC_BASE_URL").rstrip("/")
    if not base.startswith("https://"):
        raise PublicationDeliveryError("Instagram requires an HTTPS CONTENT_FABRIC_PUBLIC_BASE_URL.")
    token = publication_media_token(job.id, job.video_path)
    return f"{base}/publication-media/{job.id}/{quote(token)}/video.mp4"


def _publish_instagram(job: PublicationJob, project_slug: str) -> str:
    version = shared_env("META_GRAPH_API_VERSION", "v24.0")
    account_id = project_env(project_slug, f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{job.language.upper()}")
    access_token = project_env(project_slug, "INSTAGRAM_ACCESS_TOKEN")
    base_url = f"https://graph.facebook.com/{version}"
    create_response = requests.post(
        f"{base_url}/{account_id}/media",
        data={
            "media_type": "REELS",
            "video_url": _publication_media_url(job),
            "caption": str(job.description or "")[:2200],
            "share_to_feed": "true",
            "access_token": access_token,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    creation_id = str(_response_json(create_response, "Instagram reel container").get("id") or "")
    if not creation_id:
        raise PublicationDeliveryError("Instagram did not return a creation id.")
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
    return media_id


def _publish_tiktok(job: PublicationJob, project_slug: str) -> str:
    access_token = project_env(project_slug, "TIKTOK_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
    creator_response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers=headers,
        json={},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    creator = _response_json(creator_response, "TikTok creator info")
    creator_error = creator.get("error") if isinstance(creator.get("error"), dict) else {}
    if creator_error.get("code") not in {None, "", "ok"}:
        raise PublicationDeliveryError(f"TikTok creator info: {creator_error}")
    creator_data = creator.get("data") if isinstance(creator.get("data"), dict) else {}
    allowed_privacy = [str(item) for item in creator_data.get("privacy_level_options", [])]
    requested_privacy = shared_env("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
    privacy = requested_privacy if requested_privacy in allowed_privacy else (
        "SELF_ONLY" if "SELF_ONLY" in allowed_privacy else (allowed_privacy[0] if allowed_privacy else "SELF_ONLY")
    )
    video_path = Path(job.video_path)
    total_size = video_path.stat().st_size
    if total_size <= 0:
        raise PublicationDeliveryError("TikTok cannot upload an empty video file.")
    chunk_size = total_size if total_size <= 64 * 1024 * 1024 else 64 * 1024 * 1024
    chunk_count = max(1, math.ceil(total_size / chunk_size))
    init_response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers=headers,
        json={
            "post_info": {
                "title": str(job.description or job.title or "")[:2200],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": shared_env("TIKTOK_IS_AIGC", "1") != "0",
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
            payload = handle.read(min(chunk_size, total_size - start))
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
    expected_screen_name = project_env(project_slug, "VK_USER_SCREEN_NAME").lstrip("@").lower()
    user_response = requests.post(
        "https://api.vk.ru/method/users.get",
        data={"access_token": token, "v": api_version, "fields": "screen_name"},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    user_payload = _response_json(user_response, "VK users.get")
    _vk_error_message(user_payload, "VK users.get")
    users = user_payload.get("response") if isinstance(user_payload.get("response"), list) else []
    actual_screen_name = str(users[0].get("screen_name") or "").lstrip("@").lower() if users else ""
    if not actual_screen_name or actual_screen_name != expected_screen_name:
        raise PublicationDeliveryError(
            f"VK token belongs to @{actual_screen_name or 'unknown'}, expected @{expected_screen_name}."
        )

    groups_response = requests.post(
        "https://api.vk.ru/method/groups.get",
        data={
            "access_token": token,
            "v": api_version,
            "filter": "admin",
            "extended": 1,
            "count": 1000,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    groups_payload = _response_json(groups_response, "VK groups.get")
    _vk_error_message(groups_payload, "VK groups.get")
    response = groups_payload.get("response") if isinstance(groups_payload.get("response"), dict) else {}
    items = response.get("items") if isinstance(response.get("items"), list) else []
    managed_group_ids = {str(item.get("id")) for item in items if isinstance(item, dict)}
    if str(group_id) not in managed_group_ids:
        raise PublicationDeliveryError(
            f"VK user @{expected_screen_name} is not an administrator of configured group {group_id}."
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
            "wallpost": 1,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    save_payload = _response_json(save_response, "VK video.save")
    _vk_error_message(save_payload, "VK video.save")
    data = save_payload.get("response") if isinstance(save_payload.get("response"), dict) else {}
    upload_url = str(data.get("upload_url") or "")
    if not upload_url:
        raise PublicationDeliveryError("VK video.save did not return upload_url.")
    video_path = Path(job.video_path)
    with video_path.open("rb") as handle:
        upload_response = requests.post(
            upload_url,
            files={"video_file": (video_path.name, handle, mimetypes.guess_type(video_path.name)[0] or "video/mp4")},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
    upload_payload = _response_json(upload_response, "VK video upload")
    video_id = upload_payload.get("video_id") or data.get("video_id")
    owner_id = upload_payload.get("owner_id") or data.get("owner_id")
    return f"{owner_id}_{video_id}" if owner_id is not None and video_id is not None else str(video_id or "uploaded")


def publish_publication_job(job_id: str | uuid.UUID) -> dict[str, Any]:
    with SessionLocal() as session:
        job = session.scalar(select(PublicationJob).where(PublicationJob.id == uuid.UUID(str(job_id))))
        if job is None:
            raise PublicationDeliveryError(f"Publication job not found: {job_id}")
        if job.status == PUBLICATION_STATUS_PUBLISHED:
            return {"job_id": str(job.id), "status": job.status, "external_post_id": job.external_post_id}
        now = datetime.now(timezone.utc)
        scheduled_at = job.scheduled_at
        if scheduled_at is not None and scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        if scheduled_at is not None and scheduled_at > now:
            return {"job_id": str(job.id), "status": PUBLICATION_STATUS_SCHEDULED, "scheduled_at": scheduled_at.isoformat()}
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
        session.commit()
        try:
            external_post_id = {
                "youtube": _publish_youtube,
                "instagram": _publish_instagram,
                "tiktok": _publish_tiktok,
                "vk": _publish_vk,
            }[job.platform](job, job.project.slug)
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
