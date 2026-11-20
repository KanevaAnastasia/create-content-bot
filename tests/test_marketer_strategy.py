import json
from datetime import datetime, timedelta, timezone

import pytest

from app import marketer as metrics, marketer_market as market, marketer_strategy as strategy
from app import marketer_video_review as video


NOW = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)


def row(n, views, **kwargs):
    return {"id": str(n), "external_post_id": f"{n:011d}", "project_id": "p", "platform": "youtube",
            "language": "ru", "account_key": "main", "metrics": {"views": views}, "title": f"Ролик {n}",
            "url": f"https://www.youtube.com/watch?v={n:011d}", "posted_at": (NOW - timedelta(days=4)).isoformat(),
            **kwargs}


def test_reviewed_selection_finds_older_leader_and_deduplicates_jobs():
    rows = [row(i, 100) for i in range(65)] + [row(65, 20000), row(66, 90000, language="en")]
    rows.append({**rows[-2], "id": "duplicate"})
    sources = strategy.evidence_set({"title": "Test", "fetched_at": NOW.timestamp()}, rows, [], "")["own_videos"]
    lead = sources[0]
    assert lead["title"] == "Ролик 65" and lead["relative"] == 200 and lead["cohort_count"] == 65
    assert sum(e["url"] == lead["url"] for e in sources) == 1


def test_early_control_measures_each_post_once_and_keeps_age_language_and_trial():
    rows = [row(i, 300) for i in range(4)] + [row(4, 90000, instagram_trial=True), row(5, 90000, language="en")]
    def snapshot(age, values):
        stamp = NOW - timedelta(days=4) + timedelta(hours=age)
        return {"timestamp": stamp.timestamp(), "videos": [{**r, "metrics": {"views": v}} for r, v in zip(rows, values)]}
    result = strategy.early_controls(rows, [snapshot(22, [1] * 6), snapshot(38, [10] * 6),
                                           snapshot(49, [6000, 0, 100, 170, 90000, 90000])])
    assert result["0"] == {"metric": "views", "median": 100, "n": 3, "min_hours": 36, "max_hours": 60}
    assert "4" not in result and "5" not in result


@pytest.mark.parametrize("bad", ["http://localhost/private", "https://www.youtube.com.evil.invalid/shorts/12345678901",
                                 "https://x@youtube.com/shorts/12345678901", "https://youtube.com:444/watch?v=12345678901",
                                 "https://instagram.com/some_account/", "javascript:alert(1)"])
def test_references_only_accept_public_video_routes(bad):
    with pytest.raises(ValueError, match="Строка 1"):
        market.parse_references(bad)


def test_references_preserve_order_notes_and_remove_tracking():
    refs = market.parse_references("https://youtu.be/12345678901?t=12 | Начало\nhttps://youtube.com/shorts/12345678901\nhttps://www.instagram.com/reel/abc_1/?igsh=x")
    assert refs == [{"url": "https://www.youtube.com/watch?v=12345678901", "note": "Начало"},
                    {"url": "https://www.instagram.com/reel/abc_1/", "note": ""}]


def test_discovery_filters_identity_topic_and_explicit_language_and_carries_real_growth(monkeypatch):
    profile = {"slug": "discipline", "languages": ["ru"]}
    own = row(8, 1)
    def hit(n, **kw):
        return {"video_id": str(n).zfill(11), "url": row(n, 0)["url"], "title": "Привычки маленькими шагами",
                "platform": "youtube", "channel_id": "other", "language": "ru", "duration_seconds": 30,
                "posted_at": (NOW-timedelta(days=5)).isoformat(), "views": 1000, **kw}
    hits = [hit(1), hit(2, language="en"), hit(3, channel_id="mine"), hit(4, duration_seconds=240),
            hit(5, title="Новости футбола"), hit(8)]
    monkeypatch.setattr(market, "project_env", lambda slug, key: "mine" if key.startswith("YOUTUBE") else "")
    monkeypatch.setattr(market, "discover_niche", lambda *a: hits)
    first = market.collect_market(profile, [own], [], {}, lambda *a: "t", query="привычки", now=NOW)
    assert [(i["video_id"], i["growth_per_day"]) for i in first["items"]] == [("00000000001", None)]
    assert first["searches"][0]["found"] == 6 and first["searches"][0]["matched"] == 1
    hits[0]["views"] = 1600
    second = market.collect_market(profile, [own], [], first, lambda *a: "t", query="привычки", now=NOW+timedelta(days=1))
    assert second["items"][0]["growth_per_day"] == 600
    def offline(*a):
        raise metrics.MetricsUnavailable("Поиск недоступен")
    third = market.collect_market(profile, [own], [], second, offline, now=NOW+timedelta(days=2))
    assert third["items"][0]["observed_at"] == second["at"]
    assert third["searches"][0]["warning"] == "Поиск недоступен"


