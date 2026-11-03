"""Russian-only, project-isolated Telegram workflow for guest users."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import uuid
from asyncio import sleep as _event_loop_yield
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .db import SessionLocal
from .final_build_queue import queue_final_builds_for_session
from .guest_access import BotIdentity
from .media_file_store import (
    image_file_extension,
    image_mime_type,
    read_media_uri_bytes,
    store_project_artifact_media,
)
from .models import (
    Character,
    GenerationArtifact,
    GenerationDraft,
    GenerationRun,
    Project,
    ProjectCharacter,
    Scene,
    SceneImageCandidate,
    SceneVideoCandidate,
)
from .quiet_mode import is_quiet_mode_enabled, record_automatic_quiet_mode_generation
from .project_profiles import DISCIPLINE_PROJECT_SLUG, DISCIPLINE_VOICE_ID
from .generation_modes import GENERATION_MODE_SESSION_KEY, GENERATION_MODE_VISUALIZATION
from .visualization_generation import STATE_WAITING_VISUALIZATION_MEDIA
from .state import SESSIONS, save_loaded_session_if_possible
from .session_store import load_guest_run_snapshot, load_session_if_possible
from .syntax_fallback_output import video_bytes_from_fallback_result
from .syntax_video_fallback import try_generate_via_syntax
from .video_frame_extraction import extract_last_video_frame
from .vertex_images import generate_image_vertex


logger = logging.getLogger("bot.guest")
_QUIET_FAILURE_TASKS: set[asyncio.Task[Any]] = set()
_GUEST_SYNTAX_TASKS: dict[str, asyncio.Task[Any]] = {}
GUEST_VIDEO_SEND_TIMEOUT_SEC = 600.0
GUEST_SYNTAX_TIMEOUT_SEC_DEFAULT = 2 * 60 * 60

GUEST_IMAGE_DAILY_LIMIT = 7
GUEST_IMAGE_QUOTA_TIMEZONE = ZoneInfo("Europe/Moscow")
GUEST_IMAGE_QUOTA_ARTIFACT_TYPE = "guest_ai_image_generation"
GUEST_IMAGE_QUOTA_EXHAUSTED_MESSAGE = (
    "Дневной лимит генерации изображений исчерпан. "
    "До обновления лимита можно создавать видео только из собственных изображений — "
    "загрузите готовый стартовый кадр."
)


def _record_automatic_quiet_mode_generation_safely() -> None:
    """Accounting must never turn a successful media generation into a failure."""
    try:
        record_automatic_quiet_mode_generation(session_factory=SessionLocal)
    except Exception:
        logger.exception("guest.quiet_mode_generation_accounting_failed")

MENU_ADD_CHARACTER = "Добавить персонажа"
MENU_IMAGE = "Генерация изображения"
MENU_VIDEO = "Генерация видео"
MENU_CARTOON = "Генерация мультфильма"
MENU_GENERATIONS = "Мои генерации"
MENU_PRIMARY_CHARACTER = "Основной персонаж"
MENU_CREATE_STORY = "Создать ролик-историю"
MENU_CREATE_VISUALIZATION = "Создать визуализацию"
MENU_SAVE_DRAFT = "Сохранить черновик"
MENU_START_OVER = "В начало"
BUTTON_MENU = "В меню"
BUTTON_DONE = "Готово"
BUTTON_OK = "ОК"
BUTTON_REGENERATE = "ПЕРЕГЕНЕРИРОВАТЬ"
BUTTON_NEW_PROMPT = "Другой промпт"
BUTTON_CONTINUE_SCENE = "Продолжи сцену"
BUTTON_GENERATE_IMAGE = "Сгенерировать изображение"
BUTTON_WITHOUT_CHARACTERS = "Без персонажей"
BUTTON_RETURN_TO_SELECTION = "Вернуться к выбору"
BUTTON_BACK_TO_IMAGE = "Вернуться к генерации изображения"
BUTTON_CHANGE_CHARACTERS = "Изменить персонажей"
BUTTON_FIX_SCENES = "Исправить сцены"
BUTTON_FIX_ANOTHER_SCENE = "Исправить ещё сцену"
BUTTON_ADD_SCENE = "Добавить сцену"
BUTTON_REBUILD_CARTOON = "Пересобрать мультфильм"
BUTTON_FROM_IMAGE = "С изображения"
BUTTON_FROM_VIDEO = "С видео"
LEGACY_BUTTON_FEEDBACK = "Фидбэк"
BUTTON_RETRY = "Повторить"
BUTTON_RETRY_DELIVERY = "Повторить отправку"
BUTTON_HISTORY_PREVIOUS = "Назад"
BUTTON_HISTORY_NEXT = "Дальше"
BUTTON_FORMAT_VERTICAL = "Вертикальный 9:16"
BUTTON_FORMAT_HORIZONTAL = "Горизонтальный 16:9"

STATE_MENU = "guest_menu"
STATE_VIDEO_FORMAT = "guest_video_format"
STATE_CHARACTER_METHOD = "guest_character_method"
STATE_CHARACTER_NAME = "guest_character_name"
STATE_CHARACTER_SHORT_DESCRIPTION = "guest_character_short_description"
STATE_CHARACTER_VOICE = "guest_character_voice"
STATE_CHARACTER_UPLOAD = "guest_character_upload"
STATE_CHARACTER_UPLOAD_APPROVAL = "guest_character_upload_approval"
STATE_CHARACTER_APPEARANCE = "guest_character_appearance"
STATE_CHARACTER_IMAGE_APPROVAL = "guest_character_image_approval"
STATE_CHARACTER_FEEDBACK = "guest_character_feedback"
STATE_IMAGE_CHARACTERS = "guest_image_characters"
STATE_IMAGE_DESCRIPTION = "guest_image_description"
STATE_IMAGE_APPROVAL = "guest_image_approval"
STATE_IMAGE_FEEDBACK = "guest_image_feedback"
STATE_VIDEO_CHARACTERS = "guest_video_characters"
STATE_VIDEO_START_IMAGE = "guest_video_start_image"
STATE_VIDEO_DESCRIPTION = "guest_video_description"
STATE_VIDEO_APPROVAL = "guest_video_approval"
STATE_VIDEO_FEEDBACK = "guest_video_feedback"
STATE_CARTOON_CHARACTERS = "guest_cartoon_characters"
STATE_CARTOON_EMPTY_CHARACTERS_CONFIRM = "guest_cartoon_empty_characters_confirm"
STATE_CARTOON_DESCRIPTION = "guest_cartoon_description"
STATE_CARTOON_FRAME_APPROVAL = "guest_cartoon_frame_approval"
STATE_CARTOON_FRAME_FEEDBACK = "guest_cartoon_frame_feedback"
STATE_CARTOON_CONTINUATION_FRAME_APPROVAL = "guest_cartoon_continuation_frame_approval"
STATE_CARTOON_VIDEO_DESCRIPTION = "guest_cartoon_video_description"
STATE_CARTOON_VIDEO_APPROVAL = "guest_cartoon_video_approval"
STATE_CARTOON_VIDEO_FEEDBACK = "guest_cartoon_video_feedback"
STATE_CARTOON_SCENE_DONE = "guest_cartoon_scene_done"
STATE_CARTOON_FINISH_CONFIRM = "guest_cartoon_finish_confirm"
STATE_CARTOON_FINAL_REVIEW = "guest_cartoon_final_review"
STATE_CARTOON_EDIT_SCENE_SELECT = "guest_cartoon_edit_scene_select"
STATE_CARTOON_EDIT_STAGE_SELECT = "guest_cartoon_edit_stage_select"
STATE_CARTOON_EDIT_SCENE_DONE = "guest_cartoon_edit_scene_done"
STATE_CARTOON_INSERT_POSITION = "guest_cartoon_insert_position"
STATE_IMAGE_ERROR = "guest_image_error"
STATE_SYNTAX_PENDING = "guest_syntax_pending"
STATE_SYNTAX_ERROR = "guest_syntax_error"
STATE_VIDEO_DELIVERY_ERROR = "guest_video_delivery_error"
STATE_GENERATION_HISTORY = "guest_generation_history"
STATE_PRIMARY_CHARACTER = "guest_primary_character"

GUEST_HISTORY_PAGE_SIZE = 6

GUEST_STATES = {
    value
    for key, value in globals().items()
    if key.startswith("STATE_") and isinstance(value, str) and value.startswith("guest_")
}

_IMAGE_CHARACTER_EDIT_STATES = {
    STATE_IMAGE_DESCRIPTION,
    STATE_IMAGE_APPROVAL,
    STATE_IMAGE_FEEDBACK,
}
_VIDEO_CHARACTER_EDIT_STATES = {
    STATE_VIDEO_START_IMAGE,
    STATE_VIDEO_DESCRIPTION,
    STATE_VIDEO_APPROVAL,
    STATE_VIDEO_FEEDBACK,
}
_CARTOON_CHARACTER_EDIT_STATES = {
    STATE_CARTOON_DESCRIPTION,
    STATE_CARTOON_FRAME_APPROVAL,
    STATE_CARTOON_FRAME_FEEDBACK,
    STATE_CARTOON_CONTINUATION_FRAME_APPROVAL,
    STATE_CARTOON_VIDEO_DESCRIPTION,
    STATE_CARTOON_VIDEO_APPROVAL,
    STATE_CARTOON_VIDEO_FEEDBACK,
}


@dataclass(frozen=True, slots=True)
class GuestCharacter:
    id: uuid.UUID
    name: str
    short_description: str
    description: str
    voice_description: str
    asset_uri: str


@dataclass(frozen=True, slots=True)
class GuestRunHistoryItem:
    run_id: uuid.UUID
    workflow_type: str
    created_at: datetime
    day_number: int

    @property
    def label(self) -> str:
        workflow_labels = {
            "image": "Изображение",
            "video": "Видео",
            "cartoon": "Мультфильм",
        }
        return (
            f"{self.created_at:%d.%m.%Y} №{self.day_number} — "
            f"{workflow_labels.get(self.workflow_type, 'Генерация')}"
        )


def _can_rebuild_cartoon(session: dict[str, Any] | None) -> bool:
    if not isinstance(session, dict) or not str(session.get("generation_run_id") or "").strip():
        return False
    data = session.get("guest_data")
    return isinstance(data, dict) and any(isinstance(item, dict) for item in data.get("approved_scenes") or [])


def guest_main_menu_markup(session: dict[str, Any] | None = None) -> ReplyKeyboardMarkup:
    if isinstance(session, dict) and session.get("project_workspace_mode") is True:
        return ReplyKeyboardMarkup(
            [
                [MENU_ADD_CHARACTER],
                [MENU_PRIMARY_CHARACTER],
                [MENU_CREATE_STORY],
                [MENU_CREATE_VISUALIZATION],
                [MENU_SAVE_DRAFT],
                [MENU_START_OVER],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
        )
    rows = [
        [MENU_ADD_CHARACTER],
        [MENU_IMAGE],
        [MENU_VIDEO],
        [MENU_CARTOON],
        [MENU_GENERATIONS],
    ]
    if _can_rebuild_cartoon(session):
        rows.append([BUTTON_REBUILD_CARTOON])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _markup(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    if not any(BUTTON_MENU in row for row in rows):
        rows = [*rows, [BUTTON_MENU]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _video_format_markup() -> ReplyKeyboardMarkup:
    return _markup([[BUTTON_FORMAT_VERTICAL], [BUTTON_FORMAT_HORIZONTAL]])


def _approval_markup() -> ReplyKeyboardMarkup:
    return _markup([[BUTTON_OK], [BUTTON_REGENERATE], [BUTTON_NEW_PROMPT]])


def guest_final_review_markup() -> ReplyKeyboardMarkup:
    return _markup([[BUTTON_OK], [BUTTON_FIX_SCENES], [BUTTON_ADD_SCENE], [BUTTON_REBUILD_CARTOON]])


def _cartoon_scene_selection_markup(session: dict[str, Any]) -> ReplyKeyboardMarkup:
    scenes = [item for item in _guest_data(session).get("approved_scenes") or [] if isinstance(item, dict)]
    return _markup([[f"Сцена {index}"] for index in range(1, len(scenes) + 1)])


def _cartoon_insert_position_markup(session: dict[str, Any]) -> ReplyKeyboardMarkup:
    scenes = [item for item in _guest_data(session).get("approved_scenes") or [] if isinstance(item, dict)]
    return _markup([[f"После сцены {index}"] for index in range(1, len(scenes) + 1)])


def _guest_data(session: dict[str, Any]) -> dict[str, Any]:
    value = session.get("guest_data")
    if not isinstance(value, dict):
        value = {}
        session["guest_data"] = value
    return value


def _guest_aspect_ratio(session: dict[str, Any]) -> str:
    value = str(_guest_data(session).get("aspect_ratio") or "").strip()
    return value if value in {"9:16", "16:9"} else "9:16"


def _guest_aspect_label(aspect_ratio: str) -> str:
    return "горизонтальное изображение 16:9" if aspect_ratio == "16:9" else "вертикальное изображение 9:16"


def _stored_guest_aspect_ratio(video_row: SceneVideoCandidate | None, scene: Scene | None = None) -> str:
    params = (
        video_row.generation_params_json
        if video_row is not None and isinstance(video_row.generation_params_json, dict)
        else {}
    )
    value = str(params.get("aspect_ratio") or "").strip()
    if value in {"9:16", "16:9"}:
        return value
    meta = scene.prompt_meta_json if scene is not None and isinstance(scene.prompt_meta_json, dict) else {}
    value = str(meta.get("guest_aspect_ratio") or "").strip()
    return value if value in {"9:16", "16:9"} else "9:16"


def _generation_history_items(session: dict[str, Any]) -> list[GuestRunHistoryItem]:
    project_id = _project_uuid(session)
    current_run_id = str(session.get("generation_run_id") or "")
    with SessionLocal() as db:
        runs = (
            db.execute(
                select(GenerationRun)
                .join(Project, Project.id == GenerationRun.project_id)
                .where(
                    GenerationRun.project_id == project_id,
                    Project.owner_user_id == uuid.UUID(str(session["bot_user_id"])),
                    GenerationRun.workflow_type.in_(["image", "video", "cartoon"]),
                )
                .order_by(GenerationRun.created_at.asc(), GenerationRun.id.asc())
            )
            .scalars()
            .all()
        )
        visible_runs: list[GenerationRun] = []
        for run in runs:
            if str(run.human_title or "").startswith("Персонаж:"):
                continue
            has_media = db.execute(
                select(Scene.id)
                .outerjoin(SceneImageCandidate, SceneImageCandidate.scene_id == Scene.id)
                .outerjoin(SceneVideoCandidate, SceneVideoCandidate.scene_id == Scene.id)
                .where(
                    Scene.run_id == run.id,
                    (SceneImageCandidate.id.is_not(None)) | (SceneVideoCandidate.id.is_not(None)),
                )
                .limit(1)
            ).scalar_one_or_none()
            if has_media is not None or str(run.id) == current_run_id:
                visible_runs.append(run)

    counters: dict[object, int] = {}
    items: list[GuestRunHistoryItem] = []
    for run in visible_runs:
        created_at = run.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        local_created_at = created_at.astimezone(GUEST_IMAGE_QUOTA_TIMEZONE)
        day = local_created_at.date()
        counters[day] = counters.get(day, 0) + 1
        items.append(
            GuestRunHistoryItem(
                run_id=run.id,
                workflow_type=str(run.workflow_type or ""),
                created_at=local_created_at,
                day_number=counters[day],
            )
        )
    return list(reversed(items))


def _generation_history_markup(session: dict[str, Any]) -> ReplyKeyboardMarkup:
    data = _guest_data(session)
    items = _generation_history_items(session)
    try:
        page = max(0, int(data.get("history_page") or 0))
    except (TypeError, ValueError):
        page = 0
    max_page = max(0, (len(items) - 1) // GUEST_HISTORY_PAGE_SIZE)
    page = min(page, max_page)
    data["history_page"] = page
    start = page * GUEST_HISTORY_PAGE_SIZE
    page_items = items[start : start + GUEST_HISTORY_PAGE_SIZE]
    data["history_buttons"] = {item.label: str(item.run_id) for item in page_items}
    rows = [[item.label] for item in page_items]
    navigation: list[str] = []
    if page > 0:
        navigation.append(BUTTON_HISTORY_PREVIOUS)
    if page < max_page:
        navigation.append(BUTTON_HISTORY_NEXT)
    if navigation:
        rows.append(navigation)
    return _markup(rows)


def _base_session(identity: BotIdentity) -> dict[str, Any]:
    return {
        "state": STATE_MENU,
        "guest_mode": True,
        "bot_user_id": str(identity.user_id),
        "project_id": str(identity.project_id or ""),
        "user_role": "guest",
        "guest_data": {},
    }


def _workspace_session(identity: BotIdentity, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    source = existing if isinstance(existing, dict) else {}
    selected_project_key = str(source.get("selected_project_key") or DISCIPLINE_PROJECT_SLUG)
    project_voice_id = (
        DISCIPLINE_VOICE_ID
        if selected_project_key == DISCIPLINE_PROJECT_SLUG
        else str(source.get("project_voice_id") or "")
    )
    return {
        **_base_session(identity),
        "project_workspace_mode": True,
        "user_role": "admin",
        "selected_project_key": selected_project_key,
        "force_story_mode": True,
        "project_voice_id": project_voice_id,
    }


def _session_matches_identity(session: object, identity: BotIdentity) -> bool:
    return bool(
        isinstance(session, dict)
        and session.get("guest_mode") is True
        and str(session.get("bot_user_id") or "") == str(identity.user_id)
        and str(session.get("project_id") or "") == str(identity.project_id or "")
    )


def _selected_character_ids_from_scene(scene: Scene) -> list[str]:
    meta = scene.prompt_meta_json if isinstance(scene.prompt_meta_json, dict) else {}
    return [str(value) for value in meta.get("guest_selected_character_ids") or []]


def _image_candidate_payload(row: SceneImageCandidate | None) -> tuple[str, str]:
    if row is None:
        return "", ""
    return str(row.candidate_id or ""), str(row.media_uri or "")


def _video_candidate_user_prompt(row: SceneVideoCandidate | None) -> str:
    if row is None:
        return ""
    params = row.generation_params_json if isinstance(row.generation_params_json, dict) else {}
    explicit = str(params.get("user_prompt") or "").strip()
    stored = str((row.video_prompt_by_lang or {}).get("ru") or "").strip()
    return explicit or _user_prompt_from_stored_video_prompt(stored)


def _rebuild_guest_session_from_run(identity: BotIdentity, run_id: str) -> tuple[dict[str, Any], str, str]:
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Генерация не найдена.") from error
    with SessionLocal() as db:
        run = (
            db.execute(
                select(GenerationRun)
                .join(Project, Project.id == GenerationRun.project_id)
                .where(
                    GenerationRun.id == run_uuid,
                    GenerationRun.project_id == identity.project_id,
                    Project.owner_user_id == identity.user_id,
                )
            )
            .scalar_one_or_none()
        )
        if run is None:
            raise PermissionError("Эта генерация недоступна.")
        workflow = str(run.workflow_type or "")

        saved = load_guest_run_snapshot(run_uuid)
        if isinstance(saved, dict) and saved.get("guest_mode") is True:
            saved_data = saved.get("guest_data") if isinstance(saved.get("guest_data"), dict) else {}
            has_saved_work = bool(
                saved_data.get("approved_scenes")
                or saved_data.get("current_image_candidate_id")
                or saved_data.get("current_video_candidate_id")
            )
            if has_saved_work:
                restored = _base_session(identity)
                restored.update(saved)
                restored.update(
                    {
                        "guest_mode": True,
                        "bot_user_id": str(identity.user_id),
                        "project_id": str(identity.project_id or ""),
                        "user_role": "guest",
                        "generation_run_id": str(run_uuid),
                    }
                )
                restored_data = _guest_data(restored)
                restored_data.pop("history_page", None)
                restored_data.pop("history_buttons", None)
                if str(restored.get("state") or "") in {STATE_MENU, STATE_GENERATION_HISTORY}:
                    if workflow == "cartoon" and restored_data.get("approved_scenes"):
                        restored["state"] = STATE_CARTOON_FINAL_REVIEW
                    elif workflow == "video" and restored_data.get("current_video_candidate_id"):
                        restored["state"] = STATE_VIDEO_APPROVAL
                    elif workflow == "image" and restored_data.get("current_image_candidate_id"):
                        restored["state"] = STATE_IMAGE_APPROVAL
                preview_type = ""
                preview_uri = ""
                if str(restored.get("state") or "") == STATE_IMAGE_APPROVAL:
                    preview_type = "image"
                    preview_uri = str(restored_data.get("current_image_uri") or "")
                elif str(restored.get("state") or "") == STATE_VIDEO_APPROVAL:
                    preview_type = "video"
                    preview_uri = str(restored_data.get("current_video_uri") or "")
                return restored, preview_type, preview_uri

        scenes = (
            db.execute(select(Scene).where(Scene.run_id == run_uuid).order_by(Scene.scene_order.asc()))
            .scalars()
            .all()
        )
        if not scenes:
            raise ValueError("В этой генерации нет сохранённых данных.")

        restored = _base_session(identity)
        restored["generation_run_id"] = str(run_uuid)
        data = _guest_data(restored)

        if workflow == "image":
            result = (
                db.execute(
                    select(SceneImageCandidate, Scene)
                    .join(Scene, Scene.id == SceneImageCandidate.scene_id)
                    .where(Scene.run_id == run_uuid)
                    .order_by(SceneImageCandidate.liked.desc(), SceneImageCandidate.created_at.desc())
                )
                .first()
            )
            if result is None:
                raise ValueError("В этой генерации нет сохранённого изображения.")
            image_row, scene = result
            restored["scene_db_ids_by_index"] = {0: str(scene.id)}
            data.update(
                {
                    "scene_index": 0,
                    "scene_id": str(scene.id),
                    "selected_character_ids": _selected_character_ids_from_scene(scene),
                    "image_description": str(
                        scene.start_frame_prompt_effective or scene.start_frame_prompt or scene.text or ""
                    ),
                    "current_image_candidate_id": str(image_row.candidate_id),
                    "current_image_uri": str(image_row.media_uri or ""),
                    "last_image_target": "image",
                }
            )
            restored["state"] = STATE_IMAGE_APPROVAL
            return restored, "image", str(image_row.media_uri or "")

        if workflow == "video":
            result = (
                db.execute(
                    select(SceneVideoCandidate, Scene)
                    .join(Scene, Scene.id == SceneVideoCandidate.scene_id)
                    .where(Scene.run_id == run_uuid)
                    .order_by(SceneVideoCandidate.liked.desc(), SceneVideoCandidate.created_at.desc())
                )
                .first()
            )
            if result is None:
                raise ValueError("В этой генерации нет сохранённого видео.")
            video_row, scene = result
            image_row = None
            if str(video_row.source_image_candidate_id or ""):
                image_row = db.execute(
                    select(SceneImageCandidate).where(
                        SceneImageCandidate.scene_id == scene.id,
                        SceneImageCandidate.candidate_id == video_row.source_image_candidate_id,
                    )
                ).scalar_one_or_none()
            image_id, image_uri = _image_candidate_payload(image_row)
            effective_prompt = str((video_row.video_prompt_by_lang or {}).get("ru") or "")
            restored["scene_db_ids_by_index"] = {0: str(scene.id)}
            data.update(
                {
                    "scene_index": 0,
                    "scene_id": str(scene.id),
                    "selected_character_ids": _selected_character_ids_from_scene(scene),
                    "current_image_candidate_id": image_id,
                    "current_image_uri": image_uri,
                    "current_video_candidate_id": str(video_row.candidate_id),
                    "current_video_uri": str(video_row.media_uri or ""),
                    "video_description": _video_candidate_user_prompt(video_row),
                    "effective_video_prompt": effective_prompt,
                    "last_video_target": "video",
                    "aspect_ratio": _stored_guest_aspect_ratio(video_row, scene),
                }
            )
            restored["state"] = STATE_VIDEO_APPROVAL
            return restored, "video", str(video_row.media_uri or "")

        prompt_artifacts = (
            db.execute(
                select(GenerationArtifact)
                .where(
                    GenerationArtifact.run_id == run_uuid,
                    GenerationArtifact.artifact_type == "scene_video_prompt",
                    GenerationArtifact.language == "ru",
                )
                .order_by(GenerationArtifact.created_at.asc())
            )
            .scalars()
            .all()
        )
        authoritative_scene_ids: dict[int, uuid.UUID] = {}
        for artifact in prompt_artifacts:
            payload = artifact.payload_json if isinstance(artifact.payload_json, dict) else {}
            try:
                scene_index = int(payload.get("scene_index"))
            except (TypeError, ValueError):
                continue
            if artifact.scene_id is not None:
                authoritative_scene_ids[scene_index] = artifact.scene_id

        approved_rows: list[tuple[SceneVideoCandidate, Scene]] = []
        if authoritative_scene_ids:
            for scene_index, scene_id in sorted(authoritative_scene_ids.items()):
                _ = scene_index
                result = (
                    db.execute(
                        select(SceneVideoCandidate, Scene)
                        .join(Scene, Scene.id == SceneVideoCandidate.scene_id)
                        .where(
                            Scene.id == scene_id,
                            Scene.run_id == run_uuid,
                            SceneVideoCandidate.approved_for_production.is_(True),
                        )
                        .order_by(SceneVideoCandidate.created_at.desc())
                    )
                    .first()
                )
                if result is not None:
                    approved_rows.append(result)
        else:
            approved_rows = list(
                db.execute(
                    select(SceneVideoCandidate, Scene)
                    .join(Scene, Scene.id == SceneVideoCandidate.scene_id)
                    .where(
                        Scene.run_id == run_uuid,
                        SceneVideoCandidate.approved_for_production.is_(True),
                    )
                    .order_by(Scene.scene_order.asc(), SceneVideoCandidate.created_at.desc())
                ).all()
            )
        approved_scenes: list[dict[str, Any]] = []
        scene_map: dict[int, str] = {}
        seen_scene_ids: set[uuid.UUID] = set()
        for video_row, scene in approved_rows:
            if scene.id in seen_scene_ids:
                continue
            seen_scene_ids.add(scene.id)
            index = len(approved_scenes)
            scene_map[index] = str(scene.id)
            stored_prompt = str((video_row.video_prompt_by_lang or {}).get("ru") or "")
            approved_scenes.append(
                {
                    "scene": index + 1,
                    "guest_mode": True,
                    "text": str(scene.text or ""),
                    "duration_sec": 8,
                    "aspect_ratio": _stored_guest_aspect_ratio(video_row, scene),
                    "video_uri": str(video_row.media_uri or ""),
                    "selected_character_ids": _selected_character_ids_from_scene(scene),
                    "video_prompt_by_lang": {"ru": stored_prompt},
                    "video_candidates_by_lang": {
                        "ru": [
                            {
                                "candidate_id": str(video_row.candidate_id),
                                "language": "ru",
                                "approved_for_production": True,
                                "liked": bool(video_row.liked),
                                "speed_coef": float(video_row.speed_coef or 1.0),
                            }
                        ]
                    },
                }
            )
        if approved_scenes:
            last = approved_scenes[-1]
            data.update(
                {
                    "approved_scenes": approved_scenes,
                    "scene_index": len(approved_scenes) - 1,
                    "scene_id": scene_map[len(approved_scenes) - 1],
                    "selected_character_ids": list(last.get("selected_character_ids") or []),
                    "current_video_candidate_id": str(
                        last["video_candidates_by_lang"]["ru"][0]["candidate_id"]
                    ),
                    "current_video_uri": str(last.get("video_uri") or ""),
                    "aspect_ratio": str(last.get("aspect_ratio") or "9:16"),
                }
            )
            restored["scene_db_ids_by_index"] = scene_map
            restored["state"] = STATE_CARTOON_FINAL_REVIEW
            return restored, "cartoon", ""

        raise ValueError("В этой генерации пока нет утверждённых сцен.")


async def _open_guest_run_from_history(
    update: Update,
    session: dict[str, Any],
    identity: BotIdentity,
    run_id: str,
) -> None:
    restored, preview_type, preview_uri = _rebuild_guest_session_from_run(identity, run_id)
    session.clear()
    session.update(restored)
    data = _guest_data(session)
    if preview_type == "image":
        payload = read_media_uri_bytes(preview_uri)
        if payload:
            await _send_photo(
                update,
                payload,
                "Открыла сохранённую генерацию изображения.",
                markup=guest_keyboard_for_session(session),
            )
            return
    if preview_type == "video":
        await _deliver_current_video(update, session)
        return
    if preview_type == "cartoon":
        count = len([item for item in data.get("approved_scenes") or [] if isinstance(item, dict)])
        await _reply(
            update,
            f"Открыла сохранённый мультфильм. Утверждённых сцен: {count}. Можно исправить сцены, добавить новую или пересобрать итог.",
            markup=guest_keyboard_for_session(session),
        )
        return
    await _reply(update, "Сохранённая генерация открыта.", markup=guest_keyboard_for_session(session))


def _project_uuid(session: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(session["project_id"]))


def _run_uuid(session: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(session["generation_run_id"]))


def _scene_uuid(session: dict[str, Any]) -> uuid.UUID:
    data = _guest_data(session)
    return uuid.UUID(str(data["scene_id"]))


def _guest_image_quota_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    local_now = now.astimezone(GUEST_IMAGE_QUOTA_TIMEZONE) if now is not None else datetime.now(GUEST_IMAGE_QUOTA_TIMEZONE)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=GUEST_IMAGE_QUOTA_TIMEZONE)
    return local_start.astimezone(timezone.utc), (local_start + timedelta(days=1)).astimezone(timezone.utc)


def _guest_generated_images_today(session: dict[str, Any], *, now: datetime | None = None) -> int:
    day_start, day_end = _guest_image_quota_window(now)
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count(GenerationArtifact.id))
                .join(GenerationRun, GenerationRun.id == GenerationArtifact.run_id)
                .join(Project, Project.id == GenerationRun.project_id)
                .where(
                    Project.owner_user_id == uuid.UUID(str(session["bot_user_id"])),
                    GenerationArtifact.artifact_type == GUEST_IMAGE_QUOTA_ARTIFACT_TYPE,
                    GenerationArtifact.created_at >= day_start,
                    GenerationArtifact.created_at < day_end,
                )
            ).scalar_one()
        )


def _guest_image_limit_reached(session: dict[str, Any], *, now: datetime | None = None) -> bool:
    if session.get("project_workspace_mode") is True:
        return False
    return _guest_generated_images_today(session, now=now) >= GUEST_IMAGE_DAILY_LIMIT


def _scene_index_map(session: dict[str, Any]) -> dict[int, str]:
    raw = session.get("scene_db_ids_by_index")
    if not isinstance(raw, dict):
        return {}
    return {int(key): str(value) for key, value in raw.items()}


def _list_project_characters(project_id: uuid.UUID) -> list[GuestCharacter]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Character, ProjectCharacter)
            .join(ProjectCharacter, ProjectCharacter.character_id == Character.id)
            .where(
                ProjectCharacter.project_id == project_id,
                Character.is_active.is_(True),
                ProjectCharacter.asset_uri.is_not(None),
            )
            .order_by(ProjectCharacter.sort_order.asc(), Character.created_at.asc())
        ).all()
    return [
        GuestCharacter(
            id=character.id,
            name=str(link.display_name or character.name),
            short_description=str(
                (character.meta_json if isinstance(character.meta_json, dict) else {}).get("short_description") or ""
            ),
            description=str(character.description or ""),
            voice_description=str(character.voice_description or ""),
            asset_uri=str(link.asset_uri or character.default_asset_uri or ""),
        )
        for character, link in rows
        if str(link.asset_uri or character.default_asset_uri or "").strip()
    ]


def _character_selection_markup(session: dict[str, Any]) -> ReplyKeyboardMarkup:
    data = _guest_data(session)
    characters = _list_project_characters(_project_uuid(session))
    selected = {str(value) for value in data.get("selected_character_ids") or []}
    button_map: dict[str, str] = {}
    rows: list[list[str]] = []
    for index, character in enumerate(characters, start=1):
        prefix = "✓ " if str(character.id) in selected else ""
        label = f"{prefix}{index}. {character.name}"
        button_map[label] = str(character.id)
        rows.append([label])
    data["character_buttons"] = button_map
    if str(session.get("state") or "") == STATE_CARTOON_CHARACTERS and _can_continue_cartoon_scene(session):
        rows.append([BUTTON_CONTINUE_SCENE])
    rows.append([BUTTON_DONE])
    return _markup(rows)


def _primary_character_markup(session: dict[str, Any]) -> ReplyKeyboardMarkup:
    data = _guest_data(session)
    characters = _list_project_characters(_project_uuid(session))
    with SessionLocal() as db:
        primary_ids = set(db.execute(
            select(ProjectCharacter.character_id).where(
                ProjectCharacter.project_id == _project_uuid(session),
                ProjectCharacter.is_primary.is_(True),
            )
        ).scalars().all())
    mapping: dict[str, str] = {}
    rows: list[list[str]] = []
    for index, character in enumerate(characters, start=1):
        prefix = "★ " if character.id in primary_ids else ""
        label = f"{prefix}{index}. {character.name}"
        mapping[label] = str(character.id)
        rows.append([label])
    data["primary_character_buttons"] = mapping
    return _markup(rows)


def _set_primary_character(session: dict[str, Any], character_id: str) -> str | None:
    try:
        parsed_id = uuid.UUID(str(character_id))
    except (TypeError, ValueError, AttributeError):
        return None
    with SessionLocal() as db:
        links = db.execute(
            select(ProjectCharacter).where(ProjectCharacter.project_id == _project_uuid(session))
        ).scalars().all()
        selected = next((link for link in links if link.character_id == parsed_id), None)
        if selected is None:
            return None
        selected.is_primary = True
        meta = dict(selected.meta_json or {})
        meta.update({"recurring_in_series": True, "series_role": "primary"})
        selected.meta_json = meta
        name = db.execute(select(Character.name).where(Character.id == parsed_id)).scalar_one_or_none()
        db.commit()
    return str(selected.display_name or name or "персонаж")


def _previous_approved_video_uri(session: dict[str, Any]) -> str:
    data = _guest_data(session)
    if int(data.get("scene_index") or 0) < 1:
        return ""
    cached = str(data.get("previous_video_uri") or "").strip()
    if cached:
        return cached
    scenes = [item for item in data.get("approved_scenes") or [] if isinstance(item, dict)]
    if not scenes:
        return ""
    previous = scenes[-1]
    uri = str(previous.get("video_uri") or "").strip()
    if not uri:
        candidates = ((previous.get("video_candidates_by_lang") or {}).get("ru") or [])
        candidate_id = str(candidates[0].get("candidate_id") or "").strip() if candidates else ""
        if candidate_id:
            with SessionLocal() as db:
                uri = str(
                    db.execute(
                        select(SceneVideoCandidate.media_uri)
                        .join(Scene, Scene.id == SceneVideoCandidate.scene_id)
                        .where(
                            SceneVideoCandidate.candidate_id == candidate_id,
                            Scene.run_id == _run_uuid(session),
                        )
                    ).scalar_one_or_none()
                    or ""
                ).strip()
    if uri:
        data["previous_video_uri"] = uri
    return uri


def _previous_selected_character_ids(session: dict[str, Any]) -> list[str]:
    data = _guest_data(session)
    scenes = [item for item in data.get("approved_scenes") or [] if isinstance(item, dict)]
    if not scenes:
        return []
    previous_index = len(scenes) - 1
    if data.get("inserting_scene"):
        try:
            previous_index = max(0, int(data.get("insert_scene_at") or 0) - 1)
        except (TypeError, ValueError):
            previous_index = len(scenes) - 1
    allowed = {str(item.id) for item in _list_project_characters(_project_uuid(session))}
    return [
        str(value)
        for value in scenes[previous_index].get("selected_character_ids") or []
        if str(value) in allowed
    ]


def _ensure_cartoon_character_selection(session: dict[str, Any]) -> None:
    """Recover a lost continuation selection while preserving explicit empty scenes."""
    data = _guest_data(session)
    if data.get("selected_character_ids") or data.get("allow_empty_character_selection") is True:
        return
    previous = _previous_selected_character_ids(session)
    if not previous:
        return
    data["selected_character_ids"] = previous
    data["character_selection_confirmed"] = True
    logger.warning(
        "guest.cartoon_character_selection_recovered scene_index=%s count=%s",
        data.get("scene_index"),
        len(previous),
    )


def _can_continue_cartoon_scene(session: dict[str, Any]) -> bool:
    uri = _previous_approved_video_uri(session)
    return bool(uri and read_media_uri_bytes(uri))


def _character_selection_state_for_session(session: dict[str, Any]) -> str | None:
    state = str(session.get("state") or "")
    data = _guest_data(session)
    if state in _IMAGE_CHARACTER_EDIT_STATES:
        return STATE_IMAGE_CHARACTERS
    if state in _VIDEO_CHARACTER_EDIT_STATES:
        return STATE_VIDEO_CHARACTERS
    if state in _CARTOON_CHARACTER_EDIT_STATES:
        return STATE_CARTOON_CHARACTERS
    if state == STATE_IMAGE_ERROR:
        target = str(data.get("image_retry_target") or "image")
        if target == "image":
            return STATE_IMAGE_CHARACTERS
        if target == "cartoon_frame":
            return STATE_CARTOON_CHARACTERS
    if state == STATE_SYNTAX_ERROR:
        target = str(data.get("syntax_retry_target") or "video")
        if target == "video":
            return STATE_VIDEO_CHARACTERS
        if target == "cartoon":
            return STATE_CARTOON_CHARACTERS
    return None


def guest_keyboard_for_session(session: dict[str, Any]) -> object:
    state = str(session.get("state") or "")
    if state == STATE_MENU:
        return guest_main_menu_markup(session)
    if state == STATE_VIDEO_FORMAT:
        return _video_format_markup()
    if state == STATE_GENERATION_HISTORY:
        return _generation_history_markup(session)
    if state == STATE_PRIMARY_CHARACTER:
        return _primary_character_markup(session)
    if state == STATE_CHARACTER_METHOD:
        return _markup([["Загрузить"], ["Сгенерировать"]])
    if state in {STATE_IMAGE_CHARACTERS, STATE_VIDEO_CHARACTERS, STATE_CARTOON_CHARACTERS}:
        return _character_selection_markup(session)
    if state == STATE_CHARACTER_UPLOAD_APPROVAL:
        return _markup([[BUTTON_OK], ["Загрузить снова"]])
    if state == STATE_CARTOON_EMPTY_CHARACTERS_CONFIRM:
        return _markup([[BUTTON_WITHOUT_CHARACTERS], [BUTTON_RETURN_TO_SELECTION]])
    if state == STATE_CARTOON_CONTINUATION_FRAME_APPROVAL:
        return _markup([[BUTTON_OK], [BUTTON_GENERATE_IMAGE], [BUTTON_CHANGE_CHARACTERS]])
    if state == STATE_CARTOON_FRAME_APPROVAL:
        rows = [[BUTTON_OK], [BUTTON_REGENERATE], [BUTTON_NEW_PROMPT], [BUTTON_CHANGE_CHARACTERS]]
        if _can_continue_cartoon_scene(session):
            rows.append([BUTTON_CONTINUE_SCENE])
        return _markup(rows)
    if state == STATE_CARTOON_VIDEO_APPROVAL:
        return _markup(
            [
                [BUTTON_OK],
                [BUTTON_RETRY_DELIVERY],
                [BUTTON_REGENERATE],
                [BUTTON_NEW_PROMPT],
                [BUTTON_CHANGE_CHARACTERS],
                [BUTTON_BACK_TO_IMAGE],
            ]
        )
    if state in {STATE_CARTOON_VIDEO_DESCRIPTION, STATE_CARTOON_VIDEO_FEEDBACK}:
        return _markup([[BUTTON_CHANGE_CHARACTERS], [BUTTON_BACK_TO_IMAGE]])
    if state == STATE_VIDEO_APPROVAL:
        return _markup(
            [[BUTTON_OK], [BUTTON_RETRY_DELIVERY], [BUTTON_REGENERATE], [BUTTON_NEW_PROMPT], [BUTTON_CHANGE_CHARACTERS]]
        )
    if state == STATE_IMAGE_APPROVAL:
        return _markup([[BUTTON_OK], [BUTTON_REGENERATE], [BUTTON_NEW_PROMPT], [BUTTON_CHANGE_CHARACTERS]])
    if state == STATE_CHARACTER_IMAGE_APPROVAL:
        return _approval_markup()
    if state in {
        STATE_IMAGE_DESCRIPTION,
        STATE_IMAGE_FEEDBACK,
        STATE_VIDEO_START_IMAGE,
        STATE_VIDEO_DESCRIPTION,
        STATE_VIDEO_FEEDBACK,
        STATE_CARTOON_DESCRIPTION,
        STATE_CARTOON_FRAME_FEEDBACK,
    }:
        return _markup([[BUTTON_CHANGE_CHARACTERS]])
    if state == STATE_CARTOON_SCENE_DONE:
        return _markup([["Следующая сцена"], [BUTTON_DONE]])
    if state == STATE_CARTOON_FINISH_CONFIRM:
        return _markup([["Да, склеить"], ["Нет, продолжить"]])
    if state == STATE_CARTOON_FINAL_REVIEW:
        return guest_final_review_markup()
    if state == STATE_CARTOON_EDIT_SCENE_SELECT:
        return _cartoon_scene_selection_markup(session)
    if state == STATE_CARTOON_EDIT_STAGE_SELECT:
        return _markup([[BUTTON_FROM_IMAGE], [BUTTON_FROM_VIDEO]])
    if state == STATE_CARTOON_EDIT_SCENE_DONE:
        return _markup([[BUTTON_FIX_ANOTHER_SCENE], [BUTTON_ADD_SCENE], [BUTTON_DONE]])
    if state == STATE_CARTOON_INSERT_POSITION:
        return _cartoon_insert_position_markup(session)
    if state == STATE_SYNTAX_PENDING:
        return _markup([])
    if state == STATE_SYNTAX_ERROR and str(_guest_data(session).get("syntax_retry_target") or "") == "cartoon":
        return _markup([[BUTTON_RETRY], [BUTTON_CHANGE_CHARACTERS], [BUTTON_BACK_TO_IMAGE]])
    if state == STATE_VIDEO_DELIVERY_ERROR:
        return _markup([[BUTTON_RETRY_DELIVERY]])
    if state in {STATE_IMAGE_ERROR, STATE_SYNTAX_ERROR}:
        rows = [[BUTTON_RETRY]]
        if _character_selection_state_for_session(session) is not None:
            rows.append([BUTTON_CHANGE_CHARACTERS])
        return _markup(rows)
    return _markup([])


async def _reply(update: Update, text: str, *, markup: object | None = None) -> None:
    if update.message is None:
        return
    kwargs = {} if markup is None else {"reply_markup": markup}
    await update.message.reply_text(text, **kwargs)


async def _reject_generation_in_quiet_mode(update: Update, session: dict[str, Any]) -> bool:
    # The owner/admin workspace is never subject to the guest quiet-mode gate.
    # It shares this handler for the Discipline project, but must retain the
    # same access as the regular admin workflow while guests are paused.
    if str(session.get("user_role") or "guest").strip().lower() == "admin":
        return False
    try:
        enabled = is_quiet_mode_enabled()
    except Exception:
        logger.exception("guest.quiet_mode_read_failed")
        return False
    if not enabled:
        return False
    delay_seconds = random.randint(60, 90)
    logger.info(
        "guest.quiet_mode_generation_rejected delay_seconds=%s state=%s",
        delay_seconds,
        session.get("state"),
    )

    async def send_delayed_failure() -> None:
        await asyncio.sleep(delay_seconds)
        await _reply(
            update,
            "Не удалось сгенерировать, попробуйте позже.",
            markup=guest_keyboard_for_session(session),
        )

    task = asyncio.create_task(send_delayed_failure(), name=f"quiet-generation-failure-{delay_seconds}")
    _QUIET_FAILURE_TASKS.add(task)

    def task_finished(done: asyncio.Task[Any]) -> None:
        _QUIET_FAILURE_TASKS.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("guest.quiet_mode_delayed_reply_failed error=%s", error)

    task.add_done_callback(task_finished)
    # Give the background task a chance to start, then release the Telegram
    # update handler immediately so owner commands are never held up.
    await _event_loop_yield(0)
    return True


async def _send_photo(update: Update, data: bytes, caption: str, *, markup: object | None = None) -> None:
    if update.message is None:
        return
    stream = io.BytesIO(data)
    stream.name = "result.png"
    await update.message.reply_photo(photo=stream, caption=caption, reply_markup=markup or _approval_markup())


async def _send_video(update: Update, data: bytes, caption: str, *, markup: object | None = None) -> None:
    if update.message is None:
        return
    stream = io.BytesIO(data)
    stream.name = "result.mp4"
    await update.message.reply_video(
        video=stream,
        caption=caption,
        reply_markup=markup or _approval_markup(),
        read_timeout=GUEST_VIDEO_SEND_TIMEOUT_SEC,
        write_timeout=GUEST_VIDEO_SEND_TIMEOUT_SEC,
        connect_timeout=30.0,
        pool_timeout=60.0,
    )


async def _deliver_current_video(
    update: Update,
    session: dict[str, Any],
    *,
    video_bytes: bytes | None = None,
) -> bool:
    data = _guest_data(session)
    target = str(data.get("video_delivery_target") or data.get("last_video_target") or "video")
    approval_state = STATE_VIDEO_APPROVAL if target == "video" else STATE_CARTOON_VIDEO_APPROVAL
    payload = video_bytes or read_media_uri_bytes(str(data.get("current_video_uri") or ""))
    if not payload:
        session["state"] = STATE_SYNTAX_ERROR
        data["syntax_retry_target"] = target
        await _reply(
            update,
            "Сохранённый видеофайл не найден. Можно повторить генерацию.",
            markup=guest_keyboard_for_session(session),
        )
        return False
    session["state"] = approval_state
    try:
        await _send_video(update, payload, "Видео готово.", markup=guest_keyboard_for_session(session))
    except TelegramError as error:
        logger.warning("guest.video_delivery_failed target=%s error=%s", target, error)
        data["video_delivery_target"] = target
        session["state"] = STATE_VIDEO_DELIVERY_ERROR
        await _reply(
            update,
            "Видео уже сгенерировано и сохранено, но Telegram не смог его принять. Нажмите «Повторить отправку» — новая генерация не запустится.",
            markup=guest_keyboard_for_session(session),
        )
        return False
    data.pop("video_delivery_target", None)
    return True


def _new_run(session: dict[str, Any], workflow_type: str, *, title: str) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = _project_uuid(session)
    with SessionLocal() as db:
        project_exists = db.execute(
            select(Project.id).where(Project.id == project_id, Project.owner_user_id == uuid.UUID(str(session["bot_user_id"])))
        ).scalar_one_or_none()
        if project_exists is None:
            raise PermissionError("Проект пользователя недоступен.")
        run = GenerationRun(
            project_id=project_id,
            workflow_type=workflow_type,
            status="guest_in_progress",
            is_active=True,
            is_draft=False,
            human_title=title[:180],
            language="ru",
            script=title,
        )
        db.add(run)
        db.flush()
        scene = Scene(run_id=run.id, scene_order=1, text="", duration_sec=8.0 if workflow_type != "image" else 0.0)
        db.add(scene)
        db.flush()
        run_id, scene_id = run.id, scene.id
        db.commit()
    session["generation_run_id"] = str(run_id)
    session["scene_db_ids_by_index"] = {0: str(scene_id)}
    data = _guest_data(session)
    data["scene_id"] = str(scene_id)
    data["scene_index"] = 0
    return run_id, scene_id


def _ensure_character_run(session: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    """Give character creation its own valid generation context."""
    data = _guest_data(session)
    try:
        run_id = uuid.UUID(str(session.get("generation_run_id") or ""))
        scene_id = uuid.UUID(str(data.get("scene_id") or ""))
    except (TypeError, ValueError, AttributeError):
        run_id = scene_id = None
    if run_id is not None and scene_id is not None:
        with SessionLocal() as db:
            valid_scene = db.execute(
                select(Scene.id)
                .join(GenerationRun, GenerationRun.id == Scene.run_id)
                .where(
                    Scene.id == scene_id,
                    Scene.run_id == run_id,
                    GenerationRun.project_id == _project_uuid(session),
                    GenerationRun.workflow_type == "image",
                )
            ).scalar_one_or_none()
        if valid_scene is not None:
            return run_id, scene_id
    return _new_run(
        session,
        "image",
        title=f"Персонаж: {data.get('character_name') or ''}",
    )


def _append_cartoon_scene(session: dict[str, Any]) -> uuid.UUID:
    run_id = _run_uuid(session)
    with SessionLocal() as db:
        max_order = db.execute(select(func.max(Scene.scene_order)).where(Scene.run_id == run_id)).scalar_one() or 0
        scene = Scene(run_id=run_id, scene_order=int(max_order) + 1, text="", duration_sec=8.0)
        db.add(scene)
        db.flush()
        scene_id = scene.id
        db.commit()
    mapping = _scene_index_map(session)
    index = len(mapping)
    mapping[index] = str(scene_id)
    session["scene_db_ids_by_index"] = mapping
    data = _guest_data(session)
    data["scene_index"] = index
    data["scene_id"] = str(scene_id)
    return scene_id


def _update_run_status(session: dict[str, Any], status: str, *, active: bool) -> None:
    with SessionLocal() as db:
        run = db.get(GenerationRun, _run_uuid(session))
        if run is not None and run.project_id == _project_uuid(session):
            run.status = status
            run.is_active = active
            db.commit()


def _update_scene_text(session: dict[str, Any], text: str, *, prompt: str | None = None) -> None:
    with SessionLocal() as db:
        scene = db.get(Scene, _scene_uuid(session))
        if scene is not None and scene.run_id == _run_uuid(session):
            scene.text = text
            if prompt is not None:
                scene.start_frame_prompt = prompt
                scene.start_frame_prompt_effective = prompt
            db.commit()


def _guest_scene_prompt_meta(session: dict[str, Any], existing: object = None) -> dict[str, Any]:
    meta = dict(existing) if isinstance(existing, dict) else {}
    data = _guest_data(session)
    meta["guest_selected_character_ids"] = [
        str(value) for value in data.get("selected_character_ids") or []
    ]
    meta["guest_mode"] = True
    meta["guest_aspect_ratio"] = _guest_aspect_ratio(session)
    return meta


def _persist_image_candidate(
    session: dict[str, Any],
    image: bytes,
    *,
    kind: str,
    generated_by_ai: bool = False,
) -> tuple[str, str]:
    run_id, scene_id = _run_uuid(session), _scene_uuid(session)
    candidate_id = f"guest-img-{uuid.uuid4().hex}"
    uri = store_project_artifact_media(
        project_id=_project_uuid(session),
        category=f"runs_{run_id}_{scene_id}",
        artifact_id=candidate_id,
        data=image,
        extension=image_file_extension(image),
    )
    with SessionLocal() as db:
        candidate = SceneImageCandidate(
            candidate_id=candidate_id,
            scene_id=scene_id,
            kind=kind,
            image_blob=None,
            media_uri=uri,
            mime_type=image_mime_type(image),
            byte_size=len(image),
            liked=False,
            approved_for_video=False,
        )
        db.add(candidate)
        scene = db.get(Scene, scene_id)
        if scene is not None:
            scene.prompt_meta_json = _guest_scene_prompt_meta(session, scene.prompt_meta_json)
        if generated_by_ai:
            db.add(
                GenerationArtifact(
                    run_id=run_id,
                    scene_id=scene_id,
                    artifact_type=GUEST_IMAGE_QUOTA_ARTIFACT_TYPE,
                    label="Гостевая AI-генерация изображения",
                    uri=uri,
                    mime_type=image_mime_type(image),
                    payload_json={"candidate_id": candidate_id, "kind": kind},
                )
            )
        db.commit()
    data = _guest_data(session)
    data["current_image_candidate_id"] = candidate_id
    data["current_image_uri"] = uri
    return candidate_id, uri


def _approve_image_candidate(session: dict[str, Any]) -> None:
    candidate_id = str(_guest_data(session).get("current_image_candidate_id") or "")
    with SessionLocal() as db:
        row = db.execute(
            select(SceneImageCandidate).where(
                SceneImageCandidate.candidate_id == candidate_id,
                SceneImageCandidate.scene_id == _scene_uuid(session),
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("Кандидат изображения не найден.")
        row.liked = True
        row.approved_for_video = True
        db.commit()


def _unapprove_image_candidate(session: dict[str, Any]) -> None:
    candidate_id = str(_guest_data(session).get("current_image_candidate_id") or "")
    with SessionLocal() as db:
        row = db.execute(
            select(SceneImageCandidate).where(
                SceneImageCandidate.candidate_id == candidate_id,
                SceneImageCandidate.scene_id == _scene_uuid(session),
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("Кандидат изображения не найден.")
        row.liked = False
        row.approved_for_video = False
        db.commit()


def _persist_video_candidate(
    session: dict[str, Any],
    video: bytes,
    *,
    prompt: str,
    user_prompt: str | None = None,
) -> tuple[str, str]:
    run_id, scene_id = _run_uuid(session), _scene_uuid(session)
    data = _guest_data(session)
    aspect_ratio = _guest_aspect_ratio(session)
    candidate_id = f"guest-video-{uuid.uuid4().hex}"
    uri = store_project_artifact_media(
        project_id=_project_uuid(session),
        category=f"runs_{run_id}_{scene_id}",
        artifact_id=candidate_id,
        data=video,
        extension="mp4",
    )
    with SessionLocal() as db:
        db.add(
            SceneVideoCandidate(
                candidate_id=candidate_id,
                scene_id=scene_id,
                language="ru",
                video_blob=None,
                media_uri=uri,
                mime_type="video/mp4",
                byte_size=len(video),
                liked=False,
                approved_for_production=False,
                speed_coef=1.0,
                source_image_candidate_id=str(data.get("current_image_candidate_id") or "") or None,
                video_prompt_by_lang={"ru": prompt},
                speech_by_lang={},
                speech_cues_by_lang={},
                speech_plan_by_lang={},
                video_speech_qa_by_lang={},
                generation_params_json={
                    "provider": "syntax",
                    "syntax_provider": "veo",
                    "model": "veo3_fast",
                    "duration_sec": 8,
                    "aspect_ratio": aspect_ratio,
                    "user_prompt": str(user_prompt if user_prompt is not None else prompt),
                },
            )
        )
        scene = db.get(Scene, scene_id)
        if scene is not None:
            scene.video_prompt_by_lang = {"ru": prompt}
            scene.syntax_prompt_by_lang = {"ru": prompt}
            scene.prompt_meta_json = _guest_scene_prompt_meta(session, scene.prompt_meta_json)
        db.commit()
    data["current_video_candidate_id"] = candidate_id
    data["current_video_uri"] = uri
    return candidate_id, uri


def _approve_video_candidate(session: dict[str, Any]) -> None:
    candidate_id = str(_guest_data(session).get("current_video_candidate_id") or "")
    with SessionLocal() as db:
        row = db.execute(
            select(SceneVideoCandidate).where(
                SceneVideoCandidate.candidate_id == candidate_id,
                SceneVideoCandidate.scene_id == _scene_uuid(session),
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("Кандидат видео не найден.")
        siblings = db.execute(
            select(SceneVideoCandidate).where(
                SceneVideoCandidate.scene_id == row.scene_id,
                SceneVideoCandidate.language == row.language,
            )
        ).scalars().all()
        for sibling in siblings:
            approved = sibling.candidate_id == candidate_id
            sibling.liked = approved
            sibling.approved_for_production = approved
        db.commit()


def _selected_characters(session: dict[str, Any]) -> list[GuestCharacter]:
    data = _guest_data(session)
    wanted: list[uuid.UUID] = []
    for raw in data.get("selected_character_ids") or []:
        try:
            wanted.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError, AttributeError):
            continue
    if not wanted:
        return []
    allowed = {item.id: item for item in _list_project_characters(_project_uuid(session))}
    return [allowed[item] for item in wanted if item in allowed]


def _selected_reference_images(session: dict[str, Any]) -> list[bytes]:
    return [payload for _, payload in _selected_character_references(session)]


def _selected_character_references(session: dict[str, Any]) -> list[tuple[GuestCharacter, bytes]]:
    out: list[tuple[GuestCharacter, bytes]] = []
    for character in _selected_characters(session):
        payload = read_media_uri_bytes(character.asset_uri)
        if payload:
            out.append((character, payload))
    return out


def _primary_character_style_reference(session: dict[str, Any]) -> tuple[GuestCharacter, bytes] | None:
    """Return the first primary project character as a style-only reference for a new character."""
    project_id = _project_uuid(session)
    if project_id is None:
        return None
    with SessionLocal() as db:
        row = db.execute(
            select(Character, ProjectCharacter)
            .join(ProjectCharacter, ProjectCharacter.character_id == Character.id)
            .where(
                ProjectCharacter.project_id == project_id,
                ProjectCharacter.is_primary.is_(True),
                Character.is_active.is_(True),
            )
            .order_by(ProjectCharacter.sort_order.asc(), Character.created_at.asc())
        ).first()
    if row is None:
        return None
    character, link = row
    asset_uri = str(link.asset_uri or character.default_asset_uri or "").strip()
    payload = read_media_uri_bytes(asset_uri)
    if not payload:
        return None
    return (
        GuestCharacter(
            id=character.id,
            name=str(link.display_name or character.name),
            short_description=str(
                (character.meta_json if isinstance(character.meta_json, dict) else {}).get("short_description") or ""
            ),
            description=str(character.description or ""),
            voice_description=str(character.voice_description or ""),
            asset_uri=asset_uri,
        ),
        payload,
    )


def _character_reference_instructions(references: list[tuple[GuestCharacter, bytes]]) -> str:
    if not references:
        return ""
    lines = [
        "Используй приложенные изображения как обязательные визуальные референсы персонажей. "
        "Сохрани их узнаваемые лица, причёски, одежду, цвета и общий дизайн:"
    ]
    for index, (character, _) in enumerate(references, start=1):
        description = character.description.strip()
        suffix = f" — {description}" if description else ""
        lines.append(f"- Референс №{index}: персонаж «{character.name}»{suffix}.")
    return "\n".join(lines)


def _requirements_prompt(base: str, feedback: list[str], *, prefix: str = "") -> str:
    chunks = [prefix.strip(), base.strip()]
    if feedback:
        chunks.append("Дополнительные требования пользователя:\n" + "\n".join(f"- {item}" for item in feedback))
    return "\n\n".join(item for item in chunks if item).strip()


def _user_prompt_from_stored_video_prompt(value: str) -> str:
    """Recover the guest's text from candidates saved before user_prompt was stored separately."""
    text = str(value or "").strip()
    legacy_prefix = "Вертикальное видео 9:16, длительность 8 секунд."
    if text.startswith(legacy_prefix):
        text = text[len(legacy_prefix) :].lstrip()
    voice_marker = "\n\nКраткие описания выбранных персонажей и их голоса:\n"
    if voice_marker in text:
        text = text.split(voice_marker, 1)[0]
    legacy_suffix = (
        "Вертикальный формат, композиция для смартфона. Динамичная, понятная подача урока, "
        "естественная мимика, синхронная русская речь, без субтитров, без надписей. "
        "Без новых персонажей."
    )
    if text.endswith(legacy_suffix):
        text = text[: -len(legacy_suffix)]
    return text.strip()


