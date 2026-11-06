"""Never turn an uncertain Instagram publish into a duplicate Reel."""

import copy
import uuid
from unittest.mock import Mock

import pytest
import requests

from app.models import PublicationJob
from app.social_publishers import (
    PublicationDeliveryError,
    PublicationNeedsReview,
    _publish_instagram,
    _verify_instagram_publish_identity,
)


def response(payload, ok=True):
    result = Mock(ok=ok, status_code=200 if ok else 400)
    result.json.return_value = payload
    return result


@pytest.fixture
def upload(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.setattr("app.social_publishers._instagram_access_token", lambda *args: "secret")
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video", lambda p: p)
    monkeypatch.setattr("app.social_publishers._verify_instagram_publish_identity", lambda *args: {"account_id": "account"})
    monkeypatch.setattr("app.social_publishers.time.sleep", lambda seconds: None)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    job = PublicationJob(id=uuid.uuid4(), project_id=uuid.uuid4(), platform="instagram",
                         language="ru", bundle_key="test", video_path=str(path),
                         description="Почему не ты?", payload_json={})
    calls = []
    snapshots = []
    replies = [response({"id": "published-media"})]
    status = {"status_code": "FINISHED"}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/account/media"):
            return response({"id": "container", "uri": "https://rupload.facebook.com/upload"})
        if "rupload.facebook.com" in url:
            assert snapshots[-1]["instagram_upload"]["creation_id"] == "container"
            return response({"success": True})
        assert url.endswith("/account/media_publish")
        assert snapshots[-1]["instagram_upload"]["publish_ambiguous"] is True
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(url, **kwargs):
        if url.endswith("/published-media"):
            return response({"permalink": "https://www.instagram.com/reel/test/"})
        return response(status)

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    monkeypatch.setattr("app.social_publishers.requests.get", get)
    persist = lambda: snapshots.append(copy.deepcopy(job.payload_json))
    return job, calls, replies, status, persist


def test_checkpoints_confirmed_publish_and_reuses_id(upload):
    job, calls, _, _, persist = upload
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert job.payload_json["external_post_url"].endswith("/test/")
    before = len(calls)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len(calls) == before
    assert "secret" not in str(job.payload_json)


def test_internal_error_retries_only_same_ready_container(upload):
    job, calls, replies, _, persist = upload
    replies.insert(0, response({"error": {"code": -1, "error_subcode": 2207085}}, ok=False))
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([u for u, _ in calls if u.endswith("/account/media")]) == 1
    publishes = [kw for u, kw in calls if u.endswith("/media_publish")]
    assert len(publishes) == 2
    assert {kw["data"]["creation_id"] for kw in publishes} == {"container"}


def test_internal_error_is_bounded_and_retry_reuses_upload(upload):
    job, calls, replies, _, persist = upload
    replies[:] = [response({"error": {"code": -1, "error_subcode": 2207085}}, ok=False)] * 3
    with pytest.raises(PublicationDeliveryError):
        _publish_instagram(job, "discipline", persist)
    assert len([u for u, _ in calls if u.endswith("/media_publish")]) == 3
    replies.append(response({"id": "published-media"}))
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([u for u, _ in calls if "rupload.facebook.com" in u]) == 1


def test_lost_publish_reply_never_posts_again(upload):
    job, calls, replies, _, persist = upload
    replies[:] = [requests.Timeout("lost")]
    with pytest.raises(PublicationNeedsReview, match="response was lost"):
        _publish_instagram(job, "discipline", persist)
    before = len(calls)
    with pytest.raises(PublicationNeedsReview, match="may have succeeded"):
        _publish_instagram(job, "discipline", persist)
    assert len(calls) == before


def test_already_published_container_requires_reconciliation(upload):
    job, calls, replies, status, persist = upload
    replies[:] = [requests.Timeout("lost")]
    with pytest.raises(PublicationNeedsReview):
        _publish_instagram(job, "discipline", persist)
    status["status_code"] = "PUBLISHED"
    before = len(calls)
    with pytest.raises(PublicationNeedsReview):
        _publish_instagram(job, "discipline", persist)
    assert len(calls) == before


@pytest.mark.parametrize("change", ["account", "caption", "video"])
def test_changed_input_cannot_reuse_container(upload, monkeypatch, change):
    job, calls, replies, _, persist = upload
    replies[:] = [response({"error": {"code": 10}}, ok=False)]
    with pytest.raises(PublicationDeliveryError):
        _publish_instagram(job, "discipline", persist)
    if change == "account":
        monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "different")
    elif change == "caption":
        job.description = "Changed"
    else:
        from pathlib import Path
        Path(job.video_path).write_bytes(b"different-video")
    before = len(calls)
    with pytest.raises(PublicationNeedsReview):
        _publish_instagram(job, "discipline", persist)
    assert len(calls) == before