def test_video_review_prioritizes_favorites_and_reuses_successes(monkeypatch):
    import app.llm_utils
    monkeypatch.setattr(app.llm_utils, "text_llm_provider", lambda: "gemini")
    calls = []
    def watch(url):
        calls.append(url)
        return dict.fromkeys(("opening", "development", "ending"), "Наблюдение")
    monkeypatch.setattr(video, "_watch", watch)
    own = row(1, 10)
    refs = [{"url": row(2, 0)["url"]}, {"url": "https://instagram.com/reel/abc/"}]
    report = {"items": [row(i, 0) for i in range(3, 8)], "favorites": refs}
    first = video.review_sources(report, [own], {}, now=NOW)
    assert calls[:2] == [refs[0]["url"], own["url"]]
    assert len(calls) == len(first) == 4
    video.review_sources(report, [own], first, now=NOW+timedelta(days=1))
    assert len(calls) == len(set(calls)) == 7


def test_unknown_source_or_uncited_favorite_cannot_become_verified_plan():
    data = strategy.evidence_set({"title": "Project", "favorites": [{"url": row(2, 0)["url"], "note": "Начало"}]},
                                 [row(1, 0)], [], "")
    sources = {s["id"]: s for s in data["own_videos"] + data["favorites"]}
    card = {"title": "t", "action": "a", "evidence": "e", "check": "c", "platform":"youtube", "language":"ru", "source_ids":["O1"]}
    plan = {"summary": "s", "actions": [card.copy() for _ in range(3)]}
    with pytest.raises(metrics.MetricsUnavailable):
        strategy.parse_plan(json.dumps(plan), sources)
    plan["actions"][0]["source_ids"] = ["O1", "F90"]
    with pytest.raises(metrics.MetricsUnavailable):
        strategy.parse_plan(json.dumps(plan), sources)
    plan["actions"][0]["source_ids"] = ["O1", "F1"]
    valid = strategy.parse_plan(json.dumps(plan), sources)
    assert valid["actions"][0]["sources"][1]["url"] == row(2, 0)["url"]


def test_youtube_comment_failure_is_distinct_from_measured_zero(monkeypatch):
    def unavailable(*args):
        raise metrics.MetricsUnavailable("Запрещено")
    monkeypatch.setattr(metrics, "api_get", unavailable)
    assert metrics.youtube_comments("12345678901", "token", 0)["count"] == 0
    assert metrics.youtube_comments("12345678901", "token", 2) == {"warning": "Запрещено"}


def test_comment_text_removes_direct_mentions_contacts_and_structured_vk_names():
    comment = "Анна Иванова, [id12|Виктор] @user пишите a@b.ru или +7 (999) 100-12-13. Как испечь?"
    text = strategy.anonymous_text(comment)
    for hidden in ("Иванова", "Виктор", "@", "999", "[id"):
        assert hidden not in text
    assert "Как испечь?" in text


def test_editor_review_reads_each_candidate_and_caches_exact_evidence(monkeypatch):
    from app import marketer_screening as screen
    calls = []
    def editor(prompt, provider):
        data = json.loads(prompt.split("ДАННЫЕ JSON:\n")[-1])
        calls.append((provider, data["candidates"]))
        return json.dumps([{"id": c["id"], "fit": "direct" if c["id"] % 2 else "skip",
                            "reason": "Соответствие теме", "idea": "История Васи"} for c in data["candidates"]])
    monkeypatch.setattr(screen, "editor_text", editor)
    profile = {"provider": "openai", "description": "Короткие истории", "title": "Дисциплина"}
    data = {"items": [row(i, 0) for i in range(27)], "favorites": []}
    cache = screen.review_market(profile, data, {})
    assert len(cache) == data["screened"] == 27 and [len(items) for _, items in calls] == [24, 3]
    assert all(provider == "openai" for provider, _ in calls)
    assert len(screen.recommended_items(data)) == 14
    screen.review_market(profile, data, cache)
    assert len(calls) == 2
    data["favorites"] = [{"note": "Диалог", "url": row(1, 0)["url"]}]
    screen.review_market(profile, data, cache)
    assert len(calls) == 4
