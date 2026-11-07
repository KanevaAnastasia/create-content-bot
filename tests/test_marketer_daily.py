from datetime import datetime
from app.marketer_daily import MOSCOW, day_key, snapshot_history, dashboard


def stamp(day, hour=9):
    return datetime(2026, 11, day, hour, tzinfo=MOSCOW).timestamp()


def account(value, identity="a"):
    return {"platform": "tiktok", "language": "ru", "account_id": identity, "followers": value}


def row(views):
    return {"id": "1", "platform": "tiktok", "language": "ru", "metrics": {"views": views}}


def test_daily_snapshot_replaces_today_only_and_copies():
    accounts = [account(10)]
    h = snapshot_history([], accounts, [row(100)], stamp(7))
    accounts[0]["followers"] = 99
    assert h[0]["accounts"][0]["followers"] == 10
    h = snapshot_history(h, [account(11)], [row(101)], stamp(7, 20))
    h = snapshot_history(h, [account(14)], [row(120)], stamp(8))
    assert len(h) == 2
    rows = [row(120)]
    d = dashboard({"daily_snapshots": h, "accounts": [account(14)], "fetched_at": stamp(8)}, rows)
    assert d["accounts"][0]["daily_delta"] == 3
    assert rows[0]["daily_views"] == 19
    assert [p["views"] for p in rows[0]["daily_history"]] == [101, 120]


def test_no_invented_daily_delta_after_gap_or_identity_change():
    h = snapshot_history([], [account(10)], [row(100)], stamp(7))
    for day, identity in [(9, "a"), (8, "b")]:
        d = dashboard({"daily_snapshots": h, "accounts": [account(12, identity)], "fetched_at": stamp(day)}, [row(150)])
        assert d["accounts"][0]["daily_delta"] is None


def test_missing_is_not_zero_and_corrections_remain_negative():
    h = snapshot_history([], [account(10)], [row(100)], stamp(7))
    rows = [row(90)]
    d = dashboard({"daily_snapshots": h, "accounts": [account(None)], "fetched_at": stamp(8)}, rows)
    assert d["accounts"][0]["daily_delta"] is None
    assert rows[0]["daily_views"] == -10
    assert day_key(datetime(2026, 11, 7, 23, tzinfo=MOSCOW).timestamp()) == "2026-11-07"


def test_empty_dashboard():
    assert dashboard({}, [])["days"] == 0


def test_trial_and_normal_share_source_not_metrics():
    from app.marketer import group_videos
    rows = [{"id": "1", "project_id": "p", "bundle_key": "source", "metrics": {"views": 10}},
            {"id": "2", "project_id": "p", "bundle_key": "source-trial", "instagram_trial": True, "metrics": {"views": 20}}]
    groups = group_videos(rows)
    assert len(groups) == 1
    assert [r["metrics"]["views"] for r in groups[0]["rows"]] == [10, 20]
