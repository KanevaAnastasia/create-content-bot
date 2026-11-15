import os
import sys
from pathlib import Path


def _django_settings():
    admin_panel = Path(__file__).resolve().parents[1] / "admin_panel"
    if str(admin_panel) not in sys.path:
        sys.path.insert(0, str(admin_panel))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    from django.conf import settings

    return settings


def test_periodic_housekeeping_tasks_expire_before_they_can_build_a_backlog():
    schedule = _django_settings().CELERY_BEAT_SCHEDULE

    assert schedule["publish-due-social-videos"]["options"]["expires"] < 60
    assert schedule["scheduled-generation-nightly-0100"]["options"]["expires"] == 3600
    assert schedule["refresh-expiring-instagram-tokens"]["options"]["expires"] == 3600


def test_daily_marketer_refresh_still_runs_at_nine_moscow_time():
    settings = _django_settings()
    entry = settings.CELERY_BEAT_SCHEDULE["marketer-daily-0900"]

    assert settings.CELERY_TIMEZONE == "Europe/Moscow"
    assert str(entry["schedule"]) == "<crontab: 0 9 * * * (m/h/dM/MY/d)>"
    assert entry["options"] == {"queue": "marketer", "expires": 3600}
