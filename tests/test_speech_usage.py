from __future__ import annotations

from datetime import datetime, timezone

from app.speech_usage import format_speech_alignment_remaining, speech_alignment_monthly_usage


class _Scalars:
    def __init__(self, payloads):
        self._payloads = payloads

    def scalars(self):
        return iter(self._payloads)


class _Session:
    def __init__(self, payloads):
        self._payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _statement):
        return _Scalars(self._payloads)


def test_monthly_speech_usage_sums_saved_billed_seconds() -> None:
    status = speech_alignment_monthly_usage(
        now=datetime(2026, 11, 5, tzinfo=timezone.utc),
        session_factory=lambda: _Session(
            [
                {"billed_duration_sec": 8},
                {"billed_duration_sec": 7.5},
                {"ignored": True},
            ]
        ),
    )

    assert status["month"] == "2026-11"
    assert status["used_seconds"] == 15.5
    assert status["remaining_seconds"] == 3584.5


def test_speech_usage_format_reports_remaining_time() -> None:
    assert format_speech_alignment_remaining(
        {"used_seconds": 40, "remaining_seconds": 3560}
    ) == (
        "Точная синхронизация субтитров: осталось 59 мин 20 сек из 60 мин в этом месяце "
        "(использовано 0 мин 40 сек по учёту бота)."
    )
