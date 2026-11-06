from types import SimpleNamespace

from app import marketer as m


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
