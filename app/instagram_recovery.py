"""Explicit, job-scoped Instagram recovery using expiring public media."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


def instagram_recovery_available(job) -> bool:
    state = (job.payload_json or {}).get("instagram_upload") or {}
    return bool(
        job.platform == "instagram"
        and job.status in {"failed", "needs_manual_action"}
        and not job.external_post_id
        and not state.get("media_id")
        and not state.get("publish_ambiguous")
        and state.get("status_code") != "PUBLISHED"
        and (
            state.get("stage") in {"upload_rejected", "awaiting_url_upload"}
            or state.get("status_code") in {"ERROR", "EXPIRED"}
            or state.get("upload_transport") == "url_pull"
        )
    )


def _request_scope(job) -> dict:
    source = Path(job.video_path)
    stat = source.stat()
    return {
        "job_id": str(job.id),
        "project_id": str(job.project_id),
        "language": job.language,
        "video_path": str(source.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "caption_sha256": hashlib.sha256(str(job.description or "").encode()).hexdigest(),
        "trial": bool((job.payload_json or {}).get("instagram_trial")),
    }


def instagram_recovery_request(job, user_id) -> dict:
    return {
        "provider": "tmpfiles",
        "request_id": uuid.uuid4().hex,
        "requested_by": str(user_id),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "scope": _request_scope(job),
    }


def instagram_recovery_requested(job) -> bool:
    recovery = (job.payload_json or {}).get("instagram_url_recovery")
    if not recovery:
        return False
    if recovery.get("provider") != "tmpfiles" or recovery.get("scope") != _request_scope(job):
        raise ValueError("Instagram recovery content changed; reopen recovery in the admin.")
    return True


def _temporary_download_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "tmpfiles.org"
        or parsed.query or parsed.fragment
        or not re.fullmatch(r"/(?:dl/)?[0-9]+/[A-Za-z0-9_.-]+\.mp4", parsed.path)
    ):
        raise ValueError("Temporary hosting returned an unexpected media URL.")
    path = parsed.path if parsed.path.startswith("/dl/") else "/dl" + parsed.path
    return "https://tmpfiles.org" + path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def temporary_instagram_video(video_path: Path, saved_source: dict | None, checkpoint) -> str:
    """Upload only the selected MP4, verify its bytes, and checkpoint before Meta.

    No account credentials are sent to storage. A valid checkpoint is reused;
    expired links are replaced only while creating a new Meta container.
    """
    size = video_path.stat().st_size
    if not 0 < size <= 100_000_000:
        raise ValueError("Temporary Instagram hosting accepts MP4 files up to 100 MB.")
    digest = _file_digest(video_path)
    now = datetime.now(timezone.utc)
    source = dict(saved_source or {})
    try:
        url = _temporary_download_url(source["url"])
        reusable = (
            not source.get("invalid") and source.get("sha256") == digest
            and datetime.fromisoformat(source["expires_at"]) > now + timedelta(minutes=10)
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        reusable = False
    if not reusable:
        try:
            with video_path.open("rb") as media:
                response = requests.post(
                    "https://tmpfiles.org/api/v1/upload",
                    files={"file": (f"{uuid.uuid4().hex}.mp4", media, "video/mp4")},
                    data={"expire": "3600"}, timeout=(20, 300), allow_redirects=False,
                )
            if response.status_code != 200:
                raise ValueError(f"Temporary media upload failed: HTTP {response.status_code}.")
            payload = response.json()
            if payload.get("status") != "success":
                raise ValueError("Temporary media upload was not accepted.")
            url = _temporary_download_url(payload["data"]["url"])
        except requests.RequestException:
            raise ValueError("Temporary media upload failed; retry recovery in the admin.") from None
        source = {
            "url": url, "sha256": digest, "size": size, "verified": False,
            # Leave room for request latency and the provider's deletion clock.
            "expires_at": (now + timedelta(minutes=55)).isoformat(),
        }
        checkpoint(source)
    if not source.get("verified"):
        def invalidate():
            source["invalid"] = True
            checkpoint(source)

        downloaded = hashlib.sha256()
        received = 0
        deadline = time.monotonic() + 300
        try:
            with requests.get(url, stream=True, timeout=(20, 60), allow_redirects=False) as response:
                if response.status_code != 200:
                    if response.status_code in {404, 410}:
                        invalidate()
                    raise ValueError(f"Temporary media verification failed: HTTP {response.status_code}.")
                for chunk in response.iter_content(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise ValueError("Temporary media verification timed out; retry recovery in the admin.")
                    received += len(chunk)
                    if received > size:
                        invalidate()
                        raise ValueError("Temporary media does not match the selected video.")
                    downloaded.update(chunk)
        except requests.RequestException:
            raise ValueError("Temporary media verification failed; retry recovery in the admin.") from None
        if received != size or downloaded.hexdigest() != digest:
            invalidate()
            raise ValueError("Temporary media does not match the selected video.")
        source["verified"] = True
        checkpoint(source)
    if datetime.fromisoformat(source["expires_at"]) <= datetime.now(timezone.utc) + timedelta(minutes=10):
        raise ValueError("Temporary media link is expiring; retry recovery in the admin.")
    return url
