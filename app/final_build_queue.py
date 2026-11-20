"""Queue final video builds for the admin final_build_worker."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .models import ChatSession, GenerationArtifact, GenerationRun
from .post_production import production_languages_for_scenes
from .session_store import _chat_session_cache_payload, _serialize_value, persist_dialogue_snapshot


FINAL_VIDEO_VARIANT_LABELS = {
    "clean": "clean",
    "upscaled_4k": "4K upscale",
    "subtitled_music": "subtitled/music",
    "titled_subtitled_music": "title/subtitled/music",
}
FINAL_BUILD_STATUS_ARTIFACT_TYPE = "final_build_status"
FINAL_BUILD_ACTIVE_STATUSES = {"pending", "running"}
FINAL_BUILD_DONE_STATUSES = {"success"}
FINAL_BUILD_STALE_AFTER = timedelta(minutes=30)
INLINE_MEDIA_KEY_SUFFIXES = ("_b64", "_bytes")
INLINE_MEDIA_KEYS = {
    "image_b64",
    "video_b64",
    "start_frame_image_b64",
    "end_frame_image_b64",
    "document",
    "photo",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _final_build_status_uri(run_id: str, language: str, variant: str) -> str:
    return f"admin://final-build/{run_id}/{language}/{variant}"


def _final_build_status_label(language: str, variant: str) -> str:
    return f"Final build status {FINAL_VIDEO_VARIANT_LABELS.get(variant, variant)} ({language})"


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _final_build_status_is_stale(payload: dict[str, Any]) -> bool:
    state = str(payload.get("status") or "").strip().lower()
    if state not in FINAL_BUILD_ACTIVE_STATUSES:
        return False
    activity_at = _parse_datetime(payload.get("heartbeat_at")) or _parse_datetime(payload.get("updated_at"))
    if activity_at is None:
        return True
    return datetime.now(timezone.utc) - activity_at > FINAL_BUILD_STALE_AFTER


def _queue_progress_payload() -> dict[str, Any]:
    return {"step": "queued", "label": "Queued for final_build_worker", "percent": 5}


def _strip_inline_media_payloads(value: Any) -> Any:
    if isinstance(value, bytes):
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in INLINE_MEDIA_KEYS or key_text.endswith(INLINE_MEDIA_KEY_SUFFIXES):
                continue
            out[key_text] = _strip_inline_media_payloads(item)
        return out
    if isinstance(value, list):
        return [_strip_inline_media_payloads(item) for item in value]
    return value


def _persist_queue_session_snapshot(chat_id: int, session: dict[str, Any], run_id: uuid.UUID) -> None:
    snapshot = _chat_session_cache_payload({**session, "generation_run_id": str(run_id)})
    payload = _serialize_value(snapshot)
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        if row is None:
            row = ChatSession(
                chat_id=int(chat_id),
                state=str(session.get("state") or "").strip() or None,
                session_json=payload,
                generation_run_id=run_id,
            )
            db.add(row)
        else:
            row.state = str(session.get("state") or "").strip() or row.state
            row.session_json = payload
            row.generation_run_id = run_id
        persist_dialogue_snapshot(db, chat_id, run_id, session)
        run = db.get(GenerationRun, run_id)
        if run is not None:
            run.status = "final_render_pending"
            run.is_active = True
        db.commit()


def _upsert_final_build_status(
    *,
    run_id: uuid.UUID,
    language: str,
    variant: str,
    advance_series_after_success: bool,
) -> str:
    now = datetime.now(timezone.utc)
    uri = _final_build_status_uri(str(run_id), language, variant)
    with SessionLocal() as db:
        row = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_id,
                    GenerationArtifact.artifact_type == FINAL_BUILD_STATUS_ARTIFACT_TYPE,
                    GenerationArtifact.uri == uri,
                    GenerationArtifact.language == language,
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .first()
        )
        payload = dict(row.payload_json) if row is not None and isinstance(row.payload_json, dict) else {}
        if str(payload.get("status") or "").strip().lower() in FINAL_BUILD_DONE_STATUSES:
            return str(payload.get("status") or "success")
        if (
            str(payload.get("status") or "").strip().lower() in FINAL_BUILD_ACTIVE_STATUSES
            and not _final_build_status_is_stale(payload)
        ):
            return str(payload.get("status") or "running")
        payload.update(
            {
                "status": "pending",
                "language": language,
                "variant": variant,
                "message": "",
                "updated_at": now.isoformat(),
                "enqueued_at": now.isoformat(),
                "progress": _queue_progress_payload(),
                "advance_series_after_success": advance_series_after_success,
            }
        )
        for key in ("started_at", "finished_at", "heartbeat_at", "worker_id", "artifact_id"):
            payload.pop(key, None)
        if row is None:
            row = GenerationArtifact(
                run_id=run_id,
                scene_id=None,
                artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
                label=_final_build_status_label(language, variant),
                uri=uri,
                language=language,
                mime_type="application/json",
                payload_json=payload,
            )
            db.add(row)
        else:
            row.label = _final_build_status_label(language, variant)
            row.mime_type = "application/json"
            row.payload_json = payload
        db.commit()
    return "pending"


def kick_final_build_worker(run_id: str) -> None:
    root = _project_root()
    script = root / "scripts" / "botctl.sh"
    subprocess.run(
        [str(script), "final-worker", str(run_id)],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def queue_final_builds_for_session(
    chat_id: int,
    session: dict[str, Any],
    scenes: list[dict[str, Any]],
    *,
    languages: list[str] | tuple[str, ...] | set[str] | None = None,
    variants: list[str] | tuple[str, ...] | set[str] | None = None,
    kick_worker: bool = True,
    advance_series_after_success: bool = False,
) -> dict[str, Any]:
    if not scenes:
        raise RuntimeError("No production scenes are available for final rendering.")
    normalized_languages = [
        str(language).strip().lower()
        for language in (languages if languages is not None else production_languages_for_scenes(scenes, require_ready=True))
        if str(language).strip()
    ]
    normalized_languages = list(dict.fromkeys(normalized_languages))
    if not normalized_languages:
        raise RuntimeError("No languages were requested for final rendering.")

    requested_variants = [
        variant
        for variant in FINAL_VIDEO_VARIANT_LABELS
        if variant in {str(item).strip() for item in variants}
    ] if variants is not None else list(FINAL_VIDEO_VARIANT_LABELS)
    if not requested_variants:
        raise RuntimeError("No final render variants were requested.")

    # The render request is the approved cut. An earlier scenario draft can be longer.
    session["scenes_plan"] = scenes
    production = session.get("production")
    if not isinstance(production, dict):
        production = session["production"] = {}
    production["json_plan"] = scenes
    run_id_str, _scene_map = ensure_generation_run_for_session(session)
    if not run_id_str:
        raise RuntimeError("No generation run is available for final build queue.")
    session["generation_run_id"] = run_id_str
    run_id = uuid.UUID(run_id_str)
    _persist_queue_session_snapshot(chat_id, session, run_id)

    statuses: dict[str, dict[str, str]] = {}
    for language in normalized_languages:
        statuses[language] = {}
        for variant in requested_variants:
            statuses[language][variant] = _upsert_final_build_status(
                run_id=run_id,
                language=language,
                variant=variant,
                advance_series_after_success=advance_series_after_success,
            )

    if kick_worker:
        kick_final_build_worker(run_id_str)

    return {
        "run_id": run_id_str,
        "languages": normalized_languages,
        "variants": requested_variants,
        "statuses": statuses,
    }
