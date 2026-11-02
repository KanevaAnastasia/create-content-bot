"""Checkpoint history for restoring previous pipeline steps."""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from .constants import (
    STATE_READY_FOR_PRODUCTION,
    STATE_WAITING_CONCEPT,
    STATE_WAITING_FRAME_PROMPTS_APPROVAL,
    STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    STATE_WAITING_SCENE_MEDIA,
)
from .db import SessionLocal
from .models import PipelineStepCheckpoint
from .generation_store import SCENE_PROMPT_JSON_KEYS, SCENE_PROMPT_TEXT_KEYS
from .session_store import _deserialize_value, _serialize_value, save_session_if_possible

logger = logging.getLogger("bot.history")

TRANSIENT_SESSION_KEYS = {
    "scene_speech_shorten_pending",
    "scene_phrase_input_pending",
    "scene_phrase_translation_pending",
    "social_descriptions_pending",
    "scene_image_edit_pending",
    "scene_video_feedback_pending",
    "scene_video_speed_pending",
    "scene_prompt_input_pending",
    "scene_media_ai_human_review",
}

TRANSIENT_SCENE_KEYS = {
    "syntax_fallback_pending",
    "video_provider_choice_pending",
    "image_model_choice_pending",
}

CHECKPOINT_MAX_LARGE_STRING_CHARS = 200_000
CHECKPOINT_RETENTION_DAYS = 3
CHECKPOINT_RETENTION_LIMIT_PER_CHAT = 40

CHECKPOINT_MEDIA_KEY_FRAGMENTS = (
    "image_b64",
    "video_b64",
    "audio_b64",
    "base64",
    "image_bytes",
    "video_bytes",
    "audio_bytes",
    "image_candidates",
    "video_candidates",
    "reference_images",
    "user_reference_images",
)


@dataclass(frozen=True)
class StepSignature:
    step_key: str
    scene_index: int | None = None
    substep: str | None = None
    language: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "step_key": self.step_key,
            "scene_index": self.scene_index,
            "substep": self.substep,
            "language": self.language,
        }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_language(session: dict[str, Any]) -> str | None:
    production = session.get("production")
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    idx = _safe_int(session.get("image_scene_index"))
    if not isinstance(scenes, list) or idx is None or not (0 <= idx < len(scenes)):
        return None
    scene = scenes[idx]
    if not isinstance(scene, dict):
        return None
    prompt_map = scene.get("video_prompt_by_lang")
    if not isinstance(prompt_map, dict) or not prompt_map:
        return str(scene.get("video_prompt_current_language") or "ru").strip().lower() or "ru"
    langs = [str(key).strip().lower() for key in prompt_map if str(key).strip()]
    if not langs:
        return "ru"
    lang_idx = _safe_int(session.get("scene_media_language_index")) or 0
    if lang_idx < 0 or lang_idx >= len(langs):
        lang_idx = 0
    return langs[lang_idx]


def session_step_signature(session: dict[str, Any]) -> StepSignature:
    """Return the user-visible pipeline position for a session snapshot."""
    state = str(session.get("state") or "").strip()
    if state == STATE_WAITING_CONCEPT:
        return StepSignature("concept")
    if state == STATE_WAITING_RECIPE_APPROVAL:
        return StepSignature("recipe")
    if state == STATE_WAITING_SCENE_APPROVAL:
        return StepSignature("scenario")
    if state == STATE_WAITING_FRAME_PROMPTS_APPROVAL:
        return StepSignature("frame_prompts")
    if state == STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL:
        return StepSignature("production_plan")
    if state == STATE_WAITING_SCENE_MEDIA:
        return StepSignature(
            "scene_media",
            scene_index=_safe_int(session.get("image_scene_index")),
            substep=str(session.get("scene_media_substep") or "start_prompt"),
            language=_current_language(session),
        )
    if state == STATE_READY_FOR_PRODUCTION:
        return StepSignature("ready_for_production")
    return StepSignature(state or "first_step")


