"""Persistence helpers for scene media candidates and reaction/message links."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import load_only

from .artifact_store import persist_generation_artifact
from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .generation_store import merge_scene_prompt_payload
from .media_file_store import read_media_uri_bytes, store_scene_candidate_media
from .models import GenerationArtifact, GenerationRun, Scene, SceneImageCandidate, SceneMediaMessageLink, SceneVideoCandidate
from .scene_media_candidates import SCENE_MEDIA_MESSAGE_INDEX_KEY


def _run_and_scene_id_for_session(session: dict[str, Any], scene_idx: int) -> tuple[uuid.UUID, uuid.UUID] | None:
    run_id, scene_map = ensure_generation_run_for_session(session)
    raw = scene_map.get(int(scene_idx))
    if not run_id or not raw:
        return None
    try:
        return uuid.UUID(str(run_id)), uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _scene_id_for_session(session: dict[str, Any], scene_idx: int) -> uuid.UUID | None:
    pair = _run_and_scene_id_for_session(session, scene_idx)
    return pair[1] if pair is not None else None


def _candidate_text_snapshot_for_scene(scene: dict[str, Any], language: str) -> dict[str, Any]:
    lang = str(language or "").strip().lower() or "ru"
    snapshot: dict[str, Any] = {}
    for key in ("speech_by_lang", "speech_cues_by_lang", "speech_plan_by_lang", "video_prompt_by_lang"):
        value = scene.get(key)
        if isinstance(value, dict) and value.get(lang):
            if key == "speech_cues_by_lang":
                cues = _valid_speech_cues(value.get(lang))
                if cues:
                    snapshot[key] = {lang: cues}
                continue
            snapshot[key] = {lang: value.get(lang)}
    return snapshot


def _valid_speech_cues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for cue in value:
        if not isinstance(cue, dict):
            continue
        text = " ".join(str(cue.get("text") or "").split())
        if not text:
            continue
        try:
            start_sec = float(cue.get("start_sec") or 0.0)
            end_sec = float(cue.get("end_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        if end_sec <= start_sec:
            continue
        clean = dict(cue)
        clean["text"] = text
        clean["start_sec"] = start_sec
        clean["end_sec"] = end_sec
        out.append(clean)
    return out


def _sanitize_speech_cues_by_lang(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for language, cues in value.items():
        lang = str(language or "").strip().lower()
        valid = _valid_speech_cues(cues)
        if lang and valid:
            out[lang] = valid
    return out


def _normalize_language(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    return raw or None


def _candidate_media_bytes(row: SceneImageCandidate | SceneVideoCandidate, blob_attr: str) -> bytes:
    from_uri = read_media_uri_bytes(getattr(row, "media_uri", None))
    if from_uri is not None:
        return from_uri
    blob = getattr(row, blob_attr, None)
    return bytes(blob or b"")


def persist_image_candidate(session: dict[str, Any], *, scene_idx: int, candidate: dict[str, Any]) -> None:
    pair = _run_and_scene_id_for_session(session, scene_idx)
    if pair is None:
        return
    run_id, scene_id = pair
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    image_b64 = str(candidate.get("image_b64") or "").strip()
    if not candidate_id or not image_b64:
        return
    image_blob = __import__("base64").b64decode(image_b64)
    media_uri = store_scene_candidate_media(
        run_id=run_id,
        scene_id=scene_id,
        candidate_id=candidate_id,
        data=image_blob,
        extension="png",
    )
    with SessionLocal() as db:
        row = db.execute(
            select(SceneImageCandidate).where(SceneImageCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            row = SceneImageCandidate(
                candidate_id=candidate_id,
                scene_id=scene_id,
                kind=str(candidate.get("kind") or "start"),
            )
            db.add(row)
        row.kind = str(candidate.get("kind") or "start")
        row.image_blob = None
        row.media_uri = media_uri
        row.mime_type = "image/png"
        row.byte_size = len(image_blob)
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
            "media_uri": media_uri,
            "byte_size": len(image_blob),
            "liked": bool(candidate.get("liked")),
            "approved_for_video": bool(candidate.get("approved_for_video")),
        },
    )


def persist_video_candidate(session: dict[str, Any], *, scene_idx: int, candidate: dict[str, Any]) -> None:
    pair = _run_and_scene_id_for_session(session, scene_idx)
    if pair is None:
        return
    run_id, scene_id = pair
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    video_b64 = str(candidate.get("video_b64") or "").strip()
    if not candidate_id or not video_b64:
        return
    video_blob = __import__("base64").b64decode(video_b64)
    media_uri = store_scene_candidate_media(
        run_id=run_id,
        scene_id=scene_id,
        candidate_id=candidate_id,
        data=video_blob,
        extension="mp4",
    )
    with SessionLocal() as db:
        row = db.execute(
            select(SceneVideoCandidate).where(SceneVideoCandidate.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if row is None:
            row = SceneVideoCandidate(
                candidate_id=candidate_id,
                scene_id=scene_id,
                language=_normalize_language(candidate.get("language")) or "ru",
            )
            db.add(row)
        row.language = _normalize_language(candidate.get("language")) or "ru"
        row.video_blob = None
        row.media_uri = media_uri
        row.mime_type = "video/mp4"
        row.byte_size = len(video_blob)
        row.liked = bool(candidate.get("liked"))
        row.approved_for_production = bool(candidate.get("approved_for_production"))
        row.speed_coef = float(candidate.get("speed_coef") or 1.2)
        row.source_image_candidate_id = str(candidate.get("source_image_candidate_id") or "").strip() or None
        db.commit()
    language = _normalize_language(candidate.get("language")) or "ru"
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production, dict) else []
    scene = scenes[scene_idx] if isinstance(scenes, list) and 0 <= scene_idx < len(scenes) and isinstance(scenes[scene_idx], dict) else {}
    text_snapshot = _candidate_text_snapshot_for_scene(scene, language)
    existing_cues = _sanitize_speech_cues_by_lang(candidate.get("speech_cues_by_lang"))
    if existing_cues:
        candidate["speech_cues_by_lang"] = existing_cues
    else:
        candidate.pop("speech_cues_by_lang", None)
    candidate.update(text_snapshot)
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
            "media_uri": media_uri,
            "byte_size": len(video_blob),
            "liked": bool(candidate.get("liked")),
            "approved_for_production": bool(candidate.get("approved_for_production")),
            "speed_coef": float(candidate.get("speed_coef") or 1.2),
            "source_image_candidate_id": str(candidate.get("source_image_candidate_id") or "").strip() or None,
            **text_snapshot,
            **(
                {"video_speech_qa_by_lang": candidate.get("video_speech_qa_by_lang")}
                if isinstance(candidate.get("video_speech_qa_by_lang"), dict)
                else {}
            ),
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
        artifact_rows = db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.artifact_type == "scene_video_candidate",
                GenerationArtifact.uri == f"db://scene_video_candidates/{candidate_id}",
            )
        ).scalars().all()
        for artifact in artifact_rows:
            payload = dict(artifact.payload_json or {})
            payload.update(
                {
                    "liked": bool(candidate.get("liked")),
                    "approved_for_production": bool(candidate.get("approved_for_production")),
                    "speed_coef": float(candidate.get("speed_coef") or 1.2),
                    "source_image_candidate_id": str(candidate.get("source_image_candidate_id") or "").strip() or None,
                }
            )
            for key in (
                "speech_by_lang",
                "speech_cues_by_lang",
                "speech_plan_by_lang",
                "video_prompt_by_lang",
                "video_speech_qa_by_lang",
            ):
                value = candidate.get(key)
                if isinstance(value, dict) and value:
                    if key == "speech_cues_by_lang":
                        value = _sanitize_speech_cues_by_lang(value)
                    if value:
                        payload[key] = value
            artifact.payload_json = payload
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


def rebuild_production_session(
    session: dict[str, Any],
    generation_run_id: str | None,
    *,
    include_media_blobs: bool = True,
) -> dict[str, Any] | None:
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
    try:
        run_uuid = uuid.UUID(str(generation_run_id))
    except (TypeError, ValueError, AttributeError):
        run_uuid = None

    scene_candidates_by_id: dict[str, list[SceneImageCandidate]] = defaultdict(list)
    video_candidates_by_id: dict[str, list[SceneVideoCandidate]] = defaultdict(list)
    message_links_by_scene_id: dict[str, list[SceneMediaMessageLink]] = defaultdict(list)
    prompt_payloads_by_id: dict[str, dict[str, Any]] = {}
    video_artifact_payloads_by_candidate_id: dict[str, dict[str, Any]] = {}
    with SessionLocal() as db:
        run = db.get(GenerationRun, run_uuid) if run_uuid is not None else None
        scene_ids = []
        for raw in scene_map.values():
            try:
                scene_ids.append(uuid.UUID(raw))
            except (TypeError, ValueError, AttributeError):
                continue
        if not scene_ids:
            return production
        scene_rows = db.execute(select(Scene).where(Scene.id.in_(scene_ids))).scalars().all()
        image_stmt = select(SceneImageCandidate).where(SceneImageCandidate.scene_id.in_(scene_ids))
        video_stmt = select(SceneVideoCandidate).where(SceneVideoCandidate.scene_id.in_(scene_ids))
        if not include_media_blobs:
            image_stmt = image_stmt.options(
                load_only(
                    SceneImageCandidate.candidate_id,
                    SceneImageCandidate.scene_id,
                    SceneImageCandidate.kind,
                    SceneImageCandidate.created_at,
                    SceneImageCandidate.liked,
                    SceneImageCandidate.approved_for_video,
                    SceneImageCandidate.media_uri,
                    SceneImageCandidate.mime_type,
                    SceneImageCandidate.byte_size,
                )
            )
            video_stmt = video_stmt.options(
                load_only(
                    SceneVideoCandidate.candidate_id,
                    SceneVideoCandidate.scene_id,
                    SceneVideoCandidate.language,
                    SceneVideoCandidate.created_at,
                    SceneVideoCandidate.liked,
                    SceneVideoCandidate.approved_for_production,
                    SceneVideoCandidate.speed_coef,
                    SceneVideoCandidate.source_image_candidate_id,
                    SceneVideoCandidate.media_uri,
                    SceneVideoCandidate.mime_type,
                    SceneVideoCandidate.byte_size,
                )
            )
        image_rows = db.execute(image_stmt).scalars().all()
        video_rows = db.execute(video_stmt).scalars().all()
        candidate_ids = [str(row.candidate_id) for row in video_rows if str(row.candidate_id or "").strip()]
        if candidate_ids:
            artifact_rows = db.execute(
                select(GenerationArtifact).where(
                    GenerationArtifact.artifact_type == "scene_video_candidate",
                    GenerationArtifact.scene_id.in_(scene_ids),
                )
            ).scalars().all()
            for artifact in artifact_rows:
                payload = artifact.payload_json if isinstance(artifact.payload_json, dict) else {}
                candidate_id = str(payload.get("candidate_id") or "").strip()
                if not candidate_id:
                    raw_uri = str(artifact.uri or "").strip()
                    if raw_uri.startswith("db://scene_video_candidates/"):
                        candidate_id = raw_uri.rsplit("/", 1)[-1]
                if candidate_id in candidate_ids:
                    video_artifact_payloads_by_candidate_id[candidate_id] = dict(payload)
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
    if run is not None and str(run.title_color or "").strip():
        style = production.get("overlay_title_style")
        if not isinstance(style, dict):
            style = {}
        style["title_color"] = str(run.title_color).strip()
        production["overlay_title_style"] = style
        session["overlay_title_style"] = dict(style)

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
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "liked": bool(row.liked),
                "approved_for_video": bool(row.approved_for_video),
                "media_uri": row.media_uri,
                "mime_type": row.mime_type,
                "byte_size": row.byte_size,
            }
            for row in image_candidates
        ]
        if include_media_blobs:
            for candidate, row in zip(scene["image_candidates"], image_candidates, strict=False):
                payload = _candidate_media_bytes(row, "image_blob")
                if payload:
                    candidate["image_b64"] = __import__("base64").b64encode(payload).decode("ascii")
        by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in video_candidates:
            payload = {
                "candidate_id": row.candidate_id,
                "language": row.language,
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "liked": bool(row.liked),
                "approved_for_production": bool(row.approved_for_production),
                "speed_coef": float(row.speed_coef),
                "source_image_candidate_id": row.source_image_candidate_id,
                "media_uri": row.media_uri,
                "mime_type": row.mime_type,
                "byte_size": row.byte_size,
            }
            artifact_payload = video_artifact_payloads_by_candidate_id.get(str(row.candidate_id), {})
            for key in (
                "speech_by_lang",
                "speech_cues_by_lang",
                "speech_plan_by_lang",
                "video_prompt_by_lang",
                "video_speech_qa_by_lang",
            ):
                value = artifact_payload.get(key)
                if isinstance(value, dict) and value:
                    if key == "speech_cues_by_lang":
                        value = _sanitize_speech_cues_by_lang(value)
                    if value:
                        payload[key] = value
            if include_media_blobs:
                video_payload = _candidate_media_bytes(row, "video_blob")
                if video_payload:
                    payload["video_b64"] = __import__("base64").b64encode(video_payload).decode("ascii")
            by_lang[str(row.language)].append(payload)
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