def _without_character_name(value: str, character_name: str) -> str:
    text = " ".join(str(value or "").split())
    name = " ".join(str(character_name or "").split())
    if name:
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip(" :-—–,.;")


def _compact_character_description(value: str, character_name: str) -> str:
    description = _without_character_name(value, character_name)
    if not description:
        return ""
    description = re.split(r"[,.;\n]+", description, maxsplit=1)[0].strip(" :-—–")
    words = description.split()
    if len(words) > 8:
        description = " ".join(words[:8])
    return description[:80].rstrip(" :-—–,.;").lower()


def _short_voice_character_description(character: GuestCharacter, index: int) -> str:
    description = _compact_character_description(character.short_description, character.name)
    if not description:
        description = _compact_character_description(character.description, character.name)
    if description:
        return description

    voice_hint = _without_character_name(character.voice_description, character.name).lower()
    if "учительниц" in voice_hint:
        return "учительница"
    if "учител" in voice_hint:
        return "учитель"
    if "девочк" in voice_hint:
        return "девочка с косичками" if "косич" in voice_hint else "девочка"
    if "мальчик" in voice_hint:
        return "мальчик-хулиган" if "хулиган" in voice_hint else "мальчик"
    if "девуш" in voice_hint:
        return "молодая девушка" if "молод" in voice_hint else "девушка"
    if "женск" in voice_hint or "женщин" in voice_hint:
        return "молодая женщина" if "молод" in voice_hint else "женщина"
    if "мужск" in voice_hint or "мужчин" in voice_hint:
        return "мужчина"
    return f"персонаж №{index}"


