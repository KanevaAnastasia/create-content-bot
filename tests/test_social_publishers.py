import base64
import json
from pathlib import Path
from unittest.mock import Mock, call
from urllib.parse import parse_qs, urlparse

import pytest

from app.publication_config import project_env, project_env_name
from app.publication_media import publication_media_token, publication_media_token_is_valid
from app.social_publishers import (
    PublicationDeliveryError,
    _tiktok_access_token,
    _youtube_env,
    _vk_access_token,
    _verify_vk_identity,
    platform_readiness,
)
from app.vk_oauth import (
    VKOAuthError,
    build_authorize_url,
    exchange_authorization_code,
    new_pkce_pair,
    validate_redirect_uri,
)


def test_project_scoped_publish_env_never_falls_back_to_global_tokens(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "legacy")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CLIENT_ID", "grandma-client")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_YOUTUBE_CLIENT_ID", "discipline-client")

    assert project_env("grandma_kitchen", "YOUTUBE_CLIENT_ID") == "grandma-client"
    assert project_env("discipline", "YOUTUBE_CLIENT_ID") == "discipline-client"
    monkeypatch.delenv("PUBLISH_DISCIPLINE_YOUTUBE_CLIENT_ID")
    assert project_env("discipline", "YOUTUBE_CLIENT_ID") == ""
    assert project_env_name("grandma_kitchen", "youtube_refresh_token") == (
        "PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN"
    )


def test_platform_readiness_reports_project_scoped_missing_keys(monkeypatch) -> None:
    for key in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID_RU",
    ):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"PUBLISH_DISCIPLINE_{key}", raising=False)

    readiness = platform_readiness("discipline", "youtube", "ru")

    assert readiness.ready is False
    assert "PUBLISH_DISCIPLINE_YOUTUBE_REFRESH_TOKEN_RU" in readiness.missing


def test_platform_readiness_accepts_complete_project_credentials(monkeypatch) -> None:
    for key in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID_RU",
    ):
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", "configured")

    readiness = platform_readiness("grandma_kitchen", "youtube", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_youtube_language_credentials_override_legacy(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN", "legacy")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN_RU", "russian")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN_EN", "english")

    assert _youtube_env("grandma_kitchen", "YOUTUBE_REFRESH_TOKEN", "ru") == "russian"
    assert _youtube_env("grandma_kitchen", "YOUTUBE_REFRESH_TOKEN", "en") == "english"


def test_instagram_readiness_uses_shared_https_and_project_credentials(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.setenv("CONTENT_FABRIC_PUBLIC_BASE_URL", "https://publish.example")
    monkeypatch.setenv("PUBLICATION_MEDIA_SIGNING_KEY", "signing-key")

    readiness = platform_readiness("grandma_kitchen", "instagram", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_vk_readiness_requires_expected_user_identity(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_GROUP_ID_RU", "238619215")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID", "")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_SCREEN_NAME", "")

    readiness = platform_readiness("grandma_kitchen", "vk", "ru")

    assert readiness.ready is False
    assert readiness.missing == ("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID",)


def test_vk_readiness_accepts_refresh_credentials_without_static_token(monkeypatch) -> None:
    monkeypatch.delenv("PUBLISH_GRANDMA_KITCHEN_VK_ACCESS_TOKEN", raising=False)
    for key, value in {
        "VK_CLIENT_ID": "54871992",
        "VK_REFRESH_TOKEN": "refresh",
        "VK_DEVICE_ID": "device",
        "VK_REDIRECT_URI": "http://localhost/callback",
        "VK_USER_ID": "1114556285",
        "VK_GROUP_ID_RU": "238619215",
    }.items():
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", value)

    readiness = platform_readiness("grandma_kitchen", "vk", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_vk_access_token_refreshes_and_persists_rotated_values(monkeypatch) -> None:
    for key, value in {
        "VK_CLIENT_ID": "54871992",
        "VK_REFRESH_TOKEN": "old-refresh",
        "VK_DEVICE_ID": "old-device",
        "VK_REDIRECT_URI": "http://127.0.0.1/callback",
    }.items():
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", value)
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "state": "fixed-state",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "device_id": "new-device",
    }
    post = Mock(return_value=response)
    store = Mock()
    monkeypatch.setattr("app.social_publishers.secrets.token_urlsafe", lambda _n: "fixed-state")
    monkeypatch.setattr("app.social_publishers.requests.post", post)
    monkeypatch.setattr("app.social_publishers.set_project_secrets", store)

    assert _vk_access_token("grandma_kitchen") == "new-access"
    post.assert_called_once_with(
        "https://id.vk.ru/oauth2/auth",
        params={
            "grant_type": "refresh_token",
            "redirect_uri": "http://127.0.0.1/callback",
            "client_id": "54871992",
            "device_id": "old-device",
            "state": "fixed-state",
        },
        data={"refresh_token": "old-refresh"},
        timeout=(20, 300),
    )
    store.assert_called_once_with(
        "grandma_kitchen",
        {
            "VK_ACCESS_TOKEN": "new-access",
            "VK_REFRESH_TOKEN": "new-refresh",
            "VK_DEVICE_ID": "new-device",
        },
    )


def test_tiktok_access_token_refreshes_and_persists_rotated_values(monkeypatch) -> None:
    for key, value in {
        "TIKTOK_CLIENT_KEY": "client-key",
        "TIKTOK_CLIENT_SECRET": "client-secret",
        "TIKTOK_REFRESH_TOKEN": "old-refresh",
        "TIKTOK_OPEN_ID": "open-1",
    }.items():
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", value)
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "open_id": "open-1",
    }
    post = Mock(return_value=response)
    store = Mock()
    monkeypatch.setattr("app.social_publishers.requests.post", post)
    monkeypatch.setattr("app.social_publishers.set_project_secrets", store)

    assert _tiktok_access_token("grandma_kitchen") == "new-access"
    post.assert_called_once_with(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": "client-key",
            "client_secret": "client-secret",
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
        },
        timeout=(20, 300),
    )
    store.assert_called_once_with(
        "grandma_kitchen",
        {
            "TIKTOK_ACCESS_TOKEN": "new-access",
            "TIKTOK_REFRESH_TOKEN": "new-refresh",
            "TIKTOK_OPEN_ID": "open-1",
        },
    )


def test_vk_oauth_pkce_and_code_exchange(monkeypatch) -> None:
    verifier, challenge = new_pkce_pair()
    assert len(verifier) >= 43
    assert "=" not in challenge
    url = build_authorize_url(
        client_id="54871992",
        redirect_uri="http://localhost/callback",
        state="expected-state",
        code_challenge=challenge,
    )
    assert url.startswith("https://id.vk.ru/authorize?")
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["54871992"]
    assert query["app_id"] == ["54871992"]
    assert query["redirect_uri"] == ["http://localhost/callback"]
    assert query["scope"] == ["video groups wall"]
    assert query["sdk_type"] == ["vkid"]
    assert query["v"] == ['"2.6.1"']
    stats_info = json.loads(base64.b64decode(query["stats_info"][0]))
    assert stats_info == {
        "flow_source": "from_custom_auth",
        "session_id": "expected-state",
    }

    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "state": "expected-state",
        "access_token": "access",
        "refresh_token": "refresh",
        "device_id": "device-2",
        "expires_in": 3600,
    }
    monkeypatch.setattr("app.vk_oauth.requests.post", Mock(return_value=response))
    tokens = exchange_authorization_code(
        client_id="54871992",
        redirect_uri="http://localhost/callback",
        code="code",
        code_verifier=verifier,
        device_id="device-1",
        state="expected-state",
    )
    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh"
    assert tokens.device_id == "device-2"


