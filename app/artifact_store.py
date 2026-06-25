"""Persistent artifact index for generation runs."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .models import ChatSession, GenerationArtifact, Scene, SceneImageCandidate, SceneVideoCandidate

logger = logging.getLogger("bot.artifacts")


def _scene_uuid_for_session(session: dict[str, Any], scene_idx: int | None) -> uuid.UUID | None:
    if scene_idx is None:
        return None
    _run_id, scene_map = ensure_generation_run_for_session(session)
    raw = scene_map.get(int(scene_idx))
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def persist_generation_artifact(
    session: dict[str, Any],
    *,
    artifact_type: str,
    label: str,
    uri: str,
    scene_idx: int | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Upsert one human-readable artifact link for the session run."""
    uri = str(uri or "").strip()
    if not uri:
        return None
    try:
        run_id_str, _scene_map = ensure_generation_run_for_session(session)
        if not run_id_str:
            return None
        run_id = uuid.UUID(run_id_str)
        scene_id = _scene_uuid_for_session(session, scene_idx)
        normalized_language = str(language or "").strip().lower() or None
        with SessionLocal() as db:
            row = (
                db.execute(
                    select(GenerationArtifact).where(
                        GenerationArtifact.run_id == run_id,
                        GenerationArtifact.artifact_type == str(artifact_type),
                        GenerationArtifact.uri == uri,
                        GenerationArtifact.language == normalized_language,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = GenerationArtifact(
                    run_id=run_id,
                    scene_id=scene_id,
                    artifact_type=str(artifact_type),
                    label=str(label).strip() or str(artifact_type),
                    uri=uri,
                    language=normalized_language,
                    mime_type=mime_type,
                    payload_json=payload or {},
                )
                db.add(row)
            else:
                row.scene_id = scene_id
                row.label = str(label).strip() or row.label
                row.mime_type = mime_type
                row.payload_json = payload or {}
            db.commit()
            return str(row.id)
    except Exception:
        logger.exception("artifact.persist_failed type=%s uri=%s", artifact_type, uri)
        return None


def file_uri(path: str | Path) -> str:
    raw = str(path).strip()
    if raw.startswith(("file://", "http://", "https://", "gs://", "db://")):
        return raw
    return f"file://{raw}"


def persist_post_production_artifacts(session: dict[str, Any], post: dict[str, Any]) -> None:
    renders = post.get("renders_by_language")
    if not isinstance(renders, dict):
        return
    for language, variants in renders.items():
        if not isinstance(variants, dict):
            continue
        for variant_key, payload in variants.items():
            if not isinstance(payload, dict):
                continue
            video_path = str(payload.get("video_path") or "").strip()
            if not video_path:
                continue
            label_by_variant = {
                "clean": f"Final clean master ({language})",
                "upscaled_4k": f"Final 4K upscaled master ({language})",
                "subtitled_music": f"Final subtitled/music master ({language})",
                "titled_subtitled_music": f"Final titled/subtitled/music master ({language})",
            }
            label = label_by_variant.get(str(variant_key), f"Final {variant_key} master ({language})")
            persist_generation_artifact(
                session,
                artifact_type=f"final_{variant_key}",
                label=label,
                uri=file_uri(video_path),
                language=str(language),
                mime_type="video/mp4",
                payload={
                    "video_path": video_path,
                    "width": payload.get("width"),
                    "height": payload.get("height"),
                    "artifact_bundle_key": post.get("artifact_bundle_key"),
                },
            )


def _upsert_artifact_row(
    db,
    *,
    run_id: uuid.UUID,
    scene_id: uuid.UUID | None,
    artifact_type: str,
    label: str,
    uri: str,
    language: str | None = None,
    mime_type: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    normalized_language = str(language or "").strip().lower() or None
    row = (
        db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == run_id,
                GenerationArtifact.artifact_type == artifact_type,
                GenerationArtifact.uri == uri,
                GenerationArtifact.language == normalized_language,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        db.add(
            GenerationArtifact(
                run_id=run_id,
                scene_id=scene_id,
                artifact_type=artifact_type,
                label=label,
                uri=uri,
                language=normalized_language,
                mime_type=mime_type,
                payload_json=payload or {},
            )
        )
        return
    row.scene_id = scene_id
    row.label = label
    row.mime_type = mime_type
    row.payload_json = payload or {}


def backfill_generation_artifacts() -> int:
    """Backfill artifact links for existing runs, candidates and final renders."""
    count = 0
    with SessionLocal() as db:
        image_rows = (
            db.execute(select(SceneImageCandidate, Scene).join(Scene, SceneImageCandidate.scene_id == Scene.id))
            .all()
        )
        for row, scene in image_rows:
            _upsert_artifact_row(
                db,
                run_id=scene.run_id,
                scene_id=scene.id,
                artifact_type=f"scene_{row.kind}_image",
                label=f"Scene {scene.scene_order} {row.kind} frame",
                uri=f"db://scene_image_candidates/{row.candidate_id}",
                mime_type="image/png",
                payload={
                    "candidate_id": row.candidate_id,
                    "kind": row.kind,
                    "liked": row.liked,
                    "approved_for_video": row.approved_for_video,
                },
            )
            count += 1

        video_rows = (
            db.execute(select(SceneVideoCandidate, Scene).join(Scene, SceneVideoCandidate.scene_id == Scene.id))
            .all()
        )
        for row, scene in video_rows:
            _upsert_artifact_row(
                db,
                run_id=scene.run_id,
                scene_id=scene.id,
                artifact_type="scene_video_candidate",
                label=f"Scene {scene.scene_order} video candidate ({row.language})",
                uri=f"db://scene_video_candidates/{row.candidate_id}",
                language=row.language,
                mime_type="video/mp4",
                payload={
                    "candidate_id": row.candidate_id,
                    "language": row.language,
                    "liked": row.liked,
                    "approved_for_production": row.approved_for_production,
                    "speed_coef": row.speed_coef,
                    "source_image_candidate_id": row.source_image_candidate_id,
                },
            )
            count += 1

        sessions = db.execute(select(ChatSession).where(ChatSession.generation_run_id.is_not(None))).scalars().all()
        for chat_session in sessions:
            session_json = dict(chat_session.session_json or {})
            production = session_json.get("production")
            post = production.get("post_production") if isinstance(production, dict) else None
            renders = post.get("renders_by_language") if isinstance(post, dict) else None
            if not isinstance(renders, dict):
                continue
            for language, variants in renders.items():
                if not isinstance(variants, dict):
                    continue
                for variant_key, payload in variants.items():
                    if not isinstance(payload, dict):
                        continue
                    video_path = str(payload.get("video_path") or "").strip()
                    if not video_path:
                        continue
                    label_by_variant = {
                        "clean": f"Final clean master ({language})",
                        "upscaled_4k": f"Final 4K upscaled master ({language})",
                        "subtitled_music": f"Final subtitled/music master ({language})",
                        "titled_subtitled_music": f"Final titled/subtitled/music master ({language})",
                    }
                    label = label_by_variant.get(str(variant_key), f"Final {variant_key} master ({language})")
                    _upsert_artifact_row(
                        db,
                        run_id=chat_session.generation_run_id,
                        scene_id=None,
                        artifact_type=f"final_{variant_key}",
                        label=label,
                        uri=file_uri(video_path),
                        language=str(language),
                        mime_type="video/mp4",
                        payload={
                            "video_path": video_path,
                            "width": payload.get("width"),
                            "height": payload.get("height"),
                            "artifact_bundle_key": post.get("artifact_bundle_key"),
                        },
                    )
                    count += 1
        db.commit()
    return count