def test_caption_is_not_silently_truncated(upload):
    job, calls, _, _, persist = upload
    job.description = "я" * 2201
    with pytest.raises(PublicationDeliveryError, match="2200"):
        _publish_instagram(job, "discipline", persist)
    assert not calls


def test_upload_error_preserves_container_diagnostics(upload, monkeypatch):
    job, calls, _, status, persist = upload
    status.update(status_code="ERROR", status="Unsupported video")
    original = requests.post

    def post(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({"debug_info": {"type": "ProcessingFailedError"}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    with pytest.raises(PublicationDeliveryError, match="ProcessingFailedError"):
        _publish_instagram(job, "discipline", persist)
    assert job.payload_json["instagram_upload"]["status_detail"] == "Unsupported video"
    assert not any(u.endswith("/media_publish") for u, _ in calls)
    # If Meta later reports success, reuse the uploaded container.
    status.update(status_code="FINISHED", status="Ready")
    monkeypatch.setattr("app.social_publishers.requests.post", original)
    assert _publish_instagram(job, "discipline", persist) == "published-media"


def test_terminal_unpublished_container_can_be_replaced(upload, monkeypatch):
    job, calls, _, status, persist = upload
    status.update(status_code="ERROR", status="Rejected encoding")
    with pytest.raises(PublicationDeliveryError, match="Rejected encoding"):
        _publish_instagram(job, "discipline", persist)
    original = requests.post

    def post(url, **kwargs):
        result = original(url, **kwargs)
        if "rupload.facebook.com" in url:
            status.update(status_code="FINISHED", status="Ready")
        return result

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([u for u, _ in calls if u.endswith("/account/media")]) == 2
    assert job.payload_json["instagram_upload"]["previous_containers"] == [
        {"creation_id": "container", "status_code": "ERROR", "status_detail": "Rejected encoding"}
    ]


def test_upload_rejected_but_container_ready_uses_same_container(upload, monkeypatch):
    job, calls, _, _, persist = upload
    original = requests.post

    def post(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({"debug_info": {"type": "ProcessingFailedError"}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([u for u, _ in calls if u.endswith("/account/media")]) == 1


@pytest.mark.parametrize("linked", ["account", "grandma", None])
def test_page_token_must_belong_to_target_instagram(monkeypatch, linked):
    replies = [response({"data": {"is_valid": True, "type": "PAGE", "scopes": ["instagram_content_publish"]}}),
               response({"id": "page", "name": "Page", "instagram_business_account": {"id": linked}}),
               response({"id": "account", "username": "quietdiscipline_ru"})]
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(side_effect=replies))
    if linked == "account":
        assert _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")["username"] == "quietdiscipline_ru"
    else:
        with pytest.raises(PublicationNeedsReview, match="not configured account"):
            _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")


@pytest.mark.parametrize("targets", [["account"], ["different"], []])
def test_user_token_checks_granular_publishing_grant(monkeypatch, targets):
    replies = [response({"data": {"is_valid": True, "type": "USER", "scopes": ["instagram_content_publish"],
                                   "granular_scopes": [{"scope": "instagram_content_publish", "target_ids": targets}]}}),
               response({"id": "account", "username": "quietdiscipline_en"})]
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(side_effect=replies))
    if targets == ["account"]:
        assert _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")["token_type"] == "USER"
    else:
        with pytest.raises(PublicationNeedsReview, match="permission does not include"):
            _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")


def test_user_grant_can_name_the_linked_facebook_page(monkeypatch):
    replies = [response({"data": {"is_valid": True, "type": "USER", "scopes": ["instagram_content_publish"],
                                   "granular_scopes": [{"scope": "instagram_content_publish", "target_ids": ["page"]}]}}),
               response({"id": "page", "instagram_business_account": {"id": "account"}}),
               response({"id": "account", "username": "quietdiscipline_ru"})]
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(side_effect=replies))
    assert _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")["username"] == "quietdiscipline_ru"


@pytest.mark.parametrize("debug", [{"is_valid": False}, {"is_valid": True, "scopes": ["instagram_basic"]}])
def test_invalid_or_read_only_token_rejected(monkeypatch, debug):
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(return_value=response({"data": debug})))
    with pytest.raises(PublicationNeedsReview, match="invalid or lacks"):
        _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")


def test_identity_network_error_never_exposes_token(monkeypatch):
    monkeypatch.setattr("app.social_publishers.requests.get", Mock(side_effect=requests.Timeout("url?input_token=secret")))
    with pytest.raises(PublicationDeliveryError) as error:
        _verify_instagram_publish_identity("https://graph.facebook.com/v25.0", "secret", "account")
    assert "secret" not in str(error.value)
