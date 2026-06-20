"""Persistent storage for active Telegram sessions."""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .generation_store import ensure_generation_run_for_session
from .models import ChatSession
from .scene_media_store import rebuild_production_session, rebuild_scene_db_ids_for_session

logger = logging.getLogger("bot.session")

_BYTES_MARKER = "__cf_bytes_b64__"


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


def save_session(chat_id: int, session: dict[str, Any]) -> None:
    if not isinstance(session, dict):
        return
    run_id_str = str(session.get("generation_run_id") or "").strip() or None
    try:
        if not run_id_str and (
            isinstance(session.get("production"), dict)
            or (isinstance(session.get("scenes_plan"), list) and session.get("scenes_plan"))
        ):
            run_id_str, scene_map = ensure_generation_run_for_session(session)
            if run_id_str and scene_map and not session.get("scene_db_ids_by_index"):
                session["scene_db_ids_by_index"] = scene_map
    except Exception:
        logger.exception("session_store.ensure_generation_run_failed chat_id=%s", chat_id)
    payload = _serialize_value(session)
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        if row is None:
            row = ChatSession(chat_id=int(chat_id), session_json={})
            db.add(row)
        row.state = str(session.get("state") or "").strip() or None
        row.session_json = payload
        row.generation_run_id = uuid.UUID(run_id_str) if run_id_str else None
        db.commit()


def load_session(chat_id: int) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
    if row is None:
        return None
    session = _deserialize_value(dict(row.session_json or {}))
    if not isinstance(session, dict):
        return None
    generation_run_id = str(row.generation_run_id or session.get("generation_run_id") or "").strip() or None
    if generation_run_id:
        session["generation_run_id"] = generation_run_id
        if not isinstance(session.get("scene_db_ids_by_index"), dict):
            session["scene_db_ids_by_index"] = rebuild_scene_db_ids_for_session(session, generation_run_id)
        rebuild_production_session(session, generation_run_id)
    return session


def delete_session(chat_id: int) -> None:
    with SessionLocal() as db:
        row = db.execute(select(ChatSession).where(ChatSession.chat_id == int(chat_id))).scalar_one_or_none()
        if row is None:
            return
        db.delete(row)
        db.commit()


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
