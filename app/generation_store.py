"""Persistence helpers for generation runs and video cost estimates."""

from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa

from .db import SessionLocal
from .models import GenerationRun, Scene, VideoResult


def _language_for_session(session: dict[str, Any]) -> str:
    langs = session.get("target_languages")
    if isinstance(langs, list) and langs:
        first = str(langs[0]).strip().lower()
        if first in {"ru", "en"}:
            return first
    return "ru"


def ensure_generation_run_for_session(session: dict[str, Any]) -> tuple[str | None, dict[int, str]]:
    """Create a lightweight run+scene snapshot in DB on first persistence."""
    existing = str(session.get("generation_run_id") or "").strip()
    raw_map = session.get("scene_db_ids_by_index")
    if existing and isinstance(raw_map, dict):
        return existing, {int(k): str(v) for k, v in raw_map.items()}

    scenes_plan = session.get("scenes_plan")
    if not isinstance(scenes_plan, list) or not scenes_plan:
        return None, {}
    script = str(session.get("recipe_full_text") or "").strip()
    if not script:
        try:
            script = json.dumps(scenes_plan, ensure_ascii=False)
        except Exception:
            script = "content_fabric_generation"

    with SessionLocal() as db:
        run = GenerationRun(
            status="media_in_progress",
            language=_language_for_session(session),
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
            scene_map[idx] = str(row.id)
        db.commit()
        session["generation_run_id"] = str(run.id)
        session["scene_db_ids_by_index"] = scene_map
        return str(run.id), scene_map


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
