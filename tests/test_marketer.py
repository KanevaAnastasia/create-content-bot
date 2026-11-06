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
        "rows": [{"title": "<script>alert(1)</script>", "metric_cells": [None, 0], "advice": []}],
        "report": {}, "projects": [], "can_change": False,
    })
    assert "&lt;script&gt;" in html
    assert "<td>—</td>" in html
    assert "<td>0</td>" in html


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
    monkeypatch.setattr(app.llm_utils, "gemini_generate_sync", lambda prompt, **kw: seen.append(prompt) or "План")
    data = row("a", 10, access_token="DO_NOT_SEND", payload_json={"secret": "DO_NOT_SEND"})
    assert m.marketing_brief("Проект", [data], [], "") == "План"
    assert "DO_NOT_SEND" not in seen[0]
    assert "недоверенные" in seen[0]


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