def _voice_profiles(session: dict[str, Any]) -> str:
    characters = _selected_characters(session)
    if not characters:
        return ""
    profiles: list[str] = []
    for index, character in enumerate(characters, start=1):
        voice = _without_character_name(character.voice_description, character.name)
        if not voice:
            continue
        profiles.append(f"- {_short_voice_character_description(character, index)}: {voice}")
    return "\n".join(profiles)


async def _generate_image(update: Update, session: dict[str, Any], *, target: str) -> None:
    if await _reject_generation_in_quiet_mode(update, session):
        return
    if _guest_image_limit_reached(session):
        logger.info(
            "guest.image_daily_limit_reached user_id=%s project_id=%s limit=%s target=%s",
            session.get("bot_user_id"),
            session.get("project_id"),
            GUEST_IMAGE_DAILY_LIMIT,
            target,
        )
        await _reply(update, GUEST_IMAGE_QUOTA_EXHAUSTED_MESSAGE, markup=guest_keyboard_for_session(session))
        return
    data = _guest_data(session)
    aspect_ratio = _guest_aspect_ratio(session) if target == "cartoon_frame" else "9:16"
    if target == "character":
        base = str(data.get("appearance") or "")
        feedback = list(data.get("character_feedback") or [])
        style_reference = _primary_character_style_reference(session)
        refs = [style_reference[1]] if style_reference is not None else []
        style_rule = ""
        if style_reference is not None:
            style_rule = (
                f"Приложено изображение основного персонажа проекта «{style_reference[0].name}». "
                "Используй его ТОЛЬКО как референс художественной стилистики: материал, фактуру, способ рендера, "
                "палитру и характер освещения. Не копируй его вид животного или человека, лицо, форму тела, одежду, "
                "позу, предметы, фон или композицию. Новый персонаж должен точно соответствовать описанию пользователя."
            )
        prefix = "\n\n".join(
            item
            for item in (
                f"Персонаж по имени {data.get('character_name')}. Создай чистый визуальный референс персонажа.",
                style_rule,
            )
            if item
        )
        approval_state = STATE_CHARACTER_IMAGE_APPROVAL
        kind = "character"
        caption = "Вариант персонажа готов."
    else:
        base = str(data.get("image_description") or data.get("scene_description") or "")
        feedback_key = "image_feedback" if target == "image" else "frame_feedback"
        feedback = list(data.get(feedback_key) or [])
        if target == "cartoon_frame":
            _ensure_cartoon_character_selection(session)
        character_references = _selected_character_references(session)
        refs = [payload for _, payload in character_references]
        prefix = "\n\n".join(
            item
            for item in (
                f"Создай {_guest_aspect_label(aspect_ratio)}.",
                _character_reference_instructions(character_references),
            )
            if item
        )
        approval_state = STATE_IMAGE_APPROVAL if target == "image" else STATE_CARTOON_FRAME_APPROVAL
        kind = "result" if target == "image" else "start"
        caption = "Изображение готово." if target == "image" else "Стартовый кадр сцены готов."
    prompt = _requirements_prompt(base, feedback, prefix=prefix)
    await _reply(update, "Генерирую изображение, пожалуйста, подождите.", markup=ReplyKeyboardRemove())
    try:
        image = await asyncio.to_thread(
            generate_image_vertex,
            prompt,
            reference_images=refs,
            aspect_ratio=aspect_ratio,
        )
    except Exception:
        logger.exception("guest.image_generation_failed target=%s", target)
        data["image_retry_target"] = target
        session["state"] = STATE_IMAGE_ERROR
        await _reply(update, "Не удалось сгенерировать изображение. Текущий шаг сохранён; можно повторить запрос.", markup=guest_keyboard_for_session(session))
        return
    _persist_image_candidate(session, image, kind=kind, generated_by_ai=True)
    _record_automatic_quiet_mode_generation_safely()
    data["last_image_target"] = target
    data["effective_image_prompt"] = prompt
    session["state"] = approval_state
    await _send_photo(update, image, caption, markup=guest_keyboard_for_session(session))


