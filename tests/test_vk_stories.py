import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import social_publishers as p


@pytest.fixture
def story_job(tmp_path, monkeypatch):
    paths = [tmp_path / f'{i}.mp4' for i in range(3)]
    for path in paths:
        path.write_bytes(b'video')
    monkeypatch.setattr(p, '_vk_story_files', lambda job: paths)
    return SimpleNamespace(id=uuid.uuid4(), payload_json={}, video_path=str(paths[0]))


def setup_api(monkeypatch, *, fail_save=0, verify=True, owner=-123):
    calls = []
    saves = []
    def api(method, token, version, **params):
        assert token == 'community-secret'
        calls.append(method)
        if method == 'stories.getVideoUploadServer':
            assert params == {'group_id': '123', 'add_to_news': 1}
            return {'upload_url': 'https://upload.vk.com/story'}
        if method == 'stories.save':
            saves.append(params)
            if len(saves) == fail_save:
                raise p.PublicationDeliveryError('timeout with secret')
            return {'items': [{'owner_id': owner, 'id': len(saves)}]}
        if method == 'stories.getById':
            number = int(params['stories'].split('_')[1])
            return {'items': [{'owner_id': -123, 'id': number, 'type': 'video',
                              'video': {'id': 90} if verify else {}}]}
        raise AssertionError(method)
    upload = Mock(return_value=Mock(ok=True, json=lambda: {'response': {'upload_result': 'opaque-result'}}))
    monkeypatch.setattr(p, '_vk_call', api)
    monkeypatch.setattr(p.requests, 'post', upload)
    return calls, saves, upload


def test_all_parts_published_sequentially_and_never_repeated(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch)
    snapshots = []
    persist = lambda: snapshots.append([dict(x) for x in story_job.payload_json['vk_story_parts']])
    p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', persist)
    assert calls == ['stories.getVideoUploadServer', 'stories.save', 'stories.getById'] * 3
    parts = story_job.payload_json['vk_story_parts']
    assert all(x['verified'] for x in parts)
    assert parts[-1]['url'] == 'https://vk.com/story-123_3'
    assert 'upload_result' not in parts[0]
    assert any(x[0].get('save_started_at') and not x[0].get('id') for x in snapshots)
    assert upload.call_args.kwargs['allow_redirects'] is False
    assert 'headers' not in upload.call_args.kwargs
    calls.clear()
    p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', persist)
    assert calls == []
    assert upload.call_count == 3


def test_interrupted_second_part_preserves_first_and_blocks_duplicates(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch, fail_save=2)
    with pytest.raises(p.PublicationNeedsReview, match='save not confirmed') as error:
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    assert 'secret' not in str(error.value)
    parts = story_job.payload_json['vk_story_parts']
    assert parts[0]['verified']
    assert parts[1]['save_started_at']
    assert not parts[2].get('upload_started_at')
    calls.clear()
    with pytest.raises(p.PublicationNeedsReview, match='outcome is unknown'):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    assert not calls
    assert upload.call_count == 2


def test_saved_story_retry_only_verifies(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch, verify=False)
    with pytest.raises(p.PublicationNeedsReview, match='not verified'):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    calls.clear()
    with pytest.raises(p.PublicationNeedsReview):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    assert calls == ['stories.getById']
    assert upload.call_count == 1


def test_wrong_story_owner_is_not_success(story_job, monkeypatch):
    setup_api(monkeypatch, owner=-456)
    with pytest.raises(p.PublicationNeedsReview, match='owner/ID'):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)


def test_unknown_upload_does_not_publish_or_reupload(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch)
    upload.side_effect = requests.Timeout('secret')
    with pytest.raises(p.PublicationNeedsReview):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    original_api = p._vk_call

    def api(method, token, version, **params):
        if method == 'stories.get':
            return {'count': 1, 'items': [{'id': 9}]}
        return original_api(method, token, version, **params)

    monkeypatch.setattr(p, '_vk_call', api)
    with pytest.raises(p.PublicationNeedsReview):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)
    assert not saves
    assert upload.call_count == 1


def test_unknown_upload_recovers_only_after_empty_story_check(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch)
    story_job.payload_json = {
        'vk_story_parts': [{'path': str(Path(story_job.video_path)), 'upload_started_at': '2026-01-01T00:00:00+00:00'}]
    }

    original_api = p._vk_call

    def api(method, token, version, **params):
        if method == 'stories.get':
            assert params == {'owner_id': '-123', 'extended': 1}
            calls.append(method)
            return {'count': 0, 'items': []}
        return original_api(method, token, version, **params)

    monkeypatch.setattr(p, '_vk_call', api)
    p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)

    part = story_job.payload_json['vk_story_parts'][0]
    assert part['verified'] is True
    assert part['upload_started_at'] != '2026-01-01T00:00:00+00:00'
    assert part['upload_recovery_checked_at']
    assert calls[0] == 'stories.get'
    assert upload.call_count == 1


def test_unknown_upload_stays_blocked_when_active_story_exists(story_job, monkeypatch):
    calls, saves, upload = setup_api(monkeypatch)
    story_job.payload_json = {
        'vk_story_parts': [{'path': str(Path(story_job.video_path)), 'upload_started_at': '2026-01-01T00:00:00+00:00'}]
    }

    original_api = p._vk_call

    def api(method, token, version, **params):
        if method == 'stories.get':
            calls.append(method)
            return {'count': 1, 'items': [{'id': 9}]}
        return original_api(method, token, version, **params)

    monkeypatch.setattr(p, '_vk_call', api)
    with pytest.raises(p.PublicationNeedsReview, match='active stories exist'):
        p._publish_vk_stories(story_job, 'community-secret', '123', '5.199', lambda: None)

    assert calls == ['stories.get']
    assert upload.call_count == 0


def test_media_split_preserves_full_duration_with_safe_encoding(tmp_path, monkeypatch):
    from app import ai_approval_service as media
    source = tmp_path / 'master.mp4'
    source.write_bytes(b'video')
    job = SimpleNamespace(id=uuid.uuid4(), video_path=str(source))
    monkeypatch.setattr(media, '_resolve_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr(media, '_video_duration_seconds', lambda path: 35 if Path(path) == source else 14)
    commands = []
    def run(command, **kw):
        commands.append(command)
        Path(command[-1]).write_bytes(b'encoded')
    monkeypatch.setattr(p.subprocess, 'run', run)
    paths = p._vk_story_files(job)
    assert [c[c.index('-ss') + 1] for c in commands] == ['0', '14', '28']
    assert [c[c.index('-t') + 1] for c in commands] == ['14', '14', '7']
    assert all(path.is_file() for path in paths)
    assert all('libx264' in c and 'aac' in c for c in commands)
