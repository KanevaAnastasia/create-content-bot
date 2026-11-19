import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.instagram_recovery import (
    instagram_recovery_available,
    instagram_recovery_request,
    instagram_recovery_requested,
    temporary_instagram_video,
)


@pytest.fixture
def media(tmp_path, monkeypatch):
    path = tmp_path / "private-video.mp4"
    path.write_bytes(b"exact approved video")
    reply = Mock(status_code=200)
    reply.json.return_value = {"status": "success", "data": {"url": "https://tmpfiles.org/123/movie.mp4"}}
    post = Mock(return_value=reply)
    download = Mock(status_code=200)
    download.iter_content.return_value = [path.read_bytes()]
    context = Mock()
    context.__enter__ = Mock(return_value=download)
    context.__exit__ = Mock(return_value=False)
    get = Mock(return_value=context)
    monkeypatch.setattr("app.instagram_recovery.requests.post", post)
    monkeypatch.setattr("app.instagram_recovery.requests.get", get)
    snapshots = []
    return path, post, get, download, snapshots, lambda value: snapshots.append(copy.deepcopy(value))


def test_upload_is_expiring_scoped_and_verified_then_reused(media):
    path, post, get, _, snapshots, checkpoint = media
    url = temporary_instagram_video(path, None, checkpoint)
    assert url == "https://tmpfiles.org/dl/123/movie.mp4"
    args, kwargs = post.call_args
    assert args == ("https://tmpfiles.org/api/v1/upload",)
    assert kwargs["data"] == {"expire": "3600"}
    assert "headers" not in kwargs
    assert path.name not in kwargs["files"]["file"][0]
    assert kwargs["allow_redirects"] is False
    assert snapshots[0]["verified"] is False
    assert snapshots[1]["verified"] is True
    assert temporary_instagram_video(path, snapshots[-1], checkpoint) == url
    assert post.call_count == get.call_count == 1


def test_failed_verification_resumes_same_uploaded_file(media):
    path, post, get, _, snapshots, checkpoint = media
    original = get.return_value
    get.side_effect = requests.Timeout("lost")
    with pytest.raises(ValueError, match="verification failed"):
        temporary_instagram_video(path, None, checkpoint)
    get.side_effect = None
    get.return_value = original
    temporary_instagram_video(path, snapshots[-1], checkpoint)
    assert post.call_count == 1
    assert snapshots[-1]["verified"] is True


@pytest.mark.parametrize("change", ["expired", "different_file"])
def test_expired_or_changed_source_is_uploaded_again(media, change):
    path, post, _, download, snapshots, checkpoint = media
    temporary_instagram_video(path, None, checkpoint)
    source = snapshots[-1]
    if change == "expired":
        source["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    else:
        path.write_bytes(b"new approved video")
        download.iter_content.return_value = [path.read_bytes()]
    temporary_instagram_video(path, source, checkpoint)
    assert post.call_count == 2


@pytest.mark.parametrize("content", [b"wrong", b"longer than the approved file contents"])
def test_hash_mismatch_never_marks_source_verified(media, content):
    path, post, _, download, snapshots, checkpoint = media
    download.iter_content.return_value = [content]
    with pytest.raises(ValueError, match="does not match"):
        temporary_instagram_video(path, None, checkpoint)
    assert snapshots[-1]["verified"] is False
    download.iter_content.return_value = [path.read_bytes()]
    temporary_instagram_video(path, snapshots[-1], checkpoint)
    assert post.call_count == 2


def test_prematurely_deleted_source_can_be_uploaded_again(media):
    path, post, _, download, snapshots, checkpoint = media
    download.status_code = 404
    with pytest.raises(ValueError, match="HTTP 404"):
        temporary_instagram_video(path, None, checkpoint)
    download.status_code = 200
    temporary_instagram_video(path, snapshots[-1], checkpoint)
    assert post.call_count == 2


@pytest.mark.parametrize("url", [
    "https://attacker.example/movie.mp4", "http://tmpfiles.org/123/movie.mp4",
    "https://tmpfiles.org@127.0.0.1/123/movie.mp4", "https://tmpfiles.org/123/movie.mp4?secret=x",
    "https://tmpfiles.org/dl/../movie.mp4",
])
def test_hosting_response_cannot_redirect_download_elsewhere(media, url):
    path, post, get, _, snapshots, checkpoint = media
    post.return_value.json.return_value["data"]["url"] = url
    with pytest.raises(ValueError, match="unexpected media URL"):
        temporary_instagram_video(path, None, checkpoint)
    get.assert_not_called()
    assert snapshots == []


def test_oversized_file_is_not_uploaded(media):
    path, post, _, _, _, checkpoint = media
    with path.open("wb") as source:
        source.truncate(100_000_001)
    with pytest.raises(ValueError, match="100 MB"):
        temporary_instagram_video(path, None, checkpoint)
    post.assert_not_called()


def test_recovery_consent_is_bound_to_job_project_language_and_content(media):
    path = media[0]
    job = SimpleNamespace(id="job", project_id="project", language="ru", platform="instagram",
                          video_path=str(path), description="approved caption", payload_json={})
    request = instagram_recovery_request(job, "admin")
    job.payload_json["instagram_url_recovery"] = request
    assert instagram_recovery_requested(job)
    for field in ("id", "project_id", "language", "description"):
        original = getattr(job, field)
        setattr(job, field, "changed")
        with pytest.raises(ValueError, match="content changed"):
            instagram_recovery_requested(job)
        setattr(job, field, original)
    path.write_bytes(b"edited file")
    with pytest.raises(ValueError, match="content changed"):
        instagram_recovery_requested(job)


@pytest.mark.parametrize("blocked", [
    {"status": "published"}, {"status": "publishing"}, {"status": "approved"},
    {"external_post_id": "media"}, {"platform": "youtube"},
    {"state": {"publish_ambiguous": True}}, {"state": {"media_id": "media"}},
    {"state": {"status_code": "PUBLISHED"}},
])
def test_recovery_action_never_overrides_live_or_published_job(blocked):
    job = SimpleNamespace(platform="instagram", status="failed", external_post_id=None,
                          payload_json={"instagram_upload": {"stage": "upload_rejected"}})
    assert instagram_recovery_available(job)
    for key, value in blocked.items():
        if key == "state":
            job.payload_json["instagram_upload"].update(value)
        else:
            setattr(job, key, value)
    assert not instagram_recovery_available(job)
