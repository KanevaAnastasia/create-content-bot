"""Persistence helpers for generation runs and video cost estimates."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import sqlalchemy as sa

from .db import SessionLocal
from .models import GenerationRun, Scene, SceneTextRevision, VideoResult

MEDIA_CURSOR_STATE = "waiting_scene_media"
SCENE_PROMPT_TEXT_KEYS = (
    "start_frame_prompt",
    "start_frame_prompt_effective",
    "end_frame_prompt",
)
SCENE_PROMPT_JSON_KEYS = (
    "video_prompt_by_lang",
    "speech_by_lang",
    "syntax_prompt_by_lang",
    "prompt_meta_json",
)
DEFAULT_GENERATION_TITLE_COLOR = "#c72638"
HEX_TITLE_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _normalize_generation_title_color(value: Any) -> str:
    raw = str(value or "").strip()
    if not HEX_TITLE_COLOR_RE.fullmatch(raw):
        return ""
    return raw.lower() if raw.startswith("#") else f"#{raw.lower()}"


def _title_color_for_session(session: dict[str, Any]) -> str:
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    candidates = [
        session.get("title_color"),
        (session.get("overlay_title_style") or {}).get("title_color")
        if isinstance(session.get("overlay_title_style"), dict)
        else None,
        (production.get("overlay_title_style") or {}).get("title_color")
        if isinstance(production.get("overlay_title_style"), dict)
        else None,
    ]
    for candidate in candidates:
        normalized = _normalize_generation_title_color(candidate)
        if normalized:
            return normalized
    return DEFAULT_GENERATION_TITLE_COLOR


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scene_prompt_payload(scene: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in SCENE_PROMPT_TEXT_KEYS:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value
    for key in SCENE_PROMPT_JSON_KEYS:
        value = scene.get(key)
        if isinstance(value, dict):
            payload[key] = value
    return payload


def _apply_prompt_payload_to_scene_row(row: Scene, scene: dict[str, Any]) -> bool:
    changed = False
    for key in SCENE_PROMPT_TEXT_KEYS:
        value = scene.get(key)
        text_value = str(value) if isinstance(value, str) and value else None
        if getattr(row, key) != text_value:
            setattr(row, key, text_value)
            changed = True
    for key in SCENE_PROMPT_JSON_KEYS:
        value = scene.get(key)
        if key == "speech_by_lang" and not isinstance(value, dict) and isinstance(row.speech_by_lang, dict) and row.speech_by_lang:
            continue
        json_value = value if isinstance(value, dict) else {}
        if getattr(row, key) != json_value:
            setattr(row, key, json_value)
            changed = True
    return changed


def _clean_revision_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _add_scene_text_revision(
    db,
    row: Scene,
    *,
    field_name: str,
    old_text: Any,
    new_text: Any,
    language: str | None = None,
    source: str = "generation_store_session_sync",
) -> None:
    old_clean = _clean_revision_text(old_text)
    new_clean = _clean_revision_text(new_text)
    if old_clean == new_clean:
        return
    db.add(
        SceneTextRevision(
            run_id=row.run_id,
            scene_id=row.id,
            scene_order=row.scene_order,
            field_name=field_name,
            language=language,
            old_text=old_clean,
            new_text=new_clean,
            source=source,
            meta_json={},
        )
    )


def _record_scene_text_payload_revisions(db, row: Scene, scene: dict[str, Any]) -> None:
    new_text = str(scene.get("text") or row.text or "")
    _add_scene_text_revision(
        db,
        row,
        field_name="text",
        old_text=row.text,
        new_text=new_text,
    )
    old_speech_raw = row.speech_by_lang if isinstance(row.speech_by_lang, dict) else {}
    raw_speech = scene.get("speech_by_lang")
    if not isinstance(raw_speech, dict):
        return
    new_speech_raw = raw_speech
    old_speech = {
        str(key).strip().lower(): value
        for key, value in old_speech_raw.items()
        if str(key).strip()
    }
    new_speech = {
        str(key).strip().lower(): value
        for key, value in new_speech_raw.items()
        if str(key).strip()
    }
    for lang in sorted(set(old_speech) | set(new_speech)):
        _add_scene_text_revision(
            db,
            row,
            field_name="speech",
            language=lang,
            old_text=old_speech.get(lang, ""),
            new_text=new_speech.get(lang, ""),
        )


def merge_scene_prompt_payload(scene: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in SCENE_PROMPT_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            scene[key] = value
    for key in SCENE_PROMPT_JSON_KEYS:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            scene[key] = value


def _run_cursor_from_session(session: dict[str, Any]) -> dict[str, Any]:
    scene_index = _safe_int_or_none(session.get("image_scene_index"))
    language_index = _safe_int_or_none(session.get("scene_media_language_index"))
    substep = str(session.get("scene_media_substep") or "").strip() or None
    state = str(session.get("state") or "").strip()
    is_active = bool(state and state != "ready_for_production")
    if state == MEDIA_CURSOR_STATE:
        is_active = True
    return {
        "is_active": is_active,
        "current_scene_index": scene_index if scene_index is not None and scene_index >= 0 else None,
        "current_media_substep": substep,
        "current_media_language_index": language_index if language_index is not None and language_index >= 0 else None,
    }


def _apply_run_cursor(run: GenerationRun, session: dict[str, Any]) -> bool:
    cursor = _run_cursor_from_session(session)
    changed = False
    for key, value in cursor.items():
        if getattr(run, key) != value:
            setattr(run, key, value)
            changed = True
    return changed


def _update_scene_prompt_rows(db, run_id: uuid.UUID, scenes_plan: list[dict[str, Any]]) -> dict[int, str]:
    rows = db.execute(select(Scene).where(Scene.run_id == run_id).order_by(Scene.scene_order.asc())).scalars().all()
    rows_by_order = {int(row.scene_order): row for row in rows}
    scene_map: dict[int, str] = {}
    for idx, scene in enumerate(scenes_plan):
        if not isinstance(scene, dict):
            continue
        order = idx + 1
        row = rows_by_order.get(order)
        if row is None:
            row = Scene(
                run_id=run_id,
                scene_order=order,
                text=str(scene.get("text") or ""),
                duration_sec=float(scene.get("duration_sec") or 0.0),
            )
            db.add(row)
            db.flush()
            rows_by_order[order] = row
        else:
            _record_scene_text_payload_revisions(db, row, scene)
        row.text = str(scene.get("text") or row.text or "")
        row.duration_sec = float(scene.get("duration_sec") or row.duration_sec or 0.0)
        _apply_prompt_payload_to_scene_row(row, scene)
        scene_map[idx] = str(row.id)
    return scene_map


def _language_for_session(session: dict[str, Any]) -> str:
    langs = session.get("target_languages")
    if isinstance(langs, list) and langs:
        first = str(langs[0]).strip().lower()
        if first in {"ru", "en"}:
            return first
    return "ru"


def _scenes_plan_for_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw = session.get("scenes_plan")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    production = session.get("production")
    plan = production.get("json_plan") if isinstance(production, dict) else None
    if isinstance(plan, list) and plan:
        return [item for item in plan if isinstance(item, dict)]
    return []


def _russian_overlay_title_for_session(session: dict[str, Any]) -> str:
    candidates = [session.get("overlay_title_by_lang")]
    production = session.get("production")
    if isinstance(production, dict):
        candidates.append(production.get("overlay_title_by_lang"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        direct = candidate.get("ru")
        if not isinstance(direct, str):
            direct = next(
                (
                    item
                    for key, item in candidate.items()
                    if str(key or "").strip().lower().replace("_", "-").startswith("ru")
                ),
                "",
            )
        value = " ".join(str(direct or "").split())
        if value:
            return value[:180]
    return ""


def _human_title_for_session(session: dict[str, Any], scenes_plan: list[dict[str, Any]]) -> str:
    overlay_title = _russian_overlay_title_for_session(session)
    if overlay_title:
        return overlay_title
    for key in ("selected_concept", "recipe_title", "recipe_name"):
        raw = session.get(key)
        value = raw.strip() if isinstance(raw, str) else ""
        if value:
            return value[:180]
    viral_brief = session.get("viral_concept_brief")
    if isinstance(viral_brief, dict):
        for key in ("dish_name", "concept_name", "title", "strongest_first_phrase"):
            raw = viral_brief.get(key)
            value = raw.strip() if isinstance(raw, str) else ""
            if value:
                return value[:180]
    script = str(session.get("recipe_full_text") or "").strip()
    if script:
        first_line = next((line.strip() for line in script.splitlines() if line.strip()), "")
        if first_line:
            return first_line[:180]
    for scene in scenes_plan:
        text = str(scene.get("text") or "").strip()
        if text:
            return text[:180]
    return "Content Fabric generation"


def _series_fields_for_session(session: dict[str, Any]) -> dict[str, Any]:
    series = session.get("series")
    if not isinstance(series, dict):
        return {"series_id": None, "series_video_index": None, "series_total_videos": None}
    raw_id = str(series.get("id") or "").strip()
    series_id: uuid.UUID | None = None
    if raw_id:
        try:
            series_id = uuid.UUID(raw_id)
        except ValueError:
            series_id = None
    return {
        "series_id": series_id,
        "series_video_index": int(series.get("video_index") or series.get("current_video_index") or 0) or None,
        "series_total_videos": int(series.get("total_videos") or 0) or None,
    }


def _scheduled_item_uuid(session: dict[str, Any]) -> uuid.UUID | None:
    raw = str(session.get("scheduled_item_id") or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def ensure_generation_run_for_session(session: dict[str, Any]) -> tuple[str | None, dict[int, str]]:
    """Create a lightweight run+scene snapshot in DB on first persistence."""
    existing = str(session.get("generation_run_id") or "").strip()
    raw_map = session.get("scene_db_ids_by_index")
    if existing:
        title = _human_title_for_session(session, _scenes_plan_for_session(session))
        series_fields = _series_fields_for_session(session)
        scenes_plan = _scenes_plan_for_session(session)
        try:
            with SessionLocal() as db:
                run = db.get(GenerationRun, uuid.UUID(existing))
                if run is not None and not str(run.human_title or "").strip():
                    run.human_title = title
                    changed = True
                else:
                    changed = False
                if run is not None:
                    changed = _apply_run_cursor(run, session) or changed
                    scene_map = _update_scene_prompt_rows(db, run.id, scenes_plan) if scenes_plan else {}
                    if scene_map:
                        session["scene_db_ids_by_index"] = scene_map
                        raw_map = scene_map
                        changed = True
                if run is not None and series_fields["series_id"] and not run.series_id:
                    run.series_id = series_fields["series_id"]
                    run.series_video_index = series_fields["series_video_index"]
                    run.series_total_videos = series_fields["series_total_videos"]
                    changed = True
                if run is not None:
                    scheduled_item_id = _scheduled_item_uuid(session)
                    is_draft = bool(session.get("is_draft") or session.get("scheduled_generation_mode"))
                    if scheduled_item_id and getattr(run, "scheduled_item_id", None) != scheduled_item_id:
                        run.scheduled_item_id = scheduled_item_id
                        changed = True
                    if is_draft and not bool(getattr(run, "is_draft", False)):
                        run.is_draft = True
                        changed = True
                if changed:
                    db.commit()
        except Exception:
            pass
        if isinstance(raw_map, dict):
            return existing, {int(k): str(v) for k, v in raw_map.items()}
        return existing, {}

    scenes_plan = _scenes_plan_for_session(session)
    if not scenes_plan:
        return None, {}
    script = str(session.get("recipe_full_text") or "").strip()
    if not script:
        try:
            script = json.dumps(scenes_plan, ensure_ascii=False)
        except Exception:
            script = "content_fabric_generation"

    with SessionLocal() as db:
        series_fields = _series_fields_for_session(session)
        run = GenerationRun(
            status="media_in_progress",
            **_run_cursor_from_session(session),
            is_draft=bool(session.get("is_draft") or session.get("scheduled_generation_mode")),
            human_title=_human_title_for_session(session, scenes_plan),
            title_color=_title_color_for_session(session),
            language=_language_for_session(session),
            **series_fields,
            scheduled_item_id=_scheduled_item_uuid(session),
            script=script,
        )
        db.add(run)
        db.flush()
        scene_map: dict[int, str] = {}
        for idx, scene in enumerate(scenes_plan):
            if not isinstance(scene, dict):
                continue
            row = Scene(
                run_id=run.id,
                scene_order=idx + 1,
                text=str(scene.get("text") or ""),
                duration_sec=float(scene.get("duration_sec") or 0.0),
            )
            db.add(row)
            db.flush()
            _apply_prompt_payload_to_scene_row(row, scene)
            scene_map[idx] = str(row.id)
        db.commit()
        session["generation_run_id"] = str(run.id)
        session["scene_db_ids_by_index"] = scene_map
        return str(run.id), scene_map


def set_generation_run_title_color_for_session(session: dict[str, Any], title_color: Any) -> str:
    """Persist an explicitly chosen title color on the linked GenerationRun."""
    normalized = _normalize_generation_title_color(title_color)
    if not normalized:
        return ""
    run_id_str = str(session.get("generation_run_id") or "").strip()
    if not run_id_str:
        run_id_str, _scene_map = ensure_generation_run_for_session(session)
    if not run_id_str:
        return normalized
    try:
        run_id = uuid.UUID(run_id_str)
    except (TypeError, ValueError, AttributeError):
        return normalized
    with SessionLocal() as db:
        run = db.get(GenerationRun, run_id)
        if run is not None and getattr(run, "title_color", "") != normalized:
            run.title_color = normalized
            db.commit()
    return normalized


def update_generation_run_cursor_from_session(session: dict[str, Any]) -> None:
    run_id_str = str(session.get("generation_run_id") or "").strip()
    if not run_id_str:
        return
    try:
        run_id = uuid.UUID(run_id_str)
    except (TypeError, ValueError, AttributeError):
        return
    with SessionLocal() as db:
        run = db.get(GenerationRun, run_id)
        if run is None:
            return
        _apply_run_cursor(run, session)
        db.commit()


def mark_generation_run_ready(session: dict[str, Any], *, status: str = "ready_for_production") -> None:
    run_id_str = str(session.get("generation_run_id") or "").strip()
    if not run_id_str:
        return
    try:
        run_id = uuid.UUID(run_id_str)
    except (TypeError, ValueError, AttributeError):
        return
    with SessionLocal() as db:
        run = db.get(GenerationRun, run_id)
        if run is None:
            return
        run.status = status
        run.is_active = False
        run.current_scene_index = None
        run.current_media_substep = None
        run.current_media_language_index = None
        db.commit()


def save_video_result_estimate(
    session: dict[str, Any],
    *,
    scene_idx: int,
    language: str,
    provider: str,
    status: str,
    video_path: str | None,
    cost_usd: float | None,
    cost_breakdown: dict[str, Any] | None,
) -> str | None:
    """Persist one estimated video result row if DB/session context is available."""
    run_id_str, scene_map = ensure_generation_run_for_session(session)
    scene_id_str = scene_map.get(int(scene_idx))
    if not run_id_str or not scene_id_str:
        return None

    with SessionLocal() as db:
        bind = db.get_bind()
        inspector = sa.inspect(bind)
        cols = {col["name"] for col in inspector.get_columns("video_results")} if inspector.has_table("video_results") else set()
        row_kwargs: dict[str, Any] = {
            "run_id": uuid.UUID(run_id_str),
            "scene_id": uuid.UUID(scene_id_str),
            "provider": provider,
            "status": status,
            "video_path": video_path,
            "score": None,
        }
        if "language" in cols:
            row_kwargs["language"] = str(language or "ru")
        if "estimated_cost_usd" in cols:
            row_kwargs["estimated_cost_usd"] = round(float(cost_usd), 6) if cost_usd is not None else None
        if "cost_breakdown_json" in cols:
            row_kwargs["cost_breakdown_json"] = cost_breakdown or {}
        row = VideoResult(**row_kwargs)
        db.add(row)
        db.commit()
        return str(row.id)
