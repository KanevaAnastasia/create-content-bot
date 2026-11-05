"""Local accounting for paid Speech-to-Text subtitle alignment calls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .models import GenerationArtifact


SPEECH_ALIGNMENT_USAGE_ARTIFACT_TYPE = "speech_alignment_usage"
SPEECH_ALIGNMENT_FREE_SECONDS_PER_MONTH = 60 * 60


def speech_alignment_monthly_usage(
    *,
    now: datetime | None = None,
    session_factory: Any = None,
) -> dict[str, float | str]:
    """Return this bot's recorded V1 usage for the current UTC calendar month."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    factory = session_factory or SessionLocal
    with factory() as db:
        rows = db.execute(
            select(GenerationArtifact.payload_json).where(
                GenerationArtifact.artifact_type == SPEECH_ALIGNMENT_USAGE_ARTIFACT_TYPE,
                GenerationArtifact.created_at >= month_start,
            )
        ).scalars()
        used_seconds = 0.0
        for payload in rows:
            if not isinstance(payload, dict):
                continue
            try:
                used_seconds += max(0.0, float(payload.get("billed_duration_sec") or 0.0))
            except (TypeError, ValueError):
                continue
    remaining_seconds = max(0.0, SPEECH_ALIGNMENT_FREE_SECONDS_PER_MONTH - used_seconds)
    return {
        "month": month_start.strftime("%Y-%m"),
        "used_seconds": used_seconds,
        "remaining_seconds": remaining_seconds,
        "free_seconds": float(SPEECH_ALIGNMENT_FREE_SECONDS_PER_MONTH),
    }


def format_speech_alignment_remaining(status: dict[str, float | str]) -> str:
    remaining = max(0, int(round(float(status.get("remaining_seconds") or 0.0))))
    used = max(0, int(round(float(status.get("used_seconds") or 0.0))))
    return (
        "Точная синхронизация субтитров: осталось "
        f"{remaining // 60} мин {remaining % 60:02d} сек из 60 мин в этом месяце "
        f"(использовано {used // 60} мин {used % 60:02d} сек по учёту бота)."
    )
