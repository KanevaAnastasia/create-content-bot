"""Persistence helpers for generation runs and video cost estimates."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select

from .db import SessionLocal
from .models import GenerationArtifact, GenerationRun, Scene, SceneTextRevision, VideoResult

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
SCENE_META_SNAPSHOT_KEYS = (
    "camera_design",
    "single_take",
    "preparation_offscreen",
    "prop_descriptions",
    "episode_prop_catalog",
    "scene_purpose",
    "discipline_narrative_mode",
    "discipline_cast_override",
    "story_camera_reveal_requested",
    "continuation_source_video_candidate_id",
    "characters",
    "dialogue_mode",
    "dialogue",
    "dialogue_by_lang",
    "speech_plan",
    "speech_plan_by_lang",
    "speech_cues_by_lang",
    "speech_budget_by_lang",
    "speech_budget_override_by_lang",
    "speech_origin_by_lang",
    "action",
    "visual_description",
    "retention_function",
    "micro_events",
    "continuity_anchors",
    "continuity_state",
    "dish_description",
    "recipe_step",
    "shot_type",
    "location",
    "lighting",
    "color_palette",
    "location_segment",
    "location_transition",
    "model",
    "generation_mode",
    "aspect_ratio",
    "voice_provider",
    "voice_needed",
    "has_lipsync",
    "advertising_product_required",
    "advertising_product_lock_ru",
    "advertising_product_lock_en",
    "narration_excerpt",
    "start_sec",
    "end_sec",
    "timeline_duration_sec",
    "timeline_duration_by_lang",
    "generation_duration_sec",
    "semantic_beat",
    "semantic_clip_index",
    "semantic_clip_count",
    "end_title_by_lang",
    "end_title_style",
    "end_title_duration_sec",
    "visualization_output_policy",
    "era_style",
    "time_period",
    "grandma_age",
    "cat_present",
    "story_character_descriptions",
    "story_character_reference_prompts",
    "story_prop_ids",
    "story_prop_reference_catalog",
    "story_excluded_prop_ids",
    "story_required_reference_ids",
    "characters_absent_reason",
    "story_prop_descriptions",
    "continuity_manifest_path",
    "story_character_labels",
    "style_reference_paths",
    "story_visual_style_key",
    "story_visual_style_label",
    "references",
    "start_frame_ref_count",
    "start_frame_image_model",
    "end_frame_image_model",
    "start_image_generation_attempts",
    "transition_note",
    "sound_design",
    "silence_base_rules",
)
PROMPT_CHARACTER_PATTERNS = {
    "grandma": (r"\bgrandma\b", r"\bGRANDMA\b", r"\bбабушк\w*", r"\bvoice_lock_full_grandma\b"),
    "cat": (r"\borange\s+cartoon\s+cat\b", r"\bрыж\w*\s+кот\w*"),
    "grandson": (r"\bgrandson\b", r"\bGRANDSON\b", r"\bвнук\w*", r"\bvoice_lock_full_grandson\b"),
    "granddaughter": (r"\bgranddaughter\b", r"\bGRANDDAUGHTER\b", r"\bвнучк\w*", r"\bvoice_lock_full_granddaughter\b"),
    "grandfather": (r"\bgrandfather\b", r"\bGRANDFATHER\b", r"\bдедушк\w*", r"\bvoice_lock_full_grandfather\b"),
    "daughter": (r"\bdaughter\b", r"\bDAUGHTER\b", r"\bдоч(?:ка|ку|ке|кой|ери|ерью)\b", r"\bvoice_lock_full_daughter\b"),
    "dog": (r"\bdog\b", r"\bп(?:ё|е)с\b", r"\bсобак\w*"),
    "zaya": (r"\bzaya\b", r"\bбел\w*\s+кош\w*", r"\bwhite\s+cartoon\s+cat\b"),
    "markiz": (r"\bmarkiz\b", r"\bчерно-бел\w*\s+кот\w*", r"\bч[её]рно-бел\w*\s+кот\w*"),
}
PROMPT_CHARACTER_ORDER = ("grandma", "cat", "grandson", "granddaughter", "grandfather", "daughter", "dog", "zaya", "markiz")
PROMPT_DIALOGUE_LABELS = {
    "GRANDMA": "grandma",
    "GRANDSON": "grandson",
    "GRANDDAUGHTER": "granddaughter",
    "GRANDFATHER": "grandfather",
    "DAUGHTER": "daughter",
}
CORE_CAST_EXCEPTION_RE = re.compile(
    r"\b(?:only|without)\b.*\b(?:banana|strawberry|grandma|cat)\b|"
    r"\b(?:banana|strawberry)\b.*\bwithout\b.*\b(?:grandma|cat)\b|"
    r"\bтолько\b.*\b(?:банан|клубник)\w*\b|"
    r"\bбез\b.*\b(?:бабушк|кот)\w*\b",
    re.IGNORECASE,
)
DEFAULT_GENERATION_TITLE_COLOR = "#c72638"
HEX_TITLE_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
PLACEHOLDER_GENERATION_TITLES = {"Рецепт пользователя"}
OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE = "overlay_title_by_lang"
OVERLAY_TITLE_BY_LANG_URI_TEMPLATE = "db://generation_runs/{run_id}/overlay_title_by_lang"


def _is_placeholder_generation_title(value: Any) -> bool:
    return " ".join(str(value or "").split()) in PLACEHOLDER_GENERATION_TITLES


def _clean_overlay_title_by_lang_for_session(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        lang = str(key or "").strip().lower()
        title = " ".join(str(item or "").split())
        if lang and title:
            out[lang] = title[:180]
    return out


def _overlay_title_by_lang_for_session(session: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    production = session.get("production")
    for value in (
        session.get("overlay_title_by_lang"),
        production.get("overlay_title_by_lang") if isinstance(production, dict) else None,
    ):
        out.update(_clean_overlay_title_by_lang_for_session(value))
    return out


def _overlay_title_by_lang_uri(run_id: uuid.UUID | str) -> str:
    return OVERLAY_TITLE_BY_LANG_URI_TEMPLATE.format(run_id=run_id)


def _sync_overlay_title_by_lang_artifact(db, run_id: uuid.UUID, session: dict[str, Any]) -> bool:
    title_by_lang = _overlay_title_by_lang_for_session(session)
    if not title_by_lang:
        return False
    uri = _overlay_title_by_lang_uri(run_id)
    row = (
        db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == run_id,
                GenerationArtifact.artifact_type == OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE,
                GenerationArtifact.uri == uri,
                GenerationArtifact.language.is_(None),
            )
        )
        .scalars()
        .first()
    )
    payload = {"overlay_title_by_lang": title_by_lang, "source": "generation_store_session_sync"}
    if row is None:
        db.add(
            GenerationArtifact(
                run_id=run_id,
                scene_id=None,
                artifact_type=OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE,
                label="Overlay title by language",
                uri=uri,
                language=None,
                mime_type="application/json",
                payload_json=payload,
            )
        )
        return True
    if row.payload_json != payload or row.label != "Overlay title by language" or row.mime_type != "application/json":
        row.label = "Overlay title by language"
        row.mime_type = "application/json"
        row.payload_json = payload
        return True
    return False


def overlay_title_by_lang_for_run(run_id: uuid.UUID | str | None) -> dict[str, str]:
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError, AttributeError):
        return {}
    with SessionLocal() as db:
        row = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE,
                    GenerationArtifact.uri == _overlay_title_by_lang_uri(run_uuid),
                )
                .order_by(GenerationArtifact.created_at.desc(), GenerationArtifact.id.desc())
            )
            .scalars()
            .first()
        )
    payload = row.payload_json if row is not None and isinstance(row.payload_json, dict) else {}
    return _clean_overlay_title_by_lang_for_session(payload.get("overlay_title_by_lang"))


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
    _ensure_core_grandma_cat_characters(scene)
    payload: dict[str, Any] = {}
    for key in SCENE_PROMPT_TEXT_KEYS:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value
    for key in SCENE_PROMPT_JSON_KEYS:
        if key == "prompt_meta_json":
            continue
        value = scene.get(key)
        if isinstance(value, dict):
            payload[key] = value
    meta = dict(scene.get("prompt_meta_json") or {}) if isinstance(scene.get("prompt_meta_json"), dict) else {}
    for key in SCENE_META_SNAPSHOT_KEYS:
        if key not in scene:
            continue
        value = scene.get(key)
        if value in (None, ""):
            continue
        # A revised scene plan must replace its previous structured snapshot.
        # Keep old values only when the incoming payload omits that field;
        # otherwise stale cast/timing metadata can leak into the new plan. An
        # explicit empty list/dict is meaningful (for example, a prop-only
        # story shot with no characters) and must clear the old value.
        meta[key] = value
    if meta:
        payload["prompt_meta_json"] = meta
    return payload


def _scene_prompt_text_blob(scene: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "action", "visual_description", "recipe_step", "start_frame_prompt", "end_frame_prompt"):
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang"):
        value = scene.get(key)
        if isinstance(value, dict):
            parts.extend(str(item) for item in value.values() if str(item or "").strip())
    return "\n".join(parts)


def _infer_characters_from_scene_prompt(scene: dict[str, Any]) -> list[str]:
    blob = _scene_prompt_text_blob(scene)
    if not blob.strip():
        return []
    out: list[str] = []
    for key in PROMPT_CHARACTER_ORDER:
        if any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in PROMPT_CHARACTER_PATTERNS.get(key, ())):
            out.append(key)
    if not out and blob.strip():
        out.append("grandma")
    elif "grandma" not in out:
        out.insert(0, "grandma")
    return out


def _infer_dialogue_from_scene_prompt(scene: dict[str, Any]) -> list[dict[str, str]]:
    blob = _scene_prompt_text_blob(scene)
    if "character_dialogue_script" not in blob.lower():
        return []
    out: list[dict[str, str]] = []
    for match in re.finditer(r"(?m)^\s*([A-Z_]+)\s*:\s*(.+?)\s*$", blob):
        speaker = PROMPT_DIALOGUE_LABELS.get(match.group(1).strip().upper())
        text = match.group(2).strip()
        if speaker and text:
            out.append({"speaker": speaker, "text": text})
    return out


def _allows_missing_core_grandma_cat(scene: dict[str, Any]) -> bool:
    if str(scene.get("generation_mode") or "").strip().lower() == "story":
        return True
    chars = scene.get("characters")
    normalized = {str(ch).strip().lower() for ch in chars if str(ch).strip()} if isinstance(chars, list) else set()
    if normalized and not normalized.issubset({"banana", "strawberry"}):
        return False
    return bool(CORE_CAST_EXCEPTION_RE.search(_scene_prompt_text_blob(scene)))


def _ensure_core_grandma_cat_characters(scene: dict[str, Any]) -> None:
    if _allows_missing_core_grandma_cat(scene):
        return
    chars = scene.get("characters")
    normalized = [str(ch).strip().lower() for ch in chars if str(ch).strip()] if isinstance(chars, list) else []
    if not normalized and not _scene_prompt_text_blob(scene).strip():
        return
    if "grandma" not in normalized:
        normalized.insert(0, "grandma")
    if "cat" not in normalized:
        insert_at = normalized.index("grandma") + 1 if "grandma" in normalized else len(normalized)
        normalized.insert(insert_at, "cat")
    scene["characters"] = list(dict.fromkeys(ch for ch in normalized if ch))


def _apply_prompt_payload_to_scene_row(row: Scene, scene: dict[str, Any]) -> bool:
    payload = _scene_prompt_payload(scene)
    changed = False
    for key in SCENE_PROMPT_TEXT_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text_value = value
        if getattr(row, key) != text_value:
            setattr(row, key, text_value)
            changed = True
    for key in SCENE_PROMPT_JSON_KEYS:
        value = payload.get(key)
        if not isinstance(value, dict) or not value:
            continue
        json_value = value
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
    meta = scene.get("prompt_meta_json")
    if isinstance(meta, dict):
        for key in SCENE_META_SNAPSHOT_KEYS:
            if key in scene:
                continue
            if key not in meta:
                continue
            value = meta.get(key)
            if value not in (None, ""):
                scene[key] = value
    speech_by_lang = scene.get("speech_by_lang")
    speech = dict(speech_by_lang) if isinstance(speech_by_lang, dict) else {}
    scene_text = str(scene.get("text") or scene.get("recipe_step") or "").strip()
    if not scene_text and str(speech.get("ru") or "").strip():
        scene_text = str(speech["ru"]).strip()
        scene["text"] = scene_text
    if scene_text and not str(speech.get("ru") or "").strip():
        speech["ru"] = scene_text
        scene["speech_by_lang"] = speech
    explicit_empty_story_cast = (
        str(scene.get("generation_mode") or "").strip().lower() == "story"
        and isinstance(scene.get("characters"), list)
    )
    if not explicit_empty_story_cast and (
        not isinstance(scene.get("characters"), list) or not scene.get("characters")
    ):
        inferred_characters = _infer_characters_from_scene_prompt(scene)
        if inferred_characters:
            scene["characters"] = inferred_characters
    if not isinstance(scene.get("dialogue"), list) or not scene.get("dialogue"):
        inferred_dialogue = _infer_dialogue_from_scene_prompt(scene)
        if inferred_dialogue:
            scene["dialogue"] = inferred_dialogue
            scene["dialogue_mode"] = "multi" if len(inferred_dialogue) > 1 else "single"
    _ensure_core_grandma_cat_characters(scene)


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


def saved_plan_scene_ids(session: dict[str, Any]) -> list[str]:
    """Read the zero-based scene map from either a live session or a JSON snapshot."""
    mapping = session.get("scene_db_ids_by_index")
    if not isinstance(mapping, dict) or not mapping:
        return []
    ids = [str(mapping.get(i) or mapping.get(str(i)) or "") for i in range(len(mapping))]
    return ids if all(ids) and len(set(ids)) == len(ids) else []


def mapped_scene_rows(session: dict[str, Any], rows: Sequence[Scene]) -> list[Scene]:
    """Resolve the entire saved plan against rows from one run, in its saved order."""
    by_id = {str(row.id): row for row in rows}
    ids = saved_plan_scene_ids(session)
    return [by_id[key] for key in ids] if all(key in by_id for key in ids) else []


def _update_scene_prompt_rows(
    db, run_id: uuid.UUID, scenes_plan: list[dict[str, Any]], session: dict[str, Any],
) -> dict[int, str]:
    rows = db.execute(select(Scene).where(Scene.run_id == run_id).order_by(Scene.scene_order.asc())).scalars().all()
    rows_by_order = {int(row.scene_order): row for row in rows}
    mapped = mapped_scene_rows(session, rows)
    scene_map: dict[int, str] = {}
    for idx, scene in enumerate(scenes_plan):
        if not isinstance(scene, dict):
            continue
        order = idx + 1
        if mapped:
            row = mapped[idx] if idx < len(mapped) else None
        else:
            row = rows_by_order.get(order)
        if row is None:
            # A removed draft scene may still own this ordinal and its approved media.
            if order in rows_by_order:
                order = max(rows_by_order) + 1
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
        if value and not _is_placeholder_generation_title(value):
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
                overlay_title = _russian_overlay_title_for_session(session)
                if run is not None and (
                    (overlay_title and str(run.human_title or "").strip() != overlay_title)
                    or not str(run.human_title or "").strip()
                    or (_is_placeholder_generation_title(run.human_title) and not _is_placeholder_generation_title(title))
                ):
                    run.human_title = title
                    changed = True
                else:
                    changed = False
                if run is not None:
                    if run.project_id is None:
                        from .project_profiles import resolve_project_id

                        run.project_id = resolve_project_id(session.get("selected_project_key"))
                        changed = run.project_id is not None or changed
                    changed = _apply_run_cursor(run, session) or changed
                    scene_map = _update_scene_prompt_rows(db, run.id, scenes_plan, session) if scenes_plan else {}
                    if scene_map:
                        session["scene_db_ids_by_index"] = scene_map
                        raw_map = scene_map
                        changed = True
                    changed = _sync_overlay_title_by_lang_artifact(db, run.id, session) or changed
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
        from .project_profiles import resolve_project_id

        series_fields = _series_fields_for_session(session)
        run = GenerationRun(
            project_id=resolve_project_id(session.get("selected_project_key")),
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
        _sync_overlay_title_by_lang_artifact(db, run.id, session)
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
