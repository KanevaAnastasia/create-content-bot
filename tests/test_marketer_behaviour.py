from types import SimpleNamespace

from app import marketer as m


def test_instagram_extended_metrics_partial_failure(monkeypatch):
    def get(url, token, params):
        if url.endswith("/comments"):
            return {"data": []}
        if "fields" in params:
            return {"id": "123", "like_count": 2, "comments_count": 0}
        name = params["metric"]
        if name == "follows":
            raise m.MetricsUnavailable("Instagram API не поддерживает эту метрику для данного типа публикации.")
        return {"data": [{"name": name, "values": [{"value": {"ig_reels_avg_watch_time": 17709, "ig_reels_video_view_total_time": 3600000, "reels_skip_rate": 26.2}.get(name, 0)}]}]}
    monkeypatch.setattr(m, "api_get", get)
    result = m.collect_metrics({"platform": "instagram", "external_post_id": "123"}, "secret")
    assert result["metrics"]["views"] == 0
    assert result["metrics"]["subscribers_gained"] is None
    from admin_panel.pipeline_admin.marketer_display import behaviour_summary
    display = behaviour_summary({"platform": "instagram", **result})
    assert display["average_view_seconds"] == 17.71
    assert display["total_watch_hours"] == 1
    assert display["skip_rate"] == 26.2
    assert "не поддерживает" in display["subscribers_warning"]


def test_comment_sample_is_bounded_and_has_no_usernames():
    from app.marketer_comments import summarize_comments
    result = summarize_comments([{"text": "Как заменить молоко?", "username": "private"}] * 35, True)
    assert result["count"] == 30 and result["more"]
    assert result["groups"][0]["count"] == 30
    assert "username" not in result["items"][0]
    assert len(result["groups"][0]["examples"]) == 3


def test_unsupported_metric_error_does_not_leak_provider_message(monkeypatch):
    import pytest
    response = SimpleNamespace(ok=False, status_code=400, json=lambda: {"error": {"code": 100, "message": "does not support follows for this media product type SECRET"}})
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: response)
    with pytest.raises(m.MetricsUnavailable) as exc:
        m.api_get("https://example.com", "secret", {})
    assert "не поддерживает" in str(exc.value) and "SECRET" not in str(exc.value)


def test_safe_formatting():
    from admin_panel.pipeline_admin.marketer_display import format_brief
    html = format_brief('## План\n**Тест**\n- пункт\n<script>alert(1)</script>\n[link](javascript:x)')
    assert '<h3>План</h3>' in html
    assert '<strong>Тест</strong>' in html
    assert '<ul><li>пункт</li></ul>' in html
    assert '<script>' not in html and '<a' not in html


def test_retention_drops_do_not_invent_likes():
    from admin_panel.pipeline_admin.marketer_display import behaviour_summary
    report = behaviour_summary({'platform': 'youtube', 'external_post_id': 'abcdefghijk', 'behaviour': {
        'duration_seconds': 40, 'subscribers_gained': 0,
        'retention': [{'position': 10, 'watch_ratio': 120}, {'position': 20, 'watch_ratio': 80}, {'position': 30, 'watch_ratio': 90}]}})
    assert report['subscribers_gained'] == 0
    assert report['drops'][0]['drop'] == 40
    assert report['drops'][0]['seconds'] == 4
    assert report['drops'][0]['url'].endswith('&t=4s')
    assert 'likes' not in report


def test_hidden_subscribers_not_zero(monkeypatch):
    monkeypatch.setattr(m, 'project_env', lambda *a: 'channel')
    monkeypatch.setattr(m, 'api_get', lambda *a: {'items': [{'id': 'channel', 'statistics': {'hiddenSubscriberCount': True, 'subscriberCount': 0}}]})
    assert m.collect_followers('discipline', 'youtube', 'ru', 'token')['followers'] is None


def test_behaviour_matches_headers_and_preserves_zero(monkeypatch):
    monkeypatch.setattr(m, 'project_env', lambda *a: 'channel')
    def get(url, token, params):
        if '/videos' in url:
            return {'items': [{'snippet': {'channelId': 'channel'}, 'contentDetails': {'duration': 'PT1M'}}]}
        if params.get('dimensions'):
            return {'columnHeaders': [{'name': 'audienceWatchRatio'}, {'name': 'elapsedVideoTimeRatio'}], 'rows': [[1.2, .1], [.8, .2]]}
        return {'columnHeaders': [{'name': 'subscribersGained'}, {'name': 'averageViewDuration'}], 'rows': [[0, 15]]}
    monkeypatch.setattr(m, 'api_get', get)
    result = m.collect_behaviour({'platform': 'youtube', 'external_post_id': 'abcdefghijk', 'project_slug': 'discipline', 'language': 'ru'}, 'token')
    assert result['subscribers_gained'] == 0
    assert result['subscribers_lost'] is None
    assert result['retention'][0]['watch_ratio'] == 120
    assert result['duration_seconds'] == 60
