import base64
import json
import os
import uuid
from pathlib import Path
from unittest.mock import Mock, call
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from app.publication_config import project_env, project_env_name
from app.publication_media import publication_media_token, publication_media_token_is_valid
from app.models import PublicationJob
from app.social_publishers import (
    PublicationDeliveryError,
    _instagram_access_token,
    _instagram_compatible_video,
    _publish_instagram,
    _publish_youtube,
    _publish_vk,
    _set_youtube_thumbnail,
    _tiktok_access_token,
    tiktok_creator_info,
    _youtube_env,
    _youtube_access_token,
    _vk_access_token,
    _verify_vk_identity,
    _tiktok_chunk_plan,
    platform_readiness,
    publish_publication_job,
    refresh_expiring_instagram_tokens,
)
from app.vk_oauth import (
    VKOAuthError,
    build_authorize_url,
    exchange_authorization_code,
    new_pkce_pair,
    validate_redirect_uri,
)


@pytest.fixture(autouse=True)
def isolated_publishing_config(monkeypatch):
    # Tests must not read live publishing credentials from the developer's .env.
    monkeypatch.setattr("app.publication_config.load_env_file", lambda: None)
    for key in tuple(os.environ):
        if key.startswith("PUBLISH_"):
            monkeypatch.delenv(key)


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


def test_publishing_job_is_not_delivered_twice(monkeypatch) -> None:
    job_id = uuid.uuid4()
    job = Mock(id=job_id, status="publishing")

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, _query):
            return job

    monkeypatch.setattr("app.social_publishers.SessionLocal", FakeSession)

    assert publish_publication_job(job_id) == {
        "job_id": str(job_id),
        "status": "publishing",
    }


def test_duplicate_bundle_marker_blocks_explicit_retry(monkeypatch) -> None:
    job_id = uuid.uuid4()
    job = Mock(
        id=job_id,
        status="failed",
        payload_json={"duplicate_of_bundle": "original-bundle"},
        publish_error="old error",
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, _query):
            return job

        def commit(self):
            return None

    monkeypatch.setattr("app.social_publishers.SessionLocal", FakeSession)

    result = publish_publication_job(job_id)

    assert result["status"] == "needs_manual_action"
    assert "original-bundle" in result["error"]
    assert job.status == "needs_manual_action"


def test_published_same_video_blocks_new_bundle_before_delivery(monkeypatch) -> None:
    project_id = uuid.uuid4()
    job_id = uuid.uuid4()
    job = Mock(
        id=job_id,
        project_id=project_id,
        platform="youtube",
        language="ru",
        account_key="default",
        status="approved",
        payload_json={"run_id": "run-123", "video_sha256": "same-digest"},
        publish_error=None,
    )
    published = Mock(
        id=uuid.uuid4(),
        bundle_key="original-bundle",
        payload_json={"run_id": "run-123", "video_sha256": "same-digest"},
    )

    class ScalarRows:
        def all(self):
            return [published]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, _query):
            return job

        def scalars(self, _query):
            return ScalarRows()

        def commit(self):
            return None

    monkeypatch.setattr("app.social_publishers.SessionLocal", FakeSession)

    result = publish_publication_job(job_id)

    assert result["status"] == "needs_manual_action"
    assert "original-bundle" in result["error"]
    assert job.payload_json["duplicate_of_bundle"] == "original-bundle"
    assert job.payload_json["duplicate_reason"] == "same source run"


def test_tiktok_chunk_plan_merges_trailing_bytes_into_final_chunk() -> None:
    chunk_size, chunk_count = _tiktok_chunk_plan(135 * 1024 * 1024)

    assert chunk_size == 64 * 1024 * 1024
    assert chunk_count == 2
    assert 135 * 1024 * 1024 - chunk_size == 71 * 1024 * 1024


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


def test_youtube_invalid_grant_requests_exact_channel_reconnect(monkeypatch) -> None:
    response = Mock(ok=False, status_code=400)
    response.json.return_value = {"error": "invalid_grant"}
    monkeypatch.setattr("app.social_publishers.requests.post", Mock(return_value=response))

    with pytest.raises(PublicationDeliveryError, match=r"YouTube RU.*invalid_grant.*Reconnect"):
        _youtube_access_token("grandma_kitchen", "ru")


