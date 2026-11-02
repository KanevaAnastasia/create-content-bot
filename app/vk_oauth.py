"""VK ID OAuth 2.1 + PKCE helpers for server-side publishing access."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests


VK_ID_AUTHORIZE_URL = "https://id.vk.ru/authorize"
VK_ID_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
VK_ID_WEB_SDK_VERSION = '"2.6.1"'
VK_PUBLISH_SCOPES = ("video", "groups", "wall")
DEFAULT_REQUEST_TIMEOUT = (20, 300)


class VKOAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VKTokenSet:
    access_token: str
    refresh_token: str
    device_id: str
    expires_in: int | None = None


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def validate_redirect_uri(redirect_uri: str) -> str:
    value = str(redirect_uri or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise VKOAuthError("VK ID redirect URL is invalid.") from exc
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise VKOAuthError("VK ID redirect URL is invalid.")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        if parsed.scheme != "http" or parsed.hostname != "localhost" or port is not None:
            raise VKOAuthError(
                "VK ID local redirect must use http://localhost without a port."
            )
    elif parsed.scheme != "https":
        raise VKOAuthError("VK ID public redirect must use HTTPS.")
    return value


def build_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    redirect_uri = validate_redirect_uri(redirect_uri)
    stats_info = base64.b64encode(
        json.dumps(
            {"flow_source": "from_custom_auth", "session_id": state},
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    query = urlencode(
        {
            "code_challenge": code_challenge,
            "code_challenge_method": "s256",
            "client_id": str(client_id),
            "response_type": "code",
            "scope": " ".join(VK_PUBLISH_SCOPES),
            "state": state,
            "prompt": "consent",
            "stats_info": stats_info,
            "v": VK_ID_WEB_SDK_VERSION,
            "sdk_type": "vkid",
            "app_id": str(client_id),
            "redirect_uri": redirect_uri,
        }
    )
    return f"{VK_ID_AUTHORIZE_URL}?{query}"


def _token_payload(response: requests.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise VKOAuthError(f"{context}: VK returned invalid JSON ({response.status_code}).") from exc
    detail = payload.get("error_description") or payload.get("error") if isinstance(payload, dict) else None
    if not response.ok or not isinstance(payload, dict) or payload.get("error"):
        raise VKOAuthError(f"{context}: {detail or f'HTTP {response.status_code}'}.")
    return payload


def exchange_authorization_code(
    *,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    device_id: str,
    state: str,
) -> VKTokenSet:
    response = requests.post(
        VK_ID_TOKEN_URL,
        params={
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "device_id": device_id,
            "state": state,
        },
        data={"code": code},
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    payload = _token_payload(response, "VK ID authorization code exchange")
    if str(payload.get("state") or "") != state:
        raise VKOAuthError("VK ID authorization code exchange returned an invalid state.")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    returned_device_id = str(payload.get("device_id") or device_id).strip()
    if not access_token or not refresh_token or not returned_device_id:
        raise VKOAuthError("VK ID authorization code exchange returned incomplete credentials.")
    expires_in = payload.get("expires_in")
    return VKTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=returned_device_id,
        expires_in=int(expires_in) if expires_in is not None else None,
    )