def _generation_run_uuid(session: dict[str, Any]) -> uuid.UUID | None:
    raw = str(session.get("generation_run_id") or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _signature_matches_row(signature: StepSignature, row: PipelineStepCheckpoint) -> bool:
    return (
        str(row.step_key or "") == signature.step_key
        and row.scene_index == signature.scene_index
        and (row.substep or None) == signature.substep
        and (row.language or None) == signature.language
    )


def _clean_transient_pending(snapshot: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(snapshot)
    for key in TRANSIENT_SESSION_KEYS:
        cleaned.pop(key, None)
    production = cleaned.get("production")
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if isinstance(scenes, list):
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            for key in TRANSIENT_SCENE_KEYS:
                scene.pop(key, None)
    return cleaned


def _empty_like(value: Any) -> Any:
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return None


def _sanitize_checkpoint_value(value: Any, *, key: str | None, stats: dict[str, int]) -> Any:
    key_l = (key or "").lower()
    if key_l and any(fragment in key_l for fragment in CHECKPOINT_MEDIA_KEY_FRAGMENTS):
        stats["omitted_values"] += 1
        try:
            stats["omitted_chars"] += len(str(value))
        except Exception:
            pass
        return _empty_like(value)
    if isinstance(value, str):
        if len(value) > CHECKPOINT_MAX_LARGE_STRING_CHARS:
            stats["omitted_values"] += 1
            stats["omitted_chars"] += len(value)
            return None
        return value
    if isinstance(value, list):
        return [_sanitize_checkpoint_value(item, key=None, stats=stats) for item in value]
    if isinstance(value, dict):
        return {
            item_key: _sanitize_checkpoint_value(item_value, key=str(item_key), stats=stats)
            for item_key, item_value in value.items()
        }
    return value


def _sanitize_checkpoint_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    stats = {"omitted_values": 0, "omitted_chars": 0}
    sanitized = _sanitize_checkpoint_value(snapshot, key=None, stats=stats)
    if not isinstance(sanitized, dict):
        return {}, stats
    return sanitized, stats


def _prune_checkpoint_history(db, chat_id: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=CHECKPOINT_RETENTION_DAYS)
    old_rows = (
        db.execute(
            select(PipelineStepCheckpoint.id)
            .where(PipelineStepCheckpoint.chat_id == int(chat_id))
            .where(PipelineStepCheckpoint.created_at < cutoff)
        )
        .scalars()
        .all()
    )
    for row_id in old_rows:
        row = db.get(PipelineStepCheckpoint, row_id)
        if row is not None:
            db.delete(row)
    recent_ids = (
        db.execute(
            select(PipelineStepCheckpoint.id)
            .where(PipelineStepCheckpoint.chat_id == int(chat_id))
            .order_by(PipelineStepCheckpoint.created_at.desc(), PipelineStepCheckpoint.id.desc())
            .offset(CHECKPOINT_RETENTION_LIMIT_PER_CHAT)
        )
        .scalars()
        .all()
    )
    for row_id in recent_ids:
        row = db.get(PipelineStepCheckpoint, row_id)
        if row is not None:
            db.delete(row)


def record_checkpoint(
    chat_id: int | None,
    session: dict[str, Any] | None,
    *,
    step_key: str | None = None,
    scene_index: int | None = None,
    substep: str | None = None,
    language: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Persist a restorable snapshot for the current user-visible step."""
    if chat_id is None or not isinstance(session, dict):
        return
    signature = session_step_signature(session)
    if step_key is not None:
        signature = StepSignature(
            step_key=str(step_key),
            scene_index=scene_index if scene_index is not None else signature.scene_index,
            substep=substep if substep is not None else signature.substep,
            language=language if language is not None else signature.language,
        )
    try:
        snapshot, sanitize_stats = _sanitize_checkpoint_snapshot(_serialize_value(session))
        row_payload = dict(payload or {})
        row_payload.setdefault("signature", signature.as_payload())
        if sanitize_stats["omitted_values"]:
            row_payload["checkpoint_omitted_media"] = sanitize_stats
        with SessionLocal() as db:
            latest = (
                db.execute(
                    select(PipelineStepCheckpoint)
                    .where(PipelineStepCheckpoint.chat_id == int(chat_id))
                    .where(PipelineStepCheckpoint.restored_at.is_(None))
                    .order_by(PipelineStepCheckpoint.created_at.desc(), PipelineStepCheckpoint.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if latest is not None and _signature_matches_row(signature, latest):
                latest.session_snapshot_json = snapshot
                latest.payload_json = row_payload
                latest.created_at = datetime.now(UTC)
                _prune_checkpoint_history(db, int(chat_id))
                db.commit()
                return
            db.add(
                PipelineStepCheckpoint(
                    chat_id=int(chat_id),
                    generation_run_id=_generation_run_uuid(session),
                    step_key=signature.step_key,
                    scene_index=signature.scene_index,
                    substep=signature.substep,
                    language=signature.language,
                    session_snapshot_json=snapshot,
                    payload_json=row_payload,
                    created_at=datetime.now(UTC),
                )
            )
            _prune_checkpoint_history(db, int(chat_id))
            db.commit()
    except Exception:
        logger.exception("checkpoint.record_failed chat_id=%s", chat_id)


def restore_previous_checkpoint(chat_id: int | None, session: dict[str, Any] | None) -> dict[str, Any] | None:
    """Restore the closest checkpoint before the current visible step."""
    if chat_id is None or not isinstance(session, dict):
        return None
    current = session_step_signature(session)
    try:
        with SessionLocal() as db:
            rows = (
                db.execute(
                    select(PipelineStepCheckpoint)
                    .where(PipelineStepCheckpoint.chat_id == int(chat_id))
                    .where(PipelineStepCheckpoint.restored_at.is_(None))
                    .order_by(PipelineStepCheckpoint.created_at.desc(), PipelineStepCheckpoint.id.desc())
                    .limit(20)
                )
                .scalars()
                .all()
            )
            target: PipelineStepCheckpoint | None = None
            target_index: int | None = None
            for index, row in enumerate(rows):
                if _signature_matches_row(current, row):
                    continue
                target = row
                target_index = index
                break
            if target is None or target_index is None:
                return None
            restored = _deserialize_value(dict(target.session_snapshot_json or {}))
            if not isinstance(restored, dict):
                return None
            restored = _clean_transient_pending(restored)
            session.clear()
            session.update(restored)
            restored_meta = {
                "step_key": target.step_key,
                "scene_index": target.scene_index,
                "substep": target.substep,
                "language": target.language,
            }
            restored_at = datetime.now(UTC)
            for newer_row in rows[:target_index]:
                newer_row.restored_at = restored_at
            db.commit()
        save_session_if_possible(int(chat_id), session)
        return restored_meta
    except Exception:
        logger.exception("checkpoint.restore_failed chat_id=%s", chat_id)
        return None


def restore_scene_prompt_fields_from_history(
    chat_id: int | None,
    session: dict[str, Any] | None,
    scene_index: int,
    *,
    limit: int = 40,
) -> bool:
    """Restore missing non-media prompt fields for a scene from recent checkpoints."""
    if chat_id is None or not isinstance(session, dict):
        return False
    production = session.get("production")
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not (0 <= scene_index < len(scenes)):
        return False
    scene = scenes[scene_index]
    if not isinstance(scene, dict):
        return False

    missing_text_keys = [
        key
        for key in (*SCENE_PROMPT_TEXT_KEYS, "video_prompt")
        if not str(scene.get(key) or "").strip()
    ]
    missing_json_keys = [
        key
        for key in (*SCENE_PROMPT_JSON_KEYS, "speech_cues_by_lang", "speech_plan_by_lang")
        if not isinstance(scene.get(key), dict) or not scene.get(key)
    ]
    if not missing_text_keys and not missing_json_keys:
        return False

    changed = False
    try:
        run_id = _generation_run_uuid(session)
        with SessionLocal() as db:
            stmt = (
                select(PipelineStepCheckpoint)
                .where(PipelineStepCheckpoint.chat_id == int(chat_id))
                .where(PipelineStepCheckpoint.restored_at.is_(None))
            )
            if run_id is not None:
                stmt = stmt.where(PipelineStepCheckpoint.generation_run_id == run_id)
            rows = (
                db.execute(stmt.order_by(PipelineStepCheckpoint.created_at.desc(), PipelineStepCheckpoint.id.desc()).limit(max(1, int(limit))))
                .scalars()
                .all()
            )
        for row in rows:
            snapshot = _deserialize_value(dict(row.session_snapshot_json or {}))
            production_snapshot = snapshot.get("production") if isinstance(snapshot, dict) else None
            snapshot_scenes = production_snapshot.get("json_plan") if isinstance(production_snapshot, dict) else None
            if not isinstance(snapshot_scenes, list) or not (0 <= scene_index < len(snapshot_scenes)):
                continue
            snapshot_scene = snapshot_scenes[scene_index]
            if not isinstance(snapshot_scene, dict):
                continue
            for key in list(missing_text_keys):
                value = snapshot_scene.get(key)
                if isinstance(value, str) and value.strip():
                    scene[key] = value
                    missing_text_keys.remove(key)
                    changed = True
            for key in list(missing_json_keys):
                value = snapshot_scene.get(key)
                if isinstance(value, dict) and value:
                    scene[key] = copy.deepcopy(value)
                    missing_json_keys.remove(key)
                    changed = True
            if not missing_text_keys and not missing_json_keys:
                break
        if changed:
            save_session_if_possible(int(chat_id), session)
        return changed
    except Exception:
        logger.exception("checkpoint.restore_scene_prompt_fields_failed chat_id=%s scene_index=%s", chat_id, scene_index)
        return False