def test_youtube_custom_thumbnail_waits_for_processing_and_uploads(monkeypatch, tmp_path: Path) -> None:
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png-cover")
    status = Mock(ok=True, status_code=200)
    status.json.return_value = {
        "items": [{"processingDetails": {"processingStatus": "succeeded"}}]
    }
    uploaded = Mock(ok=True, status_code=200)
    uploaded.json.return_value = {"items": []}
    get = Mock(return_value=status)
    post = Mock(return_value=uploaded)
    monkeypatch.setattr("app.social_publishers.requests.get", get)
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    assert _set_youtube_thumbnail("token", "video-id", cover) == "applied"
    assert get.call_args.kwargs["params"] == {
        "part": "processingDetails",
        "id": "video-id",
    }
    assert post.call_args.args[0].endswith("/youtube/v3/thumbnails/set")
    assert post.call_args.kwargs["params"] == {"videoId": "video-id"}


def test_youtube_retry_resumes_saved_session_without_creating_duplicate(
    monkeypatch, tmp_path: Path
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"abcdef")
    job = Mock(
        language="ru",
        video_path=str(video),
        title="Title",
        description="Description",
        payload_json={},
    )
    monkeypatch.setattr("app.social_publishers._youtube_access_token", lambda *_args: "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CHANNEL_ID_RU", "channel-1")

    channel = Mock(ok=True, status_code=200)
    channel.json.return_value = {"items": [{"id": "channel-1"}]}
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(return_value=channel))

    initialized = Mock(ok=True, status_code=200, headers={"Location": "https://upload.example/session"})
    initialized.json.return_value = {}
    post = Mock(return_value=initialized)
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    status = Mock(ok=False, status_code=308, headers={"Range": "bytes=0-2"})
    uploaded = Mock(ok=True, status_code=200, headers={})
    uploaded.json.return_value = {"id": "video-123"}
    put = Mock(side_effect=[requests.Timeout("lost response"), status, uploaded])
    monkeypatch.setattr("app.social_publishers.requests.put", put)
    persist = Mock()

    with pytest.raises(requests.Timeout):
        _publish_youtube(job, "grandma_kitchen", persist)
    assert job.payload_json["youtube_upload"]["stage"] == "uploading"
    assert post.call_count == 1

    assert _publish_youtube(job, "grandma_kitchen", persist) == "video-123"
    assert post.call_count == 1
    assert job.payload_json["youtube_upload"]["video_id"] == "video-123"
    assert put.call_args_list[-1].kwargs["headers"]["Content-Range"] == "bytes 3-5/6"


def test_youtube_upload_uses_confirmed_bounded_chunks(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"abcdef")
    job = Mock(
        language="ru",
        video_path=str(video),
        title="Title",
        description="Description",
        payload_json={},
    )
    monkeypatch.setattr("app.social_publishers.YOUTUBE_UPLOAD_CHUNK_SIZE", 3)
    monkeypatch.setattr("app.social_publishers._youtube_access_token", lambda *_args: "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CHANNEL_ID_RU", "channel-1")

    channel = Mock(ok=True, status_code=200)
    channel.json.return_value = {"items": [{"id": "channel-1"}]}
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(return_value=channel))
    initialized = Mock(ok=True, status_code=200, headers={"Location": "https://upload.example/session"})
    initialized.json.return_value = {}
    monkeypatch.setattr("app.social_publishers.requests.post", Mock(return_value=initialized))

    first = Mock(ok=False, status_code=308, headers={"Range": "bytes=0-2"})
    second = Mock(ok=True, status_code=200, headers={})
    second.json.return_value = {"id": "video-123"}
    put = Mock(side_effect=[first, second])
    monkeypatch.setattr("app.social_publishers.requests.put", put)

    assert _publish_youtube(job, "grandma_kitchen", Mock()) == "video-123"
    assert [call.kwargs["headers"]["Content-Range"] for call in put.call_args_list] == [
        "bytes 0-2/6",
        "bytes 3-5/6",
    ]
    assert [call.kwargs["data"] for call in put.call_args_list] == [b"abc", b"def"]
    assert job.payload_json["youtube_upload"]["video_id"] == "video-123"


