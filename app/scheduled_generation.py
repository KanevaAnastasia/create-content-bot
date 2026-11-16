"""Scheduled generation queue helpers and Excel import/export."""

from __future__ import annotations

import io
import asyncio
import html
import os
import re
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from .db import SessionLocal
from .dish_visuals import build_dish_visual_brief
from .drafts import save_generation_draft
from .formatting import build_human_readable_plan, format_production_bundle_for_approval, format_viral_concept_brief
from .generation_store import ensure_generation_run_for_session, update_generation_run_cursor_from_session
from .generation_modes import (
    GENERATION_MODE_LONG,
    GENERATION_MODE_NORMAL,
    GENERATION_MODE_SESSION_KEY,
    GENERATION_MODE_SILENCE,
    normalize_generation_mode,
    target_languages_for_generation_mode,
)
from .models import GenerationRun, ScheduledGenerationEvent, ScheduledGenerationItem
from .handlers.handlers_generation_utils import fetch_recipe_text_from_url
from .pipeline import (
    generate_full_production,
    generate_recipe_from_source_text,
    generate_scenes_plan,
    generate_viral_concept_brief,
)
from .scene_media_candidates import resolve_scene_image_for_video, resolve_scene_video_for_production
from .scene_media_store import rebuild_production_session, rebuild_scene_db_ids_for_session
from .scheduled_comments import normalize_scheduled_generation_comment, scheduled_generation_every_start_prompt_comment
from .session_store import save_session_if_possible
from .series_store import attach_series_to_session, select_series_payload


SCHEDULED_STATUS_PENDING = "pending"
SCHEDULED_STATUS_RUNNING = "running"
SCHEDULED_STATUS_DRAFT_READY = "draft_ready"
SCHEDULED_STATUS_FAILED_NEEDS_REVIEW = "failed_needs_review"
SCHEDULED_STATUS_FAILED = "failed"
SCHEDULED_STATUS_WAITING_FOR_CHAK_CHAK = "waiting_for_chak_chak"

SCHEDULED_EXCEL_COLUMNS = (
    "title",
    "recipe",
    "comment",
    "overlay_subtitle",
    "title_color",
    "run_at",
    "is_draft",
    "series_id",
    "source_row_key",
    "generation_mode",
)
DEFAULT_TITLE_COLOR = "#c72638"
IMPORT_DIR = Path("/opt/content_fabric/artifacts/scheduled_generation_imports")
SCHEDULED_WORKER_LOG = Path("/opt/content_fabric/logs/scheduled_generation_worker.log")
TELEGRAM_MESSAGE_LIMIT = 3900
SCHEDULED_GENERATION_TIMEZONE_ENV = "SCHEDULED_GENERATION_TIMEZONE"
SCHEDULED_GENERATION_DUE_HOUR_ENV = "SCHEDULED_GENERATION_DUE_HOUR"
SCHEDULED_RETRY_NOT_BEFORE_KEY = "retry_not_before"
SCHEDULED_TECHNICAL_RETRY_DELAY_MINUTES = 15
SCHEDULED_MEDIA_PROGRESS_RETRY_DELAY_MINUTES = 1
SCHEDULED_MEDIA_PROGRESS_ITERATION_COUNT_ENV = "SCHEDULED_MEDIA_PROGRESS_ITERATION_COUNT"
SCHEDULED_MEDIA_PROGRESS_CONTINUE_DELAY_SEC_ENV = "SCHEDULED_MEDIA_PROGRESS_CONTINUE_DELAY_SEC"
DEFAULT_SCHEDULED_GENERATION_TIMEZONE = "Europe/Moscow"
DEFAULT_SCHEDULED_GENERATION_DUE_HOUR = 1
SCHEDULED_STEP_STATE_KEY = "step_state"
SCHEDULED_STEP_VIRAL_PLAN = "viral_plan"
SCHEDULED_STEP_SCENES_PLAN = "scenes_plan"
SCHEDULED_STEP_PRODUCTION_PROMPTS = "production_prompts"
SCHEDULED_STEP_SCENE_MEDIA = "scene_media"


@dataclass(frozen=True)
class ImportResult:
    created: int
    updated: int
    skipped: int
    saved_path: str
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueueResult:
    status: str
    backend: str
    detail: str = ""


class ScheduledGenerationNeedsReview(RuntimeError):
    """Raised when unattended scheduled media generation reaches a manual-review stop."""


class ScheduledGenerationRetryLater(RuntimeError):
    """Raised when a scheduled generation hit a temporary technical limit and should resume later."""


def _is_retryable_scheduled_network_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    retryable_names = {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimedOut",
        "TimeoutException",
        "WriteError",
        "WriteTimeout",
    }
    retryable_modules = ("httpx", "httpcore", "telegram")
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        module = type(current).__module__
        if name in retryable_names and module.startswith(retryable_modules):
            return True
        text = str(current)
        if "httpx.ConnectError" in text or "telegram.error.NetworkError" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _admin_manage_path() -> Path:
    return _project_root() / "admin_panel" / "manage.py"


