from unittest.mock import Mock
import pytest
from app import marketer as m
from app import marketer_tiktok as tt


def test_video_metrics_and_missing_values(monkeypatch):
    monkeypatch.setattr(tt, "project_env", lambda *a: "owner")
    monkeypatch.setattr(tt, "read", lambda endpoint, *a: {"user": {"open_id": "owner"}} if endpoint == "user/info" else {"videos": [{"id": "123", "view_count": 0, "like_count": 2}]})
    result = m.collect_metrics({"platform": "tiktok", "project_slug": "grandma_kitchen", "external_post_id": "123"}, "token")
    assert result["metrics"] == dict(views=0, likes=2, comments=None, shares=None, reach=None, saves=None)


def test_wrong_account_rejected(monkeypatch):
    monkeypatch.setattr(tt, "project_env", lambda *a: "expected")
    monkeypatch.setattr(tt, "read", lambda *a: {"user": {"open_id": "other"}})
    with pytest.raises(m.MetricsUnavailable, match="не совпадает"):
        tt.video_metrics({"project_slug": "test", "external_post_id": "123"}, "token")


def test_followers_zero(monkeypatch):
    monkeypatch.setattr(tt, "account", lambda *a, **kw: {"open_id": "owner", "follower_count": 0})
    assert m.collect_followers("test", "tiktok", "ru", "token")["followers"] == 0


@pytest.mark.parametrize("code,success", [("ok", True), ("scope_not_authorized", False)])
def test_error_envelope(monkeypatch, code, success):
    response = Mock(ok=True)
    response.json.return_value = {"data": {"user": {}}, "error": {"code": code}}
    monkeypatch.setattr(tt.requests, "get", Mock(return_value=response))
    if success:
        assert tt.read("user/info", "token", "open_id") == {"user": {}}
    else:
        with pytest.raises(m.MetricsUnavailable, match="video.list"):
            tt.read("user/info", "token", "open_id")
