"""TikTok-only Zernio adapter. No billing, account creation or silent fallback.

Contract: https://docs.zernio.com/platforms/tiktok/posts
Retries use the job UUID; accepted posts are polled, never recreated.
"""
from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .publication_config import project_env

BASE_URL = "https://zernio.com/api/v1"
CONFIG_KEYS = ("ZERNIO_API_KEY", "ZERNIO_TIKTOK_ACCOUNT_ID", "ZERNIO_TIKTOK_USERNAME")


class ZernioError(RuntimeError):
    pass


class ZernioPending(ZernioError):
    """Accepted remotely, not yet confirmed published."""


class ZernioNeedsReview(ZernioError):
    """Cannot safely retry without reconciling the remote state."""


def _id(value: str) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[a-fA-F0-9]{24}", value):
        raise ZernioError("Invalid Zernio object ID.")
    return value


def _handle(value: str) -> str:
    return str(value or "").strip().lstrip("@").lower()


class ZernioClient:
    def __init__(self, project_slug: str, *, api_key: str = ""):
        self.key = api_key or project_env(project_slug, "ZERNIO_API_KEY")
        if not self.key:
            raise ZernioError("Zernio API key is not configured for this project.")

    def request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", **kwargs.pop("headers", {})}
        try:
            response = requests.request(
                method, BASE_URL + path, headers=headers, timeout=(20, 300),
                allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            raise ZernioError("Zernio network error; remote outcome may be unknown.") from None
        try:
            data = response.json()
        except ValueError:
            raise ZernioError(f"Zernio returned invalid JSON (HTTP {response.status_code}).") from None
        if not isinstance(data, dict):
            raise ZernioError("Invalid Zernio response.")
        if not 200 <= response.status_code < 300:
            # Never persist raw provider payloads: they can contain tokens/URLs.
            code = str(data.get("code") or "api_error")
            code = re.sub(r"[^a-zA-Z0-9_]", "", code)[:80]
            raise ZernioError(f"Zernio HTTP {response.status_code} ({code}). Check the Zernio dashboard.")
        return data

    def accounts(self) -> list[dict]:
        data = self.request("GET", "/accounts", params={"includeOverLimit": "true", "adAccountRows": "true"})
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
            raise ZernioError("Zernio did not return the account list.")
        # Fail closed; this workflow must not require a paid connected-account tier.
        if len(accounts) > 2:
            raise ZernioError("More than 2 accounts connected in Zernio. Free-only publishing is blocked.")
        return accounts

    def verify_account(self, account_id: str, username: str) -> dict:
        account_id = _id(account_id)
        account = next((a for a in self.accounts() if a.get("_id") == account_id), None)
        if not account or account.get("platform") != "tiktok":
            raise ZernioError("The configured Zernio TikTok account was not found.")
        if not _handle(username) or _handle(account.get("username")) != _handle(username):
            raise ZernioError("TikTok username differs from this project's configured account. Reconnect explicitly.")
        if account.get("isActive") is False:
            raise ZernioError("Zernio TikTok account needs reconnection.")
        return account

    def upload(self, path: Path) -> str:
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0]
        if content_type not in {"video/mp4", "video/quicktime", "video/webm", "image/jpeg", "image/png", "image/webp"}:
            raise ZernioError("Unsupported Zernio media type.")
        max_size = 4 * 1024**3 if content_type.startswith("video/") else 20 * 1024**2
        if size <= 0 or size > max_size:
            raise ZernioError("File exceeds TikTok media limits or is empty.")
        data = self.request("POST", "/media/presign", json={
            "filename": path.name, "contentType": content_type, "size": size,
        })
        upload_url, public_url = data.get("uploadUrl", ""), data.get("publicUrl", "")
        upload_host = urlparse(upload_url).hostname or ""
        public_host = urlparse(public_url).hostname or ""
        if (urlparse(upload_url).scheme != "https" or not upload_host.endswith(".r2.cloudflarestorage.com")
                or urlparse(public_url).scheme != "https" or public_host != "media.zernio.com"):
            raise ZernioError("Unexpected Zernio media storage URL; upload blocked.")
        try:
            with path.open("rb") as source:
                result = requests.put(upload_url, data=source, headers={"Content-Type": content_type},
                                      timeout=(20, 300), allow_redirects=False)
            if not 200 <= result.status_code < 300:
                raise ZernioError(f"Zernio media upload failed (HTTP {result.status_code}).")
        except requests.RequestException:
            raise ZernioError("Zernio media upload failed; no post was created by this upload.") from None
        return public_url


def creator_info(project_slug: str) -> dict:
    client = ZernioClient(project_slug)
    account_id = project_env(project_slug, "ZERNIO_TIKTOK_ACCOUNT_ID")
    account = client.verify_account(account_id, project_env(project_slug, "ZERNIO_TIKTOK_USERNAME"))
    data = client.request("GET", f"/accounts/{_id(account_id)}/tiktok/creator-info", params={"mediaType": "video"})
    creator = data.get("creator") or {}
    limits = data.get("postingLimits") or {}
    interactions = limits.get("interactionSettings") or {}
    levels = [item["value"] for item in data.get("privacyLevels", []) if isinstance(item, dict) and item.get("value")]
    if not levels:
        raise ZernioError("Zernio did not return allowed TikTok privacy levels.")
    return {
        "creator_nickname": creator.get("nickname") or account.get("displayName") or account.get("username"),
        "creator_username": account.get("username"),
        "creator_avatar_url": account.get("profilePicture") or "",
        "privacy_level_options": levels,
        "max_video_post_duration_sec": limits.get("maxVideoDurationSec", 0),
        "can_post_more": creator.get("canPostMore") is not False,
        **{f"{kind}_disabled": not bool((interactions.get(f"allow_{kind}") or {}).get("enabled"))
           for kind in ("comment", "duet", "stitch")},
    }