def _saved_import_path(original_name: str) -> Path:
    suffix = Path(original_name or "upload.xlsx").suffix.lower()
    if suffix != ".xlsx":
        suffix = ".xlsx"
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return IMPORT_DIR / f"{stamp}_{uuid.uuid4().hex[:12]}{suffix}"


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _bool_cell(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    low = str(value).strip().lower()
    if low in {"1", "true", "yes", "y", "да", "draft", "черновик"}:
        return True
    if low in {"0", "false", "no", "n", "нет"}:
        return False
    return default


def _parse_run_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _cell_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scheduled_generation_timezone() -> ZoneInfo:
    raw = str(os.getenv(SCHEDULED_GENERATION_TIMEZONE_ENV) or DEFAULT_SCHEDULED_GENERATION_TIMEZONE).strip()
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _scheduled_generation_due_hour() -> int:
    raw = str(os.getenv(SCHEDULED_GENERATION_DUE_HOUR_ENV) or "").strip()
    if not raw:
        return DEFAULT_SCHEDULED_GENERATION_DUE_HOUR
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SCHEDULED_GENERATION_DUE_HOUR
    return min(23, max(0, value))


def _due_cutoff_for_local_date(now: datetime | None = None) -> datetime:
    tz = _scheduled_generation_timezone()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_current = current.astimezone(tz)
    local_today = local_current.date()
    due_hour = _scheduled_generation_due_hour()
    local_due_today = datetime.combine(local_today, time(hour=due_hour), tzinfo=tz)
    if local_current >= local_due_today:
        return (local_due_today + timedelta(days=1)).astimezone(timezone.utc)
    return local_due_today.astimezone(timezone.utc)


def _normalize_title_color(value: Any) -> str:
    raw = _cell_text(value) or DEFAULT_TITLE_COLOR
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if len(raw) not in {4, 7, 9}:
        return DEFAULT_TITLE_COLOR
    return raw


def _recipe_text_for_viral_plan(recipe: str, comment: str | None) -> str:
    text = (recipe or "").strip()
    extra = normalize_scheduled_generation_comment(comment)
    if not extra:
        return text
    return (
        f"{text}\n\n"
        "КОММЕНТАРИЙ К ГЕНЕРАЦИИ ВИРАЛЬНОГО ПЛАНА:\n"
        f"{extra}"
    )


def _extract_first_http_url(text: str) -> str:
    match = re.search(r"https?://\S+", text or "")
    if not match:
        return ""
    return match.group(0).rstrip(").,;!?'\"")


async def _resolve_scheduled_recipe_text(recipe: str) -> str:
    text = (recipe or "").strip()
    source_url = _extract_first_http_url(text)
    if not source_url:
        return text
    source_text = await asyncio.to_thread(fetch_recipe_text_from_url, source_url)
    return await generate_recipe_from_source_text(source_text, source_url=source_url)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    raw = _cell_text(value)
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(value: Any, *, row: int, column: int) -> str:
    ref = f"{_column_name(column)}{row}"
    text = html.escape(_cell_text(value), quote=True)
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _build_xlsx(rows: list[list[Any]]) -> bytes:
    sheet_rows = []
    for row_idx, row in enumerate(rows, start=1):
        cells = "".join(_xlsx_cell(value, row=row_idx, column=col_idx) for col_idx, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_idx}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="scheduled_generation" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return out.getvalue()


def _read_first_sheet_rows(data: bytes) -> list[list[Any]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", ns):
                texts = [node.text or "" for node in item.findall(".//m:t", ns)]
                shared_strings.append("".join(texts))
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    rows: list[list[Any]] = []
    for row in root.findall(".//m:row", ns):
        values: list[Any] = []
        for cell in row.findall("m:c", ns):
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//m:t", ns))
            else:
                raw = cell.findtext("m:v", default="", namespaces=ns)
                if cell_type == "s":
                    try:
                        value = shared_strings[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                else:
                    value = raw
            values.append(value)
        rows.append(values)
    return rows


def build_scheduled_generation_template_bytes() -> bytes:
    return _build_xlsx(
        [
            list(SCHEDULED_EXCEL_COLUMNS),
            [
                "Банановое мороженое",
                "https://example.com/recipe-or-full-recipe-text",
                "Сделай упор на быстрый летний хук и домашнюю подачу.",
                "КБЖУ на 100 гр: 126/10/4/24",
                DEFAULT_TITLE_COLOR,
                "2026-06-30",
                True,
                "",
                "banana-icecream-001",
                GENERATION_MODE_NORMAL,
            ],
        ]
    )


def export_scheduled_generation_items_to_xlsx(queryset) -> bytes:
    rows = [list(SCHEDULED_EXCEL_COLUMNS) + ["status", "error_text"]]
    for item in queryset:
        rows.append(
            [
                item.title,
                item.recipe,
                item.comment or "",
                item.overlay_subtitle or "",
                item.title_color,
                item.run_at.isoformat() if item.run_at else "",
                bool(item.is_draft),
                str(item.series_id or ""),
                item.source_row_key or "",
                normalize_generation_mode(
                    (item.payload_json if isinstance(item.payload_json, dict) else {}).get(
                        GENERATION_MODE_SESSION_KEY
                    )
                ),
                item.status,
                item.error_text or "",
            ]
        )
    return _build_xlsx(rows)


def import_scheduled_generation_xlsx(file_obj, *, original_name: str = "scheduled_generation.xlsx") -> ImportResult:
    if not str(original_name or "").lower().endswith(".xlsx"):
        raise ValueError("Поддерживается только Excel .xlsx; сохраните файл как .xlsx и загрузите снова.")
    saved_path = _saved_import_path(original_name)
    data = file_obj.read()
    saved_path.write_bytes(data)
    rows = _read_first_sheet_rows(data)
    if not rows:
        raise ValueError("Excel file is empty.")
    header = [_cell_text(cell).lower() for cell in rows[0]]
    missing = [column for column in ("title", "recipe", "run_at") if column not in header]
    if missing:
        raise ValueError(f"В Excel нет обязательных колонок: {', '.join(missing)}")
    index = {name: idx for idx, name in enumerate(header)}
    created = updated = skipped = 0
    errors: list[str] = []
    with SessionLocal() as db:
        for row_number, row in enumerate(rows[1:], start=2):
            values = {name: row[idx] if idx < len(row) else None for name, idx in index.items()}
            title = _cell_text(values.get("title"))
            recipe = _cell_text(values.get("recipe"))
            run_at = _parse_run_at(values.get("run_at"))
            if not title and not recipe and run_at is None:
                skipped += 1
                continue
            if not title or not recipe or run_at is None:
                skipped += 1
                errors.append(f"row {row_number}: required title, recipe, run_at")
                continue
            source_row_key = _cell_text(values.get("source_row_key")) or None
            item = None
            if source_row_key:
                item = db.execute(
                    select(ScheduledGenerationItem).where(ScheduledGenerationItem.source_row_key == source_row_key)
                ).scalar_one_or_none()
            if item is None:
                item = ScheduledGenerationItem(title=title, recipe=recipe, run_at=run_at)
                db.add(item)
                created += 1
            else:
                updated += 1
            item.title = title
            item.recipe = recipe
            item.comment = _cell_text(values.get("comment")) or None
            item.overlay_subtitle = _cell_text(values.get("overlay_subtitle")) or None
            item.title_color = _normalize_title_color(values.get("title_color"))
            item.run_at = run_at
            item.is_draft = _bool_cell(values.get("is_draft"), default=True)
            item.series_id = _uuid_or_none(values.get("series_id"))
            item.source_row_key = source_row_key
            item.source_file_path = str(saved_path)
            item.status = SCHEDULED_STATUS_PENDING
            item.error_text = None
            payload = dict(item.payload_json or {})
            generation_mode = normalize_generation_mode(values.get("generation_mode"))
            if generation_mode in {GENERATION_MODE_SILENCE, GENERATION_MODE_LONG}:
                payload[GENERATION_MODE_SESSION_KEY] = generation_mode
            else:
                payload.pop(GENERATION_MODE_SESSION_KEY, None)
            payload["last_imported_at"] = datetime.now(timezone.utc).isoformat()
            payload["source_file_name"] = original_name
            payload["source_row_number"] = row_number
            item.payload_json = payload
        db.commit()
    return ImportResult(created=created, updated=updated, skipped=skipped, saved_path=str(saved_path), errors=tuple(errors))


def due_scheduled_generation_items(*, now: datetime | None = None, limit: int = 0) -> list[ScheduledGenerationItem]:
    current = now or datetime.now(timezone.utc)
    cutoff = _due_cutoff_for_local_date(now)
    with SessionLocal() as db:
        query = (
            select(ScheduledGenerationItem)
            .where(ScheduledGenerationItem.status == SCHEDULED_STATUS_PENDING)
            .where(ScheduledGenerationItem.run_at < cutoff)
            .order_by(ScheduledGenerationItem.run_at.asc(), ScheduledGenerationItem.created_at.asc())
        )
        if limit:
            query = query.limit(limit)
        rows = db.execute(query).scalars().all()
        out: list[ScheduledGenerationItem] = []
        for row in rows:
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            retry_raw = str(payload.get(SCHEDULED_RETRY_NOT_BEFORE_KEY) or "").strip()
            if retry_raw:
                try:
                    retry_at = datetime.fromisoformat(retry_raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    if retry_at > current:
                        continue
                except ValueError:
                    pass
            out.append(row)
        return out


def _admin_chat_id() -> int | None:
    raw = str(os.getenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _notify_admin_chat(text: str) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    chat_id = _admin_chat_id()
    token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if chat_id is None or not token:
        return
    try:
        from telegram import Bot

        bot = Bot(token=token)
        body = str(text or "")
        chunks = _telegram_chunks(body)
        for idx, chunk in enumerate(chunks, start=1):
            prefix = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
            await bot.send_message(
                chat_id=chat_id,
                text=prefix + chunk,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=15,
                pool_timeout=15,
            )
    except Exception:
        return


def _telegram_chunks(text: str, *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
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


def _read_upload_payload(value: Any) -> tuple[bytes, str]:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value), "artifact.bin"
    filename = str(getattr(value, "name", "") or "artifact.bin")
    if hasattr(value, "seek"):
        try:
            value.seek(0)
        except Exception:
            pass
    payload = value.read() if hasattr(value, "read") else bytes(value)
    return bytes(payload), filename


class _ScheduledTelegramMessage:
    def __init__(self, *, item_id: uuid.UUID | str, chat_id: int | None, bot: Any | None):
        self._item_id = item_id
        self._chat_id = chat_id
        self._bot = bot

    async def _record(self, *, status: str, message: str, payload: dict[str, Any] | None = None) -> None:
        await record_scheduled_generation_event_async(
            self._item_id,
            event_type="media",
            stage="scene_media",
            status=status,
            message=message,
            payload=payload,
            notify=False,
        )

    async def _send_best_effort(self, label: str, send_call: Any) -> Any:
        try:
            return await send_call
        except Exception as exc:
            await self._record(
                status="telegram_send_failed",
                message=f"Headless Telegram send failed ({label}); event saved in DB: {type(exc).__name__}: {exc}",
                payload={"label": label, "error_type": type(exc).__name__, "error": str(exc)},
            )
            return SimpleNamespace(message_id=None)

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        body = str(text or "")
        await self._record(status="message", message=body)
        if self._bot is None or self._chat_id is None:
            return SimpleNamespace(message_id=None)
        chunks = _telegram_chunks(body)
        sent = None
        for idx, chunk in enumerate(chunks, start=1):
            prefix = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
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
            sent = await self._send_best_effort(
                "send_message",
                self._bot.send_message(chat_id=self._chat_id, text=prefix + chunk, **send_kwargs),
            )
        return sent or SimpleNamespace(message_id=None)

    async def reply_photo(self, *, photo: Any, caption: str = "", **kwargs: Any) -> Any:
        payload, filename = _read_upload_payload(photo)
        await self._record(
            status="image",
            message=caption or "Изображение отправлено.",
            payload={"filename": filename, "bytes": len(payload)},
        )
        if self._bot is None or self._chat_id is None:
            return SimpleNamespace(message_id=None)
        stream = io.BytesIO(payload)
        stream.name = filename if filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) else "image.png"
        return await self._send_best_effort(
            "send_photo",
            self._bot.send_photo(chat_id=self._chat_id, photo=stream, caption=caption, **kwargs),
        )

    async def reply_video(self, *, video: Any, caption: str = "", **kwargs: Any) -> Any:
        payload, filename = _read_upload_payload(video)
        await self._record(
            status="video",
            message=caption or "Видео отправлено.",
            payload={"filename": filename, "bytes": len(payload)},
        )
        if self._bot is None or self._chat_id is None:
            return SimpleNamespace(message_id=None)
        stream = io.BytesIO(payload)
        stream.name = filename if filename.lower().endswith(".mp4") else "video.mp4"
        return await self._send_best_effort(
            "send_video",
            self._bot.send_video(chat_id=self._chat_id, video=stream, caption=caption, **kwargs),
        )

    async def reply_document(self, *, document: Any, caption: str = "", **kwargs: Any) -> Any:
        payload, filename = _read_upload_payload(document)
        await self._record(
            status="document",
            message=caption or "Файл отправлен.",
            payload={"filename": filename, "bytes": len(payload)},
        )
        if self._bot is None or self._chat_id is None:
            return SimpleNamespace(message_id=None)
        stream = io.BytesIO(payload)
        stream.name = filename
        return await self._send_best_effort(
            "send_document",
            self._bot.send_document(chat_id=self._chat_id, document=stream, caption=caption, **kwargs),
        )


def _scheduled_headless_update(item_id: uuid.UUID | str) -> tuple[Any, Any, int | None]:
    chat_id = _admin_chat_id()
    token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    bot = None
    if chat_id is not None and token:
        try:
            from telegram import Bot

            bot = Bot(token=token)
        except Exception:
            bot = None
    message = _ScheduledTelegramMessage(item_id=item_id, chat_id=chat_id, bot=bot)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id) if chat_id is not None else None)
    context = SimpleNamespace(bot=bot) if bot is not None else None
    return update, context, chat_id


def _event_text(
    title: str,
    *,
    event_type: str,
    stage: str | None = None,
    status: str | None = None,
    message: str = "",
    item_id: uuid.UUID | str | None = None,
    run_id: uuid.UUID | str | None = None,
) -> str:
    parts = [f"Ночная очередь: {event_type}"]
    if stage or status:
        details = " / ".join(part for part in (stage, status) if part)
        parts.append(details)
    if title:
        parts.append(str(title))
    if message:
        parts.append(str(message))
    if item_id:
        parts.append(f"item_id={item_id}")
    if run_id:
        parts.append(f"run_id={run_id}")
    return "\n".join(parts)


def _insert_scheduled_event(
    item_id: uuid.UUID | str,
    *,
    event_type: str,
    stage: str | None = None,
    status: str | None = None,
    message: str = "",
    payload: dict[str, Any] | None = None,
    run_id: uuid.UUID | str | None = None,
) -> str:
    item_uuid = uuid.UUID(str(item_id))
    run_uuid = uuid.UUID(str(run_id)) if run_id else None
    with SessionLocal() as db:
        item = db.get(ScheduledGenerationItem, item_uuid)
        title = item.title if item is not None else ""
        if item is None:
            return _event_text(
                title,
                event_type=event_type,
                stage=stage,
                status=status,
                message=message,
                item_id=item_uuid,
                run_id=run_id,
            )
        event = ScheduledGenerationEvent(
            scheduled_item_id=item_uuid,
            generation_run_id=run_uuid,
            event_type=str(event_type or "event")[:64],
            stage=str(stage or "")[:64] or None,
            status=str(status or "")[:64] or None,
            message=str(message or ""),
            payload_json=payload or {},
        )
        db.add(event)
        if item is not None:
            meta = dict(item.payload_json or {})
            meta["last_event_type"] = event.event_type
            meta["last_event_stage"] = event.stage
            meta["last_event_status"] = event.status
            meta["last_event_at"] = datetime.now(timezone.utc).isoformat()
            item.payload_json = meta
        db.commit()
    return _event_text(
        title,
        event_type=event_type,
        stage=stage,
        status=status,
        message=message,
        item_id=item_uuid,
        run_id=run_id,
    )


async def record_scheduled_generation_event_async(
    item_id: uuid.UUID | str,
    *,
    event_type: str,
    stage: str | None = None,
    status: str | None = None,
    message: str = "",
    payload: dict[str, Any] | None = None,
    run_id: uuid.UUID | str | None = None,
    notify: bool = True,
) -> None:
    text = _insert_scheduled_event(
        item_id,
        event_type=event_type,
        stage=stage,
        status=status,
        message=message,
        payload=payload,
        run_id=run_id,
    )
    if notify:
        await _notify_admin_chat(text)


def record_scheduled_generation_event(
    item_id: uuid.UUID | str,
    *,
    event_type: str,
    stage: str | None = None,
    status: str | None = None,
    message: str = "",
    payload: dict[str, Any] | None = None,
    run_id: uuid.UUID | str | None = None,
    notify: bool = True,
) -> None:
    text = _insert_scheduled_event(
        item_id,
        event_type=event_type,
        stage=stage,
        status=status,
        message=message,
        payload=payload,
        run_id=run_id,
    )
    if not notify:
        return
    try:
        asyncio.run(_notify_admin_chat(text))
    except RuntimeError:
        pass


def claim_scheduled_generation_item(item_id: uuid.UUID) -> bool:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, item_id)
        if row is None or row.status != SCHEDULED_STATUS_PENDING:
            return False
        row.status = SCHEDULED_STATUS_RUNNING
        row.claimed_at = now
        row.started_at = now
        row.error_text = None
        payload = dict(row.payload_json or {})
        payload.pop(SCHEDULED_RETRY_NOT_BEFORE_KEY, None)
        row.payload_json = payload
        db.commit()
        return True


def _release_items_waiting_for_completed_item(db, completed_item_id: uuid.UUID, *, now: datetime) -> None:
    held_rows = (
        db.execute(
            select(ScheduledGenerationItem).where(
                ScheduledGenerationItem.status == SCHEDULED_STATUS_WAITING_FOR_CHAK_CHAK
            )
        )
        .scalars()
        .all()
    )
    for held in held_rows:
        payload = dict(held.payload_json or {})
        if str(payload.get("hold_until_item_id") or "") != str(completed_item_id):
            continue
        payload["released_after_item_id"] = str(completed_item_id)
        payload["released_at"] = now.isoformat()
        payload.pop(SCHEDULED_RETRY_NOT_BEFORE_KEY, None)
        held.status = SCHEDULED_STATUS_PENDING
        held.run_at = now
        held.error_text = None
        held.claimed_at = None
        held.started_at = None
        held.finished_at = None
        held.payload_json = payload
        db.add(
            ScheduledGenerationEvent(
                scheduled_item_id=held.id,
                generation_run_id=None,
                event_type="released",
                stage="dependency",
                status=SCHEDULED_STATUS_PENDING,
                message="Задача автоматически выпущена после завершения зависимой задачи.",
                payload_json={"released_after_item_id": str(completed_item_id)},
            )
        )


def _set_item_status(
    item_id: uuid.UUID,
    status: str,
    *,
    run_id: str | None = None,
    error_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, item_id)
        if row is None:
            return
        row.status = status
        row.error_text = error_text
        row.finished_at = now
        if run_id:
            try:
                row.generation_run_id = uuid.UUID(run_id)
                run = db.get(GenerationRun, uuid.UUID(run_id))
                if run is not None:
                    run.is_draft = bool(row.is_draft)
                    run.scheduled_item_id = row.id
            except ValueError:
                pass
        if payload:
            merged = dict(row.payload_json or {})
            merged.update(payload)
            row.payload_json = merged
        if status == SCHEDULED_STATUS_DRAFT_READY:
            _release_items_waiting_for_completed_item(db, row.id, now=now)
        db.commit()


def _defer_scheduled_generation_item_retry(
    item_id: uuid.UUID,
    *,
    message: str,
    delay_minutes: int = SCHEDULED_TECHNICAL_RETRY_DELAY_MINUTES,
) -> datetime:
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(delay_minutes)))
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, item_id)
        if row is None:
            return retry_at
        payload = dict(row.payload_json or {})
        payload[SCHEDULED_RETRY_NOT_BEFORE_KEY] = retry_at.isoformat()
        row.payload_json = payload
        row.status = SCHEDULED_STATUS_PENDING
        row.claimed_at = None
        row.error_text = message
        row.finished_at = None
        db.commit()
    return retry_at


