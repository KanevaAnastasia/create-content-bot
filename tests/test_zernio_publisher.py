import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import zernio_publisher as z
from app.social_publishers import platform_readiness, tiktok_creator_info, publish_publication_job

ACCOUNT = "a" * 24
POST = "b" * 24


@pytest.fixture
def configured(monkeypatch):
    values = {"TIKTOK_PROVIDER": "zernio", "ZERNIO_API_KEY": "secret-test-key",
              "ZERNIO_TIKTOK_ACCOUNT_ID": ACCOUNT, "ZERNIO_TIKTOK_USERNAME": "grandma"}
    lookup = lambda project, key, default="": values.get(key, default) if project == "grandma_kitchen" else default
    monkeypatch.setattr(z, "project_env", lookup)
    monkeypatch.setattr("app.social_publishers.project_env", lookup)
    return values


@pytest.fixture
def job():
    return SimpleNamespace(id=uuid.uuid4(), video_path="/tmp/test.mp4", title="Test", description="Recipe #food",
                           payload_json={"tiktok_privacy_level": "PUBLIC_TO_EVERYONE",
                                         "tiktok_music_usage_confirmed": True, "tiktok_publish_confirmed": True,
                                         "tiktok_is_aigc": True, "tiktok_provider": "zernio"})


def published():
    return {"post": {"_id": POST, "status": "published", "platforms": [{"platform": "tiktok",
            "accountId": ACCOUNT, "status": "published", "platformPostId": "123",
            "platformPostUrl": "https://www.tiktok.com/@grandma/video/123"}]}}


def test_published_placeholder_waits_for_final_tiktok_permalink(job):
    data = {"post": {"_id": POST, "status": "published", "platforms": [{
        "platform": "tiktok", "accountId": ACCOUNT, "status": "published",
        "platformPostId": "v_pub_url~temporary", "platformPostUrl": "",
    }]}}
    job.payload_json["zernio_account_id"] = ACCOUNT
    with pytest.raises(z.ZernioPending, match="final TikTok permalink"):
        z.apply_post(job, data, Mock())


def fake_client(monkeypatch):
    client = Mock()
    client.upload.return_value = "https://media.zernio.com/temp/video.mp4"
    client.request.return_value = published()
    monkeypatch.setattr(z, "ZernioClient", lambda *a, **kw: client)
    monkeypatch.setattr(z, "creator_info", lambda _: {"privacy_level_options": ["PUBLIC_TO_EVERYONE"],
                       "can_post_more": True, "comment_disabled": False, "duet_disabled": True,
                       "stitch_disabled": False})
    return client


def test_readiness_is_project_scoped(configured):
    assert platform_readiness("grandma_kitchen", "tiktok", "ru").ready
    assert not platform_readiness("discipline", "tiktok", "ru").ready


def test_account_guard_and_free_cap(configured, monkeypatch):
    client = z.ZernioClient("grandma_kitchen")
    request = Mock(return_value={"accounts": [{"_id": ACCOUNT, "platform": "tiktok", "username": "wrong"}]})
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(z.ZernioError, match="username differs"):
        client.verify_account(ACCOUNT, "grandma")
    request.return_value = {"accounts": [{}, {}, {}]}
    with pytest.raises(z.ZernioError, match="More than 2"):
        client.accounts()