def _positive_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return int(default)


def _guest_syntax_timeout_sec() -> int:
    """Keep unattended guest Veo work alive while Syntax's account queue is full."""

    return max(60, _positive_int_env("GUEST_SYNTAX_TIMEOUT_SEC", GUEST_SYNTAX_TIMEOUT_SEC_DEFAULT))


def _guest_syntax_task_key(chat_id: int, session: dict[str, Any], target: str) -> str:
    data = _guest_data(session)
    return ":".join(
        (
            str(int(chat_id)),
            str(session.get("generation_run_id") or ""),
            str(data.get("scene_id") or ""),
            str(target or "video"),
        )
    )


async def _generate_video_now(
    update: Update,
    session: dict[str, Any],
    *,
    target: str,
    quiet_checked: bool = False,
    announce: bool = True,
) -> None:
    if not quiet_checked and await _reject_generation_in_quiet_mode(update, session):
        return
    data = _guest_data(session)
    job_context = _guest_video_job_context(session)
    aspect_ratio = _guest_aspect_ratio(session)
    base = str(data.get("video_description") or data.get("scene_description") or "").strip()
    prompt = base
    voices = _voice_profiles(session)
    if voices:
        prompt = f"{prompt}\n\nКраткие описания выбранных персонажей и их голоса:\n{voices}"
    image_uri = str(data.get("current_image_uri") or "")
    start_image = read_media_uri_bytes(image_uri)
    if not start_image:
        await _reply(update, "Стартовое изображение не найдено. Вернитесь в меню и начните заново.", markup=_markup([]))
        return
    if announce:
        await _reply(
            update,
            "Создаю видео через Syntax → Veo. Это может занять несколько минут.",
            markup=ReplyKeyboardRemove(),
        )
    try:
        result = await asyncio.to_thread(
            try_generate_via_syntax,
            prompt=prompt,
            model="veo3_fast",
            duration_sec=8,
            start_image=start_image,
            end_image=None,
            syntax_provider="veo",
            aspect_ratio=aspect_ratio,
            timeout_sec=_guest_syntax_timeout_sec(),
            acquire_timeout_sec=_guest_syntax_timeout_sec(),
        )
    except Exception:
        logger.exception("guest.syntax_generation_failed target=%s", target)
        result = None
    video_bytes: bytes | None = None
    if result is not None and result.success:
        if not _guest_video_job_is_current(session, job_context):
            logger.info("guest.syntax_stale_result_ignored target=%s", target)
            return
        try:
            video_bytes = await asyncio.to_thread(video_bytes_from_fallback_result, result)
        except Exception:
            logger.exception("guest.syntax_video_download_failed target=%s", target)
    if not _guest_video_job_is_current(session, job_context):
        logger.info("guest.syntax_stale_result_ignored target=%s", target)
        return
    if result is None or not result.success or not video_bytes:
        if result is not None:
            logger.warning("guest.syntax_unsuccessful target=%s message=%s", target, result.provider_message)
        data["syntax_retry_target"] = target
        session["state"] = STATE_SYNTAX_ERROR
        await _reply(
            update,
            "Syntax не смог создать видео. Текущий шаг сохранён; можно повторить запрос.",
            markup=guest_keyboard_for_session(session),
        )
        return
    _persist_video_candidate(session, video_bytes, prompt=prompt, user_prompt=base)
    _record_automatic_quiet_mode_generation_safely()
    data["last_video_target"] = target
    data["video_delivery_target"] = target
    data["effective_video_prompt"] = prompt
    await _deliver_current_video(update, session, video_bytes=video_bytes)