def _scheduled_step_state(payload: dict[str, Any] | None) -> dict[str, Any]:
    state = (payload or {}).get(SCHEDULED_STEP_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


def _scheduled_step_done(payload: dict[str, Any] | None, step: str) -> dict[str, Any] | None:
    state = _scheduled_step_state(payload)
    item = state.get(step)
    if isinstance(item, dict) and item.get("status") == "done":
        data = item.get("data")
        return dict(data) if isinstance(data, dict) else {}
    return None


def _production_has_required_media_prompts(production: dict[str, Any]) -> bool:
    plan = production.get("json_plan")
    if not isinstance(plan, list) or not plan:
        return False
    for scene in plan:
        if not isinstance(scene, dict):
            return False
        start_prompt = str(scene.get("start_frame_prompt") or scene.get("start_frame_prompt_ru") or "").strip()
        video_prompts = scene.get("video_prompt_by_lang")
        has_video_prompt = (
            any(str(value or "").strip() for value in video_prompts.values())
            if isinstance(video_prompts, dict)
            else bool(str(scene.get("video_prompt") or scene.get("video_prompt_en") or "").strip())
        )
        if not start_prompt or not has_video_prompt:
            return False
    return True


def _set_scheduled_step_state(
    item_id: uuid.UUID,
    step: str,
    *,
    status: str,
    data: dict[str, Any] | None = None,
    cursor: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, item_id)
        if row is None:
            return
        payload = dict(row.payload_json or {})
        state = _scheduled_step_state(payload)
        entry = dict(state.get(step) or {}) if isinstance(state.get(step), dict) else {}
        entry["status"] = str(status)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if data is not None:
            entry["data"] = data
        if cursor is not None:
            entry["cursor"] = cursor
        if error_text:
            entry["error_text"] = str(error_text)
        elif "error_text" in entry and status != "failed":
            entry.pop("error_text", None)
        state[step] = entry
        payload[SCHEDULED_STEP_STATE_KEY] = state
        row.payload_json = payload
        db.commit()


def _scheduled_session_cursor(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation_run_id": str(session.get("generation_run_id") or ""),
        "scene_index": session.get("image_scene_index"),
        "substep": session.get("scene_media_substep"),
        "language_index": session.get("scene_media_language_index"),
        "state": session.get("state"),
    }


def _apply_scheduled_media_resume_state(session: dict[str, Any], item: ScheduledGenerationItem, payload: dict[str, Any]) -> None:
    media_state = _scheduled_step_state(payload).get(SCHEDULED_STEP_SCENE_MEDIA)
    media_entry = media_state if isinstance(media_state, dict) else {}
    cursor = media_entry.get("cursor") if isinstance(media_entry.get("cursor"), dict) else {}
    run_id = str(cursor.get("generation_run_id") or item.generation_run_id or "").strip()
    if run_id:
        session["generation_run_id"] = run_id
        if not isinstance(session.get("scene_db_ids_by_index"), dict):
            session["scene_db_ids_by_index"] = rebuild_scene_db_ids_for_session(session, run_id)
        rebuild_production_session(session, run_id)
    if cursor:
        if cursor.get("scene_index") is not None:
            session["image_scene_index"] = int(cursor.get("scene_index") or 0)
        if cursor.get("substep"):
            session["scene_media_substep"] = str(cursor.get("substep"))
        if cursor.get("language_index") is not None:
            session["scene_media_language_index"] = int(cursor.get("language_index") or 0)
    if session.get("state") == "ready_for_production":
        session["state"] = "waiting_scene_media"
    _reconcile_scheduled_media_resume_cursor(session)


def _scheduled_session_for_item(
    item: ScheduledGenerationItem,
    recipe_text: str,
    production: dict[str, Any],
    scenes_plan: list[dict[str, Any]],
    viral_concept_brief: dict[str, Any],
) -> dict[str, Any]:
    dish_visual_brief = build_dish_visual_brief(item.title, recipe_text)
    payload = item.payload_json if isinstance(item.payload_json, dict) else {}
    generation_mode = normalize_generation_mode(payload.get(GENERATION_MODE_SESSION_KEY))
    target_languages = target_languages_for_generation_mode(generation_mode, ["ru", "en"])
    generation_comment = normalize_scheduled_generation_comment(item.comment)
    every_start_prompt_comment = scheduled_generation_every_start_prompt_comment(item.comment)
    overlay_subtitle = " ".join(str(getattr(item, "overlay_subtitle", "") or "").split())
    overlay_subtitle_by_lang = {"ru": overlay_subtitle} if overlay_subtitle else {}
    if overlay_subtitle_by_lang:
        production["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
    else:
        production.pop("overlay_subtitle_by_lang", None)
    return {
        "state": "waiting_scene_media",
        "scheduled_generation_mode": True,
        "syntax_only": True,
        "is_draft": bool(item.is_draft),
        "scheduled_item_id": str(item.id),
        "selected_project_key": "grandma_kitchen",
        GENERATION_MODE_SESSION_KEY: generation_mode,
        "target_languages": target_languages,
        "user_input": item.recipe,
        "scheduled_generation_comment": generation_comment,
        "scheduled_generation_every_start_prompt_comment": every_start_prompt_comment,
        "selected_concept": item.title,
        "recipe_full_text": recipe_text,
        "recipe_title": item.title,
        "dish_visual_brief": dish_visual_brief,
        "viral_concept_brief": viral_concept_brief,
        "scenes_plan": scenes_plan,
        "production": production,
        "overlay_title_by_lang": {"ru": item.title},
        "overlay_subtitle_by_lang": overlay_subtitle_by_lang,
        "overlay_title_style": {"title_color": item.title_color or DEFAULT_TITLE_COLOR},
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "scene_media_language_index": 0,
        "scene_media_approve_mode": "ai",
    }


def _scheduled_media_summary(session: dict[str, Any]) -> dict[str, Any]:
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list):
        scenes = []
    scene_summaries: list[dict[str, Any]] = []
    total_videos = 0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        videos = scene.get("video_results_by_lang")
        video_languages = (
            sorted(str(lang) for lang, value in videos.items() if isinstance(value, dict))
            if isinstance(videos, dict)
            else []
        )
        candidate_mapping = scene.get("video_candidates_by_lang")
        if isinstance(candidate_mapping, dict):
            for lang, candidates in candidate_mapping.items():
                if isinstance(candidates, list) and any(isinstance(item, dict) for item in candidates):
                    language = str(lang).strip().lower()
                    if language and language not in video_languages:
                        video_languages.append(language)
        video_languages = sorted(set(video_languages))
        total_videos += len(video_languages)
        start_candidate = resolve_scene_image_for_video(scene, kind="start")
        end_candidate = resolve_scene_image_for_video(scene, kind="end")
        scene_summaries.append(
            {
                "scene": scene.get("scene") or index + 1,
                "has_start_image": bool(scene.get("start_frame_image_b64") or start_candidate),
                "has_end_image": bool(scene.get("end_frame_image_b64") or end_candidate),
                "video_languages": video_languages,
                "ai_history_count": len(scene.get("ai_approval_history") or []) if isinstance(scene.get("ai_approval_history"), list) else 0,
            }
        )
    return {
        "state": session.get("state"),
        "scene_count": len(scene_summaries),
        "video_count": total_videos,
        "scenes": scene_summaries,
        "generation_run_id": session.get("generation_run_id"),
    }


def _scene_has_approved_video_for_production(scene: dict[str, Any], language: str) -> bool:
    lang = str(language or "ru").strip().lower() or "ru"
    videos = scene.get("video_results_by_lang")
    approvals = scene.get("video_approvals_by_lang")
    has_approved_legacy = (
        isinstance(videos, dict)
        and isinstance(videos.get(lang), dict)
        and isinstance(approvals, dict)
        and bool(approvals.get(lang))
    )
    if has_approved_legacy:
        return True
    candidate = resolve_scene_video_for_production(scene, lang)
    return isinstance(candidate, dict) and bool(candidate.get("approved_for_production")) and bool(
        candidate.get("video_b64") or candidate.get("media_uri")
    )


def _scheduled_media_missing_items(session: dict[str, Any]) -> list[tuple[int, str]]:
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return [(-1, "Нет production scenes.")]
    items: list[tuple[int, str]] = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            items.append((index, f"Сцена {index + 1}: пустая запись."))
            continue
        if not (scene.get("start_frame_image_b64") or resolve_scene_image_for_video(scene, kind="start")):
            items.append((index, f"Сцена {scene.get('scene') or index + 1}: нет start image."))
        prompt_map = scene.get("video_prompt_by_lang")
        if isinstance(prompt_map, dict) and prompt_map:
            expected_languages = [str(lang).strip().lower() for lang in prompt_map if str(lang).strip()]
        else:
            expected_languages = [str(scene.get("video_prompt_current_language") or "ru").strip().lower() or "ru"]
        for language in sorted(set(expected_languages)):
            if not _scene_has_approved_video_for_production(scene, language):
                items.append((index, f"Сцена {scene.get('scene') or index + 1}: нет approved video для {language}."))
    return items


def _scheduled_media_substep_for_missing_reason(reason: str) -> str:
    normalized = str(reason or "").lower()
    if "start image" in normalized:
        return "start_prompt"
    return "video_prompt"


def _reconcile_scheduled_media_resume_cursor(session: dict[str, Any]) -> bool:
    if str(session.get("state") or "") != "waiting_scene_media":
        return False
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return False
    try:
        current_index = int(session.get("image_scene_index") or 0)
    except (TypeError, ValueError):
        current_index = 0
    missing_items = [(index, reason) for index, reason in _scheduled_media_missing_items(session) if index >= 0]
    if not missing_items:
        return False
    first_missing_index = min(index for index, _reason in missing_items)
    if first_missing_index < current_index:
        return False
    first_missing_reason = next(reason for index, reason in missing_items if index == first_missing_index)
    expected_substep = _scheduled_media_substep_for_missing_reason(first_missing_reason)
    changed = False
    if first_missing_index > current_index:
        session["image_scene_index"] = first_missing_index
        session["scene_media_language_index"] = 0
        changed = True
    if session.get("scene_media_substep") != expected_substep:
        session["scene_media_substep"] = expected_substep
        changed = True
    if changed:
        session["scheduled_media_resume_reconciled_cursor"] = {
            "from_scene_index": current_index,
            "to_scene_index": first_missing_index,
            "substep": expected_substep,
            "reason": first_missing_reason,
        }
    return changed


def _scheduled_media_missing_reasons(session: dict[str, Any]) -> list[str]:
    return [reason for _index, reason in _scheduled_media_missing_items(session)]


def _scheduled_media_can_resume_from_cursor(session: dict[str, Any], missing_items: list[tuple[int, str]]) -> bool:
    if not missing_items or str(session.get("state") or "") != "waiting_scene_media":
        return False
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return False
    try:
        current_index = int(session.get("image_scene_index") or 0)
    except (TypeError, ValueError):
        return False
    if current_index < 0 or current_index >= len(scenes):
        return False
    missing_indexes = [index for index, _reason in missing_items if index >= 0]
    return bool(missing_indexes) and min(missing_indexes) >= current_index


def _scheduled_media_progress_iteration_count() -> int:
    raw = str(os.getenv(SCHEDULED_MEDIA_PROGRESS_ITERATION_COUNT_ENV) or "").strip()
    if not raw:
        return 50
    try:
        value = int(raw)
    except ValueError:
        return 50
    return max(1, value)


def _scheduled_media_progress_continue_delay_sec() -> float:
    raw = str(os.getenv(SCHEDULED_MEDIA_PROGRESS_CONTINUE_DELAY_SEC_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


async def _run_scheduled_media_pipeline(item_id: uuid.UUID, session: dict[str, Any]) -> dict[str, Any]:
    chat_id = _admin_chat_id()
    if chat_id is None:
        raise ScheduledGenerationNeedsReview("SCHEDULED_GENERATION_ADMIN_CHAT_ID не задан, некуда отправлять медиа и хронику.")
    run_id, _scene_map = ensure_generation_run_for_session(session)
    update_generation_run_cursor_from_session(session)
    save_session_if_possible(chat_id, session)
    _set_scheduled_step_state(
        item_id,
        SCHEDULED_STEP_SCENE_MEDIA,
        status="running",
        cursor=_scheduled_session_cursor(session),
        data={"summary": _scheduled_media_summary(session), "generation_run_id": run_id},
    )
    await record_scheduled_generation_event_async(
        item_id,
        event_type="attempt",
        stage="scene_media",
        status="started",
        message="Запускаю unattended AI approve: start images, video через Syntax, проверки AI.",
        payload=_scheduled_media_summary(session),
    )
    from .handlers.handlers_state_deps import SESSIONS
    from .handlers.handlers_state_media import (
        SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY,
        _set_scene_media_manual_approval,
        _run_scene_media_ai_approve,
    )

    update, context, resolved_chat_id = _scheduled_headless_update(item_id)
    if resolved_chat_id is not None:
        SESSIONS[int(resolved_chat_id)] = session
    max_progress_iterations = _scheduled_media_progress_iteration_count()
    continue_delay_sec = _scheduled_media_progress_continue_delay_sec()
    missing_items: list[tuple[int, str]] = []
    missing: list[str] = []
    for media_iteration in range(1, max_progress_iterations + 1):
        progress_before = {
            "cursor": _scheduled_session_cursor(session),
            "summary": _scheduled_media_summary(session),
        }
        await _run_scene_media_ai_approve(update, context=context, chat_id=resolved_chat_id, session=session)
        update_generation_run_cursor_from_session(session)
        save_session_if_possible(resolved_chat_id or chat_id, session)
        technical_failure = session.get(SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY)
        if isinstance(technical_failure, dict) and technical_failure:
            message = (
                "ИИ апрув остановлен из-за технического лимита/пустого ответа проверки. "
                "Кадры не перегенерирую, состояние сохранено для повторного запуска.\n"
                f"stage={technical_failure.get('stage')}\n"
                f"attempts={technical_failure.get('attempts')}/{technical_failure.get('max_retries')}"
            )
            retry_at = _defer_scheduled_generation_item_retry(item_id, message=message)
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_SCENE_MEDIA,
                status="retry_later",
                cursor=_scheduled_session_cursor(session),
                data={
                    "summary": _scheduled_media_summary(session),
                    "technical_failure": technical_failure,
                    "retry_not_before": retry_at.isoformat(),
                },
                error_text=message,
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="retry_later",
                stage="scene_media",
                status=SCHEDULED_STATUS_PENDING,
                message=f"{message}\nretry_not_before={retry_at.isoformat()}",
                payload={"technical_failure": technical_failure, "retry_not_before": retry_at.isoformat()},
            )
            raise ScheduledGenerationRetryLater(message)
        human_review = session.get("scene_media_ai_human_review")
        if isinstance(human_review, dict) and human_review:
            message = (
                "Ночной режим остановлен на текущем media-шаге: ИИ апрув запросил ручную проверку. "
                "Сохраняю cursor и перевожу задачу в ручной режим без отката к первому кадру.\n"
                f"stage={human_review.get('stage') or '-'}\n"
                f"reason={human_review.get('reason') or '-'}"
            )
            _set_scene_media_manual_approval(session)
            update_generation_run_cursor_from_session(session)
            save_session_if_possible(resolved_chat_id or chat_id, session)
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_SCENE_MEDIA,
                status="failed_needs_review",
                cursor=_scheduled_session_cursor(session),
                data={
                    "summary": _scheduled_media_summary(session),
                    "human_review": human_review,
                    "manual_mode": True,
                },
                error_text=message,
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="failed_needs_review",
                stage="scene_media",
                status=SCHEDULED_STATUS_FAILED_NEEDS_REVIEW,
                message=message,
                payload={"human_review": human_review},
            )
            raise ScheduledGenerationNeedsReview(message)
        production = session.get("production") if isinstance(session.get("production"), dict) else {}
        post_production = production.get("post_production") if isinstance(production, dict) else None
        if isinstance(post_production, dict) and post_production.get("error"):
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_SCENE_MEDIA,
                status="failed",
                cursor=_scheduled_session_cursor(session),
                data={"summary": _scheduled_media_summary(session), "post_production": post_production},
                error_text=str(post_production.get("error")),
            )
            raise ScheduledGenerationNeedsReview(f"Финальная сборка не поставилась в очередь: {post_production.get('error')}")
        missing_items = _scheduled_media_missing_items(session)
        missing = [reason for _index, reason in missing_items]
        if not missing or not _scheduled_media_can_resume_from_cursor(session, missing_items):
            break
        progress_after = {
            "cursor": _scheduled_session_cursor(session),
            "summary": _scheduled_media_summary(session),
        }
        if progress_after == progress_before:
            break
        if media_iteration >= max_progress_iterations:
            break
        _set_scheduled_step_state(
            item_id,
            SCHEDULED_STEP_SCENE_MEDIA,
            status="running",
            cursor=_scheduled_session_cursor(session),
            data={
                "summary": _scheduled_media_summary(session),
                "missing": missing[:50],
                "progress_iteration": media_iteration,
                "max_progress_iterations": max_progress_iterations,
            },
        )
        await record_scheduled_generation_event_async(
            item_id,
            event_type="continue",
            stage="scene_media",
            status="running",
            message=(
                "Медиа-пайплайн сделал промежуточный прогресс, продолжаю без откладывания.\n"
                f"iteration={media_iteration}/{max_progress_iterations}\n"
                + "\n".join(missing[:20])
            ),
            payload={"missing": missing[:50], "iteration": media_iteration, "max_iterations": max_progress_iterations},
            notify=False,
        )
        if continue_delay_sec > 0:
            await asyncio.sleep(continue_delay_sec)
    if missing:
        if _scheduled_media_can_resume_from_cursor(session, missing_items):
            message = (
                "Медиа-пайплайн остановился на промежуточном шаге и будет продолжен с текущего cursor:\n"
                + "\n".join(missing[:20])
            )
            retry_at = _defer_scheduled_generation_item_retry(
                item_id,
                message=message,
                delay_minutes=SCHEDULED_MEDIA_PROGRESS_RETRY_DELAY_MINUTES,
            )
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_SCENE_MEDIA,
                status="retry_later",
                cursor=_scheduled_session_cursor(session),
                data={
                    "summary": _scheduled_media_summary(session),
                    "missing": missing[:50],
                    "retry_not_before": retry_at.isoformat(),
                },
                error_text=message,
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="retry_later",
                stage="scene_media",
                status=SCHEDULED_STATUS_PENDING,
                message=f"{message}\nretry_not_before={retry_at.isoformat()}",
                payload={"missing": missing[:50], "retry_not_before": retry_at.isoformat()},
            )
            raise ScheduledGenerationRetryLater(message)
        _set_scheduled_step_state(
            item_id,
            SCHEDULED_STEP_SCENE_MEDIA,
            status="failed",
            cursor=_scheduled_session_cursor(session),
            data={"summary": _scheduled_media_summary(session), "missing": missing[:50]},
            error_text="; ".join(missing[:10]),
        )
        raise ScheduledGenerationNeedsReview("Медиа-пайплайн остановился до полного результата:\n" + "\n".join(missing[:20]))
    summary = _scheduled_media_summary(session)
    _set_scheduled_step_state(
        item_id,
        SCHEDULED_STEP_SCENE_MEDIA,
        status="done",
        cursor=_scheduled_session_cursor(session),
        data={"summary": summary, "generation_run_id": session.get("generation_run_id")},
    )
    await record_scheduled_generation_event_async(
        item_id,
        event_type="approved",
        stage="scene_media",
        status="done",
        message=(
            "Scene media готово: "
            f"сцен: {summary['scene_count']}, видео: {summary['video_count']}. "
            "Финальная сборка должна быть поставлена в final_build_worker."
        ),
        payload=summary,
    )
    return summary