def test_vk_oauth_redirect_validation() -> None:
    assert validate_redirect_uri("http://localhost/callback") == "http://localhost/callback"
    assert validate_redirect_uri("https://admin.example.com/vk/callback") == (
        "https://admin.example.com/vk/callback"
    )
    for invalid in (
        "http://127.0.0.1/callback",
        "http://localhost:8000/callback",
        "http://admin.example.com/vk/callback",
    ):
        with pytest.raises(VKOAuthError):
            validate_redirect_uri(invalid)


def test_vk_identity_must_match_user_and_configured_group(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID", "1114556285")
    user_response = Mock(ok=True, status_code=200)
    user_response.json.return_value = {
        "response": [{"id": 1114556285, "screen_name": "id1114556285"}]
    }
    group_response = Mock(ok=True, status_code=200)
    group_response.json.return_value = {
        "response": {
            "groups": [
                {
                    "id": 238619215,
                    "name": "У бабушки дома",
                    "is_admin": 1,
                    "can_post": 1,
                    "can_upload_video": 1,
                    "can_upload_clip": 1,
                }
            ]
        }
    }
    post = Mock(side_effect=[user_response, group_response])
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    _verify_vk_identity(
        "grandma_kitchen", token="secret-token", group_id="238619215", api_version="5.199"
    )

    assert post.call_args_list == [
        call(
            "https://api.vk.ru/method/users.get",
            data={"access_token": "secret-token", "v": "5.199", "fields": "screen_name"},
            timeout=(20, 300),
        ),
        call(
            "https://api.vk.ru/method/groups.getById",
            data={
                "access_token": "secret-token",
                "v": "5.199",
                "group_id": "238619215",
            },
            timeout=(20, 300),
        ),
    ]


def test_vk_identity_does_not_require_administrator_role(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID", "1114556285")
    user_response = Mock(ok=True, status_code=200)
    user_response.json.return_value = {
        "response": [{"id": 1114556285, "screen_name": "id1114556285"}]
    }
    group_response = Mock(ok=True, status_code=200)
    group_response.json.return_value = {
        "response": {
            "groups": [
                {
                    "id": 238619215,
                    "is_admin": 0,
                    "can_upload_video": 0,
                    "can_post": 0,
                }
            ]
        }
    }
    monkeypatch.setattr(
        "app.social_publishers.requests.post",
        Mock(side_effect=[user_response, group_response]),
    )

    _verify_vk_identity(
        "grandma_kitchen", token="secret-token", group_id="238619215", api_version="5.199"
    )


def test_vk_identity_rejects_wrong_user_without_querying_groups(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID", "1114556285")
    user_response = Mock(ok=True, status_code=200)
    user_response.json.return_value = {"response": [{"id": 2, "screen_name": "discipline"}]}
    post = Mock(return_value=user_response)
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    with pytest.raises(PublicationDeliveryError, match="expected 1114556285"):
        _verify_vk_identity(
            "grandma_kitchen", token="secret-token", group_id="238619215", api_version="5.199"
        )

    assert post.call_count == 1


def test_publication_media_token_is_bound_to_job_and_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBLICATION_MEDIA_SIGNING_KEY", "test-signing-key")
    path = tmp_path / "video.mp4"
    token = publication_media_token("job-1", path)

    assert publication_media_token_is_valid("job-1", path, token) is True
    assert publication_media_token_is_valid("job-2", path, token) is False
    assert publication_media_token_is_valid("job-1", tmp_path / "other.mp4", token) is False
