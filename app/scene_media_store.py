"""Persistence helpers for scene media candidates and reaction/message links."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from .artifact_store import persist_generation_artifact
from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .generation_store import merge_scene_prompt_payload
from .models import Scene, SceneImageCandidate, SceneMediaMessageLink, SceneVideoCandidate
from .scene_media_candidates import SCENE_MEDIA_MESSAGE_INDEX_KEY


def _scene_id_for_session(session: dict[str, Any], scene_idx: int) -> uuid.UUID | None:
    _run_id, scene_map = ensure_generation_run_for_session(session)
    raw = scene_map.get(int(scene_idx))
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_language(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    return raw or None


def persist_image_candidate(session: dict[str, Any], *, scene_idx: int, candidate: dict[str, Any]) -> None:
    scene_id = _scene_id_for_session(session, scene_idx)
    if scene_id is None:
        return
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    image_b64 = str(candidate.get("image_b64") or "").strip()
    if not candidate_id or not image_b64:
        return
    image_blob = __import__("base64").b64decode(image_b64)
    with SessionLocal() as db:
        row = db.execute(
            select(SceneImageCandidate).where(SceneImageCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            row = SceneImageCandidate(
                candidate_id=candidate_id,
                scene_id=scene_id,
                kind=str(candidate.get("kind") or "start"),
                image_blob=image_blob,
            )
            db.add(row)
        row.kind = str(candidate.get("kind") or "start")
        row.image_blob = image_blob
        row.liked = bool(candidate.get("liked"))
        row.approved_for_video = bool(candidate.get("approved_for_video"))
        db.commit()
    persist_generation_artifact(
        session,
        artifact_type=f"scene_{str(candidate.get('kind') or 'start')}_image",
        label=f"Scene {scene_idx + 1} {str(candidate.get('kind') or 'start')} frame",
        uri=f"db://scene_image_candidates/{candidate_id}",
        scene_idx=scene_idx,
        mime_type="image/png",
        payload={
            "candidate_id": candidate_id,
            "kind": str(candidate.get("kind") or "start"),
            "liked": bool(candidate.get("liked")),
            "approved_for_video": bool(candidate.get("approved_for_video")),
        },
    )


def persist_video_candidate(session: dict[str, Any], *, scene_idx: int, candidate: dict[str, Any]) -> None:
    scene_id = _scene_id_for_session(session, scene_idx)
    if scene_id is None:
        return
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    video_b64 = str(candidate.get("video_b64") or "").strip()
    if not candidate_id or not video_b64:
        return
    video_blob = __import__("base64").b64decode(video_b64)
    with SessionLocal() as db:
        row = db.execute(
            select(SceneVideoCandidate).where(SceneVideoCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            row = SceneVideoCandidate(
                candidate_id=candidate_id,
                scene_id=scene_id,
                language=_normalize_language(candidate.get("language")) or "ru",
                video_blob=video_blob,
            )
            db.add(row)
        row.language = _normalize_language(candidate.get("language")) or "ru"
        row.video_blob = video_blob
        row.liked = bool(candidate.get("liked"))
        row.approved_for_production = bool(candidate.get("approved_for_production"))
        row.speed_coef = float(candidate.get("speed_coef") or 1.2)
        row.source_image_candidate_id = str(candidate.get("source_image_candidate_id") or "").strip() or None
        db.commit()
    language = _normalize_language(candidate.get("language")) or "ru"
    persist_generation_artifact(
        session,
        artifact_type="scene_video_candidate",
        label=f"Scene {scene_idx + 1} video candidate ({language})",
        uri=f"db://scene_video_candidates/{candidate_id}",
        scene_idx=scene_idx,
        language=language,
        mime_type="video/mp4",
        payload={
            "candidate_id": candidate_id,
            "language": language,
            "liked": bool(candidate.get("liked")),
            "approved_for_production": bool(candidate.get("approved_for_production")),
            "speed_coef": float(candidate.get("speed_coef") or 1.2),
            "source_image_candidate_id": str(candidate.get("source_image_candidate_id") or "").strip() or None,
        },
    )


def persist_message_link(
    session: dict[str, Any],
    *,
    chat_id: int,
    message_id: int | None,
    scene_idx: int,
    media_type: str,
    candidate_id: str,
    language: str | None = None,
    kind: str | None = None,
) -> None:
    if message_id is None:
        return
    scene_id = _scene_id_for_session(session, scene_idx)
    if scene_id is None:
        return
    with SessionLocal() as db:
        row = db.execute(
            select(SceneMediaMessageLink).where(
                SceneMediaMessageLink.chat_id == int(chat_id),
                SceneMediaMessageLink.telegram_message_id == int(message_id),
            )
        ).scalar_one_or_none()
        if row is None:
            row = SceneMediaMessageLink(
                chat_id=int(chat_id),
                telegram_message_id=int(message_id),
                scene_id=scene_id,
                media_type=str(media_type),
                candidate_id=str(candidate_id),
            )
            db.add(row)
        row.scene_id = scene_id
        row.media_type = str(media_type)
        row.candidate_id = str(candidate_id)
        row.language = _normalize_language(language)
        row.kind = str(kind).strip().lower() if kind else None
        db.commit()


def persist_image_candidate_state(candidate: dict[str, Any]) -> None:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if not candidate_id:
        return
    with SessionLocal() as db:
        row = db.execute(
            select(SceneImageCandidate).where(SceneImageCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            return
        row.liked = bool(candidate.get("liked"))
        row.approved_for_video = bool(candidate.get("approved_for_video"))
        db.commit()


def persist_video_candidate_state(candidate: dict[str, Any]) -> None:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if not candidate_id:
        return
    with SessionLocal() as db:
        row = db.execute(
            select(SceneVideoCandidate).where(SceneVideoCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            return
        row.liked = bool(candidate.get("liked"))
        row.approved_for_production = bool(candidate.get("approved_for_production"))
        row.speed_coef = float(candidate.get("speed_coef") or 1.2)
        row.source_image_candidate_id = str(candidate.get("source_image_candidate_id") or "").strip() or None
        db.commit()


def mark_candidate_liked_by_message(chat_id: int, message_id: int | None, liked: bool) -> dict[str, Any] | None:
    if message_id is None:
        return None
    with SessionLocal() as db:
        link = db.execute(
            select(SceneMediaMessageLink).where(
                SceneMediaMessageLink.chat_id == int(chat_id),
                SceneMediaMessageLink.telegram_message_id == int(message_id),
            )
        ).scalar_one_or_none()
        if link is None:
            return None
        payload = {
            "scene_id": str(link.scene_id),
            "candidate_id": link.candidate_id,
            "media_type": link.media_type,
            "language": link.language,
            "kind": link.kind,
        }
        if link.media_type == "video":
            row = db.execute(
                select(SceneVideoCandidate).where(SceneVideoCandidate.candidate_id == link.candidate_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.liked = bool(liked)
        else:
            row = db.execute(
                select(SceneImageCandidate).where(SceneImageCandidate.candidate_id == link.candidate_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.liked = bool(liked)
        db.commit()
        return payload


def rebuild_production_session(session: dict[str, Any], generation_run_id: str | None) -> dict[str, Any] | None:
    production = session.get("production")
    if not isinstance(production, dict):
        return None
    scenes = production.get("json_plan")
    if not isinstance(scenes, list):
        return None
    if not generation_run_id:
        return production
    scene_map_raw = session.get("scene_db_ids_by_index")
    scene_map = {int(k): str(v) for k, v in scene_map_raw.items()} if isinstance(scene_map_raw, dict) else {}
    if not scene_map:
        return production

    scene_candidates_by_id: dict[str, list[SceneImageCandidate]] = defaultdict(list)
    video_candidates_by_id: dict[str, list[SceneVideoCandidate]] = defaultdict(list)
    message_links_by_scene_id: dict[str, list[SceneMediaMessageLink]] = defaultdict(list)
    prompt_payloads_by_id: dict[str, dict[str, Any]] = {}
    with SessionLocal() as db:
        scene_ids = []
        for raw in scene_map.values():
            try:
                scene_ids.append(uuid.UUID(raw))
            except (TypeError, ValueError, AttributeError):
                continue
        if not scene_ids:
            return production
        scene_rows = db.execute(select(Scene).where(Scene.id.in_(scene_ids))).scalars().all()
        image_rows = db.execute(
            select(SceneImageCandidate).where(SceneImageCandidate.scene_id.in_(scene_ids))
        ).scalars().all()
        video_rows = db.execute(
            select(SceneVideoCandidate).where(SceneVideoCandidate.scene_id.in_(scene_ids))
        ).scalars().all()
        link_rows = db.execute(
            select(SceneMediaMessageLink).where(SceneMediaMessageLink.scene_id.in_(scene_ids))
        ).scalars().all()
        for row in scene_rows:
            prompt_payloads_by_id[str(row.id)] = {
                "start_frame_prompt": row.start_frame_prompt,
                "start_frame_prompt_effective": row.start_frame_prompt_effective,
                "end_frame_prompt": row.end_frame_prompt,
                "video_prompt_by_lang": row.video_prompt_by_lang or {},
                "speech_by_lang": row.speech_by_lang or {},
                "syntax_prompt_by_lang": row.syntax_prompt_by_lang or {},
                "prompt_meta_json": row.prompt_meta_json or {},
            }
        for row in image_rows:
            scene_candidates_by_id[str(row.scene_id)].append(row)
        for row in video_rows:
            video_candidates_by_id[str(row.scene_id)].append(row)
        for row in link_rows:
            message_links_by_scene_id[str(row.scene_id)].append(row)

    message_index: dict[str, dict[str, Any]] = {}
    for idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        scene_id = scene_map.get(idx)
        if not scene_id:
            continue
        merge_scene_prompt_payload(scene, prompt_payloads_by_id.get(scene_id, {}))
        image_candidates = sorted(
            scene_candidates_by_id.get(scene_id, []),
            key=lambda row: (row.created_at is None, row.created_at, row.candidate_id),
        )
        video_candidates = sorted(
            video_candidates_by_id.get(scene_id, []),
            key=lambda row: (row.created_at is None, row.created_at, row.candidate_id),
        )
        scene["image_candidates"] = [
            {
                "candidate_id": row.candidate_id,
                "kind": row.kind,
                "image_b64": __import__("base64").b64encode(row.image_blob).decode("ascii"),
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "liked": bool(row.liked),
                "approved_for_video": bool(row.approved_for_video),
            }
            for row in image_candidates
        ]
        by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in video_candidates:
            by_lang[str(row.language)].append(
                {
                    "candidate_id": row.candidate_id,
                    "language": row.language,
                    "video_b64": __import__("base64").b64encode(row.video_blob).decode("ascii"),
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "liked": bool(row.liked),
                    "approved_for_production": bool(row.approved_for_production),
                    "speed_coef": float(row.speed_coef),
                    "source_image_candidate_id": row.source_image_candidate_id,
                }
            )
        scene["video_candidates_by_lang"] = dict(by_lang)
        for row in message_links_by_scene_id.get(scene_id, []):
            message_index[str(row.telegram_message_id)] = {
                "scene_idx": int(idx),
                "media_type": row.media_type,
                "candidate_id": row.candidate_id,
                "language": row.language,
                "kind": row.kind,
            }
    if message_index:
        session[SCENE_MEDIA_MESSAGE_INDEX_KEY] = message_index
    return production


def rebuild_scene_db_ids_for_session(session: dict[str, Any], generation_run_id: str | None) -> dict[int, str]:
    if not generation_run_id:
        return {}
    try:
        run_uuid = uuid.UUID(str(generation_run_id))
    except (TypeError, ValueError, AttributeError):
        return {}
    with SessionLocal() as db:
        rows = db.execute(
            select(Scene).where(Scene.run_id == run_uuid).order_by(Scene.scene_order.asc())
        ).scalars().all()
    return {idx: str(row.id) for idx, row in enumerate(rows)}
