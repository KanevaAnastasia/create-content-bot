"""Media/reference helper functions extracted from handlers (part 2)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from typing import Any

from telegram import ReplyKeyboardMarkup, Update

from ..assets import ASSETS
from ..character_catalog import character_identity_lines, format_character_short_descriptions, sanitize_character_names_and_keys
from ..constants import STATE_READY_FOR_PRODUCTION
from .handlers_keyboards import DRAFT_BUTTON_SAVE, safe_reply_photo, safe_reply_text
from .handlers_media_core import (
    _decode_b64,
    _image_end_scene_prompt,
    _image_scene_prompt,
    _normalize_character_key,
    _video_prompt_by_lang_with_candidate_fallback,
)
from ..llm_utils import GEMINI_MODEL_PROFILE_EDIT, gemini_generate_sync
from ..pipeline_llm_steps import RUSSIAN_YO_RULE
from ..pipeline_history import record_checkpoint, restore_scene_prompt_fields_from_history
from ..scene_media_candidates import selected_image_bytes_for_video
from ..session_store import save_session_if_possible
from ..vertex_images import generate_image_vertex, generate_image_vertex_result, next_image_generation_model
from ..visual_props import append_visual_prop_descriptions
from ..grandma_scene_contract import append_episode_prop_locks

logger = logging.getLogger("bot")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VIDEO_HIGH_LOAD_RETRY_DELAY_SEC = 15
SCENE_MEDIA_SUBSTEP_START = "start_prompt"
SCENE_MEDIA_SUBSTEP_END = "end_prompt"
SCENE_MEDIA_SUBSTEP_VIDEO = "video_prompt"
SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL = "start_image_approval"
SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL = "end_image_approval"
SCENE_MEDIA_AUTO_APPROVE_BUTTON = "Автоапрув"
SCENE_MEDIA_AI_APPROVE_BUTTON = "🤖 ИИ апрув"
SCENE_MEDIA_STOP_BUTTON = "СТОП"
SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY = "scene_media_auto_approve"
SCENE_MEDIA_APPROVE_MODE_SESSION_KEY = "scene_media_approve_mode"
SCENE_MEDIA_APPROVE_MODE_MANUAL = "manual"
SCENE_MEDIA_APPROVE_MODE_AUTO = "auto"
SCENE_MEDIA_APPROVE_MODE_AI = "ai"
SCENE_MEDIA_CHANGE_SPEED_BUTTON = "ИЗМЕНИТЬ СКОРОСТЬ"
NAV_BUTTON_PREVIOUS_STEP = "Предыдущий шаг"
NAV_BUTTON_START_OVER = "В начало"
PIXAR_REFERENCE_STYLE_PHRASE = "Пиксаровский стиль референса"
SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON = "ПОВЫСИТЬ МОДЕЛЬ"
SCENE_MEDIA_CONTINUE_PREVIOUS_VIDEO_BUTTON = "Продолжи предыдущее видео"
SCENE_MEDIA_REQUEST_END_FRAME_BUTTON = "Нужен конечный кадр"
SCENE_MEDIA_SKIP_END_FRAME_BUTTON = "Без конечного кадра"
SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON = "Добавить изображения"
SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON = "Готово"
SCENE_MEDIA_CLEAR_REFERENCE_IMAGES_BUTTON = "Очистить изображения"
SCENE_USER_REFERENCE_IMAGES_KEY = "scene_user_reference_images_by_kind"


def _reply_markup_with_rows(*rows: list[str]) -> ReplyKeyboardMarkup:
    """Строит компактную reply-клавиатуру для выбора действий в медиа-шаге."""
    mode_buttons = {
        SCENE_MEDIA_AUTO_APPROVE_BUTTON,
        SCENE_MEDIA_AI_APPROVE_BUTTON,
        SCENE_MEDIA_STOP_BUTTON,
    }
    keyboard = [[item] for row in rows for item in row if item and item not in mode_buttons]
    keyboard.append([DRAFT_BUTTON_SAVE])
    keyboard.append([NAV_BUTTON_PREVIOUS_STEP])
    keyboard.append([NAV_BUTTON_START_OVER])
    for row in rows:
        for item in row:
            if item in mode_buttons:
                keyboard.append([item])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _persist_visible_scene_media_cursor(update: Update, session: dict) -> None:
    effective_chat = getattr(update, "effective_chat", None)
    chat_id = getattr(effective_chat, "id", None)
    if chat_id is None:
        return
    try:
        save_session_if_possible(int(chat_id), session)
    except Exception:
        logger.exception("scene_media.visible_cursor_save_failed chat_id=%s", chat_id)


def _scene_media_mode_row(session: dict | None) -> list[str]:
    """Возвращает кнопки переключения режима апрува для текущей сессии."""
    if not isinstance(session, dict):
        return [SCENE_MEDIA_AUTO_APPROVE_BUTTON, SCENE_MEDIA_AI_APPROVE_BUTTON]
    approve_mode = str(session.get(SCENE_MEDIA_APPROVE_MODE_SESSION_KEY) or "").strip()
    ai_human_review_pending = bool(session.get("scene_media_ai_human_review"))
    if bool(session.get(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY)) or (
        approve_mode == SCENE_MEDIA_APPROVE_MODE_AI and not ai_human_review_pending
    ):
        return [SCENE_MEDIA_STOP_BUTTON]
    return [SCENE_MEDIA_AUTO_APPROVE_BUTTON, SCENE_MEDIA_AI_APPROVE_BUTTON]


def _scene_media_stop_visible(session: dict | None) -> bool:
    return _scene_media_mode_row(session) == [SCENE_MEDIA_STOP_BUTTON]


def scene_image_approval_markup(
    *,
    include_continue_previous: bool = False,
    session: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Кнопки для шага стартового кадра."""
    rows = [
        ["ОК"],
        ["Добавить своё изображение"],
    ]
    if include_continue_previous:
        rows.append([SCENE_MEDIA_CONTINUE_PREVIOUS_VIDEO_BUTTON])
    rows.extend([
        _scene_media_mode_row(session),
        [SCENE_MEDIA_REQUEST_END_FRAME_BUTTON, SCENE_MEDIA_SKIP_END_FRAME_BUTTON],
        [SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON],
        ["ПРОМПТ:"],
        ["ФИДБЕК:"],
        ["старт заново"],
    ])
    return _reply_markup_with_rows(*rows)