async def _generate_video(update: Update, session: dict[str, Any], *, target: str) -> None:
    """Start guest video work without blocking all Telegram updates.

    Real Telegram updates carry a chat id, so Syntax/Veo generation runs in a
    deduplicated background task.  The task owns the full current WebView flow
    (including queue waiting and the final Generate click), persists its final
    state, and never falls back to Vertex.  Lightweight/unit callers without a
    chat id keep the synchronous contract used by tests and internal tools.
    """

    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        await _generate_video_now(update, session, target=target)
        return
    if await _reject_generation_in_quiet_mode(update, session):
        return

    key = _guest_syntax_task_key(int(chat_id), session, target)
    running = _GUEST_SYNTAX_TASKS.get(key)
    if running is not None and not running.done():
        await _reply(
            update,
            "Генерация через Syntax → Veo уже запущена. Жду очередь и пришлю видео автоматически.",
            markup=guest_keyboard_for_session(session),
        )
        return

    data = _guest_data(session)
    job_context = _guest_video_job_context(session)
    data["syntax_retry_target"] = target
    session["state"] = STATE_SYNTAX_PENDING
    save_loaded_session_if_possible(int(chat_id))
    await _reply(
        update,
        "Создаю видео через Syntax → Veo. Очередь дождусь автоматически; повторно нажимать ничего не нужно.",
        markup=ReplyKeyboardRemove(),
    )

    async def run() -> None:
        try:
            await _generate_video_now(
                update,
                session,
                target=target,
                quiet_checked=True,
                announce=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guest.syntax_background_generation_failed target=%s", target)
            data["syntax_retry_target"] = target
            session["state"] = STATE_SYNTAX_ERROR
            await _reply(
                update,
                "Syntax не смог создать видео. Текущий шаг сохранён; можно повторить запрос.",
                markup=guest_keyboard_for_session(session),
            )
        finally:
            if (
                session.get("state") == STATE_SYNTAX_PENDING
                and _guest_video_job_is_current(session, job_context)
            ):
                data["syntax_retry_target"] = target
                session["state"] = STATE_SYNTAX_ERROR
            save_loaded_session_if_possible(int(chat_id))
            _GUEST_SYNTAX_TASKS.pop(key, None)

    _GUEST_SYNTAX_TASKS[key] = asyncio.create_task(
        run(),
        name=f"guest-syntax-{int(chat_id)}-{target}",
    )


def _guest_video_job_context(session: dict[str, Any]) -> tuple[str, int, str, str, str, str]:
    """Snapshot fields that identify the exact guest scene/video request."""

    data = _guest_data(session)
    return (
        str(data.get("scene_id") or ""),
        int(data.get("scene_index") or 0),
        str(data.get("current_image_candidate_id") or ""),
        str(data.get("current_image_uri") or ""),
        str(data.get("video_description") or ""),
        _guest_aspect_ratio(session),
    )


def _guest_video_job_is_current(
    session: dict[str, Any],
    expected: tuple[str, int, str, str, str, str],
) -> bool:
    """Prevent a late Syntax worker from overwriting a newer guest scene."""

    return _guest_video_job_context(session) == expected


def _create_character_from_draft(session: dict[str, Any]) -> uuid.UUID:
    data = _guest_data(session)
    name = " ".join(str(data.get("character_name") or "").split())
    short_description = _compact_character_description(str(data.get("short_description") or ""), name)
    voice = " ".join(str(data.get("voice_description") or "").split())
    description = " ".join(str(data.get("appearance") or "").split())
    candidate_uri = str(data.get("current_image_uri") or "").strip()
    candidate_bytes = read_media_uri_bytes(candidate_uri)
    asset_uri = (
        store_project_artifact_media(
            project_id=_project_uuid(session),
            category="characters",
            artifact_id=uuid.uuid4().hex,
            data=candidate_bytes,
            extension=image_file_extension(candidate_bytes),
        )
        if candidate_bytes
        else ""
    )
    if not name or not short_description or not voice or not asset_uri:
        raise ValueError("Не заполнены обязательные данные персонажа.")
    with SessionLocal() as db:
        project = db.execute(
            select(Project).where(
                Project.id == _project_uuid(session),
                Project.owner_user_id == uuid.UUID(str(session["bot_user_id"])),
            )
        ).scalar_one_or_none()
        if project is None:
            raise PermissionError("Проект недоступен.")
        character = Character(
            canonical_key=None,
            name=name,
            description=description or None,
            voice_description=voice,
            default_asset_uri=asset_uri,
            meta_json={
                "source": str(data.get("character_method") or "guest"),
                "short_description": short_description,
            },
            is_active=True,
        )
        db.add(character)
        db.flush()
        db.add(
            ProjectCharacter(
                project_id=project.id,
                character_id=character.id,
                display_name=name,
                asset_uri=asset_uri,
                sort_order=len(project.characters),
                meta_json={"guest_approved": True},
            )
        )
        character_id = character.id
        db.commit()
    try:
        from .character_catalog import _db_character_prompt_catalog

        _db_character_prompt_catalog.cache_clear()
    except Exception:
        logger.exception("character catalog cache clear failed")
    return character_id


async def _extract_uploaded_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    if update.message is None:
        return None
    file_obj = None
    if getattr(update.message, "photo", None):
        file_obj = update.message.photo[-1]
    else:
        document = getattr(update.message, "document", None)
        mime_type = str(getattr(document, "mime_type", "") or "")
        if document is not None and mime_type.startswith("image/"):
            file_obj = document
    if file_obj is None:
        return None
    remote = await context.bot.get_file(file_obj.file_id)
    return bytes(await remote.download_as_bytearray())


def _toggle_character(session: dict[str, Any], text: str) -> bool:
    data = _guest_data(session)
    mapping = data.get("character_buttons")
    if not isinstance(mapping, dict):
        _character_selection_markup(session)
        mapping = data.get("character_buttons")
    character_id = str((mapping or {}).get(text) or "")
    if not character_id:
        return False
    selected = [str(value) for value in data.get("selected_character_ids") or []]
    if character_id in selected:
        selected.remove(character_id)
    else:
        selected.append(character_id)
    data["selected_character_ids"] = selected
    data["character_selection_changed"] = True
    data["character_selection_confirmed"] = False
    data["allow_empty_character_selection"] = False
    return True


def _clear_workflow_data(session: dict[str, Any], *, keep_cartoon_scenes: bool = False) -> None:
    data = _guest_data(session)
    preserved = {}
    if keep_cartoon_scenes:
        for key in ("approved_scenes",):
            if key in data:
                preserved[key] = data[key]
    data.clear()
    data.update(preserved)


async def guest_start(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    _ = context
    if update.effective_chat is None:
        return
    chat_id = int(update.effective_chat.id)
    session = SESSIONS.get(chat_id)
    if not _session_matches_identity(session, identity):
        session = _base_session(identity)
        SESSIONS[chat_id] = session
    state = str(session.get("state") or STATE_MENU)
    if state not in GUEST_STATES:
        session["state"] = STATE_MENU
        state = STATE_MENU
    if state == STATE_MENU:
        text = "Выберите действие."
    else:
        text = "Незавершённый шаг восстановлен. Можно продолжить или вернуться в меню."
    await _reply(update, text, markup=guest_keyboard_for_session(session))
    save_loaded_session_if_possible(chat_id)


async def project_workspace_start(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    _ = context
    if update.effective_chat is None:
        return
    chat_id = int(update.effective_chat.id)
    existing = SESSIONS.get(chat_id)
    if not _session_matches_identity(existing, identity) or not bool((existing or {}).get("project_workspace_mode")):
        existing = _workspace_session(identity, existing if isinstance(existing, dict) else None)
        SESSIONS[chat_id] = existing
    await _reply(
        update,
        "Проект «Дисциплина». Выберите действие.",
        markup=guest_main_menu_markup(existing),
    )
    save_loaded_session_if_possible(chat_id)


async def guest_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    _ = context
    if update.effective_chat is None:
        return
    chat_id = int(update.effective_chat.id)
    active_syntax = [
        task
        for key, task in _GUEST_SYNTAX_TASKS.items()
        if key.startswith(f"{chat_id}:") and not task.done()
    ]
    if active_syntax:
        session = SESSIONS.get(chat_id) or _base_session(identity)
        await _reply(
            update,
            "Запрос уже передан в Syntax → Veo. Его нельзя безопасно прервать локально; дождусь ответа и пришлю результат.",
            markup=guest_keyboard_for_session(session),
        )
        return
    existing = SESSIONS.get(int(update.effective_chat.id))
    session = (
        _workspace_session(identity, existing if isinstance(existing, dict) else None)
        if isinstance(existing, dict) and existing.get("project_workspace_mode") is True
        else _base_session(identity)
    )
    SESSIONS[chat_id] = session
    await _reply(update, "Текущий шаг отменён. Выберите действие.", markup=guest_main_menu_markup(session))


async def guest_on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    if update.effective_chat is None:
        return
    chat_id = int(update.effective_chat.id)
    session = SESSIONS.get(chat_id)
    if not _session_matches_identity(session, identity):
        session = _base_session(identity)
        SESSIONS[chat_id] = session
    state = str(session.get("state") or "")
    data = _guest_data(session)
    cartoon_upload_states = {
        STATE_CARTOON_DESCRIPTION,
        STATE_CARTOON_FRAME_APPROVAL,
        STATE_CARTOON_FRAME_FEEDBACK,
        STATE_CARTOON_CONTINUATION_FRAME_APPROVAL,
    }
    if state == STATE_IMAGE_ERROR and str(data.get("image_retry_target") or "") == "cartoon_frame":
        cartoon_upload_states.add(STATE_IMAGE_ERROR)
    accepts_cartoon_start_frame = state in cartoon_upload_states
    if state not in {STATE_CHARACTER_UPLOAD, STATE_VIDEO_START_IMAGE} and not accepts_cartoon_start_frame:
        await _reply(update, "Сейчас изображение не требуется. Используйте кнопки текущего шага.", markup=guest_keyboard_for_session(session))
        return
    image = await _extract_uploaded_image(update, context)
    if not image:
        await _reply(update, "Не удалось прочитать изображение. Пришлите фото или файл изображения ещё раз.", markup=guest_keyboard_for_session(session))
        return
    if state == STATE_CHARACTER_UPLOAD:
        _ensure_character_run(session)
        _persist_image_candidate(session, image, kind="character")
        session["state"] = STATE_CHARACTER_UPLOAD_APPROVAL
        stream = io.BytesIO(image)
        stream.name = "character.png"
        await update.message.reply_photo(
            photo=stream,
            caption="Использовать это изображение для персонажа?",
            reply_markup=guest_keyboard_for_session(session),
        )
    elif state == STATE_VIDEO_START_IMAGE:
        _persist_image_candidate(session, image, kind="start")
        _approve_image_candidate(session)
        session["state"] = STATE_VIDEO_DESCRIPTION
        await _reply(
            update,
            "Опишите движение, действие и речь в видео.",
            markup=guest_keyboard_for_session(session),
        )
    else:
        _persist_image_candidate(session, image, kind="start")
        _approve_image_candidate(session)
        data["continued_from_previous_scene"] = False
        data["last_image_target"] = "cartoon_frame"
        session["state"] = STATE_CARTOON_VIDEO_DESCRIPTION
        await _reply(
            update,
            "Стартовый кадр сцены принят. Опишите движение, действие и речь для видео этой сцены.",
            markup=guest_keyboard_for_session(session),
        )
    save_loaded_session_if_possible(chat_id)


async def guest_on_video(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    _ = (context, identity)
    if update.effective_chat is None:
        return
    session = SESSIONS.get(int(update.effective_chat.id)) or {}
    await _reply(update, "Загружать видео не нужно: оно создаётся ботом через Syntax → Veo.", markup=guest_keyboard_for_session(session))


async def _show_menu(update: Update, session: dict[str, Any]) -> None:
    if str(session.get("generation_run_id") or "") and update.effective_chat is not None:
        save_loaded_session_if_possible(int(update.effective_chat.id))
    session["state"] = STATE_MENU
    session.pop("generation_run_id", None)
    session.pop("scene_db_ids_by_index", None)
    _clear_workflow_data(session)
    await _reply(update, "Выберите действие.", markup=guest_main_menu_markup(session))


async def _begin_selection(
    update: Update,
    session: dict[str, Any],
    state: str,
    *,
    aspect_ratio: str | None = None,
) -> None:
    _clear_workflow_data(session)
    data = _guest_data(session)
    if aspect_ratio in {"9:16", "16:9"}:
        data["aspect_ratio"] = aspect_ratio
    data["selected_character_ids"] = []
    data["character_selection_confirmed"] = False
    data["character_selection_changed"] = False
    data["allow_empty_character_selection"] = False
    session["state"] = state
    await _reply(
        update,
        "Выберите персонажей, которые должны присутствовать. Можно сразу нажать «Готово».",
        markup=guest_keyboard_for_session(session),
    )


def _finish_scene_payload(session: dict[str, Any]) -> dict[str, Any]:
    data = _guest_data(session)
    return {
        "scene": int(data.get("scene_index") or 0) + 1,
        "guest_mode": True,
        "text": str(data.get("scene_description") or ""),
        "duration_sec": 8,
        "aspect_ratio": _guest_aspect_ratio(session),
        "video_uri": str(data.get("current_video_uri") or ""),
        "selected_character_ids": [str(value) for value in data.get("selected_character_ids") or []],
        "video_prompt_by_lang": {"ru": str(data.get("effective_video_prompt") or "")},
        "video_candidates_by_lang": {
            "ru": [
                {
                    "candidate_id": str(data.get("current_video_candidate_id") or ""),
                    "language": "ru",
                    "approved_for_production": True,
                    "liked": True,
                    "speed_coef": 1.0,
                }
            ]
        },
    }


def _restore_cartoon_scene_for_edit(session: dict[str, Any], scene_index: int) -> None:
    data = _guest_data(session)
    data.pop("insert_scene_at", None)
    data.pop("inserting_scene", None)
    approved_scenes = [item for item in data.get("approved_scenes") or [] if isinstance(item, dict)]
    if scene_index < 0 or scene_index >= len(approved_scenes):
        raise ValueError("Выбранная сцена не найдена.")
    scene_ids = _scene_index_map(session)
    scene_id = scene_ids.get(scene_index)
    if not scene_id:
        raise ValueError("Данные выбранной сцены не найдены.")
    scene_payload = approved_scenes[scene_index]
    selected_ids = [str(value) for value in scene_payload.get("selected_character_ids") or []]
    current_video_id = ""
    candidates = ((scene_payload.get("video_candidates_by_lang") or {}).get("ru") or [])
    if candidates:
        current_video_id = str(candidates[-1].get("candidate_id") or "").strip()

    with SessionLocal() as db:
        scene = db.get(Scene, uuid.UUID(str(scene_id)))
        if scene is None or scene.run_id != _run_uuid(session):
            raise ValueError("Данные выбранной сцены не найдены.")
        video_row = None
        if current_video_id:
            video_row = db.execute(
                select(SceneVideoCandidate).where(
                    SceneVideoCandidate.scene_id == scene.id,
                    SceneVideoCandidate.candidate_id == current_video_id,
                )
            ).scalar_one_or_none()
        if video_row is None:
            video_row = (
                db.execute(
                    select(SceneVideoCandidate)
                    .where(
                        SceneVideoCandidate.scene_id == scene.id,
                        SceneVideoCandidate.language == "ru",
                        SceneVideoCandidate.approved_for_production.is_(True),
                    )
                    .order_by(SceneVideoCandidate.created_at.desc())
                )
                .scalars()
                .first()
            )
        source_image_id = str(getattr(video_row, "source_image_candidate_id", "") or "").strip()
        image_row = None
        if source_image_id:
            image_row = db.execute(
                select(SceneImageCandidate).where(
                    SceneImageCandidate.scene_id == scene.id,
                    SceneImageCandidate.candidate_id == source_image_id,
                )
            ).scalar_one_or_none()
        if image_row is None:
            image_row = (
                db.execute(
                    select(SceneImageCandidate)
                    .where(
                        SceneImageCandidate.scene_id == scene.id,
                        SceneImageCandidate.kind == "start",
                        SceneImageCandidate.approved_for_video.is_(True),
                    )
                    .order_by(SceneImageCandidate.created_at.desc())
                )
                .scalars()
                .first()
            )
        if image_row is None:
            raise ValueError("У выбранной сцены не найден стартовый кадр.")
        scene_text = str(scene.text or scene_payload.get("text") or "").strip()
        image_prompt = str(scene.start_frame_prompt_effective or scene.start_frame_prompt or scene_text).strip()
        video_prompt = ""
        if video_row is not None and isinstance(video_row.video_prompt_by_lang, dict):
            stored_prompt = str(video_row.video_prompt_by_lang.get("ru") or "").strip()
            generation_params = (
                video_row.generation_params_json
                if isinstance(video_row.generation_params_json, dict)
                else {}
            )
            video_prompt = str(generation_params.get("user_prompt") or "").strip()
            if not video_prompt:
                video_prompt = _user_prompt_from_stored_video_prompt(stored_prompt)

    data.update(
        {
            "scene_index": scene_index,
            "scene_id": str(scene_id),
            "scene_description": image_prompt or scene_text,
            "video_description": video_prompt,
            "selected_character_ids": selected_ids,
            "character_selection_confirmed": True,
            "allow_empty_character_selection": not bool(selected_ids),
            "current_image_candidate_id": str(image_row.candidate_id),
            "current_image_uri": str(image_row.media_uri or ""),
            "current_video_candidate_id": str(getattr(video_row, "candidate_id", "") or ""),
            "current_video_uri": str(getattr(video_row, "media_uri", "") or ""),
            "editing_existing_scene": True,
            "edit_scene_index": scene_index,
            "aspect_ratio": str(scene_payload.get("aspect_ratio") or _stored_guest_aspect_ratio(video_row, scene)),
        }
    )


def _cartoon_scene_index_from_button(session: dict[str, Any], text: str) -> int | None:
    if not text.startswith("Сцена "):
        return None
    try:
        index = int(text.removeprefix("Сцена ").strip()) - 1
    except ValueError:
        return None
    scenes = [item for item in _guest_data(session).get("approved_scenes") or [] if isinstance(item, dict)]
    return index if 0 <= index < len(scenes) else None


def _cartoon_insert_index_from_button(session: dict[str, Any], text: str) -> int | None:
    if not text.startswith("После сцены "):
        return None
    try:
        insert_at = int(text.removeprefix("После сцены ").strip())
    except ValueError:
        return None
    scenes = [item for item in _guest_data(session).get("approved_scenes") or [] if isinstance(item, dict)]
    return insert_at if 1 <= insert_at <= len(scenes) else None


def _begin_cartoon_scene_insertion(session: dict[str, Any], insert_at: int) -> None:
    data = _guest_data(session)
    approved = [item for item in data.get("approved_scenes") or [] if isinstance(item, dict)]
    if insert_at < 1 or insert_at > len(approved):
        raise ValueError("Позиция для новой сцены не найдена.")
    previous = approved[insert_at - 1]
    aspect_ratio = str(previous.get("aspect_ratio") or _guest_aspect_ratio(session))
    previous_selection = [str(value) for value in previous.get("selected_character_ids") or []]
    new_scene_id = _append_cartoon_scene(session)
    data.clear()
    data.update(
        {
            "approved_scenes": approved,
            "selected_character_ids": previous_selection,
            "character_selection_confirmed": False,
            "character_selection_changed": False,
            "allow_empty_character_selection": False,
            "previous_video_uri": str(previous.get("video_uri") or ""),
            "scene_index": insert_at,
            "scene_id": str(new_scene_id),
            "insert_scene_at": insert_at,
            "inserting_scene": True,
            "aspect_ratio": aspect_ratio if aspect_ratio in {"9:16", "16:9"} else "9:16",
        }
    )


def _commit_inserted_scene_order(session: dict[str, Any], insert_at: int) -> None:
    data = _guest_data(session)
    new_scene_id = str(data.get("scene_id") or "")
    mapping = _scene_index_map(session)
    ordered_ids = [mapping[index] for index in sorted(mapping)]
    ordered_ids = [scene_id for scene_id in ordered_ids if scene_id != new_scene_id]
    ordered_ids.insert(insert_at, new_scene_id)
    session["scene_db_ids_by_index"] = {index: scene_id for index, scene_id in enumerate(ordered_ids)}

    with SessionLocal() as db:
        rows = db.execute(select(Scene).where(Scene.run_id == _run_uuid(session))).scalars().all()
        rows_by_id = {str(row.id): row for row in rows}
        for temp_order, scene_id in enumerate(ordered_ids, start=1):
            row = rows_by_id.get(scene_id)
            if row is not None:
                row.scene_order = -temp_order
        db.flush()
        for scene_order, scene_id in enumerate(ordered_ids, start=1):
            row = rows_by_id.get(scene_id)
            if row is not None:
                row.scene_order = scene_order
        db.commit()


async def _continue_cartoon_from_previous_video(update: Update, session: dict[str, Any]) -> None:
    data = _guest_data(session)
    if not data.get("selected_character_ids"):
        data["selected_character_ids"] = _previous_selected_character_ids(session)
    previous_uri = _previous_approved_video_uri(session)
    previous_video = read_media_uri_bytes(previous_uri)
    if not previous_video:
        await _reply(
            update,
            "Не удалось найти видео предыдущей сцены. Выберите персонажей и создайте новый стартовый кадр.",
            markup=guest_keyboard_for_session(session),
        )
        return
    await _reply(update, "Беру последний кадр предыдущей сцены.", markup=ReplyKeyboardRemove())
    try:
        frame = await asyncio.to_thread(extract_last_video_frame, video_bytes=previous_video)
    except Exception:
        logger.exception("guest.cartoon_last_frame_extraction_failed uri=%s", previous_uri)
        await _reply(
            update,
            "Не удалось извлечь последний кадр предыдущей сцены. Можно создать новый стартовый кадр обычным способом.",
            markup=guest_keyboard_for_session(session),
        )
        return
    _persist_image_candidate(session, frame, kind="start")
    data["continued_from_previous_scene"] = True
    data["scene_description"] = "Продолжение предыдущей сцены."
    _update_scene_text(session, data["scene_description"], prompt=data["scene_description"])
    session["state"] = STATE_CARTOON_CONTINUATION_FRAME_APPROVAL
    if update.message is not None:
        stream = io.BytesIO(frame)
        stream.name = "continuation-frame.png"
        await update.message.reply_photo(
            photo=stream,
            caption="Использовать последний кадр предыдущей сцены как стартовый?",
            reply_markup=guest_keyboard_for_session(session),
        )


async def _return_cartoon_to_image_step(update: Update, session: dict[str, Any]) -> None:
    data = _guest_data(session)
    image = read_media_uri_bytes(str(data.get("current_image_uri") or ""))
    if not image:
        await _reply(
            update,
            "Стартовый кадр не найден. Вернитесь в меню и начните сцену заново.",
            markup=guest_keyboard_for_session(session),
        )
        return
    _unapprove_image_candidate(session)
    session["state"] = (
        STATE_CARTOON_CONTINUATION_FRAME_APPROVAL
        if data.get("continued_from_previous_scene")
        else STATE_CARTOON_FRAME_APPROVAL
    )
    if update.message is not None:
        stream = io.BytesIO(image)
        stream.name = "start-frame.png"
        await update.message.reply_photo(
            photo=stream,
            caption="Возвращаюсь к стартовому кадру сцены.",
            reply_markup=guest_keyboard_for_session(session),
        )


async def _begin_character_reselection(update: Update, session: dict[str, Any]) -> bool:
    selection_state = _character_selection_state_for_session(session)
    if selection_state is None:
        return False
    data = _guest_data(session)
    data["character_selection_return_state"] = str(session.get("state") or "")
    data["character_selection_confirmed"] = False
    session["state"] = selection_state
    await _reply(
        update,
        "Измените выбор персонажей. Выбранные персонажи отмечены ✓. Затем нажмите «Готово».",
        markup=guest_keyboard_for_session(session),
    )
    return True


async def _resume_after_character_reselection(
    update: Update,
    session: dict[str, Any],
    return_state: str,
) -> None:
    data = _guest_data(session)
    data.pop("character_selection_return_state", None)

    if return_state == STATE_IMAGE_DESCRIPTION:
        session["state"] = return_state
        await _reply(update, "Опишите изображение, которое нужно создать.", markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_IMAGE_FEEDBACK:
        session["state"] = return_state
        await _reply(update, "Отправьте полный новый промпт для изображения.", markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_IMAGE_APPROVAL or (
        return_state == STATE_IMAGE_ERROR and str(data.get("image_retry_target") or "image") == "image"
    ):
        await _generate_image(update, session, target="image")
        return

    if return_state == STATE_VIDEO_START_IMAGE:
        session["state"] = return_state
        await _reply(update, "Загрузите стартовое изображение для видео.", markup=guest_keyboard_for_session(session))
        return
    if return_state in {STATE_VIDEO_DESCRIPTION, STATE_VIDEO_FEEDBACK}:
        session["state"] = return_state
        prompt = (
            "Отправьте полный новый промпт для видео."
            if return_state == STATE_VIDEO_FEEDBACK
            else "Опишите движение, действие и речь в видео."
        )
        await _reply(update, prompt, markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_VIDEO_APPROVAL or (
        return_state == STATE_SYNTAX_ERROR and str(data.get("syntax_retry_target") or "video") == "video"
    ):
        await _generate_video(update, session, target="video")
        return

    if return_state == STATE_CARTOON_DESCRIPTION:
        session["state"] = return_state
        await _reply(update, "Опишите стартовый кадр сцены или пришлите своё изображение.", markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_CARTOON_FRAME_FEEDBACK:
        session["state"] = return_state
        await _reply(update, "Отправьте новый промпт или пришлите свой стартовый кадр.", markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_CARTOON_FRAME_APPROVAL or (
        return_state == STATE_IMAGE_ERROR and str(data.get("image_retry_target") or "") == "cartoon_frame"
    ):
        await _generate_image(update, session, target="cartoon_frame")
        return
    if return_state == STATE_CARTOON_CONTINUATION_FRAME_APPROVAL:
        session["state"] = return_state
        await _reply(
            update,
            "Выбор персонажей обновлён. Подтвердите стартовый кадр или сгенерируйте новый.",
            markup=guest_keyboard_for_session(session),
        )
        return

    if return_state in {STATE_CARTOON_VIDEO_DESCRIPTION, STATE_CARTOON_VIDEO_FEEDBACK}:
        session["state"] = return_state
        prompt = (
            "Отправьте полный новый промпт для видео."
            if return_state == STATE_CARTOON_VIDEO_FEEDBACK
            else "Опишите движение, действие и речь для видео этой сцены."
        )
        await _reply(update, prompt, markup=guest_keyboard_for_session(session))
        return
    if return_state == STATE_CARTOON_VIDEO_APPROVAL or (
        return_state == STATE_SYNTAX_ERROR and str(data.get("syntax_retry_target") or "") == "cartoon"
    ):
        await _generate_video(update, session, target="cartoon")
        return

    session["state"] = STATE_MENU
    await _reply(update, "Выбор персонажей обновлён. Выберите действие.", markup=guest_main_menu_markup())


def _reset_cartoon_final_build_statuses(session: dict[str, Any]) -> None:
    with SessionLocal() as db:
        status_rows = db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == _run_uuid(session),
                GenerationArtifact.artifact_type == "final_build_status",
            )
        ).scalars().all()
        for row in status_rows:
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            if str(payload.get("variant") or "") in {"clean", "upscaled_4k"}:
                db.delete(row)
        db.commit()
    _guest_data(session).pop("final_review_prompted_run_id", None)


def _queue_cartoon_final_build(
    chat_id: int,
    session: dict[str, Any],
    *,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    data = _guest_data(session)
    scenes = [
        {**item, "guest_mode": True}
        for item in data.get("approved_scenes") or []
        if isinstance(item, dict)
    ]
    if not scenes:
        raise RuntimeError("Нет утверждённых сцен для сборки.")
    if force_rebuild or data.get("rebuilding_after_scene_edit"):
        _reset_cartoon_final_build_statuses(session)
    session["production"] = {"json_plan": scenes}
    session["target_languages"] = ["ru"]
    session["selected_project_key"] = f"guest-{session.get('bot_user_id')}"
    with SessionLocal() as db:
        existing = db.execute(
            select(GenerationDraft).where(
                GenerationDraft.run_id == _run_uuid(session),
                GenerationDraft.chat_id == chat_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                GenerationDraft(
                    chat_id=chat_id,
                    run_id=_run_uuid(session),
                    title="Гостевой мультфильм",
                    stage="final_render_pending",
                    language="ru",
                    session_snapshot_json={},
                    payload_json={"guest_mode": True},
                )
            )
            db.commit()
    return queue_final_builds_for_session(
        chat_id,
        session,
        scenes,
        languages=["ru"],
        variants=["clean", "upscaled_4k"],
        kick_worker=True,
        advance_series_after_success=False,
    )


def _refresh_final_review_session_if_needed(
    chat_id: int,
    session: dict[str, Any],
    identity: BotIdentity,
    text: str,
) -> dict[str, Any]:
    if str(session.get("state") or "") != STATE_MENU or text not in {BUTTON_OK, BUTTON_FIX_SCENES, BUTTON_ADD_SCENE}:
        return session
    persisted = load_session_if_possible(chat_id)
    if not _session_matches_identity(persisted, identity):
        return session
    if str(persisted.get("state") or "") != STATE_CARTOON_FINAL_REVIEW:
        return session
    session.clear()
    session.update(persisted)
    return session


async def guest_on_text(update: Update, context: ContextTypes.DEFAULT_TYPE, identity: BotIdentity) -> None:
    if update.message is None or update.effective_chat is None:
        return
    chat_id = int(update.effective_chat.id)
    session = SESSIONS.get(chat_id)
    if not _session_matches_identity(session, identity):
        session = _base_session(identity)
        SESSIONS[chat_id] = session
    text = " ".join(str(update.message.text or "").split())
    session = _refresh_final_review_session_if_needed(chat_id, session, identity, text)
    state = str(session.get("state") or STATE_MENU)
    data = _guest_data(session)
    try:
        if state == STATE_SYNTAX_PENDING:
            target = str(data.get("syntax_retry_target") or "video")
            key = _guest_syntax_task_key(chat_id, session, target)
            running = _GUEST_SYNTAX_TASKS.get(key)
            if running is not None and not running.done():
                await _reply(
                    update,
                    "Генерация через Syntax → Veo уже выполняется. Очередь дождусь и пришлю видео автоматически.",
                    markup=guest_keyboard_for_session(session),
                )
            else:
                session["state"] = STATE_SYNTAX_ERROR
                save_loaded_session_if_possible(chat_id)
                await _reply(
                    update,
                    "Предыдущая генерация была прервана. Нажмите «Повторить», чтобы запустить её снова.",
                    markup=guest_keyboard_for_session(session),
                )
            return

        if text == BUTTON_MENU:
            await _show_menu(update, session)
            return

        if text == BUTTON_CHANGE_CHARACTERS and await _begin_character_reselection(update, session):
            return

        if text == BUTTON_BACK_TO_IMAGE and (
            state in {
                STATE_CARTOON_VIDEO_DESCRIPTION,
                STATE_CARTOON_VIDEO_APPROVAL,
                STATE_CARTOON_VIDEO_FEEDBACK,
            }
            or (
                state == STATE_SYNTAX_ERROR
                and str(data.get("syntax_retry_target") or "") == "cartoon"
            )
        ):
            await _return_cartoon_to_image_step(update, session)
            return

        if text == BUTTON_REBUILD_CARTOON:
            if not _can_rebuild_cartoon(session):
                await _reply(update, "Нет сохранённого мультфильма для пересборки.", markup=guest_keyboard_for_session(session))
                return
            await _reply(
                update,
                "Повторно ставлю чистую и 4K-версии в очередь. Утверждённые сцены сохранены.",
                markup=ReplyKeyboardRemove(),
            )
            result = await asyncio.to_thread(
                _queue_cartoon_final_build,
                chat_id,
                session,
                force_rebuild=True,
            )
            _update_run_status(session, "final_render_pending", active=True)
            session["state"] = STATE_MENU
            await _reply(
                update,
                f"Пересборка запущена. После завершения бот снова пришлёт clean и 4K. Номер запуска: {result['run_id']}",
                markup=guest_main_menu_markup(session),
            )
            return

        if state == STATE_GENERATION_HISTORY:
            if text == BUTTON_HISTORY_PREVIOUS:
                data["history_page"] = max(0, int(data.get("history_page") or 0) - 1)
                await _reply(update, "Ваши генерации:", markup=guest_keyboard_for_session(session))
                return
            if text == BUTTON_HISTORY_NEXT:
                data["history_page"] = int(data.get("history_page") or 0) + 1
                await _reply(update, "Ваши генерации:", markup=guest_keyboard_for_session(session))
                return
            mapping = data.get("history_buttons")
            run_id = str((mapping or {}).get(text) or "") if isinstance(mapping, dict) else ""
            if not run_id:
                _generation_history_markup(session)
                mapping = data.get("history_buttons")
                run_id = str((mapping or {}).get(text) or "") if isinstance(mapping, dict) else ""
            if not run_id:
                await _reply(update, "Выберите генерацию кнопкой.", markup=guest_keyboard_for_session(session))
                return
            await _open_guest_run_from_history(update, session, identity, run_id)
            return

        if state == STATE_VIDEO_FORMAT:
            aspect_ratio = {
                BUTTON_FORMAT_VERTICAL: "9:16",
                BUTTON_FORMAT_HORIZONTAL: "16:9",
            }.get(text)
            if aspect_ratio is None:
                await _reply(update, "Выберите формат видео кнопкой.", markup=guest_keyboard_for_session(session))
                return
            target = str(data.get("format_target") or "video")
            selection_state = STATE_CARTOON_CHARACTERS if target == "cartoon" else STATE_VIDEO_CHARACTERS
            await _begin_selection(update, session, selection_state, aspect_ratio=aspect_ratio)
            if target == "cartoon":
                _new_run(session, "cartoon", title="Гостевой мультфильм")
                _guest_data(session)["approved_scenes"] = []
            return

        if state == STATE_CARTOON_FINAL_REVIEW:
            if text == BUTTON_OK:
                await _show_menu(update, session)
            elif text == BUTTON_FIX_SCENES:
                session["state"] = STATE_CARTOON_EDIT_SCENE_SELECT
                await _reply(
                    update,
                    "Какую сцену нужно исправить?",
                    markup=guest_keyboard_for_session(session),
                )
            elif text == BUTTON_ADD_SCENE:
                session["state"] = STATE_CARTOON_INSERT_POSITION
                await _reply(
                    update,
                    "После какой сцены добавить новую сцену?",
                    markup=guest_keyboard_for_session(session),
                )
            else:
                await _reply(
                    update,
                    "Подтвердите результат, исправьте, добавьте сцену или запустите пересборку.",
                    markup=guest_keyboard_for_session(session),
                )
            return

        if state == STATE_CARTOON_INSERT_POSITION:
            insert_at = _cartoon_insert_index_from_button(session, text)
            if insert_at is None:
                await _reply(update, "Выберите место для новой сцены кнопкой.", markup=guest_keyboard_for_session(session))
                return
            _begin_cartoon_scene_insertion(session, insert_at)
            session["state"] = STATE_CARTOON_CHARACTERS
            await _reply(
                update,
                f"Новая сцена будет добавлена после сцены {insert_at}. Выберите персонажей для неё.",
                markup=guest_keyboard_for_session(session),
            )
            return

        if state == STATE_CARTOON_EDIT_SCENE_SELECT:
            scene_index = _cartoon_scene_index_from_button(session, text)
            if scene_index is None:
                await _reply(update, "Выберите сцену кнопкой.", markup=guest_keyboard_for_session(session))
                return
            _restore_cartoon_scene_for_edit(session, scene_index)
            session["state"] = STATE_CARTOON_EDIT_STAGE_SELECT
            await _reply(
                update,
                f"С какого этапа исправить сцену {scene_index + 1}?",
                markup=guest_keyboard_for_session(session),
            )
            return

        if state == STATE_CARTOON_EDIT_STAGE_SELECT:
            scene_number = int(data.get("scene_index") or 0) + 1
            if text == BUTTON_FROM_IMAGE:
                data["frame_feedback"] = []
                data["continued_from_previous_scene"] = False
                session["state"] = STATE_CARTOON_FRAME_FEEDBACK
                await _reply(
                    update,
                    f"Отправьте новый промпт для изображения сцены {scene_number} или пришлите свой стартовый кадр.",
                    markup=guest_keyboard_for_session(session),
                )
            elif text == BUTTON_FROM_VIDEO:
                data["cartoon_video_feedback"] = []
                session["state"] = STATE_CARTOON_VIDEO_FEEDBACK
                await _reply(
                    update,
                    f"Отправьте полный новый промпт для видео сцены {scene_number}.",
                    markup=guest_keyboard_for_session(session),
                )
            else:
                await _reply(update, "Выберите «С изображения» или «С видео».", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CARTOON_EDIT_SCENE_DONE:
            if text == BUTTON_FIX_ANOTHER_SCENE:
                session["state"] = STATE_CARTOON_EDIT_SCENE_SELECT
                await _reply(update, "Какую ещё сцену нужно исправить?", markup=guest_keyboard_for_session(session))
            elif text == BUTTON_ADD_SCENE:
                session["state"] = STATE_CARTOON_INSERT_POSITION
                await _reply(update, "После какой сцены добавить новую сцену?", markup=guest_keyboard_for_session(session))
            elif text == BUTTON_DONE:
                session["state"] = STATE_CARTOON_FINISH_CONFIRM
                await _reply(update, "Всё, готово?", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Выберите следующее действие.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_MENU:
            if text == MENU_ADD_CHARACTER:
                session.pop("generation_run_id", None)
                session.pop("scene_db_ids_by_index", None)
                _clear_workflow_data(session)
                session["state"] = STATE_CHARACTER_METHOD
                await _reply(update, "Выберите способ добавления персонажа.", markup=guest_keyboard_for_session(session))
            elif session.get("project_workspace_mode") is True and text == MENU_PRIMARY_CHARACTER:
                if not _list_project_characters(_project_uuid(session)):
                    await _reply(
                        update,
                        "В проекте пока нет персонажей. Сначала добавьте персонажа.",
                        markup=guest_main_menu_markup(session),
                    )
                    return
                session["state"] = STATE_PRIMARY_CHARACTER
                await _reply(
                    update,
                    "Выберите персонажа, которого нужно добавить к основным. Звёздочками отмечены текущие основные персонажи.",
                    markup=guest_keyboard_for_session(session),
                )
            elif session.get("project_workspace_mode") is True and text == MENU_CREATE_STORY:
                session["guest_mode"] = False
                session["project_workspace_mode"] = False
                session["state"] = "state_waiting_first_step_generate_text"
                session["force_story_mode"] = True
                SESSIONS[chat_id] = session
                await _reply(
                    update,
                    "Пришлите тему, факты или готовый текст истории. Ролик будет создан в режиме «История» со стилистиками кулинарного проекта.",
                    markup=ReplyKeyboardRemove(),
                )
            elif session.get("project_workspace_mode") is True and text == MENU_CREATE_VISUALIZATION:
                session["guest_mode"] = False
                session["project_workspace_mode"] = False
                session["force_story_mode"] = False
                session[GENERATION_MODE_SESSION_KEY] = GENERATION_MODE_VISUALIZATION
                session["target_languages"] = ["ru"]
                session["state"] = STATE_WAITING_VISUALIZATION_MEDIA
                SESSIONS[chat_id] = session
                await _reply(
                    update,
                    "Пришлите аудио, голосовое сообщение или видео. Из видео я отделю звук, распознаю текст "
                    "с таймингами, затем предложу выбрать стилистику и построю визуальный ряд по правилам проекта.",
                    markup=ReplyKeyboardRemove(),
                )
            elif session.get("project_workspace_mode") is True and text == MENU_START_OVER:
                from .handlers.handlers_keyboards import first_step_menu_markup

                session["guest_mode"] = False
                session["project_workspace_mode"] = False
                session["state"] = ""
                SESSIONS[chat_id] = session
                await _reply(
                    update,
                    "Выберите действие. Для перехода нажмите «Проект».",
                    markup=first_step_menu_markup(),
                )
            elif session.get("project_workspace_mode") is True and text == MENU_SAVE_DRAFT:
                from .drafts import save_generation_draft

                draft = save_generation_draft(chat_id, session, title="Дисциплина")
                await _reply(
                    update,
                    f"Черновик проекта сохранён. id: {draft.id}",
                    markup=guest_main_menu_markup(session),
                )
            elif text == MENU_IMAGE:
                await _begin_selection(update, session, STATE_IMAGE_CHARACTERS)
            elif text == MENU_VIDEO:
                _clear_workflow_data(session)
                data["format_target"] = "video"
                session["state"] = STATE_VIDEO_FORMAT
                await _reply(update, "Выберите формат видео.", markup=guest_keyboard_for_session(session))
            elif text == MENU_CARTOON:
                _clear_workflow_data(session)
                data["format_target"] = "cartoon"
                session["state"] = STATE_VIDEO_FORMAT
                await _reply(update, "Выберите формат мультфильма.", markup=guest_keyboard_for_session(session))
            elif text == MENU_GENERATIONS:
                save_loaded_session_if_possible(chat_id)
                data["history_page"] = 0
                session["state"] = STATE_GENERATION_HISTORY
                items = _generation_history_items(session)
                if items:
                    await _reply(update, "Ваши генерации. Выберите нужную по дате и номеру за день.", markup=guest_keyboard_for_session(session))
                else:
                    await _reply(update, "У вас пока нет сохранённых генераций.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Выберите действие кнопкой.", markup=guest_main_menu_markup(session))
            return

        if state == STATE_PRIMARY_CHARACTER:
            mapping = data.get("primary_character_buttons")
            character_id = str((mapping or {}).get(text) or "") if isinstance(mapping, dict) else ""
            if not character_id:
                _primary_character_markup(session)
                mapping = data.get("primary_character_buttons")
                character_id = str((mapping or {}).get(text) or "") if isinstance(mapping, dict) else ""
            name = _set_primary_character(session, character_id) if character_id else None
            if not name:
                await _reply(update, "Выберите персонажа кнопкой.", markup=guest_keyboard_for_session(session))
                return
            session["state"] = STATE_MENU
            await _reply(
                update,
                f"Добавлен основной персонаж проекта: {name}.",
                markup=guest_main_menu_markup(session),
            )
            return

        if state == STATE_CHARACTER_METHOD:
            if text not in {"Загрузить", "Сгенерировать"}:
                await _reply(update, "Выберите «Загрузить» или «Сгенерировать».", markup=guest_keyboard_for_session(session))
                return
            data["character_method"] = "upload" if text == "Загрузить" else "generate"
            session["state"] = STATE_CHARACTER_NAME
            await _reply(update, "Введите имя персонажа.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CHARACTER_NAME:
            if not text:
                await _reply(update, "Имя обязательно. Введите имя персонажа.", markup=guest_keyboard_for_session(session))
                return
            data["character_name"] = text
            session["state"] = STATE_CHARACTER_SHORT_DESCRIPTION
            await _reply(
                update,
                "Введите краткое описание персонажа без имени, например: «девочка с двумя косичками» или «мальчик-хулиган». Это поле обязательно.",
                markup=guest_keyboard_for_session(session),
            )
            return

        if state == STATE_CHARACTER_SHORT_DESCRIPTION:
            short_description = _compact_character_description(text, str(data.get("character_name") or ""))
            if not short_description:
                await _reply(
                    update,
                    "Краткое описание обязательно и должно отличаться от имени персонажа. Например: «светловолосая девочка с синим бантом».",
                    markup=guest_keyboard_for_session(session),
                )
                return
            data["short_description"] = short_description
            session["state"] = STATE_CHARACTER_VOICE
            await _reply(update, "Опишите голос персонажа. Это поле обязательно.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CHARACTER_VOICE:
            if not text:
                await _reply(update, "Описание голоса обязательно. Введите его текстом.", markup=guest_keyboard_for_session(session))
                return
            data["voice_description"] = text
            if data.get("character_method") == "upload":
                session["state"] = STATE_CHARACTER_UPLOAD
                await _reply(update, "Пришлите изображение персонажа.", markup=guest_keyboard_for_session(session))
            else:
                session["state"] = STATE_CHARACTER_APPEARANCE
                await _reply(update, "Опишите внешность персонажа.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CHARACTER_APPEARANCE:
            if not text:
                await _reply(update, "Описание внешности обязательно.", markup=guest_keyboard_for_session(session))
                return
            data["appearance"] = text
            _ensure_character_run(session)
            await _generate_image(update, session, target="character")
            return

        if state == STATE_CHARACTER_UPLOAD_APPROVAL:
            if text == BUTTON_OK:
                _approve_image_candidate(session)
                _create_character_from_draft(session)
                _update_run_status(session, "completed", active=False)
                await _show_menu(update, session)
            elif text == "Загрузить снова":
                session["state"] = STATE_CHARACTER_UPLOAD
                await _reply(update, "Пришлите другое изображение.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Подтвердите изображение или загрузите другое.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CHARACTER_IMAGE_APPROVAL:
            if text == BUTTON_OK:
                _approve_image_candidate(session)
                _create_character_from_draft(session)
                _update_run_status(session, "completed", active=False)
                await _show_menu(update, session)
            elif text == BUTTON_REGENERATE:
                await _generate_image(update, session, target="character")
            elif text in {BUTTON_NEW_PROMPT, LEGACY_BUTTON_FEEDBACK}:
                session["state"] = STATE_CHARACTER_FEEDBACK
                await _reply(update, "Отправьте полный новый промпт внешности персонажа.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Используйте кнопки под изображением.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CHARACTER_FEEDBACK:
            if not text:
                await _reply(update, "Отправьте полный новый промпт внешности персонажа.", markup=guest_keyboard_for_session(session))
                return
            data["appearance"] = text
            data["character_feedback"] = []
            await _generate_image(update, session, target="character")
            return

        if state in {STATE_IMAGE_CHARACTERS, STATE_VIDEO_CHARACTERS, STATE_CARTOON_CHARACTERS}:
            if state == STATE_CARTOON_CHARACTERS and text == BUTTON_CONTINUE_SCENE:
                await _continue_cartoon_from_previous_video(update, session)
                return
            if text != BUTTON_DONE and _toggle_character(session, text):
                await _reply(update, "Выбор обновлён.", markup=guest_keyboard_for_session(session))
                return
            if text != BUTTON_DONE:
                await _reply(update, "Выберите персонажа или нажмите «Готово».", markup=guest_keyboard_for_session(session))
                return
            selected_ids = [str(value) for value in data.get("selected_character_ids") or []]
            if state == STATE_CARTOON_CHARACTERS and not selected_ids and _previous_selected_character_ids(session):
                session["state"] = STATE_CARTOON_EMPTY_CHARACTERS_CONFIRM
                await _reply(
                    update,
                    "В предыдущей сцене были персонажи. Создать эту сцену без персонажей?",
                    markup=guest_keyboard_for_session(session),
                )
                return
            data["character_selection_confirmed"] = True
            data["allow_empty_character_selection"] = not bool(selected_ids)
            return_state = str(data.get("character_selection_return_state") or "")
            if return_state:
                await _resume_after_character_reselection(update, session, return_state)
                return
            if state == STATE_IMAGE_CHARACTERS:
                _new_run(session, "image", title="Гостевое изображение")
                session["state"] = STATE_IMAGE_DESCRIPTION
                await _reply(update, "Опишите изображение, которое нужно создать.", markup=guest_keyboard_for_session(session))
            elif state == STATE_VIDEO_CHARACTERS:
                _new_run(session, "video", title="Гостевое видео")
                session["state"] = STATE_VIDEO_START_IMAGE
                await _reply(update, "Загрузите стартовое изображение для видео.", markup=guest_keyboard_for_session(session))
            else:
                session["state"] = STATE_CARTOON_DESCRIPTION
                await _reply(update, "Опишите стартовый кадр сцены или пришлите своё изображение.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CARTOON_EMPTY_CHARACTERS_CONFIRM:
            if text == BUTTON_WITHOUT_CHARACTERS:
                data["selected_character_ids"] = []
                data["character_selection_confirmed"] = True
                data["allow_empty_character_selection"] = True
                return_state = str(data.get("character_selection_return_state") or "")
                if return_state:
                    await _resume_after_character_reselection(update, session, return_state)
                else:
                    session["state"] = STATE_CARTOON_DESCRIPTION
                    await _reply(update, "Опишите стартовый кадр сцены без персонажей или пришлите своё изображение.", markup=guest_keyboard_for_session(session))
            elif text == BUTTON_RETURN_TO_SELECTION:
                data["selected_character_ids"] = _previous_selected_character_ids(session)
                data["character_selection_confirmed"] = False
                data["allow_empty_character_selection"] = False
                session["state"] = STATE_CARTOON_CHARACTERS
                await _reply(
                    update,
                    "Персонажи предыдущей сцены снова выбраны. При необходимости измените выбор.",
                    markup=guest_keyboard_for_session(session),
                )
            else:
                await _reply(update, "Подтвердите сцену без персонажей или вернитесь к выбору.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_IMAGE_DESCRIPTION:
            if not text:
                await _reply(update, "Описание изображения обязательно.", markup=guest_keyboard_for_session(session))
                return
            data["image_description"] = text
            _update_scene_text(session, text, prompt=text)
            await _generate_image(update, session, target="image")
            return

        if state == STATE_IMAGE_APPROVAL:
            if text == BUTTON_OK:
                _approve_image_candidate(session)
                _update_run_status(session, "completed", active=False)
                await _show_menu(update, session)
            elif text == BUTTON_REGENERATE:
                await _generate_image(update, session, target="image")
            elif text in {BUTTON_NEW_PROMPT, LEGACY_BUTTON_FEEDBACK}:
                session["state"] = STATE_IMAGE_FEEDBACK
                await _reply(update, "Отправьте полный новый промпт для изображения.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Используйте кнопки под изображением.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_IMAGE_FEEDBACK:
            if not text:
                await _reply(update, "Отправьте полный новый промпт для изображения.", markup=guest_keyboard_for_session(session))
                return
            data["image_description"] = text
            data["image_feedback"] = []
            _update_scene_text(session, text, prompt=text)
            await _generate_image(update, session, target="image")
            return

        if state == STATE_VIDEO_DESCRIPTION:
            if not text:
                await _reply(update, "Описание движения, действия и речи обязательно.", markup=guest_keyboard_for_session(session))
                return
            data["video_description"] = text
            _update_scene_text(session, text)
            await _generate_video(update, session, target="video")
            return

        if state == STATE_VIDEO_APPROVAL:
            if text == BUTTON_RETRY_DELIVERY:
                await _deliver_current_video(update, session)
            elif text == BUTTON_OK:
                _approve_video_candidate(session)
                _update_run_status(session, "completed", active=False)
                await _show_menu(update, session)
            elif text == BUTTON_REGENERATE:
                await _generate_video(update, session, target="video")
            elif text in {BUTTON_NEW_PROMPT, LEGACY_BUTTON_FEEDBACK}:
                session["state"] = STATE_VIDEO_FEEDBACK
                await _reply(update, "Отправьте полный новый промпт для видео.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Используйте кнопки под видео.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_VIDEO_FEEDBACK:
            if not text:
                await _reply(update, "Отправьте полный новый промпт для видео.", markup=guest_keyboard_for_session(session))
                return
            data["video_description"] = text
            data["video_feedback"] = []
            _update_scene_text(session, text)
            await _generate_video(update, session, target="video")
            return

        if state == STATE_CARTOON_DESCRIPTION:
            if not text:
                await _reply(update, "Описание сцены обязательно.", markup=guest_keyboard_for_session(session))
                return
            data["scene_description"] = text
            _update_scene_text(session, text, prompt=text)
            await _generate_image(update, session, target="cartoon_frame")
            return

        if state == STATE_CARTOON_FRAME_APPROVAL:
            if text == BUTTON_OK:
                _approve_image_candidate(session)
                session["state"] = STATE_CARTOON_VIDEO_DESCRIPTION
                await _reply(
                    update,
                    "Опишите движение, действие и речь для видео этой сцены.",
                    markup=guest_keyboard_for_session(session),
                )
            elif text == BUTTON_REGENERATE:
                await _generate_image(update, session, target="cartoon_frame")
            elif text == BUTTON_CONTINUE_SCENE and _can_continue_cartoon_scene(session):
                await _continue_cartoon_from_previous_video(update, session)
            elif text in {BUTTON_NEW_PROMPT, LEGACY_BUTTON_FEEDBACK}:
                session["state"] = STATE_CARTOON_FRAME_FEEDBACK
                await _reply(update, "Отправьте новый промпт или пришлите свой стартовый кадр.", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Используйте кнопки под стартовым кадром.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CARTOON_FRAME_FEEDBACK:
            if not text:
                await _reply(update, "Отправьте новый промпт или пришлите свой стартовый кадр.", markup=guest_keyboard_for_session(session))
                return
            data["scene_description"] = text
            data["frame_feedback"] = []
            _update_scene_text(session, text, prompt=text)
            await _generate_image(update, session, target="cartoon_frame")
            return

        if state == STATE_CARTOON_CONTINUATION_FRAME_APPROVAL:
            if text == BUTTON_OK:
                _approve_image_candidate(session)
                session["state"] = STATE_CARTOON_VIDEO_DESCRIPTION
                await _reply(
                    update,
                    "Опишите движение, действие и речь для продолжения сцены.",
                    markup=guest_keyboard_for_session(session),
                )
            elif text == BUTTON_GENERATE_IMAGE:
                data["continued_from_previous_scene"] = False
                if not data.get("selected_character_ids"):
                    data["selected_character_ids"] = _previous_selected_character_ids(session)
                data.pop("scene_description", None)
                data.pop("frame_feedback", None)
                session["state"] = STATE_CARTOON_DESCRIPTION
                await _reply(
                    update,
                    "Опишите новый стартовый кадр сцены для генерации через Vertex или пришлите своё изображение.",
                    markup=guest_keyboard_for_session(session),
                )
            else:
                await _reply(
                    update,
                    "Выберите «ОК» или «Сгенерировать изображение».",
                    markup=guest_keyboard_for_session(session),
                )
            return

        if state == STATE_CARTOON_VIDEO_DESCRIPTION:
            if not text:
                await _reply(
                    update,
                    "Промпт для видео обязателен. Опишите движение, действие и речь.",
                    markup=guest_keyboard_for_session(session),
                )
                return
            data["video_description"] = text
            if data.get("continued_from_previous_scene"):
                data["scene_description"] = text
                _update_scene_text(session, text)
            await _generate_video(update, session, target="cartoon")
            return

        if state == STATE_CARTOON_VIDEO_APPROVAL:
            if text == BUTTON_RETRY_DELIVERY:
                await _deliver_current_video(update, session)
            elif text == BUTTON_OK:
                _approve_video_candidate(session)
                scenes = data.setdefault("approved_scenes", [])
                payload = _finish_scene_payload(session)
                index = int(data.get("scene_index") or 0)
                if data.get("inserting_scene"):
                    scenes.insert(index, payload)
                    for scene_number, scene_payload in enumerate(scenes, start=1):
                        if isinstance(scene_payload, dict):
                            scene_payload["scene"] = scene_number
                    _commit_inserted_scene_order(session, index)
                elif len(scenes) > index:
                    scenes[index] = payload
                else:
                    scenes.append(payload)
                if data.get("editing_existing_scene") or data.get("inserting_scene"):
                    data["rebuilding_after_scene_edit"] = True
                    session["state"] = STATE_CARTOON_EDIT_SCENE_DONE
                    message = "Новая сцена утверждена и добавлена." if data.get("inserting_scene") else "Исправленная сцена утверждена."
                    data["inserting_scene"] = False
                    await _reply(update, message, markup=guest_keyboard_for_session(session))
                else:
                    session["state"] = STATE_CARTOON_SCENE_DONE
                    await _reply(update, "Сцена утверждена.", markup=guest_keyboard_for_session(session))
            elif text == BUTTON_REGENERATE:
                await _generate_video(update, session, target="cartoon")
            elif text in {BUTTON_NEW_PROMPT, LEGACY_BUTTON_FEEDBACK}:
                session["state"] = STATE_CARTOON_VIDEO_FEEDBACK
                await _reply(update, "Отправьте полный новый промпт для видео.", markup=guest_keyboard_for_session(session))
            else:
                # A plain text message at video approval is a complete new
                # video prompt. This also lets sessions created before the
                # dedicated video-prompt step continue without restarting the
                # scene from scratch.
                data["video_description"] = text
                data["cartoon_video_feedback"] = []
                await _generate_video(update, session, target="cartoon")
            return

        if state == STATE_CARTOON_VIDEO_FEEDBACK:
            if not text:
                await _reply(update, "Отправьте полный новый промпт для видео.", markup=guest_keyboard_for_session(session))
                return
            data["video_description"] = text
            data["cartoon_video_feedback"] = []
            await _generate_video(update, session, target="cartoon")
            return

        if state == STATE_CARTOON_SCENE_DONE:
            if text == "Следующая сцена":
                approved = list(data.get("approved_scenes") or [])
                previous = approved[-1] if approved else {}
                aspect_ratio = str(previous.get("aspect_ratio") or _guest_aspect_ratio(session))
                previous_selection = [
                    str(value)
                    for value in previous.get("selected_character_ids") or data.get("selected_character_ids") or []
                ]
                _append_cartoon_scene(session)
                data.clear()
                data["approved_scenes"] = approved
                data["selected_character_ids"] = previous_selection
                data["character_selection_confirmed"] = False
                data["character_selection_changed"] = False
                data["allow_empty_character_selection"] = False
                data["previous_video_uri"] = str(previous.get("video_uri") or "")
                data["scene_index"] = len(approved)
                data["scene_id"] = _scene_index_map(session)[len(approved)]
                data["aspect_ratio"] = aspect_ratio if aspect_ratio in {"9:16", "16:9"} else "9:16"
                session["state"] = STATE_CARTOON_CHARACTERS
                selection_text = (
                    "Персонажи предыдущей сцены уже выбраны (отмечены ✓). При необходимости измените выбор."
                    if previous_selection
                    else "Выберите персонажей для следующей сцены."
                )
                await _reply(update, selection_text, markup=guest_keyboard_for_session(session))
            elif text == BUTTON_DONE:
                session["state"] = STATE_CARTOON_FINISH_CONFIRM
                await _reply(update, "Всё, готово?", markup=guest_keyboard_for_session(session))
            else:
                await _reply(update, "Выберите следующее действие.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_CARTOON_FINISH_CONFIRM:
            if text == "Нет, продолжить":
                if data.get("editing_existing_scene") or data.get("insert_scene_at") is not None:
                    session["state"] = STATE_CARTOON_EDIT_SCENE_DONE
                    await _reply(update, "Можно исправить ещё одну сцену или снова завершить.", markup=guest_keyboard_for_session(session))
                else:
                    session["state"] = STATE_CARTOON_SCENE_DONE
                    await _reply(update, "Можно добавить следующую сцену или снова завершить.", markup=guest_keyboard_for_session(session))
            elif text == "Да, склеить":
                await _reply(update, "Ставлю чистую и 4K-версии в очередь финальной сборки.", markup=ReplyKeyboardRemove())
                result = await asyncio.to_thread(_queue_cartoon_final_build, chat_id, session)
                _update_run_status(session, "final_render_pending", active=True)
                session["state"] = STATE_MENU
                await _reply(
                    update,
                    f"Мультфильм поставлен в очередь. После сборки бот пришлёт две версии. Номер запуска: {result['run_id']}",
                    markup=guest_main_menu_markup(session),
                )
            else:
                await _reply(update, "Подтвердите финальную сборку.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_SYNTAX_ERROR:
            if text == BUTTON_RETRY:
                await _generate_video(update, session, target=str(data.get("syntax_retry_target") or "video"))
            else:
                await _reply(update, "Нажмите «Повторить» или вернитесь в меню.", markup=guest_keyboard_for_session(session))
            return

        if state == STATE_VIDEO_DELIVERY_ERROR:
            if text == BUTTON_RETRY_DELIVERY:
                await _deliver_current_video(update, session)
            else:
                await _reply(
                    update,
                    "Нажмите «Повторить отправку» или вернитесь в меню.",
                    markup=guest_keyboard_for_session(session),
                )
            return

        if state == STATE_IMAGE_ERROR:
            if text == BUTTON_RETRY:
                await _generate_image(update, session, target=str(data.get("image_retry_target") or "image"))
            else:
                await _reply(update, "Нажмите «Повторить» или вернитесь в меню.", markup=guest_keyboard_for_session(session))
            return

        await _reply(update, "Шаг не распознан. Вернитесь в меню.", markup=_markup([]))
    except Exception:
        logger.exception("guest.handler_failed state=%s", state)
        await _reply(update, "Произошла ошибка. Текущий шаг сохранён; попробуйте действие ещё раз.", markup=guest_keyboard_for_session(session))
    finally:
        save_loaded_session_if_possible(chat_id)
