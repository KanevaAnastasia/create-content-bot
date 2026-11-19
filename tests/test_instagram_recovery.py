"""Never turn an uncertain Instagram publish into a duplicate Reel."""

import copy
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app.models import PublicationJob
from app.instagram_recovery import instagram_recovery_request
from app.social_publishers import (
    PublicationDeliveryError,
    PublicationNeedsReview,
    _publish_instagram,
    _verify_instagram_publish_identity,
)


@pytest.fixture(autouse=True)
def isolated_publication_config(monkeypatch):
    # Live .env values must not leak into deterministic publisher tests.
    monkeypatch.setattr("app.publication_config.load_env_file", lambda: None)
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v25.0")


def response(payload, ok=True):
    result = Mock(ok=ok, status_code=200 if ok else 400)
    result.json.return_value = payload
    return result


@pytest.fixture
def upload(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.setattr("app.social_publishers._instagram_access_token", lambda *args: "secret")
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video", lambda p, **_kwargs: p)
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


def reject_initial_upload(upload, monkeypatch):
    job, _, _, status, persist = upload
    original = requests.post
    status.update(status_code="IN_PROGRESS")

    def post(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({"debug_info": {"retriable": False, "type": "ProcessingFailedError"}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    with pytest.raises(PublicationDeliveryError):
        _publish_instagram(job, "discipline", persist)
    monkeypatch.setattr("app.social_publishers.requests.post", original)
    job.payload_json["instagram_url_recovery"] = instagram_recovery_request(job, "operator")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_admin_recovery_needs_no_env_and_keeps_account_language_caption(upload, monkeypatch, language):
    job, calls, _, status, persist = upload
    job.language = language
    monkeypatch.setenv(f"PUBLISH_DISCIPLINE_INSTAGRAM_BUSINESS_ACCOUNT_ID_{language.upper()}", "account")
    reject_initial_upload(upload, monkeypatch)
    hosted = Mock(return_value="https://tmpfiles.org/dl/123/exact.mp4")
    monkeypatch.setattr("app.instagram_recovery.temporary_instagram_video", hosted)
    original = requests.post

    def post(url, **kwargs):
        if url.endswith("/account/media"):
            assert kwargs["data"]["video_url"] == hosted.return_value
            assert kwargs["data"]["caption"] == job.description
            assert "upload_type" not in kwargs["data"]
            status.update(status_code="FINISHED")
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", post)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    hosted.assert_called_once()
    assert len([u for u, _ in calls if u.endswith("/media_publish")]) == 1
    assert job.payload_json["instagram_upload"]["previous_containers"][0]["creation_id"] == "container"


@pytest.mark.parametrize("remote_status", ["FINISHED", "PUBLISHED", "IN_PROGRESS"])
def test_admin_recovery_checks_provider_before_hosting(upload, monkeypatch, remote_status):
    job, calls, _, status, persist = upload
    reject_initial_upload(upload, monkeypatch)
    status.update(status_code=remote_status)
    if remote_status == "IN_PROGRESS":
        job.payload_json["instagram_upload"]["publish_ambiguous"] = True
    hosted = Mock()
    monkeypatch.setattr("app.instagram_recovery.temporary_instagram_video", hosted)
    if remote_status == "FINISHED":
        assert _publish_instagram(job, "discipline", persist) == "published-media"
    else:
        with pytest.raises(PublicationNeedsReview):
            _publish_instagram(job, "discipline", persist)
        assert not [u for u, _ in calls if u.endswith("/media_publish")]
    hosted.assert_not_called()
    assert len([u for u, _ in calls if u.endswith("/account/media")]) == 1


def test_admin_recovery_storage_failure_retries_without_meta_duplicate(upload, monkeypatch):
    job, calls, _, _, persist = upload
    reject_initial_upload(upload, monkeypatch)
    hosted = Mock(side_effect=ValueError("temporary host unavailable"))
    monkeypatch.setattr("app.instagram_recovery.temporary_instagram_video", hosted)
    for _ in range(2):
        with pytest.raises(ValueError, match="host unavailable"):
            _publish_instagram(job, "discipline", persist)
    assert len([u for u, _ in calls if u.endswith("/account/media")]) == 1
    assert job.payload_json["instagram_upload"]["stage"] == "awaiting_url_upload"


def test_admin_recovery_always_hosts_compact_derivative_for_dead_container(upload, monkeypatch, tmp_path):
    job, _, _, status, persist = upload
    reject_initial_upload(upload, monkeypatch)
    compact = tmp_path / "compact.mp4"
    compact.write_bytes(b"small video")
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video",
                        lambda path, **kw: compact if kw.get("recovery_profile") else path)
    status.update(status_code="ERROR")
    hosted = Mock(side_effect=ValueError("stop before Meta"))
    monkeypatch.setattr("app.instagram_recovery.temporary_instagram_video", hosted)
    with pytest.raises(ValueError, match="stop before Meta"):
        _publish_instagram(job, "discipline", persist)
    assert hosted.call_args.args[0] == compact


def test_changed_admin_recovery_consent_stops_before_network(upload, monkeypatch):
    job, calls, _, _, persist = upload
    reject_initial_upload(upload, monkeypatch)
    job.description = "changed after click"
    before = len(calls)
    with pytest.raises(PublicationNeedsReview, match="content changed"):
        _publish_instagram(job, "discipline", persist)
    assert len(calls) == before


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


def test_explicit_upload_rejection_replaces_stuck_unpublished_container(upload, monkeypatch):
    job, calls, _, status, persist = upload
    status.update(status_code="IN_PROGRESS", status="Processing")
    original = requests.post

    def reject(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({"debug_info": {"type": "ProcessingFailedError", "retriable": False}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", reject)
    with pytest.raises(PublicationDeliveryError, match="ProcessingFailedError"):
        _publish_instagram(job, "discipline", persist)
    assert job.payload_json["instagram_upload"]["stage"] == "upload_rejected"

    def recover(url, **kwargs):
        result = original(url, **kwargs)
        if "rupload.facebook.com" in url:
            status.update(status_code="FINISHED", status="Ready")
        return result

    monkeypatch.setattr("app.social_publishers.requests.post", recover)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([url for url, _ in calls if url.endswith('/account/media')]) == 2
    assert job.payload_json['instagram_upload']['previous_containers'][0]['status_code'] == 'IN_PROGRESS'


def test_rejected_upload_that_later_finishes_reuses_original_profile(
    upload, monkeypatch, tmp_path
):
    job, calls, _, status, persist = upload
    status.update(status_code="IN_PROGRESS", status="Processing")
    normal = tmp_path / "video.instagram.v2.mp4"
    recovery = tmp_path / "video.instagram.v3.mp4"
    normal.write_bytes(b"normal-profile")
    recovery.write_bytes(b"recovery-profile")
    monkeypatch.setattr(
        "app.social_publishers._instagram_compatible_video",
        lambda _path, **kwargs: recovery if kwargs.get("recovery_profile") else normal,
    )
    original = requests.post

    def reject(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({
                "debug_info": {
                    "type": "ProcessingFailedError",
                    "retriable": False,
                }
            }, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", reject)
    with pytest.raises(PublicationDeliveryError, match="ProcessingFailedError"):
        _publish_instagram(job, "discipline", persist)
    saved_fingerprint = job.payload_json["instagram_upload"]["fingerprint"]

    status.update(status_code="FINISHED", status="Ready")
    monkeypatch.setattr("app.social_publishers.requests.post", original)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([url for url, _ in calls if url.endswith("/account/media")]) == 1
    assert job.payload_json["instagram_upload"]["fingerprint"] == saved_fingerprint


def test_explicit_upload_rejection_can_retry_regenerated_video(upload, monkeypatch, tmp_path):
    job, calls, _, status, persist = upload
    status.update(status_code="IN_PROGRESS", status="Processing")
    original = requests.post

    def reject(url, **kwargs):
        if "rupload.facebook.com" in url:
            return response({"debug_info": {"type": "ProcessingFailedError", "retriable": False}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr("app.social_publishers.requests.post", reject)
    with pytest.raises(PublicationDeliveryError, match="ProcessingFailedError"):
        _publish_instagram(job, "discipline", persist)

    replacement = tmp_path / "video.instagram.v2.mp4"
    replacement.write_bytes(b"stricter-video")
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video", lambda _path, **_kwargs: replacement)

    def recover(url, **kwargs):
        result = original(url, **kwargs)
        if "rupload.facebook.com" in url:
            status.update(status_code="FINISHED", status="Ready")
        return result

    monkeypatch.setattr("app.social_publishers.requests.post", recover)
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    assert len([url for url, _ in calls if url.endswith("/account/media")]) == 2
    assert job.payload_json["instagram_upload"]["previous_containers"][0]["upload_error"] == "ProcessingFailedError"


def test_rejected_upload_never_replaced_after_ambiguous_publish(upload, monkeypatch):
    job, calls, replies, status, persist = upload
    replies[:] = [requests.Timeout('lost')]
    with pytest.raises(PublicationNeedsReview):
        _publish_instagram(job, 'discipline', persist)
    job.payload_json['instagram_upload']['stage'] = 'upload_rejected'
    status.update(status_code='IN_PROGRESS')
    before = len(calls)
    with pytest.raises(PublicationNeedsReview):
        _publish_instagram(job, 'discipline', persist)
    assert len(calls) == before


@pytest.mark.parametrize("transport", ["zernio", "public_media"])
def test_project_enabled_url_fallback_publishes_only_new_ready_container(upload, monkeypatch, transport):
    job, calls, _, status, persist = upload
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU', transport)
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_MEDIA_BASE_URL', 'https://media.example.com')
    hosted = Mock(return_value='https://media.zernio.com/test.mp4')
    monkeypatch.setattr('app.social_publishers._instagram_hosted_video', hosted)
    original = requests.post
    status.update(status_code='IN_PROGRESS')
    create_bodies = []

    def post(url, **kwargs):
        if url.endswith('/account/media'):
            create_bodies.append(dict(kwargs['data']))
            if 'video_url' in kwargs['data']:
                status.update(status_code='FINISHED')
                return response({'id': 'url-container'})
        if 'rupload.facebook.com' in url:
            result = response({'debug_info': {'retriable': False, 'type': 'ProcessingFailedError'}}, ok=False)
            result.headers = {'x-fb-request-id': 'safe-request-id'}
            return result
        return original(url, **kwargs)

    monkeypatch.setattr('app.social_publishers.requests.post', post)
    assert _publish_instagram(job, 'discipline', persist) == 'published-media'
    hosted.assert_called_once()
    assert len(create_bodies) == 2
    assert create_bodies[0]['upload_type'] == 'resumable'
    assert create_bodies[1]['video_url'] == 'https://media.zernio.com/test.mp4'
    assert 'upload_type' not in create_bodies[1]
    assert create_bodies[0]['caption'] == create_bodies[1]['caption']
    published = [kwargs['data']['creation_id'] for url, kwargs in calls if url.endswith('/media_publish')]
    assert published == ['url-container']
    state = job.payload_json['instagram_upload']
    assert state['upload_transport'] == 'url_pull'
    assert state['previous_containers'][0]['upload_request_id'] == 'safe-request-id'
    assert 'video_url' not in str(state)


@pytest.mark.parametrize('enabled', [False, True])
def test_network_uncertainty_never_activates_url_fallback(upload, monkeypatch, enabled):
    job, _, _, status, persist = upload
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU', 'zernio' if enabled else '')
    hosted = Mock()
    monkeypatch.setattr('app.social_publishers._instagram_hosted_video', hosted)
    original = requests.post
    status.update(status_code='IN_PROGRESS')

    def post(url, **kwargs):
        if 'rupload.facebook.com' in url:
            raise requests.Timeout('upload response lost')
        return original(url, **kwargs)

    monkeypatch.setattr('app.social_publishers.requests.post', post)
    with pytest.raises(requests.Timeout):
        _publish_instagram(job, 'discipline', persist)
    hosted.assert_not_called()
    assert job.payload_json['instagram_upload']['creation_id'] == 'container'


def test_storage_failure_resumes_url_path_without_repeating_rejected_upload(upload, monkeypatch):
    job, calls, _, status, persist = upload
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU', 'zernio')
    hosted = Mock(side_effect=PublicationDeliveryError('storage unavailable'))
    monkeypatch.setattr('app.social_publishers._instagram_hosted_video', hosted)
    original = requests.post
    status.update(status_code='IN_PROGRESS')

    def post(url, **kwargs):
        if 'rupload.facebook.com' in url:
            return response({'debug_info': {'retriable': False, 'type': 'ProcessingFailedError'}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr('app.social_publishers.requests.post', post)
    for _ in range(2):
        with pytest.raises(PublicationDeliveryError, match='storage unavailable'):
            _publish_instagram(job, 'discipline', persist)
    assert len([url for url, _ in calls if url.endswith('/account/media')]) == 1
    assert job.payload_json['instagram_upload']['stage'] == 'awaiting_url_upload'
    assert hosted.call_count == 2


def test_url_fallback_is_opt_in_even_for_explicit_rejection(upload, monkeypatch):
    job, _, _, status, persist = upload
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU', '')
    hosted = Mock()
    monkeypatch.setattr('app.social_publishers._instagram_hosted_video', hosted)
    original = requests.post
    status.update(status_code='IN_PROGRESS')

    def post(url, **kwargs):
        if 'rupload.facebook.com' in url:
            return response({'debug_info': {'retriable': False, 'type': 'ProcessingFailedError'}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr('app.social_publishers.requests.post', post)
    with pytest.raises(PublicationDeliveryError):
        _publish_instagram(job, 'discipline', persist)
    hosted.assert_not_called()


def test_hosted_video_compresses_once_and_preserves_source(monkeypatch, tmp_path):
    from pathlib import Path
    from app.social_publishers import _instagram_hosted_video
    source = tmp_path / 'source.mp4'
    with source.open('wb') as f:
        f.truncate(21 * 1024 * 1024)
    stamp = source.stat().st_mtime_ns
    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b'compatible')

    monkeypatch.setattr('app.ai_approval_service._resolve_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr('app.social_publishers.subprocess.run', encode)
    client = Mock()
    client.upload.return_value = 'https://media.zernio.com/file.mp4'
    monkeypatch.setattr('app.social_publishers.zernio_publisher.ZernioClient', lambda slug: client)
    for _ in range(2):
        assert _instagram_hosted_video(source, 'test') == client.upload.return_value
    assert len(commands) == 1
    assert client.upload.call_args.args[0].read_bytes() == b'compatible'
    assert source.stat().st_size == 21 * 1024 * 1024
    assert source.stat().st_mtime_ns == stamp


def test_hosted_video_encoding_failure_never_uploads_partial_file(monkeypatch, tmp_path):
    from pathlib import Path
    import subprocess
    from app.social_publishers import _instagram_hosted_video
    source = tmp_path / 'source.mp4'
    with source.open('wb') as f:
        f.truncate(21 * 1024 * 1024)

    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(b'partial')
        raise subprocess.CalledProcessError(1, 'ffmpeg')

    monkeypatch.setattr('app.ai_approval_service._resolve_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr('app.social_publishers.subprocess.run', fail)
    client = Mock()
    monkeypatch.setattr('app.social_publishers.zernio_publisher.ZernioClient', client)
    with pytest.raises(PublicationDeliveryError, match='preparation failed'):
        _instagram_hosted_video(source, 'test')
    client.assert_not_called()
    assert list(tmp_path.iterdir()) == [source]


def test_ru_fallback_setting_does_not_enable_other_language(upload, monkeypatch):
    job, _, _, status, persist = upload
    job.language = 'en'
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_BUSINESS_ACCOUNT_ID_EN', 'account')
    monkeypatch.setenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU', 'zernio')
    monkeypatch.delenv('PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_EN', raising=False)
    hosted = Mock()
    monkeypatch.setattr('app.social_publishers._instagram_hosted_video', hosted)
    original = requests.post
    status.update(status_code='IN_PROGRESS')

    def post(url, **kwargs):
        if 'rupload.facebook.com' in url:
            return response({'debug_info': {'retriable': False, 'type': 'ProcessingFailedError'}}, ok=False)
        return original(url, **kwargs)

    monkeypatch.setattr('app.social_publishers.requests.post', post)
    with pytest.raises(PublicationDeliveryError):
        _publish_instagram(job, 'discipline', persist)
    hosted.assert_not_called()


def test_public_media_url_is_bound_to_exact_video(tmp_path, monkeypatch):
    import hashlib
    from app.social_publishers import _instagram_hosted_video
    source = tmp_path / "video.mp4"
    source.write_bytes(b"approved")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_MEDIA_BASE_URL", "https://media.example.com/private-prefix/")
    expected = hashlib.sha256(b"approved").hexdigest()
    assert _instagram_hosted_video(source, "discipline") == f"https://media.example.com/private-prefix/{expected}.mp4"
    source.write_bytes(b"changed")
    assert expected not in _instagram_hosted_video(source, "discipline")


@pytest.mark.parametrize("base", ["http://example.com", "https://user:secret@example.com", "https://example.com?key=secret", "https://example.com#fragment"])
def test_public_media_base_rejects_unsafe_urls(tmp_path, monkeypatch, base):
    from app.social_publishers import _instagram_hosted_video
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_MEDIA_BASE_URL", base)
    with pytest.raises(PublicationNeedsReview):
        _instagram_hosted_video(tmp_path / "unused.mp4", "discipline")


def test_expiring_media_manifest_rejects_wrong_file_and_expired_url(tmp_path, monkeypatch):
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone
    from app.social_publishers import _instagram_hosted_video
    source = tmp_path / "video.mp4"
    source.write_bytes(b"approved")
    digest = hashlib.sha256(b"approved").hexdigest()
    manifest = tmp_path / "sources.json"
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_MEDIA_URLS_FILE", str(manifest))
    entry = {"url": "https://media.example.com/video.mp4", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    manifest.write_text(json.dumps({digest: entry}))
    assert _instagram_hosted_video(source, "discipline") == entry["url"]
    entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    manifest.write_text(json.dumps({digest: entry}))
    with pytest.raises(PublicationNeedsReview):
        _instagram_hosted_video(source, "discipline")
    manifest.write_text(json.dumps({"wrong-digest": entry}))
    with pytest.raises(PublicationNeedsReview):
        _instagram_hosted_video(source, "discipline")


def test_pending_url_recovery_keeps_compact_delivery_profile(upload, monkeypatch):
    job, calls, _, status, persist = upload
    job.payload_json = {"instagram_upload": {"account_id": "account", "upload_transport": "url_pull", "stage": "awaiting_url_upload"}}
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_URL_FALLBACK_RU", "public_media")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_INSTAGRAM_MEDIA_BASE_URL", "https://media.example.com")
    from pathlib import Path
    compatible = Mock(return_value=Path(job.video_path))
    monkeypatch.setattr("app.social_publishers._instagram_compatible_video", compatible)
    monkeypatch.setattr("app.social_publishers._instagram_hosted_video", Mock(return_value="https://media.example.com/exact.mp4"))
    status.update(status_code="FINISHED")
    assert _publish_instagram(job, "discipline", persist) == "published-media"
    compatible.assert_called_once_with(Path(job.video_path), recovery_profile=True)
    assert not any("rupload.facebook.com" in url for url, _ in calls)