async def process_scheduled_generation_item(item_id: uuid.UUID) -> str:
    if not claim_scheduled_generation_item(item_id):
        await record_scheduled_generation_event_async(
            item_id,
            event_type="skipped",
            stage="claim",
            status="skipped",
            message="Задача не pending или уже взята другим worker.",
            notify=False,
        )
        return "skipped"
    with SessionLocal() as db:
        item = db.get(ScheduledGenerationItem, item_id)
        if item is None:
            return "missing"
        title = item.title
        recipe = item.recipe
        comment = normalize_scheduled_generation_comment(item.comment)
        series_id = item.series_id
        item_payload = dict(item.payload_json or {})
    await record_scheduled_generation_event_async(
        item_id,
        event_type="started",
        stage="claim",
        status="running",
        message="Worker взял задачу.",
        payload={"series_id": str(series_id or "")},
    )
    try:
        if series_id:
            from .directorial_notes import direction_for_session
            theme_session = {}
            attach_series_to_session(theme_session, select_series_payload(str(series_id)))
            comment = "\n".join(filter(None, (comment, direction_for_session(theme_session))))
        recipe_text = await _resolve_scheduled_recipe_text(recipe)
        if recipe_text != recipe:
            await record_scheduled_generation_event_async(
                item_id,
                event_type="source_resolved",
                stage="recipe",
                status="done",
                message="Рецепт из ссылки извлечен и будет использован вместо URL.",
                payload={"source_url": _extract_first_http_url(recipe), "recipe_chars": len(recipe_text)},
            )
        generation_mode = normalize_generation_mode(item_payload.get(GENERATION_MODE_SESSION_KEY))
        viral_recipe_text = _recipe_text_for_viral_plan(recipe_text, comment)
        viral_done = _scheduled_step_done(item_payload, SCHEDULED_STEP_VIRAL_PLAN)
        if generation_mode == "silence":
            viral_brief = {}
            await record_scheduled_generation_event_async(
                item_id,
                event_type="skipped",
                stage="viral_plan",
                status="skipped",
                message="Silence mode: viral plan пропущен, сразу идём к сценам и production prompts.",
                payload={"generation_mode": generation_mode},
                notify=False,
            )
        elif viral_done and isinstance(viral_done.get("viral_concept_brief"), dict):
            viral_brief = dict(viral_done["viral_concept_brief"])
            await record_scheduled_generation_event_async(
                item_id,
                event_type="resume",
                stage="viral_plan",
                status="done",
                message="Viral plan уже был готов, продолжаю со следующего шага.",
                payload={"viral_concept_brief": viral_brief},
                notify=False,
            )
        else:
            _set_scheduled_step_state(item_id, SCHEDULED_STEP_VIRAL_PLAN, status="running")
            await record_scheduled_generation_event_async(
                item_id,
                event_type="attempt",
                stage="viral_plan",
                status="started",
                message="Запускаю генерацию viral plan.",
                payload={"has_comment": bool(comment)},
            )
            viral_brief = await generate_viral_concept_brief(title, full_recipe_text=viral_recipe_text)
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_VIRAL_PLAN,
                status="done",
                data={"viral_concept_brief": viral_brief},
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="approved",
                stage="viral_plan",
                status="done",
                message="Viral plan готов.\n\n" + format_viral_concept_brief(viral_brief),
                payload={"viral_concept_brief": viral_brief},
            )

        async def _on_scenes_attempt_failure(payload_cb: dict[str, Any]) -> None:
            await record_scheduled_generation_event_async(
                item_id,
                event_type="rejected",
                stage="scenes_plan",
                status="attempt_failed",
                message="Сценарный план не прошел техническую проверку.",
                payload=payload_cb,
            )

        scenes_done = _scheduled_step_done(item_payload, SCHEDULED_STEP_SCENES_PLAN)
        if scenes_done and isinstance(scenes_done.get("scenes_plan"), list):
            scenes_plan = list(scenes_done["scenes_plan"])
            await record_scheduled_generation_event_async(
                item_id,
                event_type="resume",
                stage="scenes_plan",
                status="done",
                message=f"Scenes plan уже был готов, сцен: {len(scenes_plan)}. Продолжаю.",
                payload={"scene_count": len(scenes_plan)},
                notify=False,
            )
        else:
            _set_scheduled_step_state(item_id, SCHEDULED_STEP_SCENES_PLAN, status="running")
            await record_scheduled_generation_event_async(
                item_id,
                event_type="attempt",
                stage="scenes_plan",
                status="started",
                message="Запускаю генерацию scenes plan.",
            )
            scenes = await generate_scenes_plan(
                title,
                full_recipe_text=recipe_text,
                viral_concept_brief=viral_brief,
                generation_comment=comment,
                on_attempt_failure=_on_scenes_attempt_failure,
                generation_mode=normalize_generation_mode(item_payload.get(GENERATION_MODE_SESSION_KEY)),
            )
            scenes_plan = scenes.get("json_plan") if isinstance(scenes, dict) else None
            if not isinstance(scenes_plan, list) or not scenes_plan:
                raise RuntimeError("Scenes plan is empty.")
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_SCENES_PLAN,
                status="done",
                data={
                    "scenes_plan": scenes_plan,
                    "regeneration_events": scenes.get("regeneration_events") if isinstance(scenes, dict) else [],
                },
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="approved",
                stage="scenes_plan",
                status="done",
                message=f"Scenes plan готов, сцен: {len(scenes_plan)}.\n\n" + build_human_readable_plan(scenes_plan),
                payload={
                    "scene_count": len(scenes_plan),
                    "regeneration_events": scenes.get("regeneration_events") if isinstance(scenes, dict) else [],
                },
            )
        production_done = _scheduled_step_done(item_payload, SCHEDULED_STEP_PRODUCTION_PROMPTS)
        if (
            production_done
            and isinstance(production_done.get("production"), dict)
            and _production_has_required_media_prompts(dict(production_done["production"]))
        ):
            production = dict(production_done["production"])
            plan = production.get("json_plan")
            if not isinstance(plan, list) or not plan:
                raise RuntimeError("Stored production prompt plan is empty.")
            await record_scheduled_generation_event_async(
                item_id,
                event_type="resume",
                stage="production_prompts",
                status="done",
                message=f"Production prompts уже были готовы, сцен: {len(plan)}. Продолжаю.",
                payload={"scene_count": len(plan)},
                notify=False,
            )
        else:
            if production_done:
                await record_scheduled_generation_event_async(
                    item_id,
                    event_type="resume",
                    stage="production_prompts",
                    status="stale",
                    message="Stored production prompts неполные, пересоздаю production prompts.",
                    notify=False,
                )
            _set_scheduled_step_state(item_id, SCHEDULED_STEP_PRODUCTION_PROMPTS, status="running")
            await record_scheduled_generation_event_async(
                item_id,
                event_type="attempt",
                stage="production_prompts",
                status="started",
                message="Запускаю генерацию production prompts.",
            )
            production = await generate_full_production(
                scenes_plan,
                viral_concept_brief=viral_brief,
                generation_comment=comment,
                dish_visual_brief=build_dish_visual_brief(title, recipe_text),
                generation_mode=normalize_generation_mode(item_payload.get(GENERATION_MODE_SESSION_KEY)),
                target_languages=target_languages_for_generation_mode(
                    item_payload.get(GENERATION_MODE_SESSION_KEY),
                    ["ru", "en"],
                ),
            )
            plan = production.get("json_plan") if isinstance(production, dict) else None
            if not isinstance(plan, list) or not plan:
                raise RuntimeError("Production prompt plan is empty.")
            _set_scheduled_step_state(
                item_id,
                SCHEDULED_STEP_PRODUCTION_PROMPTS,
                status="done",
                data={"production": production, "scene_count": len(plan)},
            )
            await record_scheduled_generation_event_async(
                item_id,
                event_type="approved",
                stage="production_prompts",
                status="done",
                message=f"Production prompts готовы, сцен: {len(plan)}.\n\n" + format_production_bundle_for_approval(plan),
                payload={"scene_count": len(plan)},
            )
        with SessionLocal() as db:
            fresh = db.get(ScheduledGenerationItem, item_id)
            if fresh is None:
                raise RuntimeError("Scheduled item disappeared while processing.")
            session = _scheduled_session_for_item(fresh, recipe_text, production, scenes_plan, viral_brief)
            _apply_scheduled_media_resume_state(session, fresh, dict(fresh.payload_json or {}))
            if fresh.series_id:
                series_payload = select_series_payload(str(fresh.series_id))
                attach_series_to_session(session, series_payload)
                await record_scheduled_generation_event_async(
                    item_id,
                    event_type="series_attached",
                    stage="session",
                    status="done",
                    message="Серия добавлена в session.",
                    payload={"series": series_payload.as_session_dict() if series_payload else None},
                    notify=bool(series_payload),
                )
        media_summary = await _run_scheduled_media_pipeline(item_id, session)
        run_id, _scene_map = ensure_generation_run_for_session(session)
        save_generation_draft(_admin_chat_id(), session, title=f"Night draft: {title}")
        _set_item_status(
            item_id,
            SCHEDULED_STATUS_DRAFT_READY,
            run_id=run_id,
            payload={
                "draft_stage": "media_ready",
                "message": "Headless scheduled draft has generated scene media and queued final build.",
                "media_summary": media_summary,
            },
        )
        await record_scheduled_generation_event_async(
            item_id,
            event_type="draft_ready",
            stage="draft",
            status=SCHEDULED_STATUS_DRAFT_READY,
            message=(
                "Черновик готов после media pipeline. Автопубликация не создавалась.\n"
                f"Сцен: {media_summary.get('scene_count')}, видео: {media_summary.get('video_count')}.\n"
                "Финальные master mp4 собирает final_build_worker; в Telegram он отправит мини-копии при превышении лимита Bot API."
            ),
            run_id=run_id,
            payload=media_summary,
        )
        return SCHEDULED_STATUS_DRAFT_READY
    except ScheduledGenerationRetryLater as exc:
        try:
            if "session" in locals() and isinstance(session, dict):
                save_generation_draft(_admin_chat_id(), session, title=f"Retry later: {title}")
        except Exception:
            pass
        return SCHEDULED_STATUS_PENDING
    except Exception as exc:
        try:
            if "session" in locals() and isinstance(session, dict):
                save_generation_draft(_admin_chat_id(), session, title=f"Needs review: {title}")
        except Exception:
            pass
        if _is_retryable_scheduled_network_error(exc):
            message = f"Временный сетевой сбой runner-а; задачу продолжу позже: {type(exc).__name__}: {exc}"
            retry_at = _defer_scheduled_generation_item_retry(
                item_id,
                message=message,
                delay_minutes=SCHEDULED_MEDIA_PROGRESS_RETRY_DELAY_MINUTES,
            )
            if "session" in locals() and isinstance(session, dict) and session.get("state") == "waiting_scene_media":
                _set_scheduled_step_state(
                    item_id,
                    SCHEDULED_STEP_SCENE_MEDIA,
                    status="retry_later",
                    cursor=_scheduled_session_cursor(session),
                    data={
                        "summary": _scheduled_media_summary(session),
                        "retry_not_before": retry_at.isoformat(),
                        "transient_error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    error_text=message,
                )
            record_scheduled_generation_event(
                item_id,
                event_type="retry_later",
                stage="runner",
                status=SCHEDULED_STATUS_PENDING,
                message=f"{message}\nretry_not_before={retry_at.isoformat()}",
                payload={"retry_not_before": retry_at.isoformat(), "error_type": type(exc).__name__},
                notify=False,
            )
            return SCHEDULED_STATUS_PENDING
        _set_item_status(item_id, SCHEDULED_STATUS_FAILED_NEEDS_REVIEW, error_text=str(exc))
        await record_scheduled_generation_event_async(
            item_id,
            event_type="failed_needs_review",
            stage="runner",
            status=SCHEDULED_STATUS_FAILED_NEEDS_REVIEW,
            message=str(exc),
        )
        return SCHEDULED_STATUS_FAILED_NEEDS_REVIEW


def process_due_scheduled_generations(*, limit: int = 0) -> list[tuple[str, str]]:
    rows = due_scheduled_generation_items(limit=limit)
    results: list[tuple[str, str]] = []
    for row in rows:
        status = asyncio.run(process_scheduled_generation_item(row.id))
        results.append((str(row.id), status))
    return results


def process_one_scheduled_generation(item_id: uuid.UUID | str) -> str:
    return asyncio.run(process_scheduled_generation_item(uuid.UUID(str(item_id))))


def mark_scheduled_generation_item_pending_now(item_id: uuid.UUID | str) -> str:
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, uuid.UUID(str(item_id)))
        if row is None:
            return "missing"
        row.status = SCHEDULED_STATUS_PENDING
        row.run_at = datetime.now(timezone.utc)
        row.error_text = None
        row.claimed_at = None
        row.started_at = None
        row.finished_at = None
        payload = dict(row.payload_json or {})
        payload.pop(SCHEDULED_RETRY_NOT_BEFORE_KEY, None)
        row.payload_json = payload
        db.commit()
        record_scheduled_generation_event(
            row.id,
            event_type="queued_now",
            stage="admin",
            status=SCHEDULED_STATUS_PENDING,
            message="Пользователь запустил задачу из админки.",
        )
        return SCHEDULED_STATUS_PENDING