def test_instagram_readiness_only_requires_project_credentials(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.delenv("CONTENT_FABRIC_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLICATION_MEDIA_SIGNING_KEY", raising=False)

    readiness = platform_readiness("grandma_kitchen", "instagram", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_instagram_language_token_overrides_legacy(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "legacy")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_RU", "russian")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EN", "english")

    assert _instagram_access_token("grandma_kitchen", "ru") == "russian"
    assert _instagram_access_token("grandma_kitchen", "en") == "english"


def test_instagram_language_token_refreshes_near_expiry(monkeypatch) -> None:
    for key, value in {
        "INSTAGRAM_ACCESS_TOKEN_EN": "old-english",
        "INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_EN": "1001000",
        "INSTAGRAM_APP_ID_EN": "app-id-en",
        "INSTAGRAM_APP_SECRET_EN": "app-secret-en",
    }.items():
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", value)
    monkeypatch.setattr("app.social_publishers.time.time", lambda: 1_000_000)
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {"access_token": "new-english", "expires_in": 5_184_000}
    get = Mock(return_value=response)
    store = Mock()
    monkeypatch.setattr("app.social_publishers.requests.get", get)
    monkeypatch.setattr("app.social_publishers.set_project_secrets", store)

    assert _instagram_access_token("grandma_kitchen", "en") == "new-english"
    assert get.call_args.kwargs["params"] == {
        "grant_type": "fb_exchange_token",
        "client_id": "app-id-en",
        "client_secret": "app-secret-en",
        "fb_exchange_token": "old-english",
    }
    store.assert_called_once_with(
        "grandma_kitchen",
        {
            "INSTAGRAM_ACCESS_TOKEN_EN": "new-english",
            "INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_EN": "6184000",
        },
    )


def test_instagram_language_token_does_not_refresh_early(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EN", "english")
    monkeypatch.setenv(
        "PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_EN",
        "7000000",
    )
    monkeypatch.setattr("app.social_publishers.time.time", lambda: 1_000_000)
    get = Mock()
    monkeypatch.setattr("app.social_publishers.requests.get", get)

    assert _instagram_access_token("grandma_kitchen", "en") == "english"
    get.assert_not_called()


def test_instagram_zero_expiry_does_not_refresh_on_every_request(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_ACCESS_TOKEN_RU", "verified-user-token")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_RU", "0")
    refresh = Mock()
    monkeypatch.setattr("app.social_publishers._refresh_instagram_access_token", refresh)

    assert _instagram_access_token("discipline", "ru") == "verified-user-token"
    assert {"project": "discipline", "language": "ru", "status": "valid"} in refresh_expiring_instagram_tokens(["discipline"])
    refresh.assert_not_called()


def test_instagram_maintenance_only_checks_scoped_tokens_with_expiry(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EN", "english")
    monkeypatch.setenv(
        "PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_EN",
        "7000000",
    )
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "legacy-russian")
    access_token = Mock(return_value="english")
    monkeypatch.setattr("app.social_publishers._instagram_access_token", access_token)

    results = refresh_expiring_instagram_tokens(["grandma_kitchen", "discipline"])

    assert results == [
        {"project": "grandma_kitchen", "language": "en", "status": "valid"}
    ]
    access_token.assert_called_once_with("grandma_kitchen", "en")


def test_instagram_readiness_accepts_language_token_without_legacy(monkeypatch) -> None:
    monkeypatch.delenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_EN", "english")
    monkeypatch.setenv(
        "PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_EN",
        "english-account",
    )

    readiness = platform_readiness("grandma_kitchen", "instagram", "en")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_instagram_resumable_uploads_local_video_without_public_url(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.social_publishers._verify_instagram_publish_identity", lambda *args: {"account_id": "account"})
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_RU", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v25.0")
    video_path = tmp_path / "reel.mp4"
    video_path.write_bytes(b"video-bytes")
    job = PublicationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        bundle_key="bundle",
        platform="instagram",
        language="ru",
        video_path=str(video_path),
        description="Caption",
    )

    def response(payload: dict) -> Mock:
        result = Mock(ok=True, status_code=200)
        result.json.return_value = payload
        return result

    post_calls: list[tuple[str, dict]] = []

    def post(url: str, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/account/media"):
            return response(
                {
                    "id": "container-1",
                    "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container-1",
                }
            )
        if url.startswith("https://rupload.facebook.com/"):
            assert kwargs["data"].read() == b"video-bytes"
            return response({"success": True})
        assert url.endswith("/account/media_publish")
        return response({"id": "media-1"})

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    monkeypatch.setattr(
        "app.social_publishers.requests.get",
        Mock(return_value=response({"status_code": "FINISHED"})),
    )

    assert _publish_instagram(job, "grandma_kitchen") == "media-1"
    assert post_calls[0][1]["data"] == {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "Caption",
        "share_to_feed": "true",
        "access_token": "token",
    }
    assert post_calls[1][1]["headers"] == {
        "Authorization": "OAuth token",
        "offset": "0",
        "file_size": str(len(b"video-bytes")),
        "Content-Type": "video/mp4",
    }


def test_instagram_ru_trial_uses_manual_trial_params_without_share_to_feed(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.social_publishers._verify_instagram_publish_identity", lambda *args: {"account_id": "account"})
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    video_path = tmp_path / "trial.mp4"
    video_path.write_bytes(b"trial-video")
    job = PublicationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        bundle_key="bundle",
        platform="instagram",
        language="ru",
        video_path=str(video_path),
        description="Отдельное пробное описание",
        payload_json={"instagram_trial": True},
    )

    def response(payload: dict) -> Mock:
        result = Mock(ok=True, status_code=200)
        result.json.return_value = payload
        return result

    post_calls: list[tuple[str, dict]] = []

    def post(url: str, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/account/media"):
            return response({
                "id": "trial-container",
                "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/trial-container",
            })
        if url.startswith("https://rupload.facebook.com/"):
            return response({"success": True})
        return response({"id": "trial-media"})

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    monkeypatch.setattr(
        "app.social_publishers.requests.get",
        Mock(return_value=response({"status_code": "FINISHED"})),
    )

    assert _publish_instagram(job, "grandma_kitchen") == "trial-media"
    create_data = post_calls[0][1]["data"]
    assert create_data["trial_params"] == '{"graduation_strategy":"MANUAL"}'
    assert "share_to_feed" not in create_data


def test_instagram_rejects_non_meta_resumable_upload_uri(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.social_publishers._verify_instagram_publish_identity", lambda *args: {"account_id": "account"})
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    video_path = tmp_path / "reel.mp4"
    video_path.write_bytes(b"video")
    job = PublicationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        bundle_key="bundle",
        platform="instagram",
        language="ru",
        video_path=str(video_path),
    )
    create_response = Mock(ok=True, status_code=200)
    create_response.json.return_value = {
        "id": "container-1",
        "uri": "https://attacker.example/upload",
    }
    post = Mock(return_value=create_response)
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    with pytest.raises(PublicationDeliveryError, match="unsafe resumable upload URI"):
        _publish_instagram(job, "grandma_kitchen")

    assert post.call_count == 1


def test_instagram_normalization_is_cached(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "publish_ru.mp4"
    source.write_bytes(b"4k-video")
    monkeypatch.setattr("app.post_production._video_dimensions", lambda _path: (2160, 3840))
    monkeypatch.setattr("app.ai_approval_service._resolve_ffmpeg", lambda: "/fake/ffmpeg")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> Mock:
        commands.append(command)
        Path(command[-1]).write_bytes(b"instagram-video")
        return Mock(returncode=0)

    monkeypatch.setattr("app.social_publishers.subprocess.run", run)

    normalized = _instagram_compatible_video(source)

    assert normalized == tmp_path / "publish_ru.instagram.v2.mp4"
    assert normalized.read_bytes() == b"instagram-video"
    assert "scale=w='min(1080,iw)':h=-2:flags=lanczos" in commands[0]
    assert "20M" in commands[0]
    assert commands[0][commands[0].index("-profile:v") + 1] == "baseline"
    assert commands[0][commands[0].index("-g") + 1] == "60"
    assert commands[0][commands[0].index("-r") + 1] == "30"
    assert _instagram_compatible_video(source) == normalized
    assert len(commands) == 1

    recovery = _instagram_compatible_video(source, recovery_profile=True)
    assert recovery == tmp_path / "publish_ru.instagram.v3.mp4"
    assert "scale=w='min(720,iw)':h=-2:flags=lanczos" in commands[1]
    assert "2500k" in commands[1]


def test_vk_readiness_requires_expected_user_identity(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_COMMUNITY_TOKEN_RU", "community")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_GROUP_ID_RU", "238619215")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID", "")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_USER_SCREEN_NAME", "")

    readiness = platform_readiness("grandma_kitchen", "vk", "ru")

    assert readiness.ready is False
    assert readiness.missing == ("PUBLISH_GRANDMA_KITCHEN_VK_USER_ID",)


def test_vk_wall_denial_is_not_success_and_retry_reuses_video(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_GROUP_ID_RU", "238619215")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_VK_COMMUNITY_TOKEN_RU", "community")
    video_path = tmp_path / "vk.mp4"
    video_path.write_bytes(b"video")
    job = PublicationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        bundle_key="bundle",
        platform="vk",
        language="ru",
        video_path=str(video_path),
        description="Описание",
    )
    monkeypatch.setattr("app.social_publishers._vk_access_token", lambda _project: "token")
    monkeypatch.setattr("app.social_publishers._verify_vk_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video", lambda p, **_kwargs: p)

    def response(payload: dict) -> Mock:
        result = Mock(ok=True, status_code=200)
        result.json.return_value = payload
        return result

    calls: list[tuple[str, dict]] = []

    def post(url: str, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/groups.getById"):
            assert kwargs["data"]["access_token"] == "community"
            assert "group_id" not in kwargs["data"]
            return response({"response": {"groups": [{"id": 238619215}]}})
        if url.endswith("/video.save"):
            assert kwargs["data"]["access_token"] == "token"
            assert kwargs["data"]["wallpost"] == 0
            return response({
                "response": {
                    "upload_url": "https://upload.vk.example/video",
                    "owner_id": -238619215,
                    "video_id": 42,
                }
            })
        if url.startswith("https://upload.vk.example/"):
            return response({"owner_id": -238619215, "video_id": 42})
        if url.endswith("/video.get"):
            return response({"response": {"items": [{
                "owner_id": -238619215,
                "id": 42,
            }]}})
        assert url.endswith("/wall.post")
        assert kwargs["data"]["access_token"] == "community"
        assert kwargs["data"]["guid"] == f"content-fabric-wall-{job.id}"
        return response({"error": {"error_code": 15, "error_msg": "Access denied"}})

    monkeypatch.setattr("app.social_publishers.requests.post", post)

    persist = Mock()
    with pytest.raises(PublicationDeliveryError, match="Access denied"):
        _publish_vk(job, "grandma_kitchen", persist)
    assert job.payload_json["external_post_url"] == "https://vk.com/video-238619215_42"
    assert "VK wall.post: Access denied" in job.payload_json["vk_wall_post_error"]
    assert job.external_post_id == "-238619215_42"
    assert job.payload_json["vk_wall_create_started_at"] is None
    assert persist.call_count >= 4
    calls.clear()
    with pytest.raises(PublicationDeliveryError, match="Access denied"):
        _publish_vk(job, "grandma_kitchen", persist)
    assert [url.rsplit('/', 1)[-1] for url, _ in calls] == [
        "groups.getById",
        "video.get",
        "wall.post",
    ]


def test_vk_readiness_accepts_refresh_credentials_without_static_token(monkeypatch) -> None:
    monkeypatch.delenv("PUBLISH_GRANDMA_KITCHEN_VK_ACCESS_TOKEN", raising=False)
    for key, value in {
        "VK_CLIENT_ID": "54871992",
        "VK_REFRESH_TOKEN": "refresh",
        "VK_DEVICE_ID": "device",
        "VK_REDIRECT_URI": "http://localhost/callback",
        "VK_USER_ID": "1114556285",
        "VK_GROUP_ID_RU": "238619215",
        "VK_COMMUNITY_TOKEN_RU": "community",
    }.items():
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", value)

    readiness = platform_readiness("grandma_kitchen", "vk", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_vk_access_token_refreshes_and_persists_rotated_values(monkeypatch) -> None:
    from contextlib import nullcontext
    monkeypatch.setattr("app.social_publishers.locked_vk_credentials", lambda _project: nullcontext())
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


def test_tiktok_creator_info_normalizes_account_capabilities(monkeypatch) -> None:
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "data": {
            "creator_nickname": "У бабушки дома",
            "creator_username": "grandma",
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
            "comment_disabled": False,
            "duet_disabled": True,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 180,
        },
        "error": {"code": "ok"},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr("app.social_publishers.requests.post", post)

    payload = tiktok_creator_info("grandma_kitchen", access_token="access")

    assert payload == {
        "creator_nickname": "У бабушки дома",
        "creator_username": "grandma",
        "creator_avatar_url": "",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": True,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 180,
    }
    post.assert_called_once_with(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={"Authorization": "Bearer access", "Content-Type": "application/json; charset=UTF-8"},
        json={},
        timeout=(20, 300),
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
