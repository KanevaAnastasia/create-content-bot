import pytest
from app import marketer as m
from app import marketer_vk as vk


def test_vk_metrics_are_video_only(monkeypatch):
    monkeypatch.setattr(vk, "project_env", lambda *a: "123")
    monkeypatch.setattr(vk, "read_vk", lambda *a, **kw: {"items": [{
        "owner_id": -123, "id": 45, "views": 0, "comments": 2,
        "likes": {"count": 3}, "reposts": {"count": 1}}]})
    result = m.collect_metrics({"platform": "vk", "project_slug": "test", "external_post_id": "-123_45"}, "token")
    assert result["metrics"] == {"views": 0, "reach": None, "likes": 3, "comments": 2, "saves": None, "shares": 1}


def test_vk_wrong_project_is_rejected_before_request(monkeypatch):
    monkeypatch.setattr(vk, "project_env", lambda *a: "123")
    monkeypatch.setattr(vk, "read_vk", lambda *a, **kw: pytest.fail("Must not query wrong owner"))
    with pytest.raises(m.MetricsUnavailable):
        vk.video_metrics({"project_slug": "test", "external_post_id": "-999_45"}, "token")


def test_vk_followers_identity_and_zero(monkeypatch):
    monkeypatch.setattr(vk, "project_env", lambda *a: "123")
    monkeypatch.setattr(vk, "read_vk", lambda *a, **kw: {"groups": [{"id": 123, "members_count": 0}]})
    assert m.collect_followers("test", "vk", "ru", "token")["followers"] == 0


def test_vk_read_retries_rate_limit(monkeypatch):
    from app import social_publishers
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise social_publishers.PublicationDeliveryError("VK: Too many requests (code 6)")
        return {"items": []}
    monkeypatch.setattr(social_publishers, "_vk_call", request)
    monkeypatch.setattr(vk.time, "sleep", lambda _: None)
    assert vk.read_vk("test", "video.get", "token") == {"items": []}
    assert len(calls) == 3