def _save(job, persist: Callable[[], None], **values) -> None:
    job.payload_json = {**(job.payload_json or {}), **values}
    persist()


def apply_post(job, data: dict, persist: Callable[[], None]) -> str:
    post = data.get("post") or {}
    post_id = _id(post.get("_id") or data.get("postId"))
    _save(job, persist, zernio_post_id=post_id)
    entries = post.get("platforms") or []
    target_id = (job.payload_json or {}).get("zernio_account_id")
    entries = [p for p in entries if p.get("platform") == "tiktok"]
    if len(entries) != 1:
        raise ZernioPending("Zernio accepted the post; awaiting TikTok status.")
    entry = entries[0]
    returned_account = entry.get("accountId")
    if isinstance(returned_account, dict):
        returned_account = returned_account.get("_id")
    if returned_account and returned_account != target_id:
        raise ZernioNeedsReview("Zernio returned a different target account. Check the dashboard.")
    status = str(entry.get("status") or "pending")
    _save(job, persist, zernio_status=status)
    if (entry.get("platformSpecificData") or {}).get("isDraft"):
        raise ZernioNeedsReview("Delivered to TikTok inbox, not published. Finish in the TikTok app.")
    if status == "published":
        url = str(entry.get("platformPostUrl") or "")
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com")):
            _save(job, persist, external_post_url=url)
        return str(entry.get("platformPostId") or post_id)
    if status in {"failed", "error", "cancelled", "canceled"}:
        raise ZernioError(f"TikTok delivery {status}; inspect Zernio post {post_id}. Retry reuses this post.")
    raise ZernioPending(f"Zernio: {status}. Awaiting TikTok confirmation; do not create a duplicate.")


def publish(job, project_slug: str, persist: Callable[[], None]) -> str:
    client = ZernioClient(project_slug)
    account_id = _id(project_env(project_slug, "ZERNIO_TIKTOK_ACCOUNT_ID"))
    options = job.payload_json or {}
    saved_account = options.get("zernio_account_id")
    if saved_account and saved_account != account_id:
        raise ZernioNeedsReview("Project's Zernio account changed after submission; retry blocked.")
    client.verify_account(account_id, project_env(project_slug, "ZERNIO_TIKTOK_USERNAME"))
    post_id = options.get("zernio_post_id")
    if post_id:
        data = client.request("GET", f"/posts/{_id(post_id)}")
        post = data.get("post") or {}
        if post.get("status") == "failed":
            data = client.request("POST", f"/posts/{_id(post_id)}/retry")
        return apply_post(job, data, persist)
    if options.get("tiktok_publish_confirmed") is not True or options.get("tiktok_music_usage_confirmed") is not True:
        raise ZernioError("Preview the video and explicitly confirm TikTok publishing and music rights in the upload form.")
    info = creator_info(project_slug)
    privacy = options.get("tiktok_privacy_level")
    if privacy not in info["privacy_level_options"]:
        raise ZernioError("Select a privacy level allowed by the connected TikTok account.")
    if not info["can_post_more"]:
        raise ZernioError("TikTok currently does not allow this creator to post more videos.")
    content = job.description or job.title or ""
    if len(content) > 2200:
        raise ZernioError("TikTok caption exceeds 2200 characters. Shorten it in the upload form.")
    started = options.get("zernio_create_started_at")
    if started and (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() >= 23 * 3600:
        raise ZernioNeedsReview("Unconfirmed Zernio request is over 23 hours old. Reconcile it in Zernio before retrying; duplicate protection expires.")
    body = options.get("zernio_request")
    if not body:
        video_url = client.upload(Path(job.video_path))
        settings = {
            "privacyLevel": privacy,
            **{f"allow{kind.title()}": bool(options.get(f"tiktok_allow_{kind}")) and not info[f"{kind}_disabled"]
               for kind in ("comment", "duet", "stitch")},
            "contentPreviewConfirmed": True, "expressConsentGiven": True,
            "brandPartnerPromote": bool(options.get("tiktok_brand_content")),
            "isBrandOrganicPost": bool(options.get("tiktok_brand_organic")),
            "videoMadeWithAi": bool(options.get("tiktok_is_aigc", True)),
            "videoCoverTimestampMs": 1000,
        }
        if options.get("cover_path"):
            settings["videoCoverImageUrl"] = client.upload(Path(options["cover_path"]))
        body = {"content": content, "mediaItems": [{"type": "video", "url": video_url}],
                "platforms": [{"platform": "tiktok", "accountId": account_id}],
                "tiktokSettings": settings, "publishNow": True}
        _save(job, persist, zernio_request=body, zernio_account_id=account_id)
    if not started:
        _save(job, persist, zernio_create_started_at=datetime.now(timezone.utc).isoformat())
    data = client.request("POST", "/posts", json=body, headers={"Idempotency-Key": str(job.id)})
    return apply_post(job, data, persist)


def refresh(job, project_slug: str, persist: Callable[[], None]) -> str:
    """Read only: no retry, media upload or create in the status monitor."""
    post_id = (job.payload_json or {}).get("zernio_post_id")
    if not post_id:
        raise ZernioNeedsReview("Zernio response was not saved. Use the explicit retry to reconcile the request.")
    client = ZernioClient(project_slug)
    return apply_post(job, client.request("GET", f"/posts/{_id(post_id)}"), persist)
