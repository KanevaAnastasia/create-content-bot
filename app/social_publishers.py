"""Direct official-API publishing for queued social videos.

Secrets are strictly project-scoped.  For example, project
``grandma_kitchen`` reads only
``PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN``. VK user OAuth can be
explicitly shared by projects with the same pinned user ID; community keys
and publication destinations always remain project-scoped.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import requests
from sqlalchemy import or_, select

from .db import SessionLocal
from .models import Project, PublicationJob
from .publication_config import project_env, project_env_name, shared_env
from .publish_secret_store import set_project_secrets
from .vk_credentials import locked_vk_credentials
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


class PublicationNeedsReview(PublicationDeliveryError):
    """A remote write may have succeeded; do not create another copy."""


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
        wall_key = f"VK_COMMUNITY_TOKEN_{normalized_language.upper()}"
        if not project_env(project_slug, wall_key):
            missing.append(project_env_name(project_slug, wall_key))
        if not (
            project_env(project_slug, "VK_USER_ID")
            or project_env(project_slug, "VK_USER_SCREEN_NAME")
        ):
            missing.append(project_env_name(project_slug, "VK_USER_ID"))
        try:
            credentials_project = _vk_credentials_project(project_slug)
        except PublicationDeliveryError as exc:
            return PlatformReadiness(normalized_platform, normalized_language, False, (), str(exc))
        if not project_env(credentials_project, "VK_ACCESS_TOKEN"):
            oauth_keys = ("VK_CLIENT_ID", "VK_REFRESH_TOKEN", "VK_DEVICE_ID", "VK_REDIRECT_URI")
            missing.extend(
                project_env_name(credentials_project, key)
                for key in oauth_keys
                if not project_env(credentials_project, key)
            )
    supported = normalized_platform in SUPPORTED_DIRECT_PLATFORMS
    return PlatformReadiness(
        platform=normalized_platform,
        language=normalized_language,
        ready=supported and not missing,
        missing=tuple(missing),
        note=("VK · видео + стена + сториз" if normalized_platform == "vk" else "Official direct API")
        if supported and not missing else ("Unsupported platform" if not supported else ""),
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
    # Meta debug_token uses an explicit zero for no fixed token expiry. This is
    # distinct from revoked permissions or the separate data-access expiry.
    if expires_at == 0 or expires_at > int(time.time()) + INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS:
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


def _verify_instagram_publish_identity(base_url: str, access_token: str, account_id: str) -> dict[str, str]:
    """Reading an IG account does not prove that a Page token can publish to it."""

    def get(path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{base_url}/{path}", params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # requests exceptions can contain input_token in the debug URL.
            raise PublicationDeliveryError(f"Instagram identity check failed ({type(exc).__name__}).") from exc
        if not response.ok or not isinstance(payload, dict):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            raise PublicationNeedsReview(f"Instagram identity check denied (HTTP {response.status_code}, code {code}); reconnect this account.")
        return payload

    debug = get("debug_token", {"input_token": access_token}).get("data") or {}
    if debug.get("is_valid") is not True or "instagram_content_publish" not in (debug.get("scopes") or []):
        raise PublicationNeedsReview("Instagram token is invalid or lacks instagram_content_publish; reconnect this account.")
    token_type = str(debug.get("type") or "").upper()
    if token_type == "PAGE":
        page = get("me", {"fields": "id,name,instagram_business_account"})
        linked_id = str((page.get("instagram_business_account") or {}).get("id") or "")
        if linked_id != account_id:
            raise PublicationNeedsReview(
                f"Instagram Page token belongs to '{page.get('name') or page.get('id')}' "
                f"(linked Instagram {linked_id or 'none'}), not configured account {account_id}. "
                "Connect the correct Page token or an authorized user token for this project/language."
            )
    for grant in (debug.get("granular_scopes") or []) if token_type != "PAGE" else []:
        if grant.get("scope") == "instagram_content_publish" and "target_ids" in grant:
            targets = {str(value) for value in grant.get("target_ids") or []}
            authorized = account_id in targets
            # Facebook Login grants can list Page IDs, while Business Login can
            # list IG IDs. Resolve the linked IG account before rejecting a grant.
            if not authorized:
                for target in targets:
                    try:
                        page = get(target, {"fields": "id,instagram_business_account"})
                    except PublicationNeedsReview:
                        continue
                    if str((page.get("instagram_business_account") or {}).get("id") or "") == account_id:
                        authorized = True
                        break
            if not authorized:
                raise PublicationNeedsReview("Instagram publishing permission does not include the configured account; reconnect this account.")
    account = get(account_id, {"fields": "id,username"})
    if str(account.get("id") or "") != account_id:
        raise PublicationNeedsReview("Instagram returned an unexpected account; reconnect before publishing.")
    return {"account_id": account_id, "username": str(account.get("username") or ""), "token_type": token_type}


def _publish_instagram(
    job: PublicationJob, project_slug: str, persist: Callable[[], None] | None = None
) -> str:
    version = shared_env("META_GRAPH_API_VERSION", "v25.0")
    account_id = project_env(project_slug, f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{job.language.upper()}")
    access_token = _instagram_access_token(project_slug, job.language)
    base_url = f"https://graph.facebook.com/{version}"
    state = dict((job.payload_json or {}).get("instagram_upload") or {})
    url_fallback_enabled = project_env(project_slug, f"INSTAGRAM_URL_FALLBACK_{job.language.upper()}", "").lower() == "zernio"

    def save(**values: Any) -> None:
        state.update(values)
        job.payload_json = {**(job.payload_json or {}), "instagram_upload": dict(state)}
        if persist:
            persist()

    def container_status() -> str:
        response = requests.get(
            f"{base_url}/{state['creation_id']}",
            params={"fields": "status_code,status"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        payload = _response_json(response, "Instagram container status")
        status = str(payload.get("status_code") or "").upper()
        save(status_code=status, status_detail=str(payload.get("status") or "")[:1000])
        return status

    if state and state.get("account_id") != account_id:
        raise PublicationNeedsReview("Instagram account changed after upload; reconcile the saved container first.")
    if state.get("media_id"):
        return str(state["media_id"])
    identity = _verify_instagram_publish_identity(base_url, access_token, account_id)
    save(**identity)
    video_path = Path(job.video_path)
    if not video_path.is_file():
        raise PublicationDeliveryError(f"Instagram video does not exist: {video_path}")
    # A rejected direct upload is definitive (unlike a lost response), so the
    # next explicit retry may use an even smaller delivery profile.  This stays
    # within Meta's official direct API and avoids downgrading every normal Reel.
    recovery_profile = (
        state.get("stage") == "upload_rejected"
        and not state.get("publish_ambiguous")
    )
    video_path = _instagram_compatible_video(video_path, recovery_profile=recovery_profile)
    file_size = video_path.stat().st_size
    if file_size > 1_000_000_000:
        raise PublicationDeliveryError("Instagram Reels video exceeds the 1 GB API limit.")
    if len(str(job.description or "")) > 2200:
        raise PublicationDeliveryError("Instagram caption exceeds 2200 characters; shorten it before publishing.")

    # Facebook Login for Business supports Meta's resumable upload flow.  It lets
    # us send the local MP4 directly instead of exposing the admin or media through
    # a public HTTPS URL.
    create_data = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": str(job.description or ""),
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
    fingerprint = hashlib.sha256(json.dumps({
        "path": str(video_path.resolve()), "size": file_size,
        "mtime": video_path.stat().st_mtime_ns,
        "caption": create_data["caption"], "trial": is_trial_reel,
    }, sort_keys=True).encode()).hexdigest()
    # A definitive rupload rejection selects the stricter recovery derivative
    # for a *new* container.  Meta can nevertheless finish the original
    # container after returning that rejection.  In that case the saved
    # fingerprint still names the normal derivative, and treating the newly
    # selected recovery path as a user content change would strand a ready
    # container.  Check the other deterministic delivery profile before
    # blocking; captions and trial mode remain part of both fingerprints, so a
    # real edit is still rejected.
    saved_fingerprint = str(state.get("fingerprint") or "")
    if state.get("creation_id") and saved_fingerprint and saved_fingerprint != fingerprint:
        alternate_path = _instagram_compatible_video(
            Path(job.video_path),
            recovery_profile=not recovery_profile,
        )
        alternate_size = alternate_path.stat().st_size
        alternate_fingerprint = hashlib.sha256(json.dumps({
            "path": str(alternate_path.resolve()), "size": alternate_size,
            "mtime": alternate_path.stat().st_mtime_ns,
            "caption": create_data["caption"], "trial": is_trial_reel,
        }, sort_keys=True).encode()).hexdigest()
        if alternate_fingerprint == saved_fingerprint:
            video_path = alternate_path
            file_size = alternate_size
            fingerprint = alternate_fingerprint
    status = ""
    if state.get("creation_id"):
        status = container_status()
        if status == "PUBLISHED" or state.get("publish_ambiguous"):
            raise PublicationNeedsReview("Instagram publish may have succeeded. Check the account before retrying; the saved container was preserved.")
        upload_rejected = state.get("stage") == "upload_rejected" and status == "IN_PROGRESS"
        # A definitive rupload rejection is the one safe exception to the
        # fingerprint guard.  For example, we may deliberately regenerate the
        # MP4 with a stricter encoder profile before the operator presses the
        # per-platform retry button.  Meta accepted neither bytes nor a publish
        # request in this state, so archive that dead container and start one
        # new direct upload.  Any other change remains blocked: it could create
        # a duplicate Reel for an upload Meta may still process.
        if state.get("fingerprint") != fingerprint and not upload_rejected:
            raise PublicationNeedsReview("Instagram video or caption changed after upload; reconcile the saved container first.")
        if status in {"ERROR", "EXPIRED"} or upload_rejected:
            # An explicit non-retriable upload rejection can leave Meta reporting
            # IN_PROGRESS forever. No publish was sent for this upload. Preserve
            # diagnostics, and never replace after an ambiguous publish reply.
            previous = list(state.get("previous_containers") or [])
            prior = {"creation_id": state["creation_id"], "status_code": status,
                     "status_detail": state.get("status_detail", "")}
            if state.get("upload_error"):
                prior.update({key: state[key] for key in
                              ("upload_error", "upload_http_status", "upload_request_id") if key in state})
            previous.append(prior)
            use_url = state.get("upload_transport") == "url_pull" or (
                url_fallback_enabled and state.get("stage") == "upload_rejected"
            )
            state = {"account_id": account_id, "previous_containers": previous[-5:]}
            if use_url:
                save(upload_transport="url_pull", stage="awaiting_url_upload")

    if not state.get("creation_id"):
        use_url = state.get("upload_transport") == "url_pull"
        if use_url:
            if not url_fallback_enabled:
                raise PublicationNeedsReview("Instagram URL fallback is disabled for this project; enable it before retrying.")
            create_data.pop("upload_type", None)
            create_data["video_url"] = _instagram_hosted_video(video_path, project_slug)
        create_response = requests.post(
            f"{base_url}/{account_id}/media",
            data=create_data,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        create_payload = _response_json(create_response, "Instagram reel container")
        creation_id = str(create_payload.get("id") or "")
        upload_url = str(create_payload.get("uri") or "")
        if not creation_id or (not use_url and not upload_url):
            raise PublicationDeliveryError("Instagram did not return a creation id and upload URI.")
        if not use_url:
            parsed_upload_url = urlparse(upload_url)
            if (parsed_upload_url.scheme != "https" or parsed_upload_url.hostname != "rupload.facebook.com"
                    or parsed_upload_url.username or parsed_upload_url.password):
                raise PublicationDeliveryError("Instagram returned an unsafe resumable upload URI.")
        # Do not persist the upload URL (it can contain credentials). A crash before
        # upload is reconciled through the saved container, not a blind new upload.
        save(creation_id=creation_id, account_id=account_id, fingerprint=fingerprint,
             stage="processing" if use_url else "upload_started", publish_ambiguous=False)

        if not use_url:
            mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
            try:
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
                if not upload_response.ok:
                    try:
                        rejected_payload = upload_response.json()
                    except ValueError:
                        rejected_payload = {}
                    debug = rejected_payload.get("debug_info") if isinstance(rejected_payload, dict) else None
                    if (isinstance(debug, dict) and debug.get("retriable") is False
                            and debug.get("type") == "ProcessingFailedError"):
                        request_id = str(upload_response.headers.get("x-fb-request-id") or "")
                        # Keep diagnostics, not opaque provider payloads or credentials.
                        if not all(c.isalnum() or c in "_-:" for c in request_id):
                            request_id = ""
                        save(stage="upload_rejected", upload_error="ProcessingFailedError",
                             upload_http_status=upload_response.status_code,
                             upload_request_id=request_id[:200])
                _response_json(upload_response, "Instagram resumable video upload")
                save(stage="processing")
            except (requests.RequestException, PublicationDeliveryError):
                # Even an upload error may have accepted the bytes. Inspect the same
                # container rather than immediately creating another one.
                status = container_status()
                if status != "FINISHED":
                    if (url_fallback_enabled and state.get("stage") == "upload_rejected"
                            and status in {"IN_PROGRESS", "ERROR", "EXPIRED"}
                            and not state.get("publish_ambiguous")):
                        return _publish_instagram(job, project_slug, persist)
                    raise

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        status = container_status()
        if status == "FINISHED":
            break
        if status == "PUBLISHED":
            raise PublicationNeedsReview("Instagram container is already published; reconcile its media ID before retrying.")
        if status in {"ERROR", "EXPIRED"}:
            raise PublicationDeliveryError(f"Instagram container failed: {state.get('status_detail') or status}.")
        time.sleep(5)
    else:
        raise PublicationDeliveryError("Instagram reel processing timed out.")
    for attempt in range(3):
        # Persist BEFORE sending: a lost response must never cause a new Reel.
        save(stage="publishing", publish_ambiguous=True)
        try:
            publish_response = requests.post(
                f"{base_url}/{account_id}/media_publish",
                data={"creation_id": state["creation_id"], "access_token": access_token},
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
            publish_payload = publish_response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PublicationNeedsReview("Instagram publish response was lost; check the saved container before retrying.") from exc
        if publish_response.ok and isinstance(publish_payload, dict) and publish_payload.get("id"):
            media_id = str(publish_payload["id"])
            save(media_id=media_id, stage="published", publish_ambiguous=False)
            break
        error = publish_payload.get("error", {}) if isinstance(publish_payload, dict) else {}
        if not isinstance(error, dict) or not error.get("code"):
            raise PublicationNeedsReview("Instagram publish returned no media ID; reconcile the saved container before retrying.")
        # Meta sometimes rejects a just-processed Reel with 2207085. Retry only
        # this explicit rejection, only on the SAME ready container, with a bound.
        save(stage="publish_rejected", publish_ambiguous=False,
             error_code=error.get("code"), error_subcode=error.get("error_subcode"))
        if error.get("error_subcode") == 2207085 and attempt < 2:
            time.sleep(10 * (attempt + 1))
            if container_status() == "FINISHED":
                continue
            raise PublicationNeedsReview("Instagram publish was rejected and its container is no longer ready; inspect before retrying.")
        _response_json(publish_response, "Instagram reel publish")
        raise PublicationDeliveryError("Instagram publish rejected the saved container.")
    # A permalink makes the admin monitor useful after delivery.  Do not turn a
    # successful publish into a failure if Meta delays or withholds the field.
    try:
        permalink_response = requests.get(
            f"{base_url}/{media_id}",
            params={"fields": "permalink"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        permalink_payload = _response_json(permalink_response, "Instagram media permalink")
        permalink = str(permalink_payload.get("permalink") or "").strip()
        if permalink.startswith("https://"):
            job.payload_json = {**(job.payload_json or {}), "external_post_url": permalink}
            if persist:
                persist()
    except Exception:
        pass
    return media_id


def _instagram_hosted_video(video_path: Path, project_slug: str) -> str:
    """Use project-approved storage only after a definitive direct-upload rejection."""
    upload_path = video_path
    if video_path.stat().st_size > 20 * 1024 * 1024:
        from .ai_approval_service import _resolve_ffmpeg

        upload_path = video_path.with_name(f"{video_path.stem}.delivery720.mp4")
        if not (upload_path.is_file() and upload_path.stat().st_size > 0
                and upload_path.stat().st_mtime_ns >= video_path.stat().st_mtime_ns):
            temp_path = upload_path.with_name(f".{upload_path.stem}.{uuid.uuid4().hex}.tmp.mp4")
            command = [
                _resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=w='min(720,iw)':h=-2,setsar=1",
                "-r", "30", "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                "-preset", "fast", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "5000k",
                "-g", "60", "-bf", "0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-ac", "2", "-movflags", "+faststart", str(temp_path),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=900)
                os.replace(temp_path, upload_path)
            except (OSError, subprocess.SubprocessError) as exc:
                raise PublicationDeliveryError("Instagram fallback video preparation failed.") from exc
            finally:
                temp_path.unlink(missing_ok=True)
    return zernio_publisher.ZernioClient(project_slug).upload(upload_path)


def _instagram_compatible_video(video_path: Path, *, recovery_profile: bool = False) -> Path:
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

    # Bump the cached delivery variant when the encoding contract changes. This
    # makes an already generated 4K final get the stricter encoder settings on a
    # later retry rather than silently reusing an older derivative.  v3 is only
    # for a second direct attempt after Meta explicitly rejected v2.
    profile = "v3" if recovery_profile else "v2"
    output_path = video_path.with_name(f"{video_path.stem}.instagram.{profile}.mp4")
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
    max_width = 720 if recovery_profile else 1080
    max_rate = "2500k" if recovery_profile else "20M"
    buffer_size = "5000k" if recovery_profile else "40M"
    scale_filter = f"scale=w='min({max_width},iw)':h=-2:flags=lanczos"
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
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-fps_mode",
        "cfr",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-maxrate",
        max_rate,
        "-bufsize",
        buffer_size,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
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


def _vk_credentials_project(project_slug: str) -> str:
    source = project_env(project_slug, "VK_USER_CREDENTIALS_PROJECT")
    if not source or source == project_slug:
        return project_slug
    expected = project_env(project_slug, "VK_USER_ID")
    if not expected.isdigit() or expected != project_env(source, "VK_USER_ID"):
        raise PublicationDeliveryError("Shared VK OAuth requires the same explicit VK_USER_ID in both projects.")
    if project_env(source, "VK_USER_CREDENTIALS_PROJECT") not in ("", source):
        raise PublicationDeliveryError("Chained VK OAuth sharing is not supported.")
    return source


def _vk_access_token(project_slug: str) -> str:
    source = _vk_credentials_project(project_slug)
    with locked_vk_credentials(source):
        return _vk_access_token_locked(source)


def _vk_access_token_locked(project_slug: str) -> str:
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


def _vk_call(method: str, token: str, api_version: str, **params) -> dict:
    # Never include VK's request_params (which contain access_token) in errors.
    try:
        response = requests.post(
            f"https://api.vk.ru/method/{method}",
            data={"access_token": token, "v": api_version, **params},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise PublicationDeliveryError(f"VK {method}: response unavailable.") from None
    if not isinstance(payload, dict):
        raise PublicationDeliveryError(f"VK {method}: invalid response.")
    _vk_error_message(payload, f"VK {method}")
    if not response.ok or not isinstance(payload.get("response"), dict):
        raise PublicationDeliveryError(f"VK {method}: incomplete response.")
    return payload["response"]


def _vk_video_item(token: str, api_version: str, external_id: str) -> dict | None:
    owner_id, _, video_id = str(external_id or "").partition("_")
    if not owner_id or not video_id.isdigit():
        return None
    items = _vk_call(
        "video.get",
        token,
        api_version,
        owner_id=owner_id,
        videos=external_id,
        extended=0,
    ).get("items", [])
    return next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and f"{item.get('owner_id')}_{item.get('id')}" == external_id
        ),
        None,
    )


def _vk_video_is_corrupt(item: dict | None) -> bool:
    title = str((item or {}).get("title") or "").strip().casefold()
    return any(marker in title for marker in ("повреж", "corrupt", "damaged"))


def _vk_upload_is_stale(payload: dict, *, now: datetime | None = None) -> bool:
    raw = str(payload.get("vk_upload_started_at") or "").strip()
    if not raw:
        return False
    try:
        started_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - started_at >= timedelta(minutes=30)


def _vk_story_files(job: PublicationJob) -> list[Path]:
    """Keep the whole video, in conservative <15s / <10MB story parts."""
    from .ai_approval_service import _resolve_ffmpeg, _video_duration_seconds

    source = Path(job.video_path)
    duration = _video_duration_seconds(source)
    ffmpeg = _resolve_ffmpeg()
    if not duration or not math.isfinite(duration) or duration <= 0 or not ffmpeg:
        raise PublicationDeliveryError("VK Stories: cannot read video duration or find ffmpeg.")
    directory = source.parent / f"vk-stories-{job.id}"
    directory.mkdir(exist_ok=True)
    parts = []
    for index in range(math.ceil(duration / 14)):
        destination = directory / f"part-{index + 1:03d}.mp4"
        temporary = directory / f".{uuid.uuid4().hex}.mp4"
        try:
            subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(index * 14),
                "-i", str(source), "-t", str(min(14, duration - index * 14)),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart",
                str(temporary),
            ], check=True, capture_output=True, timeout=600)
            actual_duration = _video_duration_seconds(temporary)
            if (not temporary.is_file() or not 0 < temporary.stat().st_size <= 10 * 1024**2
                    or not actual_duration or not 0 < actual_duration <= 15):
                raise PublicationDeliveryError("VK Stories: encoded part exceeds media limits.")
            temporary.replace(destination)
        except (subprocess.SubprocessError, OSError):
            raise PublicationDeliveryError("VK Stories: video preparation failed.") from None
        finally:
            temporary.unlink(missing_ok=True)
        parts.append(destination)
    return parts


def _publish_vk_stories(job: PublicationJob, token: str, group_id: str, version: str,
                        persist: Callable[[], None]) -> None:
    parts = [dict(part) for part in (job.payload_json or {}).get("vk_story_parts", [])]

    def save() -> None:
        job.payload_json = {**(job.payload_json or {}), "vk_story_parts": [dict(p) for p in parts]}
        persist()

    if not parts:
        parts = [{"path": str(path)} for path in _vk_story_files(job)]
        if not parts:
            raise PublicationDeliveryError("VK Stories: no video parts were prepared.")
        save()
    for part in parts:
        if part.get("verified"):
            continue
        if not part.get("id"):
            if part.get("save_started_at"):
                raise PublicationNeedsReview("VK Stories: previous save outcome is unknown; check stories before retrying.")
            if not part.get("upload_result"):
                if part.get("upload_started_at"):
                    # Uploading to the story server alone cannot make a story
                    # visible: that requires a subsequent `stories.save` with
                    # the opaque upload result.  If that response was lost, we
                    # can safely recover only after confirming that the group
                    # has no active stories at all.  With any active story we
                    # cannot prove it is unrelated, so remain conservative.
                    current = _vk_call(
                        "stories.get", token, version, owner_id=f"-{group_id}", extended=1,
                    )
                    if current.get("items"):
                        raise PublicationNeedsReview(
                            "VK Stories: previous upload outcome is unknown and active stories exist; "
                            "automatic repeat is blocked."
                        )
                    part.pop("upload_started_at", None)
                    part["upload_recovery_checked_at"] = datetime.now(timezone.utc).isoformat()
                    save()
                path = Path(part["path"])
                if not path.is_file():
                    raise PublicationDeliveryError("VK Stories: prepared video part is missing.")
                server = _vk_call("stories.getVideoUploadServer", token, version,
                                  group_id=group_id, add_to_news=1)
                url = str(server.get("upload_url") or "")
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise PublicationDeliveryError("VK Stories: invalid upload URL.")
                part["upload_started_at"] = datetime.now(timezone.utc).isoformat()
                save()
                try:
                    with path.open("rb") as handle:
                        response = requests.post(url, files={"video_file": (path.name, handle, "video/mp4")},
                                                 timeout=DEFAULT_REQUEST_TIMEOUT, allow_redirects=False)
                    result = response.json()
                    data = result.get("response") if isinstance(result, dict) else None
                    upload_result = data.get("upload_result") if isinstance(data, dict) else None
                    if not response.ok or not isinstance(upload_result, str) or not upload_result:
                        raise ValueError("Missing upload result")
                except (requests.RequestException, ValueError):
                    raise PublicationNeedsReview("VK Stories: upload was not confirmed; check before retrying.") from None
                part["upload_result"] = upload_result
                save()
            part["save_started_at"] = datetime.now(timezone.utc).isoformat()
            save()
            # Even an error can be ambiguous after a write; never repeat save blindly.
            try:
                data = _vk_call("stories.save", token, version, upload_results=part["upload_result"])
            except PublicationDeliveryError:
                raise PublicationNeedsReview("VK Stories: save not confirmed; check stories before retrying.") from None
            items = data.get("items") or []
            if (len(items) != 1 or not isinstance(items[0], dict)
                    or str(items[0].get("owner_id")) != f"-{group_id}"
                    or not str(items[0].get("id") or "").isdigit()):
                raise PublicationNeedsReview("VK Stories: unexpected saved story owner/ID; check before retrying.")
            part["id"] = f"-{group_id}_{items[0]['id']}"
            part["url"] = f"https://vk.com/story{part['id']}"
            part.pop("upload_result", None)
            save()
        if not str(part["id"]).startswith(f"-{group_id}_"):
            raise PublicationNeedsReview("VK Stories: saved story belongs to another community.")
        data = _vk_call("stories.getById", token, version, stories=part["id"])
        item = next((s for s in data.get("items", [])
                     if f"{s.get('owner_id')}_{s.get('id')}" == part["id"]), None)
        if (not item or item.get("type") != "video" or item.get("is_deleted")
                or item.get("is_expired") or not item.get("video")):
            raise PublicationNeedsReview("VK Stories: saved story video not verified; retry checks it without reposting.")
        part["verified"] = True
        save()


def _publish_vk(job: PublicationJob, project_slug: str, persist: Callable[[], None]) -> str:
    language = str(job.language or "ru").upper()
    group_id = project_env(project_slug, f"VK_GROUP_ID_{language}")
    if not group_id.isdigit() or int(group_id) <= 0:
        raise PublicationDeliveryError("VK community ID is invalid.")
    wall_token = project_env(project_slug, f"VK_COMMUNITY_TOKEN_{language}")
    if not wall_token:
        raise PublicationDeliveryError("VK community wall token is not configured.")
    api_version = shared_env("VK_API_VERSION", "5.199")
    payload = job.payload_json or {}
    if payload.get("vk_group_id") and str(payload["vk_group_id"]) != group_id:
        raise PublicationNeedsReview("VK community changed after submission; retry blocked.")
    # With a community token, no group_id means the token's own community.
    groups = _vk_call("groups.getById", wall_token, api_version).get("groups", [])
    if len(groups) != 1 or str(groups[0].get("id")) != group_id:
        raise PublicationDeliveryError("VK wall token belongs to a different community.")

    def save(**values) -> None:
        job.payload_json = {**(job.payload_json or {}), **values}
        persist()

    def reset_failed_video(external_id: str, reason: str) -> None:
        current = dict(job.payload_json or {})
        failed_ids = [str(value) for value in current.get("vk_failed_video_external_ids", []) if value]
        if external_id and external_id not in failed_ids:
            failed_ids.append(external_id)
        for key in (
            "vk_allocated_video_id",
            "vk_video_external_id",
            "vk_upload_started_at",
            "vk_video_processing",
            "external_post_url",
        ):
            current.pop(key, None)
        current.update(
            {
                "vk_failed_video_external_ids": failed_ids,
                "vk_video_recovery_profile": True,
                "vk_video_recovery_reason": reason,
            }
        )
        job.external_post_id = None
        job.payload_json = current
        persist()

    save(vk_group_id=group_id)
    payload = job.payload_json or {}
    external_id = str(payload.get("vk_video_external_id") or job.external_post_id or "")
    video_ready_checked = False
    if external_id:
        owner, _, video = external_id.partition("_")
        if owner != f"-{group_id}" or not video.isdigit():
            raise PublicationNeedsReview("Saved VK video does not belong to the configured community.")
        # Never attach an asset which VK has not finished transcoding. Older
        # versions checked only the lost-response recovery path, so a normal
        # upload could reach wall.post while still pending and leave a blank or
        # explicitly damaged attachment on the wall.
        if not payload.get("vk_wall_post_id") and not payload.get("vk_wall_create_started_at"):
            token = _vk_access_token(project_slug)
            item = _vk_video_item(token, api_version, external_id)
            if item is None:
                raise PublicationNeedsReview(
                    "Saved VK video could not be checked; automatic re-upload is blocked."
                )
            if _vk_video_is_corrupt(item):
                reset_failed_video(external_id, "vk_reported_corrupt")
                external_id = ""
                payload = job.payload_json or {}
            elif bool(item.get("processing")):
                if _vk_upload_is_stale(payload):
                    reset_failed_video(external_id, "processing_timeout")
                    external_id = ""
                    payload = job.payload_json or {}
                else:
                    save(
                        vk_video_processing=True,
                        external_post_url=f"https://vk.com/video{external_id}",
                    )
                    raise PublicationDeliveryError(
                        "VK is still processing the uploaded video. Retry only this platform after processing completes."
                    )
            else:
                save(vk_video_processing=False)
                video_ready_checked = True
    if not external_id:
        allocated_id = str(payload.get("vk_allocated_video_id") or "")
        if allocated_id:
            owner, _, video = allocated_id.partition("_")
            if owner != f"-{group_id}" or not video.isdigit():
                raise PublicationNeedsReview("Allocated VK video does not belong to the configured community.")
            # An upload response can be lost after VK has already created the
            # video. Verify the exact pre-allocated ID instead of uploading the
            # bytes again; a second upload could become a duplicate publication.
            token = _vk_access_token(project_slug)
            try:
                item = _vk_video_item(token, api_version, allocated_id)
            except PublicationDeliveryError:
                raise PublicationNeedsReview("Allocated VK video could not be checked; automatic re-upload is blocked.") from None
            if item is None:
                raise PublicationNeedsReview("Allocated VK video was not found; automatic re-upload is blocked.")
            # `video.save` allocates an ID before VK has finished transcoding.
            # Posting that pending asset to the wall can leave a broken or blank
            # attachment, so retain the exact saved ID and wait for the next
            # explicit retry instead of publishing prematurely.
            if _vk_video_is_corrupt(item):
                reset_failed_video(allocated_id, "vk_reported_corrupt")
                payload = job.payload_json or {}
            elif bool(item.get("processing")):
                if _vk_upload_is_stale(payload):
                    reset_failed_video(allocated_id, "processing_timeout")
                    payload = job.payload_json or {}
                else:
                    save(vk_video_processing=True, vk_video_external_id=allocated_id,
                         external_post_url=f"https://vk.com/video{allocated_id}")
                    raise PublicationDeliveryError(
                        "VK is still processing the uploaded video. Retry only this platform after processing completes."
                    )
            else:
                external_id = allocated_id
                job.external_post_id = external_id
                save(vk_video_processing=False, vk_video_external_id=external_id,
                     external_post_url=f"https://vk.com/video{external_id}")
                video_ready_checked = True
        payload = job.payload_json or {}
        if payload.get("vk_upload_started_at"):
            if not external_id:
                raise PublicationNeedsReview("Previous VK upload outcome is unknown. Check the saved video ID before retrying.")
        if not external_id:
            token = _vk_access_token(project_slug)
            _verify_vk_identity(project_slug, token=token, group_id=group_id, api_version=api_version)
            if payload.get("vk_video_recovery_profile"):
                video_path = _instagram_compatible_video(Path(job.video_path), recovery_profile=True)
            else:
                video_path = _instagram_compatible_video(Path(job.video_path))
            # Persist before the first remote write: an interruption must not re-upload.
            save(vk_upload_started_at=datetime.now(timezone.utc).isoformat())
            try:
                data = _vk_call(
                    "video.save", token, api_version, group_id=group_id,
                    name=str(job.title or job.description or "Video")[:128],
                    description=str(job.description or ""), wallpost=0,
                )
                upload_url = str(data.get("upload_url") or "")
                parsed = urlparse(upload_url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise PublicationDeliveryError("VK returned an invalid upload URL.")
                if str(data.get("owner_id")) != f"-{group_id}" or not str(data.get("video_id", "")).isdigit():
                    raise PublicationDeliveryError("VK returned an unexpected upload owner/video ID.")
                save(vk_allocated_video_id=f"{data['owner_id']}_{data['video_id']}")
                with video_path.open("rb") as handle:
                    response = requests.post(upload_url, files={"video_file": (video_path.name, handle, "video/mp4")},
                                             timeout=DEFAULT_REQUEST_TIMEOUT, allow_redirects=False)
                uploaded = response.json()
                if not response.ok or not isinstance(uploaded, dict) or uploaded.get("error"):
                    raise PublicationDeliveryError("VK video upload was not confirmed.")
                if str(uploaded.get("owner_id")) != f"-{group_id}" or str(uploaded.get("video_id")) != str(data["video_id"]):
                    raise PublicationDeliveryError("VK upload returned an unexpected video ID.")
            except (PublicationDeliveryError, requests.RequestException, ValueError):
                raise PublicationNeedsReview("VK upload not confirmed. Check the saved upload stage; automatic re-upload is blocked.") from None
            external_id = f"{data['owner_id']}_{data['video_id']}"
            job.external_post_id = external_id
            save(vk_video_external_id=external_id, external_post_url=f"https://vk.com/video{external_id}")

    # A successful upload response still means only that VK accepted the
    # bytes. Verify transcoding before wall.post; an explicit retry can resume
    # from this exact ID without uploading another copy.
    payload = job.payload_json or {}
    if (
        not payload.get("vk_wall_post_id")
        and not payload.get("vk_wall_create_started_at")
        and not video_ready_checked
    ):
        token = _vk_access_token(project_slug)
        item = _vk_video_item(token, api_version, external_id)
        if item is None:
            raise PublicationNeedsReview("Uploaded VK video could not be verified; retry will not re-upload it.")
        if _vk_video_is_corrupt(item):
            reset_failed_video(external_id, "vk_reported_corrupt")
            raise PublicationDeliveryError(
                "VK rejected the uploaded video. Retry only this platform to use the safer delivery profile."
            )
        if bool(item.get("processing")):
            save(vk_video_processing=True, external_post_url=f"https://vk.com/video{external_id}")
            raise PublicationDeliveryError(
                "VK is still processing the uploaded video. Retry only this platform after processing completes."
            )
        save(vk_video_processing=False)

    payload = job.payload_json or {}
    post_id = payload.get("vk_wall_post_id")
    if not post_id:
        if payload.get("vk_wall_create_started_at"):
            raise PublicationNeedsReview("VK wall request outcome is unknown. Check the wall before retrying; video will not be uploaded again.")
        guid = f"content-fabric-wall-{job.id}"
        save(vk_wall_publish_guid=guid, vk_wall_create_started_at=datetime.now(timezone.utc).isoformat())
        try:
            # Decode separately to distinguish an explicit rejection from an unknown outcome.
            response = requests.post("https://api.vk.ru/method/wall.post", data={
                "access_token": wall_token, "v": api_version, "owner_id": f"-{group_id}",
                "from_group": 1, "message": str(job.description or ""),
                "attachments": f"video{external_id}", "guid": guid,
            }, timeout=DEFAULT_REQUEST_TIMEOUT)
            result = response.json()
        except (requests.RequestException, ValueError):
            raise PublicationNeedsReview("VK wall response unavailable. Check the wall before retrying.") from None
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            save(vk_wall_create_started_at=None)
            try:
                _vk_error_message(result, "VK wall.post")
            except PublicationDeliveryError as exc:
                save(vk_wall_post_error=str(exc))
                raise
        post_id = (result.get("response") or {}).get("post_id") if isinstance(result, dict) and isinstance(result.get("response"), dict) else None
        if not response.ok or not str(post_id or "").isdigit():
            raise PublicationNeedsReview("VK wall post ID missing. Check the wall before retrying.")
        save(vk_wall_post_id=str(post_id), external_post_url=f"https://vk.com/wall-{group_id}_{post_id}",
             vk_wall_post_error=None)

    # A returned post ID is not sufficient: VK can silently drop attachments.
    token = _vk_access_token(project_slug)
    wall = _vk_call("wall.get", token, api_version, owner_id=f"-{group_id}", count=100, filter="owner")
    post = next((p for p in wall.get("items", []) if str(p.get("id")) == str(post_id)), None)
    videos = [(a.get("video") or {}) for a in (post or {}).get("attachments", []) if a.get("type") == "video"]
    if (not post or str(post.get("owner_id")) != f"-{group_id}"
            or str(post.get("from_id")) != f"-{group_id}"
            or not any(f"{v.get('owner_id')}_{v.get('id')}" == external_id for v in videos)
            or str(post.get("text") or "").replace("\r\n", "\n") != str(job.description or "").replace("\r\n", "\n")):
        raise PublicationNeedsReview("VK wall post exists but its video/text was not verified. Inspect the saved post link; retry only verifies it.")
    save(vk_wall_post_verified=True)
    if (job.payload_json or {}).get("vk_stories"):
        _publish_vk_stories(job, wall_token, group_id, api_version, persist)
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
        vk_uploaded = job.platform == "vk" and bool((job.payload_json or {}).get("vk_video_external_id") or job.external_post_id)
        if not video_path.is_file() and not vk_uploaded:
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
            elif job.platform == "vk":
                external_post_id = _publish_vk(job, job.project.slug, session.commit)
            elif job.platform == "instagram":
                external_post_id = _publish_instagram(job, job.project.slug, session.commit)
            else:
                external_post_id = {
                    "youtube": _publish_youtube,
                    "tiktok": _publish_tiktok,
                }[job.platform](job, job.project.slug)
        except (PublicationNeedsReview, zernio_publisher.ZernioPending, zernio_publisher.ZernioNeedsReview) as exc:
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
