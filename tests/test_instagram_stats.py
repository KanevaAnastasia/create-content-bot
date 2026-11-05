import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app import instagram_stats as stats
from app.handlers import instagram_stats as ui


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 11, 5), date(2026, 10, 10)),
    (date(2026, 11, 10), date(2026, 11, 10)),
    (date(2026, 11, 30), date(2026, 11, 10)),
    (date(2027, 1, 1), date(2026, 12, 10)),
    (date(2024, 3, 1), date(2024, 2, 10)),
])
def test_default_start(today, expected):
    assert stats.default_start(today) == expected


@pytest.mark.parametrize(("caption", "matches"), [
    ("Спасибо @syntx_ai!", True), ("@SYNTX_AI.", True),
    ("(@syntx_ai)", True), ("@syntx_ai.other", False),
    ("@syntx_ai_team", False), ("@syntx_aix", False),
    ("mail@syntx_ai.com", False), ("syntx_ai", False),
])
def test_exact_mention(caption, matches):
    assert bool(stats.MENTION.search(caption)) is matches


def test_account_configuration_deduplicates_accounts(monkeypatch):
    db = Mock()
    db.scalars.return_value = ["one", "two"]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=db)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(stats, "SessionLocal", factory)
    monkeypatch.setattr(stats, "project_env", lambda project, key: "123" if key.endswith("RU") else "")
    assert stats.configured_accounts() == [stats.InstagramAccount("one", "ru", "123")]


def media(media_id, timestamp="2026-10-10T00:00:00+0300", **kwargs):
    return {"id": media_id, "timestamp": timestamp, "caption": "@syntx_ai", "media_type": "VIDEO", "media_product_type": "REELS", "permalink": f"https://www.instagram.com/reel/{media_id}/", **kwargs}


def setup_account(monkeypatch):
    monkeypatch.setattr(stats, "configured_accounts", lambda: [stats.InstagramAccount("project", "ru", "123")])
    monkeypatch.setattr(stats, "_instagram_access_token", lambda *_: "secret")


def test_report_paginates_filters_dates_and_deduplicates(monkeypatch):
    setup_account(monkeypatch)
    calls = []
    def get(session, path, **params):
        calls.append((path, params))
        if path.endswith("/insights"):
            return {"data": [{"name": "views", "values": [{"value": 25}]}]}
        if params.get("after"):
            return {"data": [media("ok"), media("today", "2026-11-05T10:00:00+0300")]}
        return {"data": [
            media("old", "2026-10-09T20:59:59+0000"),
            media("ok", "2026-10-09T21:00:00+0000"),
            media("future", "2026-11-05T13:00:00+0300"),
            media("photo", media_type="IMAGE"),
            media("story", media_product_type="STORY"),
            media("suffix", caption="@syntx_ai_team"),
        ], "paging": {"next": "https://untrusted.example/?access_token=secret", "cursors": {"after": "page2"}}}
    monkeypatch.setattr(stats, "_get", get)
    report = stats.fetch_instagram_report(date(2026, 10, 10), now=datetime(2026, 11, 5, 12, tzinfo=stats.MOSCOW))
    assert [v.media_id for v in report.videos] == ["today", "ok"]
    assert sum(v.views for v in report.videos) == 50
    assert report.listing_complete
    assert len([path for path, _ in calls if path.endswith("/insights")]) == 2
    assert calls[2][1]["after"] == "page2"


def test_missing_permission_preserves_links_without_zero_views(monkeypatch):
    setup_account(monkeypatch)
    monkeypatch.setattr(stats, "_media", lambda *_: iter([media("one"), media("two")]))
    denied = Mock(side_effect=stats.InstagramInsightsPermissionError("Нужно instagram_manage_insights"))
    monkeypatch.setattr(stats, "_views", denied)
    report = stats.fetch_instagram_report(date(2026, 10, 10))
    assert len(report.videos) == 2
    assert all(v.views is None for v in report.videos)
    assert len(report.warnings) == 1
    denied.assert_called_once()
    text = "\n".join(ui.report_chunks(report))
    assert "Всего роликов: 2" in text
    assert "Сумма просмотров недоступна" in text
    assert "Всего просмотров: 0" not in text
    assert "https://www.instagram.com/reel/one/" in text


def test_incomplete_listing_keeps_partial_results(monkeypatch):
    setup_account(monkeypatch)
    def listing(*_):
        yield media("one")
        raise stats.InstagramStatsError("Ошибка следующей страницы")
    monkeypatch.setattr(stats, "_media", listing)
    monkeypatch.setattr(stats, "_views", lambda *_: 10)
    report = stats.fetch_instagram_report(date(2026, 10, 10))
    assert not report.listing_complete
    text = "\n".join(ui.report_chunks(report))
    assert "список неполный" in text
    assert "Известная сумма просмотров: 10 (итог неполный)" in text


