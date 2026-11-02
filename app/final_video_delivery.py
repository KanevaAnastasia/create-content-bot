"""Telegram delivery for rendered final video artifacts."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .constants import TOKEN_ENV_NAME
from .db import SessionLocal
from .models import ChatSession, GenerationArtifact, GenerationDraft

logger = logging.getLogger("bot.final_delivery")

FINAL_DELIVERY_KEY = "telegram_delivery"
FINAL_DELIVERY_MAX_ATTEMPTS = 3
FINAL_VIDEO_ARTIFACT_TYPES = {
    "final_clean",
    "final_upscaled_4k",
    "final_subtitled_music",
    "final_titled_subtitled_music",
}
FINAL_BUILD_STATUS_ARTIFACT_TYPE = "final_build_status"
FINAL_PUBLICATION_READY_VARIANT = "titled_subtitled_music"
FINAL_SOCIAL_DESCRIPTIONS_TRIGGER_KEY = "final_social_descriptions_triggered_at"
GUEST_FINAL_REVIEW_TRIGGER_KEY = "final_review_prompted_run_id"
TELEGRAM_CLOUD_UPLOAD_LIMIT_BYTES = 49 * 1024 * 1024
TELEGRAM_DELIVERY_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "telegram_delivery"


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


def _telegram_bot_kwargs() -> dict[str, Any]:
    base_url = str(os.getenv("TELEGRAM_BOT_API_BASE_URL") or "").strip()
    base_file_url = str(os.getenv("TELEGRAM_BOT_API_BASE_FILE_URL") or "").strip()
    local_mode = str(os.getenv("TELEGRAM_BOT_API_LOCAL_MODE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "да",
    }
    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if base_file_url:
        kwargs["base_file_url"] = base_file_url
    if local_mode:
        kwargs["local_mode"] = True
    return kwargs


def _uses_cloud_bot_api() -> bool:
    base_url = str(os.getenv("TELEGRAM_BOT_API_BASE_URL") or "").strip()
    if not base_url:
        return True
    return "api.telegram.org" in base_url.lower()


def _resolve_ffmpeg() -> str | None:
    try:
        from .ai_approval_service import _resolve_ffmpeg as resolve

        return resolve()
    except Exception:
        logger.exception("final_delivery.ffmpeg_resolve_failed")
        return None


def _run_ffmpeg(cmd: list[str], *, timeout: int, stage: str) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "final_delivery.ffmpeg_timeout stage=%s timeout=%ss cmd=%s",
            stage,
            timeout,
            " ".join(shlex.quote(part) for part in cmd[:8]),
        )
        raise RuntimeError(f"final delivery timeout at ffmpeg:{stage} after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        raise RuntimeError(f"final delivery ffmpeg failed: {stderr[:1200]}") from exc


def _telegram_delivery_copy_path(run_uuid: uuid.UUID, artifact_uuid: uuid.UUID, source_path: Path) -> Path:
    return TELEGRAM_DELIVERY_ARTIFACT_DIR / str(run_uuid) / f"{source_path.stem}.{artifact_uuid}.telegram.mp4"


def _prepare_telegram_delivery_file(
    path: Path,
    *,
    run_uuid: uuid.UUID,
    artifact_uuid: uuid.UUID,
) -> tuple[Path, dict[str, Any]]:
    if not _uses_cloud_bot_api() or path.stat().st_size <= TELEGRAM_CLOUD_UPLOAD_LIMIT_BYTES:
        return path, {}
    dest = _telegram_delivery_copy_path(run_uuid, artifact_uuid, path)
    if dest.exists() and dest.stat().st_size <= TELEGRAM_CLOUD_UPLOAD_LIMIT_BYTES:
        return dest, {
            "original_uri": f"file://{path}",
            "sent_uri": f"file://{dest}",
            "compressed_for_telegram": True,
        }
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available for Telegram delivery copy")
    dest.parent.mkdir(parents=True, exist_ok=True)
    profiles = [
        (720, 30, "96k"),
        (720, 33, "96k"),
        (540, 34, "80k"),
        (480, 36, "64k"),
        (360, 38, "64k"),
    ]
    last_size = 0
    for width, crf, audio_bitrate in profiles:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            f"scale='min({width},iw)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-movflags",
            "+faststart",
            str(dest),
        ]
        _run_ffmpeg(cmd, timeout=3600, stage="telegram_delivery_copy")
        last_size = dest.stat().st_size if dest.exists() else 0
        if 0 < last_size <= TELEGRAM_CLOUD_UPLOAD_LIMIT_BYTES:
            return dest, {
                "original_uri": f"file://{path}",
                "sent_uri": f"file://{dest}",
                "compressed_for_telegram": True,
                "delivery_copy_profile": {"width": width, "crf": crf, "audio_bitrate": audio_bitrate},
                "delivery_copy_size": last_size,
            }
    raise RuntimeError(
        f"Telegram delivery copy is still too large: {last_size} bytes > {TELEGRAM_CLOUD_UPLOAD_LIMIT_BYTES}"
    )


async def _send_video_file(token: str, chat_id: int, path: Path, caption: str) -> int:
    from telegram import Bot

    bot = Bot(token=token, **_telegram_bot_kwargs())
    with path.open("rb") as fh:
        message = await bot.send_document(
            chat_id=chat_id,
            document=fh,
            filename=path.name,
            caption=caption[:1024],
            read_timeout=900,
            write_timeout=900,
            connect_timeout=30,
            pool_timeout=60,
        )
    return int(getattr(message, "message_id", 0) or 0)


def _admin_chat_id() -> int | None:
    raw = str(os.getenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _chat_session_for_run(db, run_uuid: uuid.UUID) -> ChatSession | None:
    return (
        db.execute(
            select(ChatSession)
            .where(ChatSession.generation_run_id == run_uuid)
            .order_by(ChatSession.updated_at.desc())
        )
        .scalars()
        .first()
    )


def _chat_id_for_run(db, run_uuid: uuid.UUID) -> int | None:
    chat_session = _chat_session_for_run(db, run_uuid)
    if chat_session is not None:
        return int(chat_session.chat_id)
    draft = (
        db.execute(
            select(GenerationDraft)
            .where(GenerationDraft.run_id == run_uuid, GenerationDraft.chat_id.is_not(None))
            .order_by(GenerationDraft.created_at.desc())
        )
        .scalars()
        .first()
    )
    if draft is not None and draft.chat_id is not None:
        return int(draft.chat_id)
    return _admin_chat_id()


def _draft_session_for_run(db, run_uuid: uuid.UUID) -> dict[str, Any] | None:
    draft = (
        db.execute(
            select(GenerationDraft)
            .where(GenerationDraft.run_id == run_uuid)
            .order_by(GenerationDraft.created_at.desc())
        )
        .scalars()
        .first()
    )
    if draft is None:
        return None
    snapshot = draft.session_snapshot_json if isinstance(draft.session_snapshot_json, dict) else {}
    return dict(snapshot)


def _guest_final_artifacts_delivered(run_uuid: uuid.UUID) -> bool:
    required = {("ru", "clean"), ("ru", "upscaled_4k")}
    with SessionLocal() as db:
        status_rows = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == FINAL_BUILD_STATUS_ARTIFACT_TYPE,
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .all()
        )
        final_rows = (
            db.execute(
                select(GenerationArtifact).where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type.in_({"final_clean", "final_upscaled_4k"}),
                )
            )
            .scalars()
            .all()
        )
    artifacts_by_id = {str(row.id): row for row in final_rows}
    latest_statuses: dict[tuple[str, str], dict[str, Any]] = {}
    for row in status_rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        key = (
            str(payload.get("language") or row.language or "").strip().lower(),
            str(payload.get("variant") or "").strip(),
        )
        if key in required:
            latest_statuses.setdefault(key, dict(payload))
    if set(latest_statuses) != required:
        return False
    for payload in latest_statuses.values():
        if str(payload.get("status") or "").strip().lower() != "success":
            return False
        artifact = artifacts_by_id.get(str(payload.get("artifact_id") or ""))
        artifact_payload = artifact.payload_json if artifact is not None and isinstance(artifact.payload_json, dict) else {}
        delivery = artifact_payload.get(FINAL_DELIVERY_KEY) if isinstance(artifact_payload, dict) else None
        if not isinstance(delivery, dict) or str(delivery.get("status") or "").strip().lower() != "sent":
            return False
    return True


def _trigger_guest_final_review_if_ready(run_id: str | uuid.UUID, chat_id: int) -> bool:
    run_uuid = uuid.UUID(str(run_id))
    with SessionLocal() as db:
        chat_session = _chat_session_for_run(db, run_uuid)
        raw_session = dict(chat_session.session_json or {}) if chat_session is not None else {}
    if not bool(raw_session.get("guest_mode")):
        return False
    if not _guest_final_artifacts_delivered(run_uuid):
        return True

    from .guest_handlers import STATE_CARTOON_FINAL_REVIEW, guest_final_review_markup
    from .session_store import load_session_if_possible, save_session_if_possible

    session = load_session_if_possible(chat_id)
    if not isinstance(session, dict) or str(session.get("generation_run_id") or "") != str(run_uuid):
        return True
    data = session.get("guest_data")
    if not isinstance(data, dict):
        data = {}
        session["guest_data"] = data
    already_prompted = str(data.get(GUEST_FINAL_REVIEW_TRIGGER_KEY) or "") == str(run_uuid)
    session["state"] = STATE_CARTOON_FINAL_REVIEW
    save_session_if_possible(chat_id, session)
    if already_prompted:
        return True

    token = os.getenv(TOKEN_ENV_NAME, "").strip()
    if not token:
        return True
    try:
        message = _FinalDeliveryMessage(token=token, chat_id=chat_id)
        asyncio.run(
            message.reply_text(
                "Итоговый мультфильм готов. Всё устраивает или нужно исправить сцены?",
                reply_markup=guest_final_review_markup(),
            )
        )
        data[GUEST_FINAL_REVIEW_TRIGGER_KEY] = str(run_uuid)
        save_session_if_possible(chat_id, session)
    except Exception:
        logger.exception("guest final review prompt failed run=%s chat=%s", run_uuid, chat_id)
    return True


def _telegram_chunks(text: str, *, limit: int = 3900) -> list[str]:
    body = str(text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    current = ""
    for part in body.split("\n\n"):
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(part) > limit:
            chunks.append(part[:limit])
            part = part[limit:]
        current = part
    if current:
        chunks.append(current)
    return chunks


class _FinalDeliveryMessage:
    def __init__(self, *, token: str, chat_id: int):
        self._token = token
        self._chat_id = chat_id

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        from telegram import Bot

        bot = Bot(token=self._token, **_telegram_bot_kwargs())
        chunks = _telegram_chunks(text)
        sent = None
        for idx, chunk in enumerate(chunks, start=1):
            send_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key
                in {
                    "parse_mode",
                    "disable_web_page_preview",
                    "reply_markup",
                    "read_timeout",
                    "write_timeout",
                    "connect_timeout",
                    "pool_timeout",
                }
            }
            if len(chunks) > 1 and idx < len(chunks):
                send_kwargs.pop("reply_markup", None)
            prefix = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
            sent = await bot.send_message(chat_id=self._chat_id, text=prefix + chunk, **send_kwargs)
        return sent


def final_builds_settled(run_id: str | uuid.UUID) -> bool:
    """Return true once every queued final-build track reached a terminal state.

    Publication copy is independent from rendering and Telegram video delivery.  It
    should wait until the batch is finished (so it is sent once), but a failed
    render or delivery must not suppress it.
    """
    run_uuid = uuid.UUID(str(run_id))
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == FINAL_BUILD_STATUS_ARTIFACT_TYPE,
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .all()
        )
    latest: dict[tuple[str, str], str] = {}
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        key = (
            str(payload.get("language") or row.language or "").strip().lower(),
            str(payload.get("variant") or "").strip(),
        )
        if not all(key) or key in latest:
            continue
        latest[key] = str(payload.get("status") or "").strip().lower()
    if not latest:
        # Compatibility for older runs created before per-track status artifacts.
        return all_final_artifacts_delivered(run_uuid)
    return all(status in {"success", "failed"} for status in latest.values())


def _trigger_final_social_descriptions_if_ready(run_id: str | uuid.UUID, chat_id: int) -> None:
    token = os.getenv(TOKEN_ENV_NAME, "").strip()
    if not token:
        return
    run_uuid = uuid.UUID(str(run_id))
    if not final_builds_settled(run_uuid):
        return

    from types import SimpleNamespace

    from .session_store import load_session_if_possible, save_session_if_possible

    session: dict[str, Any] | None = None
    try:
        with SessionLocal() as db:
            chat_session = _chat_session_for_run(db, run_uuid)
            if chat_session is not None:
                loaded = load_session_if_possible(int(chat_session.chat_id)) or {}
                session = loaded if isinstance(loaded, dict) else None
            else:
                session = _draft_session_for_run(db, run_uuid)
        if not isinstance(session, dict):
            loaded = load_session_if_possible(int(chat_id)) or {}
            if not isinstance(loaded, dict):
                return
            # ``chat_id`` can be the configured admin fallback for an archived
            # run which no longer owns a ChatSession or a GenerationDraft.  In
            # that case the admin may already be working on another run.  Do
            # not relabel and save that unrelated live session as the completed
            # run: save_session() would also persist its scene plan over the
            # archived run's scenes.
            if str(loaded.get("generation_run_id") or "").strip() != str(run_uuid):
                logger.warning(
                    "final social descriptions skipped: chat session belongs to another run "
                    "target_run=%s chat=%s session_run=%s",
                    run_uuid,
                    chat_id,
                    loaded.get("generation_run_id"),
                )
                return
            session = loaded
        if not isinstance(session, dict):
            return
        from .handlers.handlers_state_media import (
            SOCIAL_DESCRIPTIONS_PENDING_KEY,
            SOCIAL_DESCRIPTIONS_SENT_KEY,
            _send_final_social_descriptions,
        )

        if session.get(SOCIAL_DESCRIPTIONS_SENT_KEY) or session.get(SOCIAL_DESCRIPTIONS_PENDING_KEY):
            return
        if session.get(FINAL_SOCIAL_DESCRIPTIONS_TRIGGER_KEY):
            return
        session["generation_run_id"] = str(run_uuid)
        session[FINAL_SOCIAL_DESCRIPTIONS_TRIGGER_KEY] = _now_iso()
        save_session_if_possible(int(chat_id), session)
        update = SimpleNamespace(
            message=_FinalDeliveryMessage(token=token, chat_id=int(chat_id)),
            effective_chat=SimpleNamespace(id=int(chat_id)),
        )
        sent = asyncio.run(_send_final_social_descriptions(update, session))
        if sent:
            save_session_if_possible(int(chat_id), session)
            return
        session.pop(FINAL_SOCIAL_DESCRIPTIONS_TRIGGER_KEY, None)
        save_session_if_possible(int(chat_id), session)
    except Exception:
        if isinstance(session, dict):
            try:
                session.pop(FINAL_SOCIAL_DESCRIPTIONS_TRIGGER_KEY, None)
                save_session_if_possible(int(chat_id), session)
            except Exception:
                pass
        logger.exception("final social descriptions trigger failed run=%s chat=%s", run_id, chat_id)


def trigger_final_completion_followup(run_id: str | uuid.UUID) -> None:
    """Send the post-build prompt even when one or more video tracks failed."""
    run_uuid = uuid.UUID(str(run_id))
    with SessionLocal() as db:
        chat_id = _chat_id_for_run(db, run_uuid)
    if chat_id is None:
        return
    if not _trigger_guest_final_review_if_ready(run_uuid, chat_id):
        _trigger_final_social_descriptions_if_ready(run_uuid, chat_id)


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
        chat_id = _chat_id_for_run(db, run_uuid)
        if artifact is None:
            return _delivery_payload("failed", attempt_count=0, last_error="artifact not found")
        if artifact.run_id != run_uuid:
            return _delivery_payload("failed", attempt_count=0, last_error="artifact does not belong to run")
        payload = dict(artifact.payload_json or {})
        delivery = dict(payload.get(FINAL_DELIVERY_KEY) or {})
        if str(delivery.get("status") or "").strip().lower() == "sent" and delivery.get("telegram_message_id"):
            if chat_id is not None:
                if not _trigger_guest_final_review_if_ready(run_uuid, chat_id):
                    _trigger_final_social_descriptions_if_ready(run_uuid, chat_id)
            return delivery
        attempt_count = int(delivery.get("attempt_count") or 0)

        if not token:
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error=f"missing {TOKEN_ENV_NAME}")
            payload[FINAL_DELIVERY_KEY] = delivery
            artifact.payload_json = payload
            db.commit()
            return delivery
        if chat_id is None:
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error="delivery chat id not found")
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
        try:
            send_path, send_extra = _prepare_telegram_delivery_file(path, run_uuid=run_uuid, artifact_uuid=artifact_uuid)
        except Exception as exc:
            delivery = _delivery_payload("failed", attempt_count=attempt_count, last_error=str(exc))
            payload[FINAL_DELIVERY_KEY] = delivery
            artifact.payload_json = payload
            db.commit()
            return delivery
        caption = f"{artifact.label}\nRun: {run_uuid}"
        if send_path != path:
            caption = f"{artifact.label}\nTelegram delivery copy (<50 MB)\nRun: {run_uuid}"

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
            message_id = asyncio.run(_send_video_file(token, chat_id, send_path, caption))
            if message_id <= 0:
                raise RuntimeError("Telegram API did not return a message id")
            sent = _delivery_payload(
                "sent",
                attempt_count=attempt,
                telegram_message_id=message_id,
                sent_at=_now_iso(),
                **send_extra,
            )
            with SessionLocal() as db:
                artifact = db.get(GenerationArtifact, artifact_uuid)
                if artifact is not None:
                    payload = dict(artifact.payload_json or {})
                    payload[FINAL_DELIVERY_KEY] = sent
                    artifact.payload_json = payload
                    db.commit()
            if not _trigger_guest_final_review_if_ready(run_uuid, chat_id):
                _trigger_final_social_descriptions_if_ready(run_uuid, chat_id)
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
        artifact_rows = (
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
        status_rows = (
            db.execute(
                select(GenerationArtifact).where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == FINAL_BUILD_STATUS_ARTIFACT_TYPE,
                )
                .order_by(GenerationArtifact.created_at.desc())
            )
            .scalars()
            .all()
        )
    latest_rows_by_track: dict[tuple[str, str], GenerationArtifact] = {}
    artifact_rows_by_id = {str(row.id): row for row in artifact_rows}
    for row in artifact_rows:
        key = (str(row.language or "").strip().lower(), row.artifact_type)
        latest_rows_by_track.setdefault(key, row)
    latest_statuses_by_track: dict[tuple[str, str], dict[str, Any]] = {}
    for row in status_rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        language = str(payload.get("language") or row.language or "").strip().lower()
        variant = str(payload.get("variant") or "").strip()
        if not language or not variant:
            continue
        latest_statuses_by_track.setdefault((language, variant), dict(payload))

    def track_artifact_for_status(language: str, variant: str, status: dict[str, Any]) -> GenerationArtifact | None:
        artifact = None
        artifact_id = str(status.get("artifact_id") or "").strip()
        if artifact_id:
            artifact = artifact_rows_by_id.get(artifact_id)
        if artifact is None:
            artifact = latest_rows_by_track.get((language, f"final_{variant}"))
        return artifact

    def artifact_was_sent(artifact: GenerationArtifact | None) -> bool:
        if artifact is None:
            return False
        payload = artifact.payload_json if isinstance(artifact.payload_json, dict) else {}
        delivery = payload.get(FINAL_DELIVERY_KEY) if isinstance(payload, dict) else None
        return isinstance(delivery, dict) and str(delivery.get("status") or "").strip().lower() == "sent"

    if latest_statuses_by_track:
        if not any(variant == FINAL_PUBLICATION_READY_VARIANT for _language, variant in latest_statuses_by_track):
            return False
        for (language, variant), status in latest_statuses_by_track.items():
            artifact = track_artifact_for_status(language, variant, status)
            if artifact_was_sent(artifact):
                continue
            if str(status.get("status") or "").strip().lower() != "success":
                return False
            if not artifact_was_sent(artifact):
                return False
        return True
    latest_rows = list(latest_rows_by_track.values())
    if not latest_rows:
        return False
    for row in latest_rows:
        if not artifact_was_sent(row):
            return False
    return True
