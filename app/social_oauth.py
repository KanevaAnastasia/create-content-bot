"""OAuth helpers for direct YouTube and TikTok publishing connections."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests


DEFAULT_REQUEST_TIMEOUT = (20, 300)
YOUTUBE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_SCOPES = ("user.info.basic", "video.publish")


class SocialOAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeTokenSet:
    access_token: str
    refresh_token: str
    scope: str


@dataclass(frozen=True, slots=True)
class TikTokTokenSet:
    access_token: str
    refresh_token: str
    open_id: str
    scope: str


def _payload(response: requests.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise SocialOAuthError(f"{context}: invalid JSON ({response.status_code}).") from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    error_is_failure = bool(error)
    if isinstance(error, dict) and error.get("code") in {None, "", "ok"}:
        error_is_failure = False
    detail = payload.get("error_description") or error if isinstance(payload, dict) else None
    if not response.ok or not isinstance(payload, dict) or error_is_failure:
        raise SocialOAuthError(f"{context}: {detail or f'HTTP {response.status_code}' }.")
    return payload


def build_youtube_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent select_account",
            "state": state,
        }
    )
    return f"{YOUTUBE_AUTHORIZE_URL}?{query}"


def exchange_youtube_code(
    *, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> YouTubeTokenSet:
    response = requests.post(
        YOUTUBE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _payload(response, "YouTube OAuth code exchange")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    if not access_token or not refresh_token:
        raise SocialOAuthError(
            "YouTube OAuth did not return an offline refresh token; revoke the old grant and connect again."
        )
    return YouTubeTokenSet(access_token=access_token, refresh_token=refresh_token, scope=scope)


def youtube_channels(access_token: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id,snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _payload(response, "YouTube channel lookup")
    rows: list[dict[str, str]] = []
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or "").strip()
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        if channel_id:
            rows.append({"id": channel_id, "title": str(snippet.get("title") or channel_id)})
    return rows


def new_tiktok_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


def validate_tiktok_desktop_redirect_uri(redirect_uri: str) -> str:
    value = str(redirect_uri or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SocialOAuthError("TikTok redirect URI is invalid.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port is None
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise SocialOAuthError(
            "TikTok Desktop redirect must use http://localhost:<port>/... without query or fragment."
        )
    return value


def build_tiktok_authorize_url(
    *, client_key: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    redirect_uri = validate_tiktok_desktop_redirect_uri(redirect_uri)
    query = urlencode(
        {
            "client_key": client_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(TIKTOK_SCOPES),
            "state": state,
            "disable_auto_auth": "1",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{TIKTOK_AUTHORIZE_URL}?{query}"


def exchange_tiktok_code(
    *,
    client_key: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> TikTokTokenSet:
    response = requests.post(
        TIKTOK_TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": validate_tiktok_desktop_redirect_uri(redirect_uri),
            "code_verifier": code_verifier,
        },
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _payload(response, "TikTok OAuth code exchange")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    open_id = str(payload.get("open_id") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    granted = {item.strip() for item in scope.split(",") if item.strip()}
    if not access_token or not refresh_token or not open_id:
        raise SocialOAuthError("TikTok OAuth returned incomplete credentials.")
    if "video.publish" not in granted:
        raise SocialOAuthError("TikTok account did not grant the video.publish scope.")
    return TikTokTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        open_id=open_id,
        scope=scope,
    )


def tiktok_user(access_token: str) -> dict[str, str]:
    response = requests.get(
        "https://open.tiktokapis.com/v2/user/info/",
        params={"fields": "open_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _payload(response, "TikTok user lookup")
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error.get("code") not in {None, "", "ok"}:
        raise SocialOAuthError(f"TikTok user lookup: {error}.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {
        "open_id": str(user.get("open_id") or "").strip(),
        "display_name": str(user.get("display_name") or "").strip(),
    }
