"""Telegram delivery for rendered final video artifacts."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .constants import TOKEN_ENV_NAME
from .db import SessionLocal
from .models import ChatSession, GenerationArtifact

logger = logging.getLogger("bot.final_delivery")

FINAL_DELIVERY_KEY = "telegram_delivery"
FINAL_DELIVERY_MAX_ATTEMPTS = 3
FINAL_VIDEO_ARTIFACT_TYPES = {
    "final_clean",
    "final_upscaled_4k",
    "final_subtitled_music",
    "final_titled_subtitled_music",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_path_from_uri(uri: str) -> Path | None:
    raw = str(uri or "").strip()
    if raw.startswith("file://"):
        raw = raw[7:]
    if not raw or "://" in raw:
        return None
    return Path(raw)


def _delivery_payload(status: str, *, attempt_count: int, **extra: Any) -> dict[str, Any]:
    payload = {
        "status": status,
        "attempt_count": int(attempt_count),
        "updated_at": _now_iso(),
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


async def _send_video_file(token: str, chat_id: int, path: Path, caption: str) -> int:
    from telegram import Bot

    bot = Bot(token=token)
    with path.open("rb") as fh:
        message = await bot.send_document(
            chat_id=chat_id,
            document=fh,
            filename=path.name,
            caption=caption[:1024],
            read_timeout=180,
            write_timeout=180,
            connect_timeout=30,
            pool_timeout=30,
        )
    return int(getattr(message, "message_id", 0) or 0)


def deliver_final_artifact_to_telegram(
    run_id: str | uuid.UUID,
    artifact_id: str | uuid.UUID,
    *,
    max_attempts: int = FINAL_DELIVERY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Send one final artifact to the owning Telegram chat and persist delivery status."""
    token = os.getenv(TOKEN_ENV_NAME, "").strip()
    run_uuid = uuid.UUID(str(run_id))
    artifact_uuid = uuid.UUID(str(artifact_id))
    max_attempts = max(1, int(max_attempts or FINAL_DELIVERY_MAX_ATTEMPTS))

    with SessionLocal() as db:
        artifact = db.get(GenerationArtifact, artifact_uuid)
        chat_session = (
            db.execute(
                select(ChatSession)
                .where(ChatSession.generation_run_id == run_uuid)
                .order_by(ChatSession.updated_at.desc())
            )
            .scalars()
            .first()
        )
        if artifact is None:
            return _delivery_payload("failed", attempt_count=0, last_error="artifact not found")
        if artifact.run_id != run_uuid:
            return _delivery_payload("failed", attempt_count=0, last_error="artifact does not belong to run")
        payload = dict(artifact.payload_json or {})
        delivery = dict(payload.get(FINAL_DELIVERY_KEY) or {})
        if str(delivery.get("status") or "").strip().lower() == "sent" and delivery.get("telegram_message_id"):
            return delivery
        attempt_count = int(delivery.get("attempt_count") or 0)

        if not token:
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error=f"missing {TOKEN_ENV_NAME}")
            payload[FINAL_DELIVERY_KEY] = delivery
            artifact.payload_json = payload
            db.commit()
            return delivery
        if chat_session is None:
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error="chat session not found")
            payload[FINAL_DELIVERY_KEY] = delivery
            artifact.payload_json = payload
            db.commit()
            return delivery
        path = _local_path_from_uri(artifact.uri)
        if path is None or not path.exists():
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error="final video file not found")
            payload[FINAL_DELIVERY_KEY] = delivery
            artifact.payload_json = payload
            db.commit()
            return delivery
        chat_id = int(chat_session.chat_id)
        caption = f"{artifact.label}\nRun: {run_uuid}"

    last_error = ""
    for attempt in range(attempt_count + 1, max_attempts + 1):
        running = _delivery_payload("running", attempt_count=attempt)
        with SessionLocal() as db:
            artifact = db.get(GenerationArtifact, artifact_uuid)
            if artifact is not None:
                payload = dict(artifact.payload_json or {})
                payload[FINAL_DELIVERY_KEY] = running
                artifact.payload_json = payload
                db.commit()
        try:
            message_id = asyncio.run(_send_video_file(token, chat_id, path, caption))
            if message_id <= 0:
                raise RuntimeError("Telegram API did not return a message id")
            sent = _delivery_payload("sent", attempt_count=attempt, telegram_message_id=message_id, sent_at=_now_iso())
            with SessionLocal() as db:
                artifact = db.get(GenerationArtifact, artifact_uuid)
                if artifact is not None:
                    payload = dict(artifact.payload_json or {})
                    payload[FINAL_DELIVERY_KEY] = sent
                    artifact.payload_json = payload
                    db.commit()
            return sent
        except Exception as exc:
            last_error = str(exc)
            logger.warning("final artifact telegram delivery failed attempt=%s artifact=%s err=%s", attempt, artifact_id, exc)
            if attempt < max_attempts:
                import time

                time.sleep(min(30, 2**attempt))

    failed = _delivery_payload("failed", attempt_count=max_attempts, last_error=last_error)
    with SessionLocal() as db:
        artifact = db.get(GenerationArtifact, artifact_uuid)
        if artifact is not None:
            payload = dict(artifact.payload_json or {})
            payload[FINAL_DELIVERY_KEY] = failed
            artifact.payload_json = payload
            db.commit()
    return failed


def all_final_artifacts_delivered(run_id: str | uuid.UUID) -> bool:
    run_uuid = uuid.UUID(str(run_id))
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(GenerationArtifact).where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type.in_(FINAL_VIDEO_ARTIFACT_TYPES),
                    GenerationArtifact.mime_type == "video/mp4",
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .all()
        )
    latest_rows_by_track: dict[tuple[str, str], GenerationArtifact] = {}
    for row in rows:
        key = (str(row.language or "").strip().lower(), row.artifact_type)
        latest_rows_by_track.setdefault(key, row)
    latest_rows = list(latest_rows_by_track.values())
    if not latest_rows:
        return False
    for row in latest_rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        delivery = payload.get(FINAL_DELIVERY_KEY) if isinstance(payload, dict) else None
        if not isinstance(delivery, dict) or str(delivery.get("status") or "").strip().lower() != "sent":
            return False
    return True
