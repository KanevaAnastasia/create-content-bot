"""Persistent storage for active Telegram sessions."""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session, merge_scene_prompt_payload, overlay_title_by_lang_for_run
from .models import ChatSession, GenerationArtifact, GenerationRun, Scene
from .scene_media_store import persist_scene_video_prompts, rebuild_production_session, rebuild_scene_db_ids_for_session

logger = logging.getLogger("bot.session")

GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE = "guest_run_session_snapshot"
GUEST_SYNTAX_PENDING_STATE = "guest_syntax_pending"
GUEST_SYNTAX_RETRY_STATE = "guest_syntax_error"

_BYTES_MARKER = "__cf_bytes_b64__"
_PENDING_SESSION_KEYS = (
    "scene_image_edit_pending",
    "scene_video_feedback_pending",
    "scene_prompt_input_pending",
    "scene_phrase_input_pending",
    "scene_video_speed_pending",
)
_RUN_PENDING_SESSION_KEYS = (
    "social_descriptions_pending",
)
_CHAT_SESSION_CACHE_KEYS = {
    "directorial_notes",
    "state",
    "selected_project_key",
    "project_workspace_mode",
    "force_story_mode",
    "project_voice_id",
    "generation_run_id",
    "scene_db_ids_by_index",
    "image_scene_index",
    "scene_media_substep",
    "scene_media_language_index",
    "target_languages",
    "overlay_title",
    "overlay_title_by_lang",
    "overlay_subtitle_by_lang",
    "overlay_title_style",
    "overlay_subtitle_decided",
    "scene_media_approve_mode",
    "scene_media_ai_human_review",
    "scheduled_generation_mode",
    "scheduled_item_id",
    "is_draft",
    "generation_mode",
    "copy_mode_analysis",
    "copy_mode_chunks",
    "copy_mode_current_chunk_index",
    "copy_mode_generated_chunks",
    "copy_mode_last_generated_chunk_index",
    "copy_mode_source_video",
    "copy_mode_user_feedback",
    "copy_mode_weather",
    "copy_mode_description_text",
    "story_source_text",
    "story_title",
    "story_voiceover_text",
    "story_voiceover_versions",
    "story_custom_script_input_pending",
    "story_english_requested",
    "story_visual_style",
    "story_title_en",
    "story_voiceover_text_en",
    "story_voiceover_versions_en",
    "story_audio_path",
    "story_audio_duration_sec",
    "story_audio_alignment",
    "story_audio_versions",
    "story_audio_path_en",
    "story_audio_duration_sec_en",
    "story_audio_alignment_en",
    "story_audio_versions_en",
    "story_visual_duration_sec",
    "story_character_bible",
    "story_character_references",
    "story_character_approval_index",
    "visualization_source_media",
    "visualization_transcript_segments",
    "visualization_input_kind",
    "discipline_narrative_mode",
    "discipline_intro_enabled",
    "discipline_intro_dialogue",
    "discipline_intro_dialogue_versions",
    "discipline_intro_image_path",
    "discipline_intro_image_versions",
    "discipline_intro_audio_path",
    "discipline_intro_video_path",
    "discipline_intro_video_versions",
    "discipline_intro_approved_video_path",
    "discipline_intro_approved_provider",
    "advertising_source_text",
    "advertising_product_path",
    "advertising_product_brief",
    "advertising_content_type",
    "advertising_campaigns",
    "advertising_selected_campaign",
    "advertising_format",
    "advertising_post_text",
    "advertising_post_image_paths",
    "sponsored_content",
    "brand_context",
    "series_pending_next",
    "series_choice_ids",
    "series_choice_confirmed",
    "guest_mode",
    "bot_user_id",
    "project_id",
    "user_role",
    "guest_data",
}
_CHAT_SESSION_FORBIDDEN_KEYS = {
    "production",
    "scenes_plan",
    "frame_prompts",
    "video_prompt",
    "video_prompt_by_lang",
    "image_candidates",
    "video_candidates_by_lang",
    *_PENDING_SESSION_KEYS,
    *_RUN_PENDING_SESSION_KEYS,
}
_INLINE_MEDIA_KEYS = {
    "image_b64",
    "video_b64",
    "video_bytes",
    "start_frame_image_b64",
    "end_frame_image_b64",
}
_INLINE_MEDIA_KEY_SUFFIXES = ("_image_b64", "_video_b64")


