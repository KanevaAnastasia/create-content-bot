"""Helpers for storing and summarizing published video analytics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import PublishedVideoAnalytics


ANALYTICS_FIELDS = {
    "platform",
    "title",
    "recipe",
    "format",
    "hook",
    "first_frame_type",
    "duration",
    "views",
    "reach",
    "average_view_duration",
    "average_percentage_viewed",
    "retention_3s",
    "retention_5s",
    "retention_10s",
    "likes",
    "comments",
    "saves",
    "shares",
    "follows",
    "posted_at",
    "notes",
}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    n = _number(value)
    return None if n is None else int(n)


def _datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def normalize_analytics_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize analytics data before storing it."""
    data = {k: payload.get(k) for k in ANALYTICS_FIELDS if k in payload}
    data["platform"] = str(data.get("platform") or "").strip()
    if not data["platform"]:
        raise ValueError("platform is required")
    for key in ("title", "recipe", "format", "hook", "first_frame_type", "notes"):
        if key in data and data[key] is not None:
            data[key] = str(data[key]).strip() or None
    for key in ("duration", "average_view_duration", "average_percentage_viewed", "retention_3s", "retention_5s", "retention_10s"):
        if key in data:
            data[key] = _number(data[key])
    for key in ("views", "reach", "likes", "comments", "saves", "shares", "follows"):
        if key in data:
            data[key] = _int_or_none(data[key])
    if "posted_at" in data:
        data["posted_at"] = _datetime_or_none(data["posted_at"])
    return data


def save_published_video_analytics(session: Session, payload: dict[str, Any]) -> PublishedVideoAnalytics:
    """Persist a published video analytics row."""
    data = normalize_analytics_payload(payload)
    row = PublishedVideoAnalytics(**data)
    session.add(row)
    session.flush()
    return row


def _avg(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _rank_group(rows: list[PublishedVideoAnalytics], key: str, metric: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        label = getattr(row, key, None)
        value = getattr(row, metric, None)
        if label and value is not None:
            grouped[str(label)].append(float(value))
    ranked = [
        {"value": label, "avg": round(avg, 3), "count": len(values)}
        for label, values in grouped.items()
        if (avg := _avg(values)) is not None
    ]
    return sorted(ranked, key=lambda x: (x["avg"], x["count"]), reverse=True)


def build_analytics_feedback(rows: list[PublishedVideoAnalytics]) -> dict[str, Any]:
    """Build compact insights for the next Viral Concept Brief prompt."""
    return {
        "best_hooks_by_average_percentage_viewed": _rank_group(rows, "hook", "average_percentage_viewed")[:5],
        "best_first_frame_types_by_retention_3s": _rank_group(rows, "first_frame_type", "retention_3s")[:5],
        "best_recipes_by_saves": _rank_group(rows, "recipe", "saves")[:5],
        "formats_to_repeat": _rank_group(rows, "format", "average_percentage_viewed")[:5],
        "formats_to_pause": list(reversed(_rank_group(rows, "format", "average_percentage_viewed")))[:5],
        "common_dropoff_hint": "compare retention_3s, retention_5s and retention_10s; large gaps mean the next script needs faster micro-events",
        "next_video_suggestions": [
            "use the best first_frame_type from recent high-retention posts",
            "repeat hooks with high average_percentage_viewed",
            "add stronger payoff and save reason for recipes with high saves",
        ],
    }
