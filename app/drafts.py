"""Generation draft persistence and lightweight repair helpers."""

from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .models import GenerationArtifact, GenerationDraft, GenerationRun, SceneImageCandidate, SceneVideoCandidate
from .scene_media_store import rebuild_production_session, rebuild_scene_db_ids_for_session
from .session_store import _deserialize_value, _serialize_value, _strip_inline_media_payloads


DRAFT_STAGE_PROMPT = "prompt"
DRAFT_STAGE_IMAGE = "image"
DRAFT_STAGE_VIDEO = "video"
DRAFTS_PAGE_SIZE = 5
HIDDEN_FROM_TELEGRAM_DRAFTS_KEY = "hidden_from_telegram_drafts"
DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY = "draft_repair_ignore_restored_video_candidates_by_lang"
DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY = "draft_repair_generated_video_by_lang"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _draft_title(session: dict[str, Any]) -> str:
    for key in ("overlay_title_by_lang", "production"):
        value = session.get(key)
        if key == "production" and isinstance(value, dict):
            value = value.get("overlay_title_by_lang")
        if isinstance(value, dict):
            title = str(value.get("ru") or value.get("en") or "").strip()
            if title:
                return title[:180]
    for key in ("recipe_title", "selected_concept", "recipe_full_text", "user_input"):
        value = str(session.get(key) or "").strip()
        if value:
            return " ".join(value.split())[:180]
    return "Content Fabric draft"


def save_generation_draft(chat_id: int | None, session: dict[str, Any], *, title: str | None = None) -> GenerationDraft:
    run_id_str, _scene_map = ensure_generation_run_for_session(session)
    run_uuid = None
    if run_id_str:
        try:
            run_uuid = uuid.UUID(run_id_str)
        except ValueError:
            run_uuid = None
    session["is_draft"] = True
    snapshot = _serialize_value(_strip_inline_media_payloads(session))
    stage = str(session.get("state") or "").strip() or None
    scene_index = _safe_int(session.get("image_scene_index"))
    substep = str(session.get("scene_media_substep") or "").strip() or None
    language = None
    with SessionLocal() as db:
        row = GenerationDraft(
            chat_id=int(chat_id) if chat_id is not None else None,
            run_id=run_uuid,
            title=(title or _draft_title(session)),
            stage=stage,
            scene_index=scene_index,
            substep=substep,
            language=language,
            session_snapshot_json=snapshot,
            payload_json={"saved_from_state": stage},
        )
        db.add(row)
        if run_uuid is not None:
            run = db.get(GenerationRun, run_uuid)
            if run is not None:
                run.is_draft = True
        db.commit()
        db.refresh(row)
        return row


def _draft_visible_in_telegram(row: GenerationDraft, run: GenerationRun | None) -> bool:
    payload = row.payload_json if isinstance(row.payload_json, dict) else {}
    if bool(payload.get(HIDDEN_FROM_TELEGRAM_DRAFTS_KEY)):
        return False
    if row.run_id is not None and run is not None and not bool(run.is_draft):
        return False
    return True


def list_generation_drafts(
    chat_id: int | None = None,
    *,
    limit: int = 10,
    offset: int = 0,
    include_hidden: bool = False,
) -> list[GenerationDraft]:
    with SessionLocal() as db:
        query = select(GenerationDraft).order_by(GenerationDraft.created_at.desc()).limit(max(limit + offset + 50, limit))
        if chat_id is not None:
            query = query.where(GenerationDraft.chat_id.in_([int(chat_id), None]))
        rows = db.execute(query).scalars().all()
        visible: list[GenerationDraft] = []
        for row in rows:
            run = db.get(GenerationRun, row.run_id) if row.run_id is not None else None
            if include_hidden or _draft_visible_in_telegram(row, run):
                visible.append(row)
            _ = row.id, row.title, row.stage, row.scene_index, row.created_at
        return visible[offset : offset + limit]