def scene_end_approval_markup(*, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Кнопки для шага конечного кадра."""
    return _reply_markup_with_rows(
        ["ОК"],
        ["Добавить своё изображение"],
        _scene_media_mode_row(session),
        [SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON],
        ["ПРОМПТ:"],
        ["ФИДБЕК:"],
        ["старт заново"],
    )


def scene_video_approval_markup(
    *,
    include_restart_end: bool,
    languages: list[str] | None = None,
    session: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Кнопки для шага видео: подтверждение, правки prompt и рестарты кадров."""
    langs = [str(x).strip().lower() for x in (languages or []) if str(x).strip()]
    rows = [
        ["ОК"],
        _scene_media_mode_row(session),
        [SCENE_MEDIA_CHANGE_SPEED_BUTTON],
        ["ПРОМПТ:"],
        ["ФИДБЕК:"],
        ["ФРАЗА:"],
    ]
    if len(langs) > 1:
        for lang in langs:
            rows.append([f"ПРОМПТ {lang}:"])
        for lang in langs:
            rows.append([f"ФРАЗА {lang}:"])
    rows.append(["старт заново"])
    if include_restart_end:
        rows.append(["конец заново"])
    return _reply_markup_with_rows(*rows)


def scene_generated_image_approval_markup(
    *,
    include_model_upgrade: bool = False,
    include_end_frame_choice: bool = False,
    session: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Кнопки апрува уже сгенерированного изображения перед следующим шагом."""
    rows = [
        ["ОК"],
        ["Добавить своё изображение"],
        _scene_media_mode_row(session),
        [SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON],
        ["ПРОМПТ:"],
        ["ИЗМЕНИТЬ"],
        ["ФИДБЕК"],
        ["ПЕРЕГЕНЕРИРОВАТЬ"],
    ]
    if include_end_frame_choice:
        rows.append([SCENE_MEDIA_REQUEST_END_FRAME_BUTTON, SCENE_MEDIA_SKIP_END_FRAME_BUTTON])
    if include_model_upgrade:
        rows.append([SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON])
    return _reply_markup_with_rows(*rows)


def scene_reference_images_markup() -> ReplyKeyboardMarkup:
    """Кнопки режима добавления пользовательских референсов к фрейму."""
    return _reply_markup_with_rows(
        [SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON],
        [SCENE_MEDIA_CLEAR_REFERENCE_IMAGES_BUTTON],
    )


def scene_language_approval_markup(languages: list[str], *, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Кнопки апрува для мультиязычной сцены после генерации всех языков."""
    langs = [str(x).strip().lower() for x in languages if str(x).strip()]
    rows: list[list[str]] = [["ОК все"], _scene_media_mode_row(session)]
    for lang in langs:
        rows.append([f"ОК {lang}"])
    rows.append([SCENE_MEDIA_CHANGE_SPEED_BUTTON])
    rows.append(["ФИДБЕК:"])
    for lang in langs:
        rows.append([f"ПРОМПТ {lang}:"])
    for lang in langs:
        rows.append([f"ФРАЗА {lang}:"])
    for lang in langs:
        rows.append([f"ПЕРЕГЕНЕРИРОВАТЬ {lang}"])
    return _reply_markup_with_rows(*rows)


def video_policy_retry_markup(*, include_restart_hint: bool, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Кнопки для policy-retry video prompt."""
    rows = [
        ["ОК"],
        ["ПРОМПТ:"],
        ["ФОРМАТ:"],
        ["ФИДБЕК:"],
    ]
    if include_restart_hint:
        rows.append(["старт заново"])
    if _scene_media_stop_visible(session):
        rows.append([SCENE_MEDIA_STOP_BUTTON])
    return _reply_markup_with_rows(*rows)


def ok_only_markup() -> ReplyKeyboardMarkup:
    """Унифицированные кнопки подтверждения и правок для текстовых шагов."""
    return _reply_markup_with_rows(
        ["ОК"],
        ["Добавь"],
        ["Исправь"],
    )


async def _reply_text_with_optional_markup(update: Update, text: str, reply_markup=None) -> None:
    """Отправляет текст с клавиатурой; в тестах без kwargs безопасно деградирует."""
    if update.message is None:
        return
    chunks: list[str] = []
    raw = str(text or "")
    while len(raw) > 3900:
        cut = raw.rfind("\n", 0, 3900)
        if cut < 1200:
            cut = 3900
        chunks.append(raw[:cut].rstrip())
        raw = raw[cut:].lstrip()
    chunks.append(raw)
    for chunk in chunks[:-1]:
        await safe_reply_text(update.message, chunk)
    if reply_markup is None:
        await safe_reply_text(update.message, chunks[-1])
        return
    await safe_reply_text(update.message, chunks[-1], reply_markup=reply_markup)

def _video_approval_text(scene: dict, scene_index: int, total: int, *, languages: list[str] | None = None) -> str:
    prompt_by_lang = _video_prompt_by_lang_with_candidate_fallback(scene)
    resolved_languages: list[str] = []
    if languages:
        for key in languages:
            code = str(key).strip().lower()
            if code and code not in resolved_languages:
                resolved_languages.append(code)
    if not resolved_languages:
        for key in prompt_by_lang:
            code = str(key).strip().lower()
            if code and code not in resolved_languages:
                resolved_languages.append(code)
    if not resolved_languages:
        resolved_languages = ["ru"]
    current_lang = str(scene.get("video_prompt_current_language", "")).strip().lower() or "ru"
    fallback_prompt = str(scene.get("video_prompt", "")).strip()
    if not any(prompt_by_lang.values()) and fallback_prompt:
        prompt_by_lang[current_lang] = fallback_prompt
    prompt_blocks = []
    for lang in resolved_languages:
        prompt_blocks.append(f"[{lang}]\n{prompt_by_lang.get(lang, '').strip() or '—'}")
    prompts_text = "\n\n".join(prompt_blocks) if prompt_blocks else "—"
    model = str(scene.get("model", "kling")).strip()
    duration = scene.get("duration_sec", 4)
    end_extra = ""
    if _scene_needs_end_frame_step(scene):
        end_extra = (
            "\n• «конец заново» — вернуться к дельте конечного кадра и перегенерировать только конечное изображение "
            "(стартовый кадр сохранится)."
        )
    dialogue_block = ""
    if str(scene.get("dialogue_mode") or "").strip().lower() == "multi" and isinstance(scene.get("dialogue"), list):
        lines: list[str] = []
        for item in scene.get("dialogue") or []:
            if not isinstance(item, dict):
                continue
            speaker_key = str(item.get("speaker") or "").strip()
            speaker = format_character_short_descriptions([speaker_key]) or speaker_key
            text = str(item.get("text") or "").strip()
            if speaker and text:
                lines.append(f"• {speaker}: {text}")
        if lines:
            dialogue_block = "Структура говорящих в сцене 1:\n" + "\n".join(lines) + "\n\n"
    return (
        f"Сцена {scene_index}/{total} — апрув перед генерацией видео.\n"
        f"Модель: {model}, длительность: {duration} сек\n"
        f"Язык текущей генерации: {current_lang}\n"
        f"Языков сцены: {', '.join(resolved_languages)}\n\n"
        f"{dialogue_block}"
        "VIDEO PROMPT (как уйдут в генератор) — по языкам:\n"
        f"{prompts_text}\n\n"
        "Варианты:\n"
        '• «ОК» — сгенерировать видео.\n'
        '• «ПРОМПТ: ...» — вручную заменить video prompt.\n'
        '• «ФИДБЕК: ...» — перегенерировать video prompt по фидбеку.\n'
        '• «ФРАЗА: ...» — отдельно обновить фразу для озвучки.\n'
        '• «ПРОМПТ <lang>: ...» — вручную заменить prompt конкретного языка.\n'
        '• «ФИДБЕК: ...» — правка текущего video prompt через нейросеть.\n'
        '• «ФРАЗА <lang>: ...» — обновить фразу конкретного языка до генерации.\n'
        '• «ИЗМЕНИТЬ СКОРОСТЬ» — бот попросит коэффициент и после ответа сразу заапрувит текущее видео.\n'
        '• «СКОРОСТЬ 1.25» — сохранить коэффициент для выбранного видео этой сцены перед финальной склейкой.\n'
        '• «ОК 1.25» — для одноязычной сцены сразу утвердить видео с этим коэффициентом.\n'
        "• «старт заново» — вернуться к стартовому промпту: правь текст или «ФИДБЕК», затем «ОК» снова сгенерирует стартовый кадр "
        "(конечный кадр сбросится, если был)."
        f"{end_extra}"
    )


def _scene_media_prompt_languages(session: dict, prompt_map: dict[str, str]) -> list[str]:
    try:
        idx = int(session.get("image_scene_index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    repair_plan = session.get("draft_repair_active_plan")
    if isinstance(repair_plan, list):
        for item in repair_plan:
            if not isinstance(item, dict):
                continue
            try:
                item_idx = int(item.get("scene_index"))
            except (TypeError, ValueError):
                continue
            if item_idx != idx:
                continue
            languages = [
                str(lang).strip().lower()
                for lang in item.get("languages", [])
                if str(lang).strip()
            ]
            if languages:
                return list(dict.fromkeys(languages))
    target_languages = session.get("target_languages")
    if isinstance(target_languages, list):
        languages = [str(lang).strip().lower() for lang in target_languages if str(lang).strip()]
        if languages:
            return list(dict.fromkeys(languages))
    languages = [str(lang).strip().lower() for lang in prompt_map if str(lang).strip()]
    return list(dict.fromkeys(languages)) or ["ru"]


def _scene_has_any_generated_video(scene: dict) -> bool:
    results = scene.get("video_results_by_lang")
    if isinstance(results, dict):
        for item in results.values():
            if isinstance(item, dict) and str(item.get("video_b64") or item.get("media_uri") or "").strip():
                return True
    candidates_by_lang = scene.get("video_candidates_by_lang")
    if isinstance(candidates_by_lang, dict):
        for candidates in candidates_by_lang.values():
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if isinstance(candidate, dict) and str(candidate.get("video_b64") or candidate.get("media_uri") or "").strip():
                    return True
    return False


def _scene_needs_end_frame_step(scene: dict) -> bool:
    """Нужен ли отдельный шаг конечного кадра (дельта + референс старта)."""
    end_prompt = str(scene.get("end_frame_prompt", "") or scene.get("end_frame_prompt_ru", "")).strip()
    if not end_prompt:
        return False
    if end_prompt.lower() == "пусто":
        return False
    return bool(scene.get("end_frame_requested"))


def _raw_end_frame_text(scene: dict) -> str:
    return str(scene.get("end_frame_prompt", "") or scene.get("end_frame_prompt_ru", "")).strip()


def _set_end_frame_text(scene: dict, value: str) -> None:
    v = value.strip()
    scene["end_frame_prompt"] = v
    scene["end_frame_prompt_ru"] = v


def _media_cmd_first_line(text: str) -> str:
    """Первая строка сообщения, нижний регистр, без хвостовой пунктуации — для команд «старт заново» и т.п."""
    line = (text or "").strip().split("\n")[0].strip().lower()
    return line.rstrip(".!?,;:").strip()


def _extract_prefixed_payload(text: str, prefixes: tuple[str, ...]) -> str | None:
    """Возвращает текст после первого совпавшего префикса `xxx:` (без двоеточия)."""
    low = (text or "").strip().lower()
    for p in prefixes:
        marker = f"{p}:"
        if low.startswith(marker):
            return text.split(":", 1)[1].strip()
    return None


def _strip_wrapping_code_fence(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2:
            body = "\n".join(lines[1:-1]).strip()
            return body or raw
    return raw


def _extract_prompt_override(text: str) -> str | None:
    """Поддерживает оба варианта команды: ПРОМПТ и ПРОМТ."""
    payload = _extract_prefixed_payload(text, ("промпт", "промт"))
    if payload is None:
        return None
    return _strip_wrapping_code_fence(payload)


def _extract_format_or_feedback(text: str) -> str | None:
    """Возвращает payload для `ФОРМАТ:` или `ФИДБЕК:`."""
    return _extract_prefixed_payload(text, ("формат", "фидбек"))


VIDEO_PROMPT_POLICY_FAILURE_THRESHOLD = 4

_START_FRAME_REGEN_HINT = (
    "\n\nПосле нескольких отклонений подряд имеет смысл сменить входное изображение: "
    "напишите «старт заново» — вернёмся к шагу стартового кадра, пересгенерируем кадр "
    "и попробуем видео снова."
)


def _should_suggest_regenerate_start_frame_after_video_prompt_fails(scene: dict) -> bool:
    """True, если уже было достаточно неудачных попыток видео по policy с переписыванием prompt."""
    try:
        n = int(scene.get("video_prompt_policy_failure_count", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return n >= VIDEO_PROMPT_POLICY_FAILURE_THRESHOLD


def _video_policy_prompt_approval_text(
    prompt_text: str,
    *,
    suggest_regenerate_start_frame: bool = False,
) -> str:
    base = (
        "[Policy retry mode]\n"
        "Видео отклонено политиками/SAFETY. Ниже кандидат prompt для перегенерации:\n"
        f"{prompt_text}\n\n"
        "Варианты:\n"
        '• «ОК» — запустить перегенерацию с этим prompt.\n'
        '• «ПРОМПТ: ...» или «ПРОМТ: ...» — задать prompt вручную.\n'
        '• «ФОРМАТ: ...» или «ФИДБЕК: ...» — переписать prompt в нужном формате/стиле перед запуском.'
    )
    if suggest_regenerate_start_frame:
        return f"{base}{_START_FRAME_REGEN_HINT}"
    return base


async def _rewrite_video_prompt_by_format_instruction(current_prompt: str, format_instruction: str) -> str:
    """
    Переписывает video prompt по пользовательскому формату.
    Возвращает только один готовый prompt на английском.
    """
    prompt = (
        "Перепиши video prompt по фидбеку пользователя.\n"
        "Верни только один готовый prompt на английском, без пояснений, заголовков и служебных меток.\n"
        "Не копируй в ответ строки `ФИДБЕК ПОЛЬЗОВАТЕЛЯ`, `ТЕКУЩИЙ VIDEO PROMPT` или похожую разметку.\n"
        "Сохрани смысл сцены, действие и предметную непрерывность.\n\n"
        f"{RUSSIAN_YO_RULE}\n\n"
        f"ФИДБЕК ПОЛЬЗОВАТЕЛЯ:\n{format_instruction.strip()}\n\n"
        f"ТЕКУЩИЙ VIDEO PROMPT:\n{current_prompt.strip()}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
    return raw.strip()


def _wants_restart_start_frame(text: str) -> bool:
    """Пользователь просит снова согласовать стартовый кадр (промпт ± генерация)."""
    fl = _media_cmd_first_line(text)
    return fl in {
        "старт заново",
        "пересогласовать старт",
        "назад к старту",
        "перегенерировать стартовый кадр",
        "перегенерировать старт",
        "restart start",
        "redo start",
    }


def _wants_restart_end_frame(text: str) -> bool:
    """Пользователь просит снова согласовать только конечный кадр (дельта ± генерация)."""
    fl = _media_cmd_first_line(text)
    return fl in {
        "конец заново",
        "пересогласовать конец",
        "перегенерировать конечный кадр",
        "перегенерировать конец",
        "restart end",
        "redo end",
    }


def _end_frame_approval_text(scene: dict, scene_index: int, total: int) -> str:
    raw = _raw_end_frame_text(scene)
    if not raw or raw.lower() == "пусто":
        raw = "—"
    shot = scene.get("shot_type", "medium")
    anchors = scene.get("continuity_anchors") or []
    anchors_text = ", ".join(str(x) for x in anchors if str(x).strip()) or "—"
    return (
        f"Сцена {scene_index}/{total} — конечный кадр (дельта к старту; старт уже сгенерирован как референс).\n"
        f"Ракурс: {shot}\n"
        f"Опоры консистентности: {anchors_text}\n\n"
        "Текст дельты для конечного изображения:\n"
        f"{raw}\n\n"
        "Варианты:\n"
        '• «ОК» — сгенерировать конечный кадр.\n'
        '• «ПРОМПТ: ...» — заменить текст дельты вручную.\n'
        '• «ФИДБЕК: ...» — переписать дельту через нейросеть.\n'
        "• «старт заново» — вернуться к стартовому промпту и перегенерировать стартовый кадр (сброс конечного)."
    )


async def _regenerate_end_frame_prompt_from_feedback(scene: dict, feedback: str) -> str:
    current = _raw_end_frame_text(scene)
    try:
        start_snapshot = _image_scene_prompt(scene)[:2000]
    except Exception:
        start_snapshot = ""
    anchors = ", ".join(str(x) for x in (scene.get("continuity_anchors") or []) if str(x).strip())
    prompt = (
        "Ты правишь только ДЕЛЬТУ для конечного кадра относительно уже утверждённого стартового кадра.\n"
        "Верни один короткий текст на русском: что изменилось к финалу (кто вошёл, положение рук, еда, готовность). "
        "Не описывай весь кадр с нуля. Сохраняй предметную непрерывность: та же посуда, кастрюля/сковорода, "
        "плита или рабочая поверхность, ингредиенты и состояние еды, если они ещё должны быть в кадре.\n\n"
        f"CONTINUITY ANCHORS: {anchors or '-'}\n"
        f"СТАРТОВЫЙ КАДР (контекст):\n{start_snapshot}\n\n"
        f"ТЕКУЩАЯ ДЕЛЬТА END:\n{current}\n\n"
        f"ФИДБЕК:\n{feedback}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
    return (raw or "").strip()


async def _send_next_scene_media_step(update: Update, session: dict) -> None:
    """
    Показывает текущий подшаг для сцены ``image_scene_index``: стартовый промпт, конечный или video.
    """
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Нет сцен для пошаговой генерации.")
    idx = int(session.get("image_scene_index", 0))
    if idx >= len(scenes):
        session["state"] = STATE_READY_FOR_PRODUCTION
        await update.message.reply_text("Все сцены обработаны. Production завершён.")
        return
    scene = scenes[idx]
    sub = session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START)
    total = len(scenes)
    effective_chat = getattr(update, "effective_chat", None)
    restore_scene_prompt_fields_from_history(getattr(effective_chat, "id", None), session, idx)
    record_checkpoint(getattr(effective_chat, "id", None), session)
    from .handlers_state_scene import _maybe_ask_title_on_first_frame
    if await _maybe_ask_title_on_first_frame(update, session):
        return
    if session.get(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY):
        return
    if (
        sub == SCENE_MEDIA_SUBSTEP_VIDEO
        and not selected_image_bytes_for_video(scene, kind="start")
        and not _scene_has_any_generated_video(scene)
    ):
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        session["scene_media_language_index"] = 0
        sub = SCENE_MEDIA_SUBSTEP_START
    _persist_visible_scene_media_cursor(update, session)
    if sub == SCENE_MEDIA_SUBSTEP_START:
        from ..scene_media_candidates import continuation_source_for_scene
        from ..discipline_modes import FLOW, MODE_KEY
        can_continue = continuation_source_for_scene(session, scenes, idx) is not None
        step_text = _image_approval_text(scene, idx + 1, total, include_continue_previous=can_continue)
        if idx > 0 and scene.get(MODE_KEY) == FLOW:
            step_text = "Плавная 2D-история. «ОК» возьмёт последний кадр утверждённого предыдущего клипа.\n\n" + step_text
        await _reply_text_with_optional_markup(
            update,
            step_text,
            reply_markup=scene_image_approval_markup(
                include_continue_previous=can_continue,
                session=session,
            ),
        )
        return
    if sub == SCENE_MEDIA_SUBSTEP_END:
        await _reply_text_with_optional_markup(
            update,
            _end_frame_approval_text(scene, idx + 1, total),
            reply_markup=scene_end_approval_markup(session=session),
        )
        return
    start_img = selected_image_bytes_for_video(scene, kind="start") or _decode_b64(str(scene.get("start_frame_image_b64", "")).strip())
    end_img = selected_image_bytes_for_video(scene, kind="end") or _decode_b64(str(scene.get("end_frame_image_b64", "")).strip())
    if sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        if start_img:
            b = io.BytesIO(start_img)
            b.name = f"scene_{idx + 1}_start_approval.png"
            await safe_reply_photo(update.message, photo=b, caption=f"Сцена {idx + 1}: стартовый кадр.")
        end_prompt = _raw_end_frame_text(scene)
        await _reply_text_with_optional_markup(
            update,
            "Проверьте стартовый кадр.\n"
            "«ОК» — перейти дальше.\n"
            "При необходимости выберите, использовать ли конечный кадр.",
            reply_markup=scene_generated_image_approval_markup(
                include_end_frame_choice=bool(end_prompt and end_prompt.lower() != "пусто"),
                session=session,
            ),
        )
        return
    if sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        if start_img:
            b = io.BytesIO(start_img)
            b.name = f"scene_{idx + 1}_start_reference.png"
            await safe_reply_photo(update.message, photo=b, caption=f"Сцена {idx + 1}: стартовый кадр-референс.")
        if end_img:
            eb = io.BytesIO(end_img)
            eb.name = f"scene_{idx + 1}_end_approval.png"
            await safe_reply_photo(update.message, photo=eb, caption=f"Сцена {idx + 1}: конечный кадр.")
        await _reply_text_with_optional_markup(
            update,
            "Проверьте конечный кадр.\n"
            "«ОК» — перейти к апруву видео.",
            reply_markup=scene_generated_image_approval_markup(session=session),
        )
        return
    if start_img:
        b = io.BytesIO(start_img)
        b.name = f"scene_{idx + 1}_start_preview.png"
        await safe_reply_photo(update.message, photo=b, caption=f"Сцена {idx + 1}: стартовый кадр (для video-этапа).")
    if end_img:
        eb = io.BytesIO(end_img)
        eb.name = f"scene_{idx + 1}_end_preview.png"
        await safe_reply_photo(update.message, photo=eb, caption=f"Сцена {idx + 1}: конечный кадр (для video-этапа).")
    prompt_map = _video_prompt_by_lang_with_candidate_fallback(scene)
    if prompt_map:
        scene["video_prompt_by_lang"] = dict(prompt_map)
    langs = _scene_media_prompt_languages(session, prompt_map)
    lang_idx = int(session.get("scene_media_language_index", 0) or 0)
    if lang_idx < 0 or lang_idx >= len(langs):
        lang_idx = 0
        session["scene_media_language_index"] = 0
    current_lang = langs[lang_idx]
    current_prompt = str(prompt_map.get(current_lang, "")).strip()
    if current_prompt:
        scene["video_prompt"] = current_prompt
        scene["video_prompt_current_language"] = current_lang
    await _reply_text_with_optional_markup(
        update,
        _video_approval_text(scene, idx + 1, total, languages=langs),
        reply_markup=scene_video_approval_markup(
            include_restart_end=_scene_needs_end_frame_step(scene),
            languages=langs,
            session=session,
        ),
    )


def _soften_generation_prompt(prompt: str) -> str:
    """
    Смягчает промпт для фолбэка при отказе генератора (safety/policy/validation).
    """
    p = prompt.strip()
    # Удаляем потенциально проблемные куски и добавляем безопасные ограничения.
    p = re.sub(r"\b(blood|gore|weapon|violence|nude|nsfw)\b", "", p, flags=re.IGNORECASE)
    p = re.sub(r"\s+", " ", p).strip(" ,.;")
    suffix = (
        " Family-friendly cooking scene, non-violent, fully clothed, no brands, no logos, no text overlay, "
        "neutral expression, safe domestic environment, photorealistic details."
    )
    return f"{p}.{suffix}"


def _should_soften_image_prompt_after_error(e: Exception) -> bool:
    """
    Разрешает fallback на смягчённый image prompt только для policy/safety-like ошибок.

    Иначе лучше вернуть пользователю ошибку, чем молча генерировать "другую" картинку.
    """
    low = str(e).lower()
    return (
        "policy" in low
        or "safety" in low
        or "moderation" in low
        or "content violation" in low
        or "blocked" in low
        or "filtered" in low
        or "usage guidelines" in low
    )


def _append_pixar_reference_style_phrase(prompt: str) -> str:
    text = (prompt or "").strip()
    historical_watercolor_markers = (
        "dry-watercolor",
        "dry watercolor",
        "сухая акварель",
        "тонкий рисунок тушью",
        "фактурная бумага",
        "watercolor sepia historical style",
    )
    if any(marker in text.lower() for marker in historical_watercolor_markers):
        return text
    if PIXAR_REFERENCE_STYLE_PHRASE.lower() in text.lower():
        return text
    if not text:
        return PIXAR_REFERENCE_STYLE_PHRASE
    return f"{text}\n\n{PIXAR_REFERENCE_STYLE_PHRASE}"


async def _generate_image_with_lenient_fallback(
    prompt: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
    model: str | None = None,
    aspect_ratio: str = "9:16",
) -> bytes:
    result = await _generate_image_with_lenient_fallback_result(
        prompt,
        reference_image=reference_image,
        reference_images=reference_images,
        model=model,
        aspect_ratio=aspect_ratio,
    )
    return result.data


async def _generate_image_with_lenient_fallback_result(
    prompt: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
    model: str | None = None,
    aspect_ratio: str = "9:16",
):
    """
    Сначала пробует исходный промпт, затем 1 фолбэк со смягчённой формулировкой.
    """
    if reference_images is not None:
        imgs = [b for b in reference_images if b]
    elif reference_image:
        imgs = [reference_image]
    else:
        imgs = []
    ref_kw = imgs if imgs else None
    prompt = _append_pixar_reference_style_phrase(prompt)
    def _result_call(text: str):
        kwargs = {"reference_images": ref_kw, "model": model}
        if str(aspect_ratio or "9:16").strip() != "9:16":
            kwargs["aspect_ratio"] = aspect_ratio
        return generate_image_vertex_result(text, **kwargs)

    try:
        return await asyncio.to_thread(_result_call, prompt)
    except Exception as e:
        if not _should_soften_image_prompt_after_error(e):
            raise
        soft_prompt = _soften_generation_prompt(prompt)
        return await asyncio.to_thread(_result_call, soft_prompt)


def _next_image_quality_model(current_model: str | None) -> str | None:
    return next_image_generation_model(current_model)


_IMAGE_REFERENCE_STYLE_BLOCK = (
    "Обязательно: визуальный стиль, палитра и внешность персонажей должны совпадать с приложенными "
    "референсами персонажей. Не брать другой стиль или другие лица; приложенные референсы — единственный "
    "источник стиля персонажей.\n"
    "Если на референсах есть буквы, слова, логотипы или надписи, не переносить их в итоговый кадр; "
    "оставить только форму, цвета, одежду и декоративные узоры без читаемого текста.\n"
    "CRITICAL: Match art style, lighting, color palette, and character designs exactly to the attached reference "
    "images (character references). Do not copy any letters, words, labels, "
    "logos, or readable text from reference images. Do not change illustration style or invent different-looking "
    "characters. Do not switch to a children's storybook, Disney-like, Pixar-like, anime, watercolor, oil-painting, "
    "3D render, or generic cute cartoon look if that is not the attached reference style."
)

_GRANDMA_FACE_IDENTITY_LOCK = (
    "Замок идентичности бабушки: если scene prompt просит другой наряд, robe, apron, headband, hat или "
    "культурный/тематический костюм, менять можно только одежду и аксессуары. Лицо, возраст, мягкие округлые "
    "пропорции, добрая улыбка, круглое лицо, пухлые румяные щеки, маленький подбородок, круглый красноватый нос, "
    "добрые карие глаза за тонкими круглыми очками в медной оправе, светло-седые брови/волосы у лба и общий "
    "2D cartoon project style должны оставаться как у приложенного референса и описания персонажа. Не превращать "
    "ее в другую пожилую женщину, generic Japanese/anime/Pixar/3D character или новый дизайн лица."
)

_YOUNG_GRANDMA_FACE_IDENTITY_LOCK = (
    "Возрастной замок молодой бабушки: это та же героиня проекта в возрасте примерно 22–30 лет. "
    "Сохранить узнаваемую семейную идентичность референса — круглое лицо, мягкие щеки, маленький подбородок, "
    "круглый нос, добрые карие глаза и тонкие круглые очки — но правдоподобно омолодить кожу, осанку и руки. "
    "Волосы имеют естественный молодой цвет без седины; одежда, причёска и аксессуары строго соответствуют "
    "указанной исторической эпохе. Не рисовать пожилую женщину, морщины, седые волосы, современную одежду или "
    "другой дизайн лица. Возраст и ретро-одежда в этом правиле имеют приоритет над возрастом и одеждой в базовом "
    "описании приложенного референса."
)

_STORY_GRANDMA_FACE_IDENTITY_LOCK = (
    "Замок идентичности бабушки для истории: взять из приложенного референса только узнаваемое лицо, возраст, "
    "круглые очки, платок и основные черты одежды. Перерисовать бабушку в художественной технике, палитре и "
    "фактуре, явно заданных сценой; не сохранять 3D-render, пластиковый объём или фон исходного character sheet."
)

_STORY_CAT_IDENTITY_LOCK = (
    "Замок идентичности Тимоши для истории: приложенный референс рыжего кота — обязательный источник внешности. "
    "Сохранить его круглую широкую мордочку, очень большие круглые карие глаза, маленький розовый нос, небольшие "
    "треугольные уши, компактное пухлое тело, короткую плюшевую шерсть и толстый пушистый загнутый хвост. "
    "Окрас неизменен: тёплый рыжий верх, светлая кремовая мордочка, грудка и лапы. Не рисовать другого кота, "
    "тонкого реалистичного кота, новые полосы или тёмные пятна. Для исторической сцены менять только технику на "
    "сухую акварель/сепию; форма морды, пропорции и рисунок окраса должны остаться как у Тимоши."
)


def _fruit_only_closeup_prompt(prompt: str) -> bool:
    """
    Узкое исключение для первого момента сцены: говорящие банан и клубника крупно.
    Общий cast сцены может появиться позже в клипе или в end frame.
    """
    low = sanitize_character_names_and_keys(prompt or "").lower()
    has_banana = "banana" in low or "банан" in low
    has_strawberry = "strawberry" in low or "клубник" in low
    if not (has_banana and has_strawberry):
        return False
    blender_reveal = any(
        x in low
        for x in (
            "внутри прозрачной чаш",
            "чаша-кувшин",
            "моторная база",
            "кнопки теперь хорошо видны",
            "раскрыл",
        )
    )
    if blender_reveal:
        return False
    explicit_only = any(x in low for x in ("только банан", "только клубник", "only banana", "only strawberry"))
    explicit_without_cast = any(
        x in low
        for x in (
            "без бабуш",
            "без кот",
            "ни бабуш",
            "ни кот",
            "no grandma",
            "no grandmother",
            "no cat",
        )
    )
    closeup = any(x in low for x in ("крупный план", "крупно", "close-up", "close up"))
    talking_food = any(x in low for x in ("говор", "разговарива", "шепч", "talk", "speak", "whisper"))
    office_bit = "офис" in low or "office" in low
    return explicit_without_cast or explicit_only or (closeup and talking_food and office_bit)


def _append_reference_style_to_image_prompt(base_prompt: str, attach_style_instruction: bool) -> str:
    from ..image_atmosphere import add_image_atmosphere

    text = add_image_atmosphere(sanitize_character_names_and_keys(base_prompt).strip())
    if not attach_style_instruction:
        return text
    if not text:
        return _IMAGE_REFERENCE_STYLE_BLOCK.strip()
    return f"{text}\n\n{_IMAGE_REFERENCE_STYLE_BLOCK}"


def _has_advertising_cast(scene: dict) -> bool:
    meta = scene.get("prompt_meta_json") if isinstance(scene.get("prompt_meta_json"), dict) else {}
    return bool(
        scene.get("generation_mode") == "advertising"
        or scene.get("advertising_product_required")
        or meta.get("advertising_product_required")
    ) and isinstance(scene.get("characters"), list)


def _append_scene_identity_to_image_prompt(
    base_prompt: str,
    scene: dict,
    *,
    include_location: bool = False,
    allow_grandma_face_crop_rule: bool = True,
) -> str:
    """
    Фиксирует персонажей в промпте image generation.
    """
    text = sanitize_character_names_and_keys(base_prompt).strip()
    scene_generation_mode = str(scene.get("generation_mode") or "").strip().lower()
    scene_model = str(scene.get("model") or "").strip().lower()
    silence_scene = scene_generation_mode == "silence" or scene_model.startswith("kling")
    story_scene = scene_generation_mode == "story"
    if silence_scene:
        allow_grandma_face_crop_rule = False
        text = re.sub(
            r"Если бабушка присутствует в кадре, её лицо и голова должны быть полностью внутри кадра:.*?"
            r"подбородок и головной убор не должны быть обрезаны верхним или боковым краем\.\s*",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
    chars = scene.get("characters")
    if isinstance(chars, list):
        char_list = [str(c).strip() for c in chars if str(c).strip()]
    else:
        char_list = []
    fruit_only_closeup = _fruit_only_closeup_prompt(text)
    if fruit_only_closeup:
        normalized = [_normalize_character_key(c) for c in char_list]
        char_list = [c for c in ("banana", "strawberry") if c in normalized or c in text.lower()]
        if not char_list:
            char_list = ["banana", "strawberry"]
    if not fruit_only_closeup and not story_scene and "grandma" not in [c.lower() for c in char_list]:
        char_list.insert(0, "grandma")
    prompt_blob = text.lower()
    if not story_scene and not _has_advertising_cast(scene):
        for key in (ASSETS.get("characters") or {}):
            ch_norm = _normalize_character_key(str(key))
            if ch_norm in {_normalize_character_key(c) for c in char_list}:
                continue
            if _prompt_mentions_character(ch_norm, prompt_blob):
                char_list.append(ch_norm)
    _ = include_location
    normalized_keys = [_normalize_character_key(c) for c in char_list]
    chars_text = format_character_short_descriptions(char_list) or ", ".join(char_list)
    if story_scene:
        dynamic_labels = scene.get("story_character_labels")
        dynamic_labels = dynamic_labels if isinstance(dynamic_labels, dict) else {}
        story_fallback_labels = {
            "grandma": "бабушка",
            "cat": "рыжий кот",
            "queen_charlotte": "королева Шарлотта",
            "antonin_young": "Антонен, 10 лет",
            "antonin_adult": "взрослый Антонен",
        }
        chars_text = ", ".join(
            str(dynamic_labels.get(key) or story_fallback_labels.get(key) or key)
            for key in normalized_keys
        )
    grandma_age = str(scene.get("grandma_age") or "none").strip().lower()
    identity_description_keys = list(normalized_keys)
    if story_scene and grandma_age == "young":
        identity_description_keys = [key for key in identity_description_keys if key != "grandma"]
    character_lines = character_identity_lines(identity_description_keys)
    dynamic_descriptions = scene.get("story_character_descriptions")
    if isinstance(dynamic_descriptions, dict):
        character_context = (
            "Исторический герой"
            if str(scene.get("time_period") or "present").strip().lower() == "past"
            else "Герой проекта"
        )
        for key in normalized_keys:
            description = str(dynamic_descriptions.get(key) or "").strip()
            if description:
                character_lines.append(
                    f"{character_context} {key}: {description}. Во всех сценах сохранять одно и то же лицо и телосложение."
                )
    for key in scene.get("story_prop_ids") or []:
        description = (scene.get("story_prop_descriptions") or {}).get(key, "")
        character_lines.append(f"Основной предмет {key}: {description}. Сохранять форму, материал и цвет по утверждённому референсу.")
    for key in normalized_keys:
        approved_look = (scene.get("story_character_reference_prompts") or {}).get(key, "")
        if approved_look:
            character_lines.append(f"Образ {key} для этого ролика: {approved_look}. Лицо исполнителя и костюм брать с утверждённого референса этого ролика; описание базовой одежды проекта не заменяет костюм роли.")
    descriptions = "\n".join(character_lines)
    drama_beat = "" if fruit_only_closeup else sanitize_character_names_and_keys(scene.get("romantic_drama_beat") or "").strip()
    identity_lines = [
        f"Персонажи в кадре: {chars_text}. Состав персонажей не менять.",
        "Описание персонажей обязательно соблюдать; если персонаж есть в prompt, он должен выглядеть согласно описанию.",
    ]
    if story_scene:
        identity_lines.append(
            "Приложенные character sheets используются только для внешности явно перечисленных героев. "
            "Не копировать компоновку референс-листа, рамки, панели, фон, нейтральные позы или дополнительных людей из референсов."
        )
        historical_watercolor = any(
            marker in f"{text} {scene.get('era_style') or ''}".lower()
            for marker in ("dry watercolor", "dry-watercolor", "сухая акварель", "sepia", "сепия")
        )
        if historical_watercolor:
            identity_lines.append(
                "Композиция строго full bleed: рисунок и фон продолжаются до всех четырёх краёв изображения. "
                "Не показывать белую рамку, пустые поля листа, край бумаги, паспарту, виньетку или рамку вокруг иллюстрации. "
                "Фактура бумаги видна внутри изображения, но сам лист как отдельный объект не виден. "
                "Цветовая палитра не монохромная: должны отчётливо различаться приглушённый пыльно-голубой, "
                "оливково-зелёный, тёплая охра и выцветшая терракота с естественными оттенками кожи. "
                "Сепия используется только как лёгкий оттенок теней; не заливать весь кадр серо-бежевым или коричневым."
            )
    if "grandma" in normalized_keys and not fruit_only_closeup:
        if story_scene and grandma_age == "young":
            identity_lines.append(_YOUNG_GRANDMA_FACE_IDENTITY_LOCK)
        elif story_scene:
            identity_lines.append(_STORY_GRANDMA_FACE_IDENTITY_LOCK)
        else:
            identity_lines.append(_GRANDMA_FACE_IDENTITY_LOCK)
    if story_scene and "cat" in normalized_keys and not fruit_only_closeup:
        identity_lines.append(_STORY_CAT_IDENTITY_LOCK)
    prompt_meta = scene.get("prompt_meta_json") if isinstance(scene.get("prompt_meta_json"), dict) else {}
    advertising_product_lock = str(
        scene.get("advertising_product_lock_ru")
        or prompt_meta.get("advertising_product_lock_ru")
        or ""
    ).strip()
    if advertising_product_lock:
        identity_lines.append(advertising_product_lock)
    if allow_grandma_face_crop_rule:
        identity_lines.append(
            "Если бабушка присутствует в кадре, её лицо и голова должны быть полностью внутри кадра: глаза, нос, рот, "
            "подбородок и головной убор не должны быть обрезаны верхним или боковым краем."
        )
    if any(key in {"cat", "zaya", "markiz"} or "cat" in key for key in normalized_keys):
        identity_lines.append(
            "Правило котов: каждый canonical кот может появиться только один раз. Не рисовать двух рыжих котов, "
            "двух белых кошек или двух черно-белых котов; если в кадре несколько котов, они должны быть разными "
            "персонажами из списка выше и с разным окрасом."
        )
    if scene.get("single_take"):
        identity_lines.append(
            "Бабушка и кот максимально мультяшные: округлые стилизованные формы и выразительные лица, "
            "никакого фотореализма, реалистичной кожи и натуралистичной шерсти. "
            "Кот не ест, не облизывает и не трогает человеческую еду."
        )
    identity_block = "\n".join(identity_lines)
    if drama_beat:
        identity_block = f"{identity_block}\nФоновая драма этой сцены должна быть видна в кадре: {drama_beat}"
    if descriptions:
        identity_block = f"{identity_block}\n{descriptions}"
    if not text:
        return append_episode_prop_locks(identity_block if story_scene else append_visual_prop_descriptions(identity_block), scene)
    combined = f"{text}\n\n{identity_block}"
    # The shared prop catalog belongs to the culinary/grandmother project.
    # Story scenes (including Discipline) carry their own setting and props;
    # injecting the kitchen table/window locks would pull every scene back
    # into the same room and can contradict outdoor or public locations.
    return append_episode_prop_locks(combined if story_scene else append_visual_prop_descriptions(combined), scene)


_IMAGE_PROMPT_REFERENCE_KEYS = ("start_frame_prompt", "start_frame_prompt_ru", "end_frame_prompt", "end_frame_prompt_ru", "video_prompt")
_START_IMAGE_PROMPT_REFERENCE_KEYS = ("start_frame_prompt", "start_frame_prompt_ru")
_END_IMAGE_PROMPT_REFERENCE_KEYS = ("end_frame_prompt", "end_frame_prompt_ru", "video_prompt")


_CHARACTER_PROMPT_ALIASES: dict[str, tuple[str, ...]] = {
    "zaya": ("zaya", "white cat", "white cartoon cat", "зая", "заю", "заи", "белая кошка", "белая кошечка"),
    "markiz": ("маркиз", "маркиза", "черно-белый кот", "чёрно-белый кот"),
}
_CHARACTER_PROMPT_PATTERNS: dict[str, tuple[str, ...]] = {
    "grandma": (r"\bбабушк\w*",),
    "cat": (r"\bкот(?:а|у|ом|е)?\b",),
    "granddaughter": (r"\bвнучк\w*",),
    "grandson": (r"\bвнук(?:а|у|ом|е)?\b",),
    "grandfather": (r"\bдедушк\w*",),
    "daughter": (r"\bдочь\b", r"\bдоч(?:ка|ку|ке|кой|ери|ерью)\b"),
    "dog": (r"\bсобак\w*", r"\bп(?:ё|е)с\b"),
}


def _prompt_mentions_character(character_key: str, prompt_blob: str) -> bool:
    ch_norm = _normalize_character_key(character_key)
    text = str(prompt_blob or "").lower()
    if not ch_norm or not text:
        return False
    if ch_norm in text:
        return True
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _CHARACTER_PROMPT_PATTERNS.get(ch_norm, ())):
        return True
    return any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) for alias in _CHARACTER_PROMPT_ALIASES.get(ch_norm, ()))


def _scene_prompt_blob(scene: dict, prompt_keys: tuple[str, ...] | None = None) -> str:
    keys = prompt_keys or _IMAGE_PROMPT_REFERENCE_KEYS
    return " ".join(str(scene.get(k, "")).lower() for k in keys)


def _scene_supplemental_prompt_blob(scene: dict) -> str:
    parts: list[str] = []
    for key in ("video_prompt_by_lang", "syntax_prompt_by_lang"):
        value = scene.get(key)
        if isinstance(value, dict):
            parts.extend(str(item or "").lower() for item in value.values())
    candidates_by_lang = scene.get("video_candidates_by_lang")
    if isinstance(candidates_by_lang, dict):
        for candidates in candidates_by_lang.values():
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict) or not bool(candidate.get("approved_for_production")):
                    continue
                prompt_by_lang = candidate.get("video_prompt_by_lang")
                if isinstance(prompt_by_lang, dict):
                    parts.extend(str(item or "").lower() for item in prompt_by_lang.values())
    return " ".join(part for part in parts if part)


def _reference_keys_for_scene(
    scene: dict,
    refs: dict[str, Any],
    *,
    include_location: bool = False,
    prompt_keys: tuple[str, ...] | None = None,
) -> list[str]:
    """
    Определяет порядок ключей референсов для image generation.

    Всегда включает персонажей сцены (в начале: grandma, затем cat если он есть, затем остальные).
    Локации не добавляются; `include_location` оставлен только для совместимости старых вызовов.
    """
    _ = include_location
    keys_order: list[str] = []
    chars = scene.get("characters")
    prompt_blob = _scene_prompt_blob(scene, prompt_keys)
    character_prompt_blob = f"{prompt_blob} {_scene_supplemental_prompt_blob(scene)}".strip()
    fruit_only_closeup = _fruit_only_closeup_prompt(prompt_blob)
    story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
    if fruit_only_closeup:
        ch_list = [
            ch
            for ch in ([_normalize_character_key(str(c)) for c in chars] if isinstance(chars, list) else [])
            if ch in {"banana", "strawberry"}
        ]
    else:
        ch_list = [_normalize_character_key(str(c)) for c in chars] if isinstance(chars, list) else []
    # Если в тексте промпта упоминаются персонажи, добавляем их в список референсов.
    for ch_key, ref_path in refs.items():
        if ch_key in {"location", "location_top_view"}:
            continue
        if not isinstance(ref_path, str) or not ref_path.strip():
            continue
        ch_norm = _normalize_character_key(ch_key)
        if fruit_only_closeup and ch_norm not in {"banana", "strawberry"}:
            continue
        mention = not story_scene and not _has_advertising_cast(scene) and _prompt_mentions_character(ch_norm, character_prompt_blob)
        if mention and ch_norm not in ch_list:
            ch_list.append(ch_norm)
    # Базовый минимум по продуктовым требованиям.
    if not fruit_only_closeup and not story_scene:
        if "grandma" not in ch_list:
            ch_list.append("grandma")
    # Стабильный порядок: grandma, cat, потом остальные.
    for fixed in ("grandma", "cat"):
        if not fruit_only_closeup and fixed in ch_list:
            keys_order.append(fixed)
    for ch in ch_list:
        if ch not in keys_order and isinstance(refs.get(ch), str) and refs.get(ch, "").strip():
            keys_order.append(ch)
    for key in scene.get("story_prop_ids") or []:
        if key not in keys_order and isinstance(refs.get(key), str) and refs[key].strip():
            keys_order.append(key)
    for key in refs:
        if str(key).startswith("__story_style_") and key not in keys_order:
            keys_order.append(str(key))
    return keys_order


def _resolve_scene_references(scene: dict, *, prompt_keys: tuple[str, ...] | None = None) -> dict[str, str]:
    """
    Возвращает полный набор референсов для сцены даже если `scene["references"]` отсутствует.
    """
    out: dict[str, str] = {}
    refs = scene.get("references")
    prompt_blob = _scene_prompt_blob(scene, prompt_keys)
    character_prompt_blob = f"{prompt_blob} {_scene_supplemental_prompt_blob(scene)}".strip()
    fruit_only_closeup = _fruit_only_closeup_prompt(prompt_blob)
    story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
    if isinstance(refs, dict):
        for k, v in refs.items():
            if isinstance(v, str) and v.strip():
                if str(k) in {"location", "location_top_view"}:
                    continue
                if fruit_only_closeup and _normalize_character_key(str(k)) not in {"banana", "strawberry"}:
                    continue
                out[str(k)] = v.strip()
    style_paths = scene.get("style_reference_paths")
    if not story_scene and isinstance(style_paths, list):
        for index, value in enumerate(style_paths, start=1):
            path = str(value or "").strip()
            if path:
                out[f"__story_style_{index}"] = path

    # Минимальный обязательный набор персонажей: grandma.
    chars = scene.get("characters")
    ch_list = [_normalize_character_key(str(c)) for c in chars] if isinstance(chars, list) else []
    if fruit_only_closeup:
        ch_list = [ch for ch in ch_list if ch in {"banana", "strawberry"}]
    elif not story_scene:
        if "grandma" not in ch_list:
            ch_list.append("grandma")
    # Персонажи, упомянутые в промптах, тоже учитываем.
    all_char_refs = ASSETS.get("characters") or {}
    for key, cfg in all_char_refs.items():
        k = _normalize_character_key(str(key))
        if fruit_only_closeup and k not in {"banana", "strawberry"}:
            continue
        if not story_scene and not _has_advertising_cast(scene) and _prompt_mentions_character(k, character_prompt_blob) and k not in ch_list:
            ch_list.append(k)
        if k in ch_list and isinstance(cfg, dict):
            base = cfg.get("base")
            if isinstance(base, str) and base.strip():
                out.setdefault(k, base.strip())

    return out


def _load_scene_reference_images(
    scene: dict,
    *,
    include_location: bool = False,
    prompt_keys: tuple[str, ...] | None = None,
) -> list[bytes]:
    """
    Загружает референсы для генерации: только персонажи, если явно не включить локацию.
    Раньше брался только первый ключ (локация), из-за чего character sheets не попадали в запрос.
    """
    refs = _resolve_scene_references(scene, prompt_keys=prompt_keys)
    for key in scene.get("story_required_reference_ids") or []:
        path = refs.get(key, "")
        absolute = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
        if not path or not os.path.isfile(absolute) or os.path.getsize(absolute) == 0:
            raise ValueError(f"Утверждённый референс {key} недоступен. Восстановите файл до генерации кадра.")
    if not refs:
        return []
    story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
    if story_scene:
        from ..story_image_postprocess import crop_story_reference_border
    keys_order = _reference_keys_for_scene(scene, refs, include_location=include_location, prompt_keys=prompt_keys)
    out: list[bytes] = []
    seen_paths: set[str] = set()
    for key in keys_order:
        ref_path = refs.get(key)
        if not isinstance(ref_path, str) or not ref_path.strip():
            continue
        candidate = ref_path.strip()
        if candidate in seen_paths:
            continue
        abs_path = candidate if os.path.isabs(candidate) else os.path.join(PROJECT_ROOT, candidate)
        try:
            with open(abs_path, "rb") as f:
                data = f.read()
        except Exception:
            if key in (scene.get("story_required_reference_ids") or []):
                raise ValueError(f"Не удалось прочитать утверждённый референс {key}.")
            continue
        if not data:
            if key in (scene.get("story_required_reference_ids") or []):
                raise ValueError(f"Утверждённый референс {key} пуст.")
            continue
        if story_scene:
            data = crop_story_reference_border(data)
        seen_paths.add(candidate)
        out.append(data)
    return out


def _user_reference_images_count(scene: dict, target: str) -> int:
    mapping = scene.get(SCENE_USER_REFERENCE_IMAGES_KEY)
    if not isinstance(mapping, dict):
        return 0
    items = mapping.get(str(target or "start"))
    return len([item for item in items if str(item or "").strip()]) if isinstance(items, list) else 0


def _loaded_reference_paths(scene: dict, *, include_location: bool = False) -> list[str]:
    """
    Возвращает список путей референсов, которые реально удалось прочитать с диска.
    Порядок совпадает с отправкой в модель.
    """
    refs = _resolve_scene_references(scene)
    if not refs:
        return []
    keys_order = _reference_keys_for_scene(scene, refs, include_location=include_location)
    out: list[str] = []
    seen_paths: set[str] = set()
    for key in keys_order:
        ref_path = refs.get(key)
        if not isinstance(ref_path, str) or not ref_path.strip():
            continue
        candidate = ref_path.strip()
        if candidate in seen_paths:
            continue
        abs_path = candidate if os.path.isabs(candidate) else os.path.join(PROJECT_ROOT, candidate)
        try:
            with open(abs_path, "rb") as f:
                data = f.read()
        except Exception:
            continue
        if not data:
            continue
        seen_paths.add(candidate)
        out.append(candidate)
    return out


def _planned_reference_summary(scene: dict, *, include_location: bool = False) -> tuple[str, str, list[str]]:
    """
    Возвращает планируемые референсы перед генерацией:
    - персонажи, которые будут отправлены;
    - пути всех файлов, которые будут отправлены (в порядке отправки).
    """
    refs = _resolve_scene_references(scene)
    if not refs:
        return "—", "—", []
    keys_order = _reference_keys_for_scene(scene, refs, include_location=include_location)
    sent_chars: list[str] = []
    sent_paths: list[str] = []
    seen_paths: set[str] = set()
    for key in keys_order:
        ref_path = refs.get(key)
        if not isinstance(ref_path, str) or not ref_path.strip():
            continue
        candidate = ref_path.strip()
        if candidate in seen_paths:
            continue
        seen_paths.add(candidate)
        sent_paths.append(candidate)
        if key not in sent_chars:
            sent_chars.append(key)
    chars_text = format_character_short_descriptions(sent_chars) if sent_chars else "—"
    return chars_text, "—", sent_paths


def _end_frame_delta_instruction(scene: dict) -> str:
    """
    Правило для генерации end-frame: только дельта к стартовому референсу, без полного пересказа кадра.
    """
    chars = scene.get("characters")
    ch_list = [str(c).strip() for c in chars] if isinstance(chars, list) else []
    story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
    if not story_scene and "grandma" not in [c.lower() for c in ch_list]:
        ch_list.insert(0, "grandma")
    chars_text = format_character_short_descriptions(ch_list) if ch_list else "бабушка"
    return (
        "Start-frame (изображение-референс) — полная база: не перерисовывай сцену с нуля. "
        "Покажи только изменения к финалу: кто из персонажей впервые входит в кадр (если это в тексте дельты), "
        "сдвиг рук и посуды, прогресс готовки. "
        f"Новые в кадре персонажи — строго по референсам character sheets: {chars_text}."
    )


def _image_approval_text(
    scene: dict,
    scene_index: int,
    total: int,
    *,
    include_continue_previous: bool = False,
) -> str:
    shot = scene.get("shot_type", "medium")
    anchors = scene.get("continuity_anchors") or []
    anchors_text = ", ".join(str(x) for x in anchors if str(x).strip()) or "—"
    base_prompt = _image_scene_prompt(scene)
    ref_files = _load_scene_reference_images(scene, include_location=False)
    raw_mode = bool(scene.get("start_frame_prompt_raw_mode"))
    if raw_mode:
        prompt = str(base_prompt or "").strip()
    else:
        story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
        # Keep preview and generation identical. Story scenes own their
        # setting/style, so the cooking project's default Soviet interior
        # must not override a planned street, park, hall, library, or cafe.
        prompt = (
            str(base_prompt or "").strip()
            if story_scene
            else _append_reference_style_to_image_prompt(base_prompt, bool(ref_files))
        )
        prompt = _append_scene_identity_to_image_prompt(prompt, scene, include_location=False)
    ref_note = (
        f"Референсы в запрос: персонажи ({len(ref_files)} файл(ов)); "
        f"добавленные изображения ({_user_reference_images_count(scene, 'start')} файл(ов)); локация не отправляется."
        if ref_files
        else (
            "Референсы персонажей из файлов не загрузились — "
            f"добавленные изображения: {_user_reference_images_count(scene, 'start')} файл(ов)."
        )
    )
    planned_chars, _planned_location, planned_paths = _planned_reference_summary(scene, include_location=False)
    loaded_paths = _loaded_reference_paths(scene, include_location=False)
    paths_block = (
        "\n".join(f"  - {p}" for p in planned_paths)
        if planned_paths
        else "  - —"
    )
    loaded_paths_block = (
        "\n".join(f"  - {p}" for p in loaded_paths)
        if loaded_paths
        else "  - —"
    )
    return (
        f"Сцена {scene_index}/{total} — апрув перед генерацией изображения.\n"
        f"Ракурс: {shot}\n"
        f"Опоры консистентности: {anchors_text}\n"
        f"{ref_note}\n\n"
        "Какие референсы будут отправлены в модель:\n"
        "• Локация: не отправляется\n"
        f"• Персонажи: {planned_chars}\n"
        "• Пути файлов:\n"
        f"{paths_block}\n\n"
        "Диагностика загрузки референсов (факт чтения с диска):\n"
        "• Реально загружены:\n"
        f"{loaded_paths_block}\n\n"
        f"{'Режим raw prompt: в модель уйдёт ваш текст один в один; персонажи и референсы сцены прикладываются отдельно файлами.\n\n' if raw_mode else ''}"
        "Промпт (как уйдёт в модель):\n"
        f"{prompt}\n\n"
        "Варианты:\n"
        '• «ОК» — сгенерировать изображение.\n'
        '• «ПРОМПТ: ...» — вручную заменить промпт.\n'
        '• «ПРОМПТ: ```...```» — вставить свой многострочный prompt как код-блок; кадр сгенерируется именно по нему с персонажами этой сцены.\n'
        '• «ФИДБЕК: ...» — описать, что не так; перегенерирую промпт нейросетью.\n'
        + ('• «Продолжи предыдущее видео» — взять точный последний кадр утверждённого предыдущего клипа и перейти к video-этапу.\n'
           if include_continue_previous else '')
        + "• «старт заново» — то же (если вернулись с следующего шага): снова правки промпта, затем «ОК»."
    )
