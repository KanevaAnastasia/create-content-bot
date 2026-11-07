import hashlib
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from app.social_oauth import (
    SocialOAuthError,
    build_tiktok_authorize_url,
    build_youtube_authorize_url,
    exchange_tiktok_code,
    exchange_youtube_code,
    new_tiktok_pkce_pair,
    tiktok_user,
    validate_tiktok_desktop_redirect_uri,
    youtube_channels,
)


def test_youtube_oauth_url_and_code_exchange(monkeypatch) -> None:
    url = build_youtube_authorize_url(
        client_id="client-id",
        redirect_uri="http://localhost/youtube/callback/",
        state="state-1",
    )
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent select_account"]
    assert "https://www.googleapis.com/auth/youtube.upload" in query["scope"][0]
    assert "https://www.googleapis.com/auth/youtube.readonly" in query["scope"][0]
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in query["scope"][0]

    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "scope": query["scope"][0],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr("app.social_oauth.requests.post", post)

    tokens = exchange_youtube_code(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://localhost/youtube/callback/",
        code="code-1",
    )

    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh"
    assert post.call_args.kwargs["data"]["grant_type"] == "authorization_code"


def test_youtube_channel_lookup(monkeypatch) -> None:
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "items": [{"id": "UC123", "snippet": {"title": "Grandma Kitchen"}}]
    }
    monkeypatch.setattr("app.social_oauth.requests.get", Mock(return_value=response))

    assert youtube_channels("access") == [{"id": "UC123", "title": "Grandma Kitchen"}]


def test_tiktok_desktop_oauth_pkce_and_exchange(monkeypatch) -> None:
    verifier, challenge = new_tiktok_pkce_pair()
    assert challenge == hashlib.sha256(verifier.encode("ascii")).hexdigest()
    redirect_uri = "http://localhost:8000/tiktok/callback/"
    url = build_tiktok_authorize_url(
        client_key="client-key",
        redirect_uri=redirect_uri,
        state="state-1",
        code_challenge=challenge,
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["user.info.basic,video.publish,video.list,user.info.stats"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [redirect_uri]

    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "open_id": "open-1",
        "scope": "user.info.basic,video.publish",
    }
    post = Mock(return_value=response)
    monkeypatch.setattr("app.social_oauth.requests.post", post)

    tokens = exchange_tiktok_code(
        client_key="client-key",
        client_secret="client-secret",
        redirect_uri=redirect_uri,
        code="code-1",
        code_verifier=verifier,
    )

    assert tokens.open_id == "open-1"
    assert post.call_args.kwargs["data"]["code_verifier"] == verifier


def test_tiktok_oauth_requires_loopback_port_and_publish_scope(monkeypatch) -> None:
    with pytest.raises(SocialOAuthError, match="localhost:<port>"):
        validate_tiktok_desktop_redirect_uri("http://localhost/callback/")

    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "open_id": "open-1",
        "scope": "user.info.basic",
    }
    monkeypatch.setattr("app.social_oauth.requests.post", Mock(return_value=response))
    with pytest.raises(SocialOAuthError, match="video.publish"):
        exchange_tiktok_code(
            client_key="client-key",
            client_secret="client-secret",
            redirect_uri="http://localhost:8000/tiktok/callback/",
            code="code-1",
            code_verifier="verifier",
        )


def test_tiktok_user_accepts_ok_error_envelope(monkeypatch) -> None:
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "data": {"user": {"open_id": "open-1", "display_name": "Grandma"}},
        "error": {"code": "ok", "message": "", "log_id": "log-1"},
    }
    monkeypatch.setattr("app.social_oauth.requests.get", Mock(return_value=response))

    assert tiktok_user("access") == {"open_id": "open-1", "display_name": "Grandma"}