def hide_generation_draft(draft_id: str) -> bool:
    try:
        draft_uuid = uuid.UUID(str(draft_id).strip())
    except ValueError:
        return False
    with SessionLocal() as db:
        row = db.get(GenerationDraft, draft_uuid)
        if row is None:
            return False
        if row.run_id is not None:
            run = db.get(GenerationRun, row.run_id)
            if run is not None:
                run.is_draft = False
        else:
            payload = dict(row.payload_json or {})
            payload[HIDDEN_FROM_TELEGRAM_DRAFTS_KEY] = True
            row.payload_json = payload
        db.commit()
        return True


def load_generation_draft(draft_id: str) -> tuple[GenerationDraft, dict[str, Any]] | None:
    try:
        draft_uuid = uuid.UUID(str(draft_id).strip())
    except ValueError:
        return None
    with SessionLocal() as db:
        row = db.get(GenerationDraft, draft_uuid)
        if row is None:
            return None
        snapshot = _deserialize_value(dict(row.session_snapshot_json or {}))
        if not isinstance(snapshot, dict):
            return None
        run_id = str(row.run_id or snapshot.get("generation_run_id") or "").strip()
        if run_id:
            snapshot["generation_run_id"] = run_id
            if not isinstance(snapshot.get("scene_db_ids_by_index"), dict):
                snapshot["scene_db_ids_by_index"] = rebuild_scene_db_ids_for_session(snapshot, run_id)
            rebuild_production_session(snapshot, run_id)
        _ = row.id, row.title, row.chat_id, row.run_id, row.created_at
        return row, snapshot


def format_drafts_list(drafts: list[GenerationDraft]) -> str:
    if not drafts:
        return "Черновиков пока нет."
    lines = ["Черновики:"]
    for index, draft in enumerate(drafts, start=1):
        created = draft.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        scene = "" if draft.scene_index is None else f", сцена {int(draft.scene_index) + 1}"
        lines.append(f"{index}. {draft.title} ({created}{scene})\n   id: {draft.id}")
    lines.append("\nЧтобы открыть: пришлите `Черновик <id>`.")
    return "\n".join(lines)


def format_drafts_page(drafts: list[GenerationDraft], *, page: int, has_next: bool) -> str:
    if not drafts:
        return "Черновиков пока нет."
    page_num = max(0, int(page)) + 1
    lines = [f"Черновики, страница {page_num}:"]
    for index, draft in enumerate(drafts, start=1):
        created = draft.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        scene = "" if draft.scene_index is None else f", сцена {int(draft.scene_index) + 1}"
        lines.append(f"{index}. {draft.title} ({created}{scene})")
    nav = []
    if page > 0:
        nav.append("Назад")
    if has_next:
        nav.append("Дальше")
    if nav:
        lines.append("Навигация: " + ", ".join(nav))
    return "\n".join(lines)


def parse_draft_open_command(text: str) -> str | None:
    raw = str(text or "").strip()
    low = raw.lower()
    for prefix in ("черновик ", "draft "):
        if low.startswith(prefix):
            return raw[len(prefix) :].strip()
    return None


