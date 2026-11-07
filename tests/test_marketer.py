import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import marketer as m


def test_missing_and_invalid_metrics_are_not_zero():
    assert m.number(None) is None
    assert m.number(float("nan")) is None
    assert m.number(-1) is None
    assert m.number("0") == 0


def row(id, views, **kwargs):
    return {"id": id, "project_id": "one", "project_slug": "discipline", "platform": "youtube",
            "language": "ru", "account_key": "default", "metrics": {"views": views},
            "posted_at": "2026-11-01T00:00:00+00:00", **kwargs}


def test_comparison_is_scoped_and_age_matched():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    rows = [row("a", 1000), row("b", 100), row("c", 200), row("d", 300),
            row("e", 999999, project_id="other"), row("f", 999999, language="en"),
            row("g", 999999, posted_at="2025-01-01T00:00:00Z")]
    result = m.diagnose(rows, now)[0]
    assert result["cohort_count"] == 3
    assert result["baseline"] == 200
    assert result["relative"] == 5


def test_new_video_and_missing_retention_do_not_produce_false_verdict():
    item = m.diagnose([row("a", 0)], datetime(2026, 11, 1, 1, tzinfo=timezone.utc))[0]
    assert "48 часов" in item["advice"][0]
    assert item["interaction_rate"] is None
    assert "Удержание не измерено" in item["advice"][-1]


def test_instagram_partial_failure_preserves_valid_metrics(monkeypatch):
    def get(url, token, params):
        if "metric" not in params:
            return {"like_count": 0, "comments_count": 2}
        if params["metric"] == "saved":
            raise m.MetricsUnavailable("Нет доступа")
        return {"data": [{"name": params["metric"], "values": [{"value": 10}]}]}
    monkeypatch.setattr(m, "api_get", get)
    result = m.collect_metrics({"platform": "instagram", "external_post_id": "123"}, "secret")
    assert result["metrics"]["likes"] == 0
    assert result["metrics"]["saves"] is None
    assert result["metrics"]["views"] == 10
    assert len(result["warnings"]) == 1


def test_api_errors_never_leak_credentials(monkeypatch):
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: SimpleNamespace(
        ok=False, status_code=403, json=lambda: {"error": {"message": "access_token=SECRET"}}))
    with pytest.raises(m.MetricsUnavailable) as caught:
        m.api_get("https://example.org", "SECRET", {})
    assert "SECRET" not in str(caught.value)


def test_youtube_channel_mismatch(monkeypatch):
    monkeypatch.setattr(m, "project_env", lambda *args: "correct-channel")
    monkeypatch.setattr(m, "api_get", lambda *args: {"items": [{"snippet": {"channelId": "wrong"}}]})
    with pytest.raises(m.MetricsUnavailable, match="не совпадает"):
        m.collect_metrics({"project_slug": "discipline", "platform": "youtube", "language": "ru", "external_post_id": "abc"}, "s")