def _queue_with_celery(item_id: uuid.UUID) -> QueueResult | None:
    try:
        from pipeline_admin.tasks import process_scheduled_generation_item_task

        app = getattr(process_scheduled_generation_item_task, "app", None)
        inspector = app.control.inspect(timeout=1) if app is not None else None
        ping = inspector.ping() if inspector is not None else None
        if not ping:
            raise RuntimeError("No live Celery worker replied to ping.")
        async_result = process_scheduled_generation_item_task.delay(str(item_id))
        return QueueResult(status="queued", backend="celery", detail=str(getattr(async_result, "id", "") or ""))
    except Exception as exc:
        return QueueResult(status="fallback", backend="celery_unavailable", detail=str(exc))


def _queue_with_subprocess(item_id: uuid.UUID) -> QueueResult:
    SCHEDULED_WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    paths = [str(_project_root()), str(_project_root() / "admin_panel")]
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    with SCHEDULED_WORKER_LOG.open("ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(_admin_manage_path()),
                "process_scheduled_generations",
                "--once",
                "--item-id",
                str(item_id),
            ],
            cwd=str(_project_root()),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return QueueResult(status="queued", backend="subprocess", detail=f"pid={proc.pid}")


def queue_scheduled_generation_item_now(item_id: uuid.UUID | str) -> QueueResult:
    item_uuid = uuid.UUID(str(item_id))
    status = mark_scheduled_generation_item_pending_now(item_uuid)
    if status == "missing":
        return QueueResult(status="missing", backend="none")
    celery_result = _queue_with_celery(item_uuid)
    if celery_result is not None and celery_result.backend == "celery" and celery_result.status == "queued":
        record_scheduled_generation_event(
            item_uuid,
            event_type="enqueued",
            stage="celery",
            status="queued",
            message="Задача поставлена в Celery.",
            payload={"task_id": celery_result.detail},
            notify=False,
        )
        return celery_result
    fallback = _queue_with_subprocess(item_uuid)
    record_scheduled_generation_event(
        item_uuid,
        event_type="enqueued",
        stage="subprocess",
        status="queued",
        message="Задача поставлена в fallback subprocess worker.",
        payload={"detail": fallback.detail, "celery": celery_result.detail if celery_result else ""},
        notify=False,
    )
    if celery_result is not None and celery_result.detail:
        return QueueResult(status=fallback.status, backend=fallback.backend, detail=f"{fallback.detail}; celery={celery_result.detail}")
    return fallback


def run_scheduled_generation_item_now(item_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        row = db.get(ScheduledGenerationItem, item_id)
        if row is None:
            return "missing"
        row.status = SCHEDULED_STATUS_PENDING
        row.run_at = datetime.now(timezone.utc)
        row.error_text = None
        row.claimed_at = None
        row.started_at = None
        row.finished_at = None
        db.commit()
    return asyncio.run(process_scheduled_generation_item(item_id))