def _is_inline_media_key(key: Any) -> bool:
    text = str(key)
    return text in _INLINE_MEDIA_KEYS or text.endswith(_INLINE_MEDIA_KEY_SUFFIXES)


def _strip_inline_media_payloads(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_inline_media_payloads(item)
            for key, item in value.items()
            if not _is_inline_media_key(key)
        }
    if isinstance(value, list):
        return [_strip_inline_media_payloads(item) for item in value]
    return value


def _chat_session_cache_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Return the small UI cache allowed in chat_sessions.session_json."""
    payload = {
        key: session[key]
        for key in _CHAT_SESSION_CACHE_KEYS
        if key in session and session[key] not in (None, "", [], {})
    }
    for key in _CHAT_SESSION_FORBIDDEN_KEYS:
        payload.pop(key, None)
    return _strip_inline_media_payloads(payload)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value


def _deserialize_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {_BYTES_MARKER}:
            raw = str(value.get(_BYTES_MARKER) or "").strip()
            return base64.b64decode(raw) if raw else b""
        return {str(key): _deserialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deserialize_value(item) for item in value]
    return value


def _pending_scene_index(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    try:
        scene_idx = int(value.get("scene_idx"))
    except (TypeError, ValueError):
        return None
    return scene_idx if scene_idx >= 0 else None


def _scene_map_for_session(session: dict[str, Any]) -> dict[int, str]:
    raw = session.get("scene_db_ids_by_index")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        raw_id = str(value or "").strip()
        if raw_id:
            out[idx] = raw_id
    return out


def _sync_pending_inputs_to_artifacts(session: dict[str, Any], run_id_str: str | None) -> None:
    if not run_id_str:
        return
    try:
        run_id = uuid.UUID(str(run_id_str))
    except (TypeError, ValueError, AttributeError):
        return
    scene_map = _scene_map_for_session(session)
    with SessionLocal() as db:
        stale_rows = db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == run_id,
                GenerationArtifact.artifact_type.in_(["scene_pending_input", "run_pending_input"]),
            )
        ).scalars().all()
        for row in stale_rows:
            db.delete(row)
        for key in _PENDING_SESSION_KEYS:
            pending = session.get(key)
            scene_idx = _pending_scene_index(pending)
            if scene_idx is None:
                continue
            scene_id_raw = scene_map.get(scene_idx)
            if not scene_id_raw:
                continue
            try:
                scene_id = uuid.UUID(str(scene_id_raw))
            except (TypeError, ValueError, AttributeError):
                continue
            language = str(pending.get("language") or "").strip().lower() or None
            db.add(
                GenerationArtifact(
                    run_id=run_id,
                    scene_id=scene_id,
                    artifact_type="scene_pending_input",
                    label=f"Scene {scene_idx + 1} pending input: {key}",
                    uri=f"db://scenes/{scene_id}/pending_input/{key}",
                    language=language,
                    mime_type="application/json",
                    payload_json={
                        "session_key": key,
                        "pending": _serialize_value(pending),
                    },
                )
            )
        for key in _RUN_PENDING_SESSION_KEYS:
            pending = session.get(key)
            if not isinstance(pending, dict):
                continue
            db.add(
                GenerationArtifact(
                    run_id=run_id,
                    scene_id=None,
                    artifact_type="run_pending_input",
                    label=f"Run pending input: {key}",
                    uri=f"db://generation_runs/{run_id}/pending_input/{key}",
                    language=None,
                    mime_type="application/json",
                    payload_json={
                        "session_key": key,
                        "pending": _serialize_value(pending),
                    },
                )
            )
        db.commit()


def _restore_pending_inputs_from_artifacts(session: dict[str, Any], run_id_str: str | None) -> None:
    if not run_id_str:
        return
    try:
        run_id = uuid.UUID(str(run_id_str))
    except (TypeError, ValueError, AttributeError):
        return
    with SessionLocal() as db:
        rows = db.execute(
            select(GenerationArtifact)
            .where(
                GenerationArtifact.run_id == run_id,
                GenerationArtifact.artifact_type.in_(["scene_pending_input", "run_pending_input"]),
            )
            .order_by(GenerationArtifact.created_at.asc(), GenerationArtifact.id.asc())
        ).scalars().all()
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        key = str(payload.get("session_key") or "").strip()
        if key not in (*_PENDING_SESSION_KEYS, *_RUN_PENDING_SESSION_KEYS) or key in session:
            continue
        pending = _deserialize_value(payload.get("pending"))
        if key in _RUN_PENDING_SESSION_KEYS and isinstance(pending, dict):
            session[key] = pending
        elif isinstance(pending, dict) and _pending_scene_index(pending) is not None:
            session[key] = pending


def _ensure_archive_production_shell(session: dict[str, Any], run_id_str: str | None) -> None:
    if not run_id_str:
        return
    production = session.get("production")
    plan = production.get("json_plan") if isinstance(production, dict) else None
    if isinstance(plan, list) and plan:
        return
    try:
        run_uuid = uuid.UUID(str(run_id_str))
    except (TypeError, ValueError, AttributeError):
        return
    with SessionLocal() as db:
        rows = db.execute(
            select(Scene).where(Scene.run_id == run_uuid).order_by(Scene.scene_order.asc())
        ).scalars().all()
    if not rows:
        return
    scene_map: dict[int, str] = {}
    json_plan: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        scene_map[idx] = str(row.id)
        scene = {
            "scene": int(row.scene_order),
            "text": row.text or "",
            "duration_sec": float(row.duration_sec or 0.0),
        }
        for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
            value = getattr(row, key, None)
            if isinstance(value, str) and value.strip():
                scene[key] = value
        for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang", "prompt_meta_json"):
            value = getattr(row, key, None)
            if isinstance(value, dict) and value:
                scene[key] = dict(value)
        merge_scene_prompt_payload(scene, scene)
        json_plan.append(scene)
    if not isinstance(session.get("scene_db_ids_by_index"), dict):
        session["scene_db_ids_by_index"] = scene_map
    session["production"] = {"json_plan": json_plan}


def _ensure_scene_plan_from_production(session: dict[str, Any]) -> None:
    if isinstance(session.get("scenes_plan"), list) and session["scenes_plan"]:
        return
    production = session.get("production")
    plan = production.get("json_plan") if isinstance(production, dict) else None
    if isinstance(plan, list) and plan:
        session["scenes_plan"] = [dict(item) for item in plan if isinstance(item, dict)]


def _restore_overlay_title_by_lang_from_artifact(session: dict[str, Any], generation_run_id: str | None) -> None:
    title_by_lang = overlay_title_by_lang_for_run(generation_run_id)
    if not title_by_lang:
        return
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    existing: dict[str, str] = {}
    for source in (production.get("overlay_title_by_lang"), session.get("overlay_title_by_lang")):
        if not isinstance(source, dict):
            continue
        existing.update(
            {
                str(key).strip().lower(): " ".join(str(value or "").split())
                for key, value in source.items()
                if str(key).strip() and str(value or "").strip()
            }
        )
    title_by_lang.update(existing)
    production["overlay_title_by_lang"] = dict(title_by_lang)
    session["production"] = production
    session["overlay_title_by_lang"] = dict(title_by_lang)


def save_session(chat_id: int, session: dict[str, Any]) -> None:
    if not isinstance(session, dict):
        return
    run_id_str = str(session.get("generation_run_id") or "").strip() or None
    try:
        has_scene_snapshot = isinstance(session.get("production"), dict) or (
            isinstance(session.get("scenes_plan"), list) and bool(session.get("scenes_plan"))
        )
        if has_scene_snapshot:
            run_id_str, scene_map = ensure_generation_run_for_session(session)
            if run_id_str and scene_map:
                session["scene_db_ids_by_index"] = scene_map
    except Exception:
        logger.exception("session_store.ensure_generation_run_failed chat_id=%s", chat_id)
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production.get("json_plan"), list) else []
    for scene_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        prompt_map = scene.get("video_prompt_by_lang")
        if isinstance(prompt_map, dict) and any(str(value or "").strip() for value in prompt_map.values()):
            try:
                persist_scene_video_prompts(
                    session,
                    scene_idx=scene_idx,
                    prompts_by_lang=prompt_map,
                    source="session_store.save_session",
                )
            except Exception:
                logger.exception("session_store.persist_video_prompts_failed chat_id=%s scene_idx=%s", chat_id, scene_idx)
    try:
        _sync_pending_inputs_to_artifacts(session, run_id_str)
    except Exception:
        logger.exception("session_store.persist_pending_inputs_failed chat_id=%s", chat_id)
    if run_id_str:
        payload = _serialize_value(_chat_session_cache_payload(session))
    else:
        payload = _serialize_value(_strip_inline_media_payloads(session))
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        if row is None:
            row = ChatSession(chat_id=int(chat_id), session_json={})
            db.add(row)
        row.state = str(session.get("state") or "").strip() or None
        row.session_json = payload
        row.generation_run_id = uuid.UUID(run_id_str) if run_id_str else None
        if row.generation_run_id is not None:
            run = db.get(GenerationRun, row.generation_run_id)
            if run is not None:
                try:
                    run.current_scene_index = int(session.get("image_scene_index"))
                except (TypeError, ValueError):
                    run.current_scene_index = None
                substep = str(session.get("scene_media_substep") or "").strip()
                run.current_media_substep = substep or None
                try:
                    run.current_media_language_index = int(session.get("scene_media_language_index"))
                except (TypeError, ValueError):
                    run.current_media_language_index = None
            if session.get("guest_mode") is True and str(session.get("state") or "") != "guest_generation_history":
                snapshot = (
                    db.execute(
                        select(GenerationArtifact)
                        .where(
                            GenerationArtifact.run_id == row.generation_run_id,
                            GenerationArtifact.artifact_type == GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
                        )
                        .order_by(GenerationArtifact.created_at.desc())
                    )
                    .scalars()
                    .first()
                )
                snapshot_payload = _serialize_value(_chat_session_cache_payload(session))
                if snapshot is None:
                    snapshot = GenerationArtifact(
                        run_id=row.generation_run_id,
                        artifact_type=GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
                        label="Состояние гостевой генерации",
                        uri=f"db://generation_runs/{row.generation_run_id}/guest_session",
                        mime_type="application/json",
                        payload_json={"session": snapshot_payload},
                    )
                    db.add(snapshot)
                else:
                    snapshot.payload_json = {"session": snapshot_payload}
        db.commit()


def load_guest_run_snapshot(run_id: str | uuid.UUID) -> dict[str, Any] | None:
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError, AttributeError):
        return None
    with SessionLocal() as db:
        row = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .first()
        )
        payload = dict(row.payload_json or {}) if row is not None else {}
    session = _deserialize_value(payload.get("session"))
    return session if isinstance(session, dict) else None


def load_session(chat_id: int) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        run = db.get(GenerationRun, row.generation_run_id) if row is not None and row.generation_run_id else None
    if row is None:
        return None
    session = _deserialize_value(dict(row.session_json or {}))
    if not isinstance(session, dict):
        return None
    generation_run_id = str(row.generation_run_id or session.get("generation_run_id") or "").strip() or None
    if generation_run_id:
        session["generation_run_id"] = generation_run_id
        if run is not None and not str(session.get("recipe_full_text") or "").strip() and str(run.script or "").strip():
            session["recipe_full_text"] = str(run.script)
        if run is not None and not str(session.get("selected_concept") or "").strip():
            title = " ".join(str(run.human_title or "").split())
            session["selected_concept"] = title or "Рецепт пользователя"
            session["selected_concept_name"] = title or "Рецепт пользователя"
        if run is not None and bool(run.is_active):
            if run.current_scene_index is not None:
                session["image_scene_index"] = int(run.current_scene_index)
            if run.current_media_substep:
                session["scene_media_substep"] = str(run.current_media_substep)
            if run.current_media_language_index is not None:
                session["scene_media_language_index"] = int(run.current_media_language_index)
        if not isinstance(session.get("scene_db_ids_by_index"), dict):
            session["scene_db_ids_by_index"] = rebuild_scene_db_ids_for_session(session, generation_run_id)
        _ensure_archive_production_shell(session, generation_run_id)
        rebuild_production_session(session, generation_run_id)
        _restore_overlay_title_by_lang_from_artifact(session, generation_run_id)
        _ensure_scene_plan_from_production(session)
        _restore_pending_inputs_from_artifacts(session, generation_run_id)
    return session


def delete_session(chat_id: int) -> None:
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        if row is None:
            return
        db.delete(row)
        db.commit()


def recover_interrupted_guest_syntax_sessions(*, session_factory=None) -> int:
    """Make persisted guest Syntax jobs retryable after a process restart.

    Syntax jobs live in asyncio tasks and cannot survive process termination. A
    persisted ``guest_syntax_pending`` state therefore has no live worker when a
    new bot process starts. Reconcile both the active chat snapshot and the
    guest-run history snapshot without sending any Telegram messages.
    """
    factory = session_factory or SessionLocal
    recovered = 0
    with factory() as db:
        rows = (
            db.execute(
                select(ChatSession)
                .where(ChatSession.state == GUEST_SYNTAX_PENDING_STATE)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for row in rows:
            session_payload = dict(row.session_json or {})
            session_payload["state"] = GUEST_SYNTAX_RETRY_STATE
            row.state = GUEST_SYNTAX_RETRY_STATE
            row.session_json = session_payload

            if row.generation_run_id is not None:
                snapshot = (
                    db.execute(
                        select(GenerationArtifact)
                        .where(
                            GenerationArtifact.run_id == row.generation_run_id,
                            GenerationArtifact.artifact_type == GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
                        )
                        .order_by(GenerationArtifact.created_at.desc())
                    )
                    .scalars()
                    .first()
                )
                if snapshot is not None:
                    snapshot_payload = dict(snapshot.payload_json or {})
                    snapshot_session = dict(snapshot_payload.get("session") or {})
                    snapshot_session["state"] = GUEST_SYNTAX_RETRY_STATE
                    snapshot_payload["session"] = snapshot_session
                    snapshot.payload_json = snapshot_payload
            recovered += 1
        if recovered:
            db.commit()
    if recovered:
        logger.warning("session_store.recovered_interrupted_guest_syntax count=%s", recovered)
    return recovered


def save_session_if_possible(chat_id: int | None, session: dict[str, Any] | None) -> None:
    if chat_id is None or not isinstance(session, dict):
        return
    try:
        save_session(int(chat_id), session)
    except Exception:
        logger.exception("session_store.save_failed chat_id=%s", chat_id)


def load_session_if_possible(chat_id: int | None) -> dict[str, Any] | None:
    if chat_id is None:
        return None
    try:
        return load_session(int(chat_id))
    except Exception:
        logger.exception("session_store.load_failed chat_id=%s", chat_id)
        return None


def delete_session_if_possible(chat_id: int | None) -> None:
    if chat_id is None:
        return
    try:
        delete_session(int(chat_id))
    except Exception:
        logger.exception("session_store.delete_failed chat_id=%s", chat_id)