@pytest.fixture
def page(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin_panel"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    import django
    django.setup()
    from pipeline_admin import marketer
    monkeypatch.setattr(marketer, "REPORT_DIR", tmp_path)
    return marketer


def test_report_reservation_isolated_and_old_worker_cannot_overwrite(page):
    a, b = uuid.uuid4(), uuid.uuid4()
    task = page.reserve_report(a, "refresh")
    assert task
    assert page.reserve_report(a, "refresh") is None
    assert page.reserve_report(b, "refresh")
    assert not page.update_report(a, "wrong-task", state="done")
    assert page.read_report(a)["state"] == "queued"
    assert page.update_report(a, task, state="done", rows=[{"id": "a"}])
    assert page.read_report(b).get("rows") is None
    with pytest.raises(ValueError):
        page.read_report("../../env")


def test_page_requires_publication_permission(page):
    from django.core.exceptions import PermissionDenied
    with pytest.raises(PermissionDenied):
        page.marketer_view(SimpleNamespace(user=SimpleNamespace(has_perm=lambda p: False)))


def test_template_renders_nulls_and_escapes_titles(page):
    from django.template.loader import render_to_string
    html = render_to_string("admin/pipeline_admin/marketer.html", {
        "visible_groups": [{"title": "<script>alert(1)</script>", "rows": [{"metric_cells": [None, 0], "advice": [], "metrics": {"views": 0}}]}],
        "report": {}, "projects": [], "can_change": False,
    })
    assert "&lt;script&gt;" in html
    assert "<td>—</td>" in html
    assert "<td>0</td>" in html
    assert "<strong>0</strong>" in html
    assert "нет дневного сравнения" in html


def test_grouping_never_uses_titles_or_crosses_projects():
    rows = [{"id": str(i), "project_id": project, "bundle_key": bundle, "title": "Same"}
            for i, (project, bundle) in enumerate([("a", "one"), ("a", "one"), ("b", "one"), ("a", ""), ("a", "")])]
    groups = m.group_videos(rows)
    assert [len(g["rows"]) for g in groups] == [2, 1, 1, 1]


def test_history_is_bounded_and_filters_dates(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(m, "project_env", lambda *a: "channel")
    calls = []
    def get(url, token, params):
        calls.append(params)
        if url.endswith("channels"):
            return {"items": [{"id": "channel", "contentDetails": {"relatedPlaylists": {"uploads": "uploads"}}}]}
        return {"nextPageToken": "more", "items": [
            {"snippet": {"title": "Recent"}, "contentDetails": {"videoId": "abcdefghijk", "videoPublishedAt": "2026-11-01T00:00:00Z"}},
            {"contentDetails": {"videoId": "oldabcdefgh", "videoPublishedAt": "2025-01-01T00:00:00Z"}}]}
    monkeypatch.setattr(m, "api_get", get)
    result = m.youtube_history("discipline", "en", "secret", datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert len(result) == 1
    assert result[0]["external_post_id"] == "abcdefghijk"
    assert len(calls) == 2
    assert calls[0]["id"] == "channel"


def test_niche_empty_results_does_not_call_videos(monkeypatch):
    calls = []
    def get(url, token, params):
        calls.append(url)
        return {"items": []}
    monkeypatch.setattr(m, "api_get", get)
    assert m.discover_niche("recipes", "en", "s") == []
    assert len(calls) == 1


def test_ai_only_sends_allowlisted_report_fields(monkeypatch):
    import app.llm_utils
    seen = []
    plan = {"summary": "План", "actions": [{"title": "Тест", "action": "Хук", "evidence": "Нет данных", "check": "Через 7 дней"}] * 3}
    monkeypatch.setattr(app.llm_utils, "gemini_generate_sync", lambda prompt, **kw: seen.append(prompt) or __import__('json').dumps(plan))
    data = row("a", 10, access_token="DO_NOT_SEND", payload_json={"secret": "DO_NOT_SEND"})
    assert m.marketing_brief("Проект", [data], [], "")["summary"] == "План"
    assert "DO_NOT_SEND" not in seen[0]
    assert "недоверенные" in seen[0]


def test_plan_rejects_unstructured_response():
    with pytest.raises(m.MetricsUnavailable):
        m.parse_marketing_plan("Обычный длинный текст")


def test_plan_cards_and_subscribers_render_safely(page):
    from django.template.loader import render_to_string
    html = render_to_string("admin/pipeline_admin/marketer.html", {
        "brief_plan": {"summary": "Решение", "actions": [{"priority": "now", "title": "<script>x</script>", "action": "Снять", "evidence": "Факт", "check": "7 дней"}]},
        "visible_groups": [{"rows": [{"metrics": {"views": 0}, "behaviour_display": {"subscribers_gained": 0, "period": "90 дней"}}]}],
    })
    assert "plan-now" in html and "&lt;script&gt;" in html
    assert "Подписались с ролика: <b>0</b>" in html
    assert "90 дней" in html


def test_background_report_keeps_project_scope(page, monkeypatch):
    project_id = uuid.uuid4()
    t = page.reserve_report(project_id, "refresh")
    project = SimpleNamespace(id=project_id, slug="discipline")
    monkeypatch.setattr(page.Project.objects, "get", lambda **kwargs: project)
    class Query:
        def select_related(self, *args): return self
        def order_by(self, *args): return self
        def __getitem__(self, item): return []
    calls = []
    monkeypatch.setattr(page.PublicationJob.objects, "filter", lambda **kwargs: calls.append(kwargs) or Query())
    page.run_report(project_id, t, "refresh")
    assert calls == [{"project": project, "status": "published"}]
    assert page.read_report(project_id)["state"] == "done"


def test_marketer_queue_cannot_receive_publication_routes(page):
    from cf_admin.celery import app
    queue = app.amqp.queues['marketer']
    assert queue.exchange.name == 'marketer'
    assert queue.routing_key == 'marketer'


def test_refresh_collects_new_youtube_behaviour(page, monkeypatch):
    pid = uuid.uuid4()
    task = page.reserve_report(pid, "refresh")
    project = SimpleNamespace(id=pid, slug="discipline")
    monkeypatch.setattr(page.Project.objects, "get", lambda **kw: project)
    class Query:
        def select_related(self, *a): return self
        def order_by(self, *a): return self
        def __getitem__(self, key): return [object()]
    monkeypatch.setattr(page.PublicationJob.objects, "filter", lambda **kw: Query())
    monkeypatch.setattr(page, "job_dict", lambda job: {"id": "new", "platform": "youtube", "language": "ru", "external_post_id": "abcdefghijk"})
    monkeypatch.setattr(page, "_youtube_access_token", lambda *a: "token")
    monkeypatch.setattr(page, "collect_metrics", lambda *a: {"metrics": {"views": 4}})
    monkeypatch.setattr(page, "collect_followers", lambda *a: {"platform": "youtube", "language": "ru", "account_id": "c", "followers": 2})
    monkeypatch.setattr(page, "collect_behaviour", lambda *a: {"subscribers_gained": 1})
    page.run_report(pid, task, "refresh")
    result = page.read_report(pid)
    assert result["state"] == "done"
    assert result["rows"][0]["behaviour"]["subscribers_gained"] == 1


def test_homepage_marketer_link_matches_registered_route(page):
    from django.conf import settings
    from django.template import Context, Template
    from django.urls import resolve, reverse
    url = reverse('marketer')
    assert url == f'/{settings.ADMIN_URL_PATH}/marketer/'
    assert resolve(url).url_name == 'marketer'
    assert Template("{% url 'marketer' %}").render(Context()) == url


def test_youtube_destination_link_pins_en_even_with_ru_post(page, monkeypatch):
    from django.contrib import admin
    from django.test import RequestFactory
    from pipeline_admin import admin as module
    from pipeline_admin.models import PublicationJob
    monkeypatch.setattr(module, '_publication_project_choices', lambda: [('grandma_kitchen', 'Grandma')])
    monkeypatch.setattr(module, 'project_env', lambda *a: 'client')
    captured = []
    monkeypatch.setattr(module, '_save_youtube_oauth_flow', lambda request, flow: captured.append(flow) or 'state')
    monkeypatch.setattr(module, 'set_project_secrets', lambda *a: None)
    request = RequestFactory().post('/admin/pipeline_admin/publicationjob/youtube-oauth/?project_slug=grandma_kitchen&language=en',
        {'project_slug': 'grandma_kitchen', 'language': 'ru', 'client_id': 'client', 'client_secret': 'secret'},
        HTTP_HOST='127.0.0.1:8000')
    response = admin.site._registry[PublicationJob].youtube_oauth_view(request)
    assert response.status_code == 302
    assert captured[0]['language'] == 'en'
    assert captured[0]['project_slug'] == 'grandma_kitchen'