def clear_scene_for_repair(scene: dict[str, Any], *, stage: str, languages: list[str]) -> None:
    normalized_stage = str(stage or "").strip().lower()
    normalized_languages = [str(lang).strip().lower() for lang in languages if str(lang).strip()]
    if normalized_stage == DRAFT_STAGE_PROMPT:
        for key in (
            "start_frame_prompt",
            "start_frame_prompt_effective",
            "end_frame_prompt",
            "syntax_prompt_by_lang",
            "ai_approval_history",
        ):
            scene.pop(key, None)
    if normalized_stage in {DRAFT_STAGE_PROMPT, DRAFT_STAGE_IMAGE}:
        for item in scene.get("image_candidates") or []:
            if isinstance(item, dict):
                item["approved_for_video"] = False
        for item in scene.get("end_image_candidates") or []:
            if isinstance(item, dict):
                item["approved_for_video"] = False
        for key in (
            "start_frame_image_b64",
            "end_frame_image_b64",
            "image_candidates",
            "end_image_candidates",
            "ai_video_approved_by_lang",
        ):
            scene.pop(key, None)
    if normalized_stage in {DRAFT_STAGE_PROMPT, DRAFT_STAGE_IMAGE, DRAFT_STAGE_VIDEO}:
        ignored_restored_candidates = scene.get(DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY)
        if not isinstance(ignored_restored_candidates, dict):
            ignored_restored_candidates = {}
        for lang in normalized_languages:
            ignored_restored_candidates[lang] = True
        scene[DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY] = ignored_restored_candidates
        generated = scene.get(DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY)
        if isinstance(generated, dict):
            for lang in normalized_languages:
                generated.pop(lang, None)
            if generated:
                scene[DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY] = generated
            else:
                scene.pop(DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY, None)
        results = scene.get("video_results_by_lang")
        if isinstance(results, dict):
            for lang in normalized_languages:
                results.pop(lang, None)
            scene["video_results_by_lang"] = results
        candidates = scene.get("video_candidates_by_lang")
        if isinstance(candidates, dict):
            for lang in normalized_languages:
                bucket = candidates.get(lang)
                if isinstance(bucket, list):
                    for item in bucket:
                        if isinstance(item, dict):
                            item["approved_for_production"] = False
                candidates.pop(lang, None)
            scene["video_candidates_by_lang"] = candidates
        approved = scene.get("video_approvals_by_lang")
        if isinstance(approved, dict):
            for lang in normalized_languages:
                approved[lang] = False
            scene["video_approvals_by_lang"] = approved


def clear_scene_db_approvals_for_repair(
    scene_id: str | None,
    *,
    stage: str,
    languages: list[str],
) -> None:
    raw_scene_id = str(scene_id or "").strip()
    if not raw_scene_id:
        return
    try:
        scene_uuid = uuid.UUID(raw_scene_id)
    except ValueError:
        return
    normalized_stage = str(stage or "").strip().lower()
    normalized_languages = [str(lang).strip().lower() for lang in languages if str(lang).strip()]
    if normalized_stage not in {DRAFT_STAGE_PROMPT, DRAFT_STAGE_IMAGE, DRAFT_STAGE_VIDEO}:
        return
    with SessionLocal() as db:
        image_candidate_ids: list[str] = []
        video_candidate_ids: list[str] = []
        if normalized_stage in {DRAFT_STAGE_PROMPT, DRAFT_STAGE_IMAGE}:
            image_rows = db.execute(
                select(SceneImageCandidate).where(SceneImageCandidate.scene_id == scene_uuid)
            ).scalars().all()
            for row in image_rows:
                row.approved_for_video = False
                image_candidate_ids.append(str(row.candidate_id))
        if normalized_languages:
            video_rows = db.execute(
                select(SceneVideoCandidate).where(
                    SceneVideoCandidate.scene_id == scene_uuid,
                    SceneVideoCandidate.language.in_(normalized_languages),
                )
            ).scalars().all()
            for row in video_rows:
                row.approved_for_production = False
                video_candidate_ids.append(str(row.candidate_id))
        artifact_rows = []
        if image_candidate_ids:
            artifact_rows.extend(
                db.execute(
                    select(GenerationArtifact).where(
                        GenerationArtifact.uri.in_(
                            [f"db://scene_image_candidates/{candidate_id}" for candidate_id in image_candidate_ids]
                        )
                    )
                ).scalars().all()
            )
        if video_candidate_ids:
            artifact_rows.extend(
                db.execute(
                    select(GenerationArtifact).where(
                        GenerationArtifact.uri.in_(
                            [f"db://scene_video_candidates/{candidate_id}" for candidate_id in video_candidate_ids]
                        )
                    )
                ).scalars().all()
            )
        image_ids = set(image_candidate_ids)
        video_ids = set(video_candidate_ids)
        for artifact in artifact_rows:
            payload = dict(artifact.payload_json or {})
            candidate_id = str(payload.get("candidate_id") or "").strip()
            if candidate_id in image_ids:
                payload["approved_for_video"] = False
            if candidate_id in video_ids:
                payload["approved_for_production"] = False
            artifact.payload_json = payload
        db.commit()
