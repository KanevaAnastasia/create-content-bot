"""Persistence helpers for content series selection during generation."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG
from .db import SessionLocal
from .models import ContentSeries, Project


SERIES_SESSION_KEY = "series"
SERIES_TITLE_BY_LANG_SESSION_KEY = "series_title_by_lang"
SERIES_VIDEO_INDEX_SESSION_KEY = "series_video_index"
SERIES_TOTAL_VIDEOS_SESSION_KEY = "series_total_videos"
SERIES_COUNTER_BY_LANG_SESSION_KEY = "series_counter_by_lang"
SERIES_RENDER_COUNTER_ADVANCED_SESSION_KEY = "series_render_counter_advanced"
SERIES_PENDING_RECIPE_SESSION_KEY = "series_pending_recipe"
SERIES_PENDING_PREFER_VERTEX_SESSION_KEY = "series_pending_prefer_vertex_api"


@dataclass(frozen=True, slots=True)
class SeriesPayload:
    id: str
    title_by_lang: dict[str, str]
    video_index: int
    total_videos: int
    counter_by_lang: dict[str, str]

    def as_session_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title_by_lang": dict(self.title_by_lang),
            "video_index": self.video_index,
            "total_videos": self.total_videos,
            "counter_by_lang": dict(self.counter_by_lang),
        }


def normalize_series_slug(title: str) -> str:
    text = str(title or "").strip().lower()
    text = re.sub(r"[^0-9a-zа-яё]+", "-", text, flags=re.IGNORECASE).strip("-")
    return text[:96] or f"series-{uuid.uuid4().hex[:8]}"


def _project_id_for_slug(db, project_slug: str | None) -> uuid.UUID | None:
    slug = str(project_slug or GRANDMA_KITCHEN_PROJECT_SLUG).strip() or GRANDMA_KITCHEN_PROJECT_SLUG
    row = db.execute(select(Project).where(Project.slug == slug)).scalar_one_or_none()
    if row is None and slug != GRANDMA_KITCHEN_PROJECT_SLUG:
        row = db.execute(select(Project).where(Project.slug == GRANDMA_KITCHEN_PROJECT_SLUG)).scalar_one_or_none()
    return row.id if row is not None else None


def payload_for_series(row: ContentSeries) -> SeriesPayload:
    video_index = max(1, int(row.current_video_index or 1))
    total_videos = max(1, int(row.total_videos or 1))
    title_by_lang = {"ru": row.title_ru, "en": row.title_en}
    raw_meta = getattr(row, "meta_json", None)
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    meta_titles = meta.get("title_by_lang")
    if isinstance(meta_titles, dict):
        for key, value in meta_titles.items():
            lang = str(key or "").strip().lower()
            title = " ".join(str(value or "").split())
            if lang and title:
                title_by_lang[lang] = title
    title_by_lang["ru"] = " ".join(str(row.title_ru or "").split())
    title_by_lang["en"] = " ".join(str(row.title_en or "").split())
    counter_by_lang = {
        "ru": f"день {video_index} из {total_videos}",
        "en": f"day {video_index} of {total_videos}",
    }
    for lang in title_by_lang:
        counter_by_lang.setdefault(lang, counter_by_lang["en"])
    return SeriesPayload(
        id=str(row.id),
        title_by_lang=title_by_lang,
        video_index=video_index,
        total_videos=total_videos,
        counter_by_lang=counter_by_lang,
    )


def list_active_series(project_slug: str | None = None, *, limit: int = 8) -> list[SeriesPayload]:
    with SessionLocal() as db:
        project_id = _project_id_for_slug(db, project_slug)
        stmt = select(ContentSeries).where(
            ContentSeries.is_active.is_(True),
            ContentSeries.current_video_index <= ContentSeries.total_videos,
        )
        if project_id is not None:
            stmt = stmt.where(ContentSeries.project_id == project_id)
        rows = db.execute(
            stmt.order_by(ContentSeries.updated_at.desc(), ContentSeries.created_at.desc()).limit(limit)
        ).scalars()
        return [payload_for_series(row) for row in rows]


def get_series_payload(series_id: str) -> SeriesPayload | None:
    try:
        sid = uuid.UUID(str(series_id))
    except (TypeError, ValueError):
        return None
    with SessionLocal() as db:
        row = db.get(ContentSeries, sid)
        return payload_for_series(row) if row is not None else None


def select_series_payload(series_id: str) -> SeriesPayload | None:
    """Return the next day payload without advancing the DB counter."""
    try:
        sid = uuid.UUID(str(series_id))
    except (TypeError, ValueError):
        return None
    with SessionLocal() as db:
        row = db.execute(
            select(ContentSeries).where(ContentSeries.id == sid).with_for_update()
        ).scalar_one_or_none()
        if row is None or not row.is_active or int(row.current_video_index or 1) > int(row.total_videos or 1):
            return None
        return payload_for_series(row)


def reserve_series_payload(series_id: str) -> SeriesPayload | None:
    """Backward-compatible alias: selection no longer advances the day counter."""
    return select_series_payload(series_id)


def advance_series_after_successful_render(session: dict[str, Any]) -> SeriesPayload | None:
    """Advance a selected series only after final render succeeds."""
    if not isinstance(session, dict) or session.get(SERIES_RENDER_COUNTER_ADVANCED_SESSION_KEY):
        return None
    series = session.get(SERIES_SESSION_KEY)
    if not isinstance(series, dict):
        return None
    try:
        sid = uuid.UUID(str(series.get("id") or ""))
    except (TypeError, ValueError):
        return None
    rendered_index = int(
        session.get(SERIES_VIDEO_INDEX_SESSION_KEY)
        or series.get("video_index")
        or series.get("current_video_index")
        or 0
    )
    if rendered_index <= 0:
        return None
    with SessionLocal() as db:
        row = db.execute(select(ContentSeries).where(ContentSeries.id == sid).with_for_update()).scalar_one_or_none()
        if row is None:
            return None
        total = max(1, int(row.total_videos or series.get("total_videos") or 1))
        rendered_index = min(max(1, rendered_index), total)
        if rendered_index >= total:
            row.current_video_index = total
            row.is_active = False
        else:
            row.current_video_index = rendered_index + 1
            row.is_active = True
        db.commit()
        session[SERIES_RENDER_COUNTER_ADVANCED_SESSION_KEY] = True
        return payload_for_series(row)


def create_series(
    *,
    title_ru: str,
    title_en: str | None = None,
    title_by_lang: dict[str, str] | None = None,
    total_videos: int,
    project_slug: str | None = None,
) -> SeriesPayload:
    ru = " ".join(str(title_ru or "").split())
    en = " ".join(str(title_en or "").split()) if title_en else ru
    if not ru or not en:
        raise ValueError("Series needs both Russian and English titles.")
    titles = {"ru": ru, "en": en}
    if isinstance(title_by_lang, dict):
        for key, value in title_by_lang.items():
            lang = str(key or "").strip().lower()
            title = " ".join(str(value or "").split())
            if lang and title:
                titles[lang] = title
    titles["ru"] = ru
    titles["en"] = en
    total = max(1, int(total_videos or 10))
    with SessionLocal() as db:
        project_id = _project_id_for_slug(db, project_slug)
        base_slug = normalize_series_slug(en or ru)
        slug = base_slug
        suffix = 2
        while db.execute(
            select(ContentSeries.id).where(ContentSeries.project_id == project_id, ContentSeries.slug == slug)
        ).scalar_one_or_none():
            slug = f"{base_slug[:88]}-{suffix}"
            suffix += 1
        row = ContentSeries(
            project_id=project_id,
            slug=slug,
            title_ru=ru,
            title_en=en,
            current_video_index=1,
            total_videos=total,
            is_active=True,
            meta_json={"title_by_lang": titles},
        )
        db.add(row)
        db.flush()
        payload = payload_for_series(row)
        db.commit()
        return payload


def attach_series_to_session(session: dict[str, Any], payload: SeriesPayload | None) -> None:
    if payload is None:
        for key in (
            SERIES_SESSION_KEY,
            SERIES_TITLE_BY_LANG_SESSION_KEY,
            SERIES_VIDEO_INDEX_SESSION_KEY,
            SERIES_TOTAL_VIDEOS_SESSION_KEY,
            SERIES_COUNTER_BY_LANG_SESSION_KEY,
            SERIES_RENDER_COUNTER_ADVANCED_SESSION_KEY,
        ):
            session.pop(key, None)
        return
    data = payload.as_session_dict()
    session[SERIES_SESSION_KEY] = data
    session[SERIES_TITLE_BY_LANG_SESSION_KEY] = data["title_by_lang"]
    session[SERIES_VIDEO_INDEX_SESSION_KEY] = data["video_index"]
    session[SERIES_TOTAL_VIDEOS_SESSION_KEY] = data["total_videos"]
    session[SERIES_COUNTER_BY_LANG_SESSION_KEY] = data["counter_by_lang"]


def format_series_label(payload: SeriesPayload) -> str:
    return f"{payload.title_by_lang['ru']} / {payload.title_by_lang['en']} ({payload.video_index}/{payload.total_videos})"
