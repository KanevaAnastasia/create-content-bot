"""No network: exercise wall delivery and interruption-safe retries."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import social_publishers as p


@pytest.fixture
def configured(monkeypatch):
    values = {"VK_GROUP_ID_RU": "123", "VK_COMMUNITY_TOKEN_RU": "wall-secret",
              "VK_USER_ID": "7", "VK_ACCESS_TOKEN": "video-secret"}
    monkeypatch.setattr(p, "project_env", lambda project, key, default="":
                        values.get(key, default) if project == "grandma" else default)
    monkeypatch.setattr(p, "_vk_access_token", lambda project: "video-secret")
    monkeypatch.setattr(p, "_verify_vk_identity", lambda *a, **kw: None)
    monkeypatch.setattr(p, "_instagram_compatible_video", lambda path, **kwargs: path)
    return values


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"test")
    return SimpleNamespace(id=uuid.uuid4(), language="ru", video_path=str(path),
                           description="Recipe\r\n#food", title="Drink", external_post_id=None,
                           payload_json={})


def response(data):
    return Mock(ok=True, status_code=200, json=lambda: data)


def setup_api(monkeypatch, job, *, wall_effect=None, own_group=123, attachment=True):
    calls = []
    def post(url, **kw):
        calls.append((url, kw))
        method = url.rsplit('/', 1)[-1]
        if method == "groups.getById":
            assert kw['data']['access_token'] == 'wall-secret'
            return response({'response': {'groups': [{'id': own_group}]}})
        if method == "video.save":
            assert job.payload_json['vk_upload_started_at']
            assert kw['data']['access_token'] == 'video-secret'
            return response({'response': {'upload_url': 'https://pu.vk.com/upload',
                                          'owner_id': -123, 'video_id': 42}})
        if method == "video.get":
            assert kw['data']['access_token'] == 'video-secret'
            assert kw['data']['videos'] == '-123_42'
            return response({'response': {'items': [{'owner_id': -123, 'id': 42}]}})
        if method == "upload":
            assert 'access_token' not in kw
            return response({'owner_id': -123, 'video_id': 42})
        if method == "wall.post":
            assert job.external_post_id == '-123_42'
            assert job.payload_json['vk_wall_create_started_at']
            assert kw['data']['access_token'] == 'wall-secret'
            assert kw['data']['attachments'] == 'video-123_42'
            if wall_effect:
                return wall_effect()
            return response({'response': {'post_id': 99}})
        if method == "wall.get":
            assert kw['data']['access_token'] == 'video-secret'
            return response({'response': {'items': [{'id': 99, 'owner_id': -123,
                'from_id': -123, 'text': 'Recipe\n#food', 'attachments':
                [{'type': 'video', 'video': {'owner_id': -123, 'id': 42}}] if attachment else []}]}})
        raise AssertionError(method)
    monkeypatch.setattr(p.requests, 'post', post)
    return calls


def test_upload_wall_and_verification_are_checkpointed(configured, monkeypatch, job):
    calls = setup_api(monkeypatch, job)
    snapshots = []
    assert p._publish_vk(job, 'grandma', lambda: snapshots.append(dict(job.payload_json))) == '-123_42'
    assert job.payload_json['external_post_url'] == 'https://vk.com/wall-123_99'
    assert job.payload_json['vk_wall_post_verified'] is True
    assert any(x.get('vk_video_external_id') and not x.get('vk_wall_post_id') for x in snapshots)
    calls.clear()
    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    assert [url.rsplit('/', 1)[-1] for url, _ in calls] == ['groups.getById', 'wall.get']


def test_story_option_marks_manual_clip_repost_after_verified_wall(configured, monkeypatch, job):
    setup_api(monkeypatch, job)
    p._publish_vk(job, 'grandma', lambda: None)
    assert 'vk_story_share_mode' not in job.payload_json
    job.payload_json = {**job.payload_json, 'vk_stories': True}
    persist = Mock()
    p._publish_vk(job, 'grandma', persist)
    assert job.payload_json['vk_wall_post_verified']
    assert job.payload_json['vk_story_share_mode'] == 'manual_clip_repost'
    assert job.payload_json['vk_story_share_status'] == 'required'
    assert job.payload_json['vk_story_share_source_url'] == 'https://vk.com/clip-123_42'
    assert persist.called


def test_story_share_marker_retry_never_repeats_video_or_wall(configured, monkeypatch, job):
    calls = setup_api(monkeypatch, job)
    job.payload_json = {'vk_stories': True}
    p._publish_vk(job, 'grandma', lambda: None)
    assert job.payload_json['vk_wall_post_verified']
    calls.clear()
    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    assert [url.rsplit('/', 1)[-1] for url, _ in calls] == ['groups.getById', 'wall.get']


def test_unknown_wall_result_never_reposts(configured, monkeypatch, job):
    def timeout():
        raise requests.Timeout('request body includes secret')
    calls = setup_api(monkeypatch, job, wall_effect=timeout)
    with pytest.raises(p.PublicationNeedsReview, match='response unavailable') as e:
        p._publish_vk(job, 'grandma', lambda: None)
    assert 'secret' not in str(e.value)
    calls.clear()
    with pytest.raises(p.PublicationNeedsReview, match='outcome is unknown'):
        p._publish_vk(job, 'grandma', lambda: None)
    assert len(calls) == 1


def test_missing_attachment_is_not_success(configured, monkeypatch, job):
    calls = setup_api(monkeypatch, job, attachment=False)
    with pytest.raises(p.PublicationNeedsReview, match='not verified'):
        p._publish_vk(job, 'grandma', lambda: None)
    assert job.payload_json['vk_wall_post_id'] == '99'
    calls.clear()
    with pytest.raises(p.PublicationNeedsReview):
        p._publish_vk(job, 'grandma', lambda: None)
    assert [url.rsplit('/', 1)[-1] for url, _ in calls] == ['groups.getById', 'wall.get']


def test_wrong_community_token_blocks_before_upload(configured, monkeypatch, job):
    calls = setup_api(monkeypatch, job, own_group=456)
    with pytest.raises(p.PublicationDeliveryError, match='different community'):
        p._publish_vk(job, 'grandma', lambda: None)
    assert len(calls) == 1
    assert not job.payload_json


@pytest.mark.parametrize('state', [{'vk_group_id': '456'},
    {'vk_upload_started_at': '2026-01-01'}, {'vk_video_external_id': '-456_42'}])
def test_saved_target_or_unknown_upload_blocks_retry(configured, monkeypatch, job, state):
    job.payload_json = state
    calls = setup_api(monkeypatch, job)
    with pytest.raises(p.PublicationNeedsReview):
        p._publish_vk(job, 'grandma', lambda: None)
    assert len(calls) <= 1


def test_allocated_video_is_verified_and_reused_after_lost_upload_response(configured, monkeypatch, job):
    job.payload_json = {
        'vk_upload_started_at': '2026-01-01T00:00:00+00:00',
        'vk_allocated_video_id': '-123_42',
    }
    calls = setup_api(monkeypatch, job)
    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    assert job.external_post_id == '-123_42'
    methods = [url.rsplit('/', 1)[-1] for url, _ in calls]
    assert methods == ['groups.getById', 'video.get', 'wall.post', 'wall.get']


def test_lost_upload_response_reconciles_allocated_id_without_manual_dead_end(
    configured, monkeypatch, job
):
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def lose_upload_response(url, **kwargs):
        result = original(url, **kwargs)
        if url.rsplit('/', 1)[-1] == 'upload':
            result.json = Mock(side_effect=ValueError("invalid response"))
        return result

    monkeypatch.setattr(p.requests, 'post', lose_upload_response)

    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    assert job.external_post_id == '-123_42'
    methods = [url.rsplit('/', 1)[-1] for url, _ in calls]
    assert methods == [
        'groups.getById', 'video.save', 'upload', 'video.get', 'wall.post', 'wall.get'
    ]


def test_allocated_video_waits_for_vk_processing_before_wall_post(configured, monkeypatch, job):
    job.payload_json = {
        'vk_upload_started_at': p.datetime.now(p.timezone.utc).isoformat(),
        'vk_allocated_video_id': '-123_42',
    }
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def pending(url, **kwargs):
        result = original(url, **kwargs)
        if url.rsplit('/', 1)[-1] == 'video.get':
            result.json = lambda: {'response': {'items': [{'owner_id': -123, 'id': 42, 'processing': 1}]}}
        return result

    monkeypatch.setattr(p.requests, 'post', pending)
    with pytest.raises(p.PublicationDeliveryError, match='still processing'):
        p._publish_vk(job, 'grandma', lambda: None)
    assert job.payload_json['vk_video_processing'] is True
    assert [url.rsplit('/', 1)[-1] for url, _ in calls] == ['groups.getById', 'video.get']


def test_fresh_upload_waits_for_vk_processing_before_wall_post(configured, monkeypatch, job):
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def pending(url, **kwargs):
        result = original(url, **kwargs)
        if url.rsplit('/', 1)[-1] == 'video.get':
            result.json = lambda: {'response': {'items': [{'owner_id': -123, 'id': 42, 'processing': 1}]}}
        return result

    monkeypatch.setattr(p.requests, 'post', pending)
    with pytest.raises(p.PublicationDeliveryError, match='still processing'):
        p._publish_vk(job, 'grandma', lambda: None)
    assert job.payload_json['vk_video_processing'] is True
    assert 'wall.post' not in [url.rsplit('/', 1)[-1] for url, _ in calls]


def test_stale_processing_video_is_reuploaded_with_recovery_profile(configured, monkeypatch, job):
    job.payload_json = {
        'vk_upload_started_at': '2026-01-01T00:00:00+00:00',
        'vk_video_external_id': '-123_41',
    }
    calls = setup_api(monkeypatch, job)
    original = p.requests.post
    profiles = []
    monkeypatch.setattr(
        p,
        '_instagram_compatible_video',
        lambda path, **kwargs: profiles.append(bool(kwargs.get('recovery_profile'))) or path,
    )

    def video_state(url, **kwargs):
        if url.rsplit('/', 1)[-1] == 'video.get' and kwargs['data']['videos'] == '-123_41':
            calls.append((url, kwargs))
            return response({'response': {'items': [{'owner_id': -123, 'id': 41, 'processing': 1}]}})
        return original(url, **kwargs)

    monkeypatch.setattr(p.requests, 'post', video_state)
    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    assert profiles == [True]
    assert job.payload_json['vk_failed_video_external_ids'] == ['-123_41']
    assert job.payload_json['vk_video_recovery_reason'] == 'processing_timeout'


def test_recovery_video_processing_is_preserved_instead_of_allocating_another_duplicate(
    configured, monkeypatch, job
):
    job.external_post_id = '-123_41'
    job.payload_json = {
        'vk_upload_started_at': (
            datetime.now(timezone.utc) - timedelta(minutes=31)
        ).isoformat(),
        'vk_video_external_id': '-123_41',
        'vk_video_recovery_profile': True,
    }
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def processing(url, **kwargs):
        if url.rsplit('/', 1)[-1] == 'video.get':
            calls.append((url, kwargs))
            return response({'response': {'items': [{
                'owner_id': -123, 'id': 41, 'processing': 1,
            }]}})
        return original(url, **kwargs)

    monkeypatch.setattr(p.requests, 'post', processing)

    with pytest.raises(p.PublicationDeliveryError, match='still processing'):
        p._publish_vk(job, 'grandma', lambda: None)

    assert job.payload_json['vk_video_external_id'] == '-123_41'
    assert job.external_post_id == '-123_41'
    assert 'video.save' not in [url.rsplit('/', 1)[-1] for url, _ in calls]


def test_recovery_video_processing_stops_background_polling_after_two_hours(
    configured, monkeypatch, job
):
    job.external_post_id = '-123_41'
    job.payload_json = {
        'vk_upload_started_at': (
            datetime.now(timezone.utc) - timedelta(hours=2, minutes=1)
        ).isoformat(),
        'vk_video_external_id': '-123_41',
        'vk_video_recovery_profile': True,
    }
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def processing(url, **kwargs):
        if url.rsplit('/', 1)[-1] == 'video.get':
            calls.append((url, kwargs))
            return response({'response': {'items': [{
                'owner_id': -123, 'id': 41, 'processing': 1,
            }]}})
        return original(url, **kwargs)

    monkeypatch.setattr(p.requests, 'post', processing)

    with pytest.raises(p.PublicationNeedsReview, match='within 2 hours'):
        p._publish_vk(job, 'grandma', lambda: None)

    assert job.payload_json['vk_video_external_id'] == '-123_41'
    assert job.external_post_id == '-123_41'
    assert 'video.save' not in [url.rsplit('/', 1)[-1] for url, _ in calls]


def test_explicit_compact_recovery_uses_small_delivery_video(configured, monkeypatch, job):
    job.payload_json = {
        'vk_video_recovery_profile': True,
        'vk_video_compact_recovery_profile': True,
        'vk_failed_video_external_ids': ['-123_40', '-123_41'],
    }
    setup_api(monkeypatch, job)
    compact = Mock(return_value=p.Path(job.video_path))
    instagram = Mock(side_effect=AssertionError('Instagram profile must not be used'))
    monkeypatch.setattr(p.zernio_publisher, '_delivery_video', compact)
    monkeypatch.setattr(p, '_instagram_compatible_video', instagram)

    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'

    compact.assert_called_once_with(p.Path(job.video_path))
    instagram.assert_not_called()
    assert job.payload_json['vk_failed_video_external_ids'] == ['-123_40', '-123_41']


def test_corrupt_video_is_never_attached_to_wall(configured, monkeypatch, job):
    job.payload_json = {
        'vk_upload_started_at': '2026-11-13T09:00:00+00:00',
        'vk_video_external_id': '-123_41',
    }
    calls = setup_api(monkeypatch, job)
    original = p.requests.post

    def corrupt(url, **kwargs):
        if (url.rsplit('/', 1)[-1] == 'video.get'
                and kwargs['data']['videos'] == '-123_41'):
            calls.append((url, kwargs))
            return response({'response': {'items': [{
                'owner_id': -123, 'id': 41, 'title': 'Файл повреждён при загрузке',
            }]}})
        return original(url, **kwargs)

    monkeypatch.setattr(p.requests, 'post', corrupt)
    assert p._publish_vk(job, 'grandma', lambda: None) == '-123_42'
    wall_calls = [kwargs for url, kwargs in calls if url.rsplit('/', 1)[-1] == 'wall.post']
    assert len(wall_calls) == 1
    assert wall_calls[0]['data']['attachments'] == 'video-123_42'
    assert job.payload_json['vk_video_recovery_profile'] is True
    assert job.payload_json['vk_failed_video_external_ids'] == ['-123_41']


def test_wall_key_required_and_never_crosses_projects_or_languages(configured):
    assert p.platform_readiness('grandma', 'vk', 'ru').ready
    assert not p.platform_readiness('discipline', 'vk', 'ru').ready
    assert not p.platform_readiness('grandma', 'vk', 'en').ready
    configured.pop('VK_COMMUNITY_TOKEN_RU')
    assert 'PUBLISH_GRANDMA_VK_COMMUNITY_TOKEN_RU' in p.platform_readiness('grandma', 'vk', 'ru').missing


@pytest.mark.parametrize('outcome,expected', [
    (p.PublicationDeliveryError('wall rejected'), 'failed'),
    (p.PublicationNeedsReview('wall outcome unknown'), 'needs_manual_action'),
    ('-123_42', 'published'),
])
def test_dispatcher_preserves_wall_outcome_without_local_video(configured, monkeypatch, job, outcome, expected):
    job.project = SimpleNamespace(slug='grandma')
    job.platform, job.status = 'vk', 'approved'
    job.scheduled_at, job.attempt_count = None, 0
    job.video_path += '.removed'
    job.external_post_id = '-123_42'
    job.payload_json = {'vk_video_external_id': '-123_42'}
    session = Mock()
    session.scalar.return_value = job
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(p, 'SessionLocal', lambda: manager)
    sender = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    monkeypatch.setattr(p, '_publish_vk', sender)
    result = p.publish_publication_job(job.id)
    assert result['status'] == expected
    assert job.status == expected
    assert job.external_post_id == '-123_42'
    sender.assert_called_once_with(job, 'grandma', session.commit)


def test_dispatcher_reschedules_vk_processing_without_manual_retry(configured, monkeypatch, job):
    job.project = SimpleNamespace(slug='grandma')
    job.platform, job.status = 'vk', 'approved'
    job.scheduled_at, job.attempt_count = None, 0
    job.external_post_id = '-123_42'
    job.payload_json = {'vk_video_external_id': '-123_42', 'vk_video_processing': True}
    session = Mock()
    session.scalar.return_value = job
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(p, 'SessionLocal', lambda: manager)
    monkeypatch.setattr(
        p,
        '_publish_vk',
        Mock(side_effect=p.PublicationDeliveryError('VK is still processing the uploaded video.')),
    )

    result = p.publish_publication_job(job.id)

    assert result['status'] == 'scheduled'
    assert job.status == 'scheduled'
    assert job.scheduled_at is not None
    assert job.external_post_id == '-123_42'