def test_create_persists_before_write_and_maps_settings(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    job.payload_json.update(tiktok_allow_comment=True, tiktok_allow_duet=True, cover_path="/tmp/cover.jpg")
    snapshots = []
    persist = lambda: snapshots.append(dict(job.payload_json))
    def respond(*args, **kwargs):
        assert snapshots[-1]["zernio_create_started_at"]
        assert snapshots[-1]["zernio_request"] == kwargs["json"]
        return published()
    client.request.side_effect = respond
    assert z.publish(job, "grandma_kitchen", persist) == "123"
    body = client.request.call_args.kwargs["json"]
    assert body["platforms"] == [{"platform": "tiktok", "accountId": ACCOUNT}]
    assert body["tiktokSettings"]["videoMadeWithAi"] is True
    assert body["tiktokSettings"]["allowDuet"] is False
    assert body["tiktokSettings"]["allowComment"] is True
    assert body["tiktokSettings"]["videoCoverImageUrl"]
    assert client.request.call_args.kwargs["headers"]["Idempotency-Key"] == str(job.id)
    assert job.payload_json["external_post_url"].endswith("/123")


@pytest.mark.parametrize("field", ["tiktok_music_usage_confirmed", "tiktok_publish_confirmed"])
def test_no_upload_without_consent(configured, monkeypatch, job, field):
    client = fake_client(monkeypatch)
    job.payload_json.pop(field)
    with pytest.raises(z.ZernioError, match="explicitly confirm"):
        z.publish(job, "grandma_kitchen", lambda: None)
    client.upload.assert_not_called()
    client.request.assert_not_called()


def test_pending_is_not_published_or_recreated(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    data = published()
    data["post"].update(status="scheduled")
    data["post"]["platforms"][0].update(status="pending")
    client.request.return_value = data
    with pytest.raises(z.ZernioPending):
        z.publish(job, "grandma_kitchen", lambda: None)
    assert job.payload_json["zernio_post_id"] == POST
    client.request.reset_mock()
    client.upload.reset_mock()
    with pytest.raises(z.ZernioPending):
        z.publish(job, "grandma_kitchen", lambda: None)
    client.request.assert_called_once_with("GET", f"/posts/{POST}")
    client.upload.assert_not_called()


def test_failed_remote_job_uses_retry_endpoint(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    job.payload_json.update(zernio_post_id=POST, zernio_account_id=ACCOUNT)
    client.request.side_effect = [{"post": {"status": "failed"}}, published()]
    assert z.publish(job, "grandma_kitchen", lambda: None) == "123"
    assert [call.args for call in client.request.call_args_list] == [
        ("GET", f"/posts/{POST}"), ("POST", f"/posts/{POST}/retry")]
    client.upload.assert_not_called()


def test_old_unknown_request_requires_reconciliation(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    job.payload_json["zernio_create_started_at"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with pytest.raises(z.ZernioNeedsReview, match="23 hours"):
        z.publish(job, "grandma_kitchen", lambda: None)
    client.upload.assert_not_called()
    client.request.assert_not_called()


def test_account_switch_never_retargets_old_post(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    job.payload_json.update(zernio_account_id="c" * 24, zernio_post_id=POST)
    with pytest.raises(z.ZernioNeedsReview, match="account changed"):
        z.publish(job, "grandma_kitchen", lambda: None)
    client.request.assert_not_called()


def test_draft_and_wrong_account_not_success(job):
    job.payload_json["zernio_account_id"] = ACCOUNT
    data = published()
    data["post"]["platforms"][0]["accountId"] = "c" * 24
    with pytest.raises(z.ZernioNeedsReview, match="different target"):
        z.apply_post(job, data, lambda: None)
    data = published()
    data["post"]["platforms"][0]["platformSpecificData"] = {"isDraft": True}
    with pytest.raises(z.ZernioNeedsReview, match="not published"):
        z.apply_post(job, data, lambda: None)


def test_refresh_does_not_publish_or_retry(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    job.payload_json.update(zernio_account_id=ACCOUNT, zernio_post_id=POST)
    assert z.refresh(job, "grandma_kitchen", lambda: None) == "123"
    client.request.assert_called_once_with("GET", f"/posts/{POST}")


def test_creator_info_is_normalized(configured, monkeypatch):
    monkeypatch.setattr(z.ZernioClient, "verify_account", lambda *a: {"username": "grandma"})
    monkeypatch.setattr(z.ZernioClient, "request", lambda *a, **kw: {
        "creator": {"nickname": "Grandma"}, "privacyLevels": [{"value": "PUBLIC_TO_EVERYONE"}],
        "postingLimits": {"maxVideoDurationSec": 600, "interactionSettings": {"allow_comment": {"enabled": True}}}})
    info = tiktok_creator_info("grandma_kitchen")
    assert info["creator_username"] == "grandma"
    assert info["privacy_level_options"] == ["PUBLIC_TO_EVERYONE"]
    assert info["comment_disabled"] is False
    assert info["duet_disabled"] is True


def test_api_error_does_not_leak_secret(configured, monkeypatch):
    monkeypatch.setattr(z.requests, "request", lambda *a, **kw: Mock(status_code=401,
        json=lambda: {"code": "invalid_credentials", "error": "secret-test-key"}))
    with pytest.raises(z.ZernioError) as exc:
        z.ZernioClient("grandma_kitchen").accounts()
    assert "secret-test-key" not in str(exc.value)


def test_media_upload_never_sends_api_key_to_storage(configured, monkeypatch, tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = z.ZernioClient("grandma_kitchen")
    monkeypatch.setattr(client, "request", lambda *a, **kw: {
        "uploadUrl": "https://bucket.r2.cloudflarestorage.com/file?signed=yes",
        "publicUrl": "https://media.zernio.com/temp/file.mp4"})
    put = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(z.requests, "put", put)
    client.upload(path)
    assert put.call_args.kwargs["headers"] == {"Content-Type": "video/mp4"}
    assert put.call_args.kwargs["allow_redirects"] is False


def test_large_media_is_prepared_once_before_zernio_upload(monkeypatch, tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"large")
    monkeypatch.setattr(z, "DIRECT_UPLOAD_MAX_BYTES", 3)
    monkeypatch.setattr("app.ai_approval_service._resolve_ffmpeg", lambda: "/fake/ffmpeg")

    calls = []
    def transcode(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"small")
    monkeypatch.setattr(z.subprocess, "run", transcode)

    prepared = z._delivery_video(source)
    assert prepared.name == "video.zernio.v1.mp4"
    assert prepared.read_bytes() == b"small"
    assert len(calls) == 1
    assert z._delivery_video(source) == prepared
    assert len(calls) == 1


@pytest.mark.parametrize("outcome, expected", [
    (z.ZernioPending("queued"), "publishing"),
    (z.ZernioNeedsReview("unknown"), "needs_manual_action"),
    (z.ZernioError("failed"), "failed"),
    ("123", "published"),
])
def test_dispatcher_respects_remote_outcome(configured, monkeypatch, tmp_path, job, outcome, expected):
    video = tmp_path / "test.mp4"
    video.write_bytes(b"video")
    job.video_path = str(video)
    job.project = SimpleNamespace(slug="grandma_kitchen")
    job.platform, job.language, job.status = "tiktok", "ru", "approved"
    job.scheduled_at, job.attempt_count = None, 0
    session = Mock()
    session.scalar.return_value = job
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("app.social_publishers.SessionLocal", lambda: manager)
    sender = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    monkeypatch.setattr(z, "publish", sender)
    result = publish_publication_job(job.id)
    assert result["status"] == expected
    assert job.status == expected
    assert job.attempt_count == 1


def test_unknown_retry_reuses_persisted_body_and_key(configured, monkeypatch, job):
    client = fake_client(monkeypatch)
    saved_body = {"content": "Original", "publishNow": True}
    job.payload_json.update(zernio_account_id=ACCOUNT, zernio_request=saved_body,
                            zernio_create_started_at=datetime.now(timezone.utc).isoformat())
    z.publish(job, "grandma_kitchen", lambda: None)
    client.upload.assert_not_called()
    assert client.request.call_args.kwargs["json"] is saved_body
    assert client.request.call_args.kwargs["headers"] == {"Idempotency-Key": str(job.id)}