def test_pagination_cycle_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(stats, "_get", lambda *_args, **_kwargs: {"data": [], "paging": {"next": "url", "cursors": {"after": "same"}}})
    with pytest.raises(stats.InstagramStatsError, match="итог неполный"):
        list(stats._media(Mock(), "123"))


@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"name": "views", "values": [{"value": 0}]}]}, 0),
    ({"data": [{"name": "views", "total_value": {"value": 44}}]}, 44),
    ({"data": []}, None),
    ({"data": [{"name": "reach", "values": [{"value": 25}]}]}, None),
])
def test_views_only_uses_views_metric(monkeypatch, payload, expected):
    monkeypatch.setattr(stats, "_get", lambda *_args, **_kwargs: payload)
    assert stats._views(Mock(), "1") == expected


def test_api_errors_do_not_expose_tokens(monkeypatch):
    monkeypatch.setattr(stats, "shared_env", lambda *_: "v25.0")
    session = Mock()
    session.get.side_effect = requests.ConnectionError("https://host?access_token=SECRET")
    with pytest.raises(stats.InstagramStatsError) as exc:
        stats._get(session, "123/media")
    assert "SECRET" not in str(exc.value)
    session.get.side_effect = None
    session.get.return_value = Mock(ok=False, json=lambda: {"error": {"code": 10, "message": "SECRET"}})
    with pytest.raises(stats.InstagramInsightsPermissionError, match="instagram_manage_insights") as exc:
        stats._get(session, "123/insights")
    assert "SECRET" not in str(exc.value)


def test_report_chunks_include_all_links_and_total():
    now = datetime(2026, 11, 5, 12, tzinfo=stats.MOSCOW)
    report = stats.InstagramReport(date(2026, 10, 10), now, accounts=1)
    report.videos = [stats.InstagramVideo(str(i), "project / RU", now, f"https://www.instagram.com/reel/{i}/", 100) for i in range(100)]
    chunks = ui.report_chunks(report)
    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    text = "\n".join(chunks)
    assert "Всего просмотров: 10 000" in text
    for video in report.videos:
        assert text.count(video.permalink) == 1


def test_calendar_handles_leap_year_and_disables_future_days():
    buttons = [b for row in ui.calendar_markup(date(2024, 2, 1), date(2024, 2, 29)).inline_keyboard for b in row]
    assert any(b.callback_data == "igstat:date:2024-02-29" for b in buttons)
    buttons = [b for row in ui.calendar_markup(date(2026, 11, 1), date(2026, 11, 5)).inline_keyboard for b in row]
    assert next(b for b in buttons if b.text == "6").callback_data == "igstat:noop"
    assert not any(b.text == "Месяц ›" for b in buttons)


def test_date_callback_uses_selected_date(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(ui, "send_instagram_stats", send)
    monkeypatch.setattr(ui, "moscow_now", lambda: datetime(2026, 11, 5, tzinfo=stats.MOSCOW))
    query = SimpleNamespace(data="igstat:date:2026-09-02", answer=AsyncMock(), message=object())
    asyncio.run(ui.handle_calendar(SimpleNamespace(callback_query=query), None))
    send.assert_awaited_once_with(query.message, date(2026, 9, 2))


@pytest.mark.parametrize("data", ["igstat:date:2026-11-06", "igstat:date:bad", "igstat:date:2026-02-30", "igstat:unexpected:2026-10-10"])
def test_invalid_calendar_callback_does_not_fetch(monkeypatch, data):
    send = AsyncMock()
    monkeypatch.setattr(ui, "send_instagram_stats", send)
    monkeypatch.setattr(ui, "moscow_now", lambda: datetime(2026, 11, 5, tzinfo=stats.MOSCOW))
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=object())
    asyncio.run(ui.handle_calendar(SimpleNamespace(callback_query=query), None))
    send.assert_not_awaited()
    assert query.answer.call_args.kwargs["show_alert"] is True


def test_send_report_edits_loading_and_attaches_date_button(monkeypatch):
    now = datetime(2026, 11, 5, 12, tzinfo=stats.MOSCOW)
    fetch = Mock(return_value=stats.InstagramReport(date(2026, 10, 10), now, accounts=1))
    monkeypatch.setattr(ui, "fetch_instagram_report", fetch)
    monkeypatch.setattr(ui, "moscow_now", lambda: now)
    loading = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=loading))
    asyncio.run(ui.send_instagram_stats(message))
    fetch.assert_called_once_with(date(2026, 10, 10), now=now)
    assert "Всего просмотров: 0" in loading.edit_text.call_args.args[0]
    assert loading.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "Другая дата"
