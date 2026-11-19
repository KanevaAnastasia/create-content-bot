import json

from ..story_style_previews import (
    PREVIEW_BUTTON, NEXT_BUTTON, PREVIOUS_BUTTON, PAGE_KEY,
    page_rows, page_description, resolve_choice, send_story_style_previews,
)

from .handlers_state_deps import (
    Any,
    ContextTypes,
    SESSIONS,
    STATE_REFINING_CONCEPT,
    STATE_REFINING_RECIPE,
    STATE_WAITING_CONCEPT,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    Update,
    _bump_debounce_generation,
    _call_with_503_retries,
    _elevenlabs_voice_warning,
    _extract_recipe_from_command_text,
    _extract_first_http_url,
    _fetch_recipe_text_from_url,
    _format_scenes_plan_attempt_notice,
    _format_scenes_regeneration_report,
    _try_parse_direct_recipe_from_user_text,
    _user_error_message,
    _build_concepts_session,
    _build_direct_recipe_session,
    asyncio,
    ok_only_markup,
    format_full_recipe_for_approval,
    format_viral_application_summary,
    format_viral_concept_brief,
    gemini_generate_sync,
    generate_recipe_from_source_text,
    generate_recipe_concepts,
    generate_recipe_full_text,
    generate_scenes_plan,
    generate_viral_concept_brief,
    logger,
    re,
    ServiceUnavailableRetriesExhaustedError,
)
from .handlers_state_commands import _reply_text_chunked
from .handlers_state_commands import NAV_BUTTON_START_OVER
from .handlers_generation_utils import (
    OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY,
    OVERLAY_SUBTITLE_DECIDED_SESSION_KEY,
    OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY,
    OVERLAY_TITLE_BY_LANG_SESSION_KEY,
    OVERLAY_TITLE_NEXT_RECIPE_PLAN,
    OVERLAY_TITLE_NEXT_SERIES_CHOICE,
    OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY,
    OVERLAY_TITLE_SESSION_KEY,
    OVERLAY_TITLE_STYLE_SESSION_KEY,
    STATE_WAITING_OVERLAY_SUBTITLE_RU,
    STATE_WAITING_OVERLAY_TITLE_RU,
)
from .handlers_keyboards import DRAFT_BUTTON_SAVE, reply_text_without_keyboard, safe_reply_text
from .handlers_state_media import send_social_descriptions_for_recipe
from ..project_modes import PENDING_MODE_KEY, project_mode_rows, project_mode_labels
from ..directorial_notes import direction_for_session
from ..dish_visuals import build_dish_visual_brief
from ..llm_utils import GEMINI_MODEL_PROFILE_EDIT, extract_json_object
from ..advertising_generation import (
    ADVERTISING_CAMPAIGNS_SESSION_KEY,
    ADVERTISING_CONTENT_TYPE_SESSION_KEY,
    ADVERTISING_FORMAT_SESSION_KEY,
    ADVERTISING_POST_CURRENT_IMAGE_PATH_SESSION_KEY,
    ADVERTISING_POST_IMAGE_INDEX_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY,
    ADVERTISING_POST_TEXT_SESSION_KEY,
    ADVERTISING_PRODUCT_BRIEF_SESSION_KEY,
    ADVERTISING_PRODUCT_PATH_SESSION_KEY,
    ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY,
    ADVERTISING_SOURCE_SESSION_KEY,
    STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
    STATE_WAITING_ADVERTISING_CUSTOM_CAMPAIGN,
    STATE_WAITING_ADVERTISING_FORMAT_CHOICE,
    STATE_WAITING_ADVERTISING_PRODUCT_IMAGE,
    generate_advertising_campaigns,
    generate_advertising_scene_plan,
)
from ..artifact_store import file_uri, persist_generation_artifact
from ..discipline_intro import (
    DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY,
    DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_AUDIO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY,
    DISCIPLINE_INTRO_DIALOGUE_VERSIONS_SESSION_KEY,
    DISCIPLINE_INTRO_ENABLED_SESSION_KEY,
    DISCIPLINE_INTRO_IMAGE_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_IMAGE_VERSIONS_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_VERSIONS_SESSION_KEY,
    STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL,
    format_discipline_intro_dialogue,
    generate_discipline_intro_dialogue,
    intro_character_names,
    parse_intro_dialogue_text,
)
from ..advice_generation import ADVICE_SCENE_RULE, advice_concept_input, format_advice_choices
from ..generation_modes import (
    GENERATION_MODE_ADVICE,
    GENERATION_MODE_ADVERTISING,
    GENERATION_MODE_COPY,
    GENERATION_MODE_LONG,
    GENERATION_MODE_NORMAL,
    GENERATION_MODE_SESSION_KEY,
    GENERATION_MODE_SILENCE,
    GENERATION_MODE_STORY,
    GENERATION_MODE_VISUALIZATION,
    is_visualization_generation,
    normalize_generation_mode,
    target_languages_for_generation_mode,
)
from ..generation_store import ensure_generation_run_for_session
from ..story_generation import (
    STATE_WAITING_STORY_ENGLISH_CHOICE,
    STATE_WAITING_STORY_STYLE_CHOICE,
    STATE_WAITING_STORY_SCRIPT_APPROVAL,
    STORY_ENGLISH_REQUESTED_SESSION_KEY,
    STORY_STYLE_PRIMARY,
    STORY_STYLE_RETRO_CARTOON,
    STORY_STYLE_CLAYMATION,
    STORY_STYLE_FELT_STOP_MOTION,
    STORY_STYLE_RETRO_FAIRYTALE,
    STORY_STYLE_STORYBOOK_GOUACHE,
    STORY_STYLE_TWILIGHT_WATERCOLOR,
    STORY_STYLE_SOFT_ANIMATION,
    STORY_STYLE_CLASSIC_CEL_CARTOON,
    STORY_STYLE_FELT_APPLIQUE,
    STORY_STYLE_FOLK_FAIRYTALE,
    STORY_STYLE_OIL_PASTEL,
    STORY_STYLE_CROCHET,
    STORY_STYLE_MINIMAL_RETRO_CARTOON,
    STORY_STYLE_WATERCOLOR,
    STORY_SOURCE_SESSION_KEY,
    STORY_TITLE_SESSION_KEY,
    STORY_VISUAL_STYLE_SESSION_KEY,
    STORY_VOICEOVER_SESSION_KEY,
    archive_story_voiceover_version,
    generate_story_voiceover,
)
from ..multilingual import language_name, normalize_language_code, target_languages_from_env
from ..visualization_generation import (
    STATE_WAITING_VISUALIZATION_MEDIA,
    VISUALIZATION_INPUT_KIND_SESSION_KEY,
    VISUALIZATION_SOURCE_MEDIA_SESSION_KEY,
    VISUALIZATION_TRANSCRIPT_SEGMENTS_SESSION_KEY,
)
from ..series_store import (
    SERIES_PENDING_PREFER_VERTEX_SESSION_KEY,
    SERIES_PENDING_RECIPE_SESSION_KEY,
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SERIES_TOTAL_VIDEOS_SESSION_KEY,
    SERIES_VIDEO_INDEX_SESSION_KEY,
    attach_series_to_session,
    create_series,
    format_series_label,
    list_active_series,
    select_series_payload,
)

_PROJECT_KEY_SESSION = "selected_project_key"
STATE_WAITING_GENERATION_MODE = "waiting_generation_mode"
STATE_WAITING_SERIES_CHOICE = "waiting_series_choice"
STATE_WAITING_SERIES_CREATE = "waiting_series_create"
STATE_WAITING_SERIES_TRANSLATION_APPROVAL = "waiting_series_translation_approval"
_SERIES_CHOICE_IDS_SESSION_KEY = "series_choice_ids"
_SERIES_TRANSLATION_PENDING_SESSION_KEY = "series_translation_pending"
SERIES_CHOICE_CONFIRMED_SESSION_KEY = "series_choice_confirmed"
SERIES_PENDING_NEXT_SESSION_KEY = "series_pending_next"
SERIES_NEXT_GENERATION = "generation"
SERIES_NEXT_OVERLAY_TITLE = "overlay_title"
SERIES_NEXT_ADVERTISING_PLAN = "advertising_plan"
_GENERATION_MODE_BUTTON_NORMAL = "Обычный"
_GENERATION_MODE_BUTTON_SILENCE = "Silence"
_GENERATION_MODE_BUTTON_LONG = "Длинный"
_GENERATION_MODE_BUTTON_COPY = "Копия"
_GENERATION_MODE_BUTTON_STORY = "История"
_GENERATION_MODE_BUTTON_VISUALIZATION = "Визуализация"
_GENERATION_MODE_BUTTON_ADVERTISING = "Реклама"
_ADVERTISING_CAMPAIGN_OTHER_BUTTON = "Другие варианты"
_ADVERTISING_CAMPAIGN_STRENGTHEN_BUTTON = "Усилить варианты"
_ADVERTISING_CAMPAIGN_CUSTOM_BUTTON = "Свой вариант"
_ADVERTISING_FORMAT_CLIP_BUTTON = "Клип"
_ADVERTISING_FORMAT_POST_BUTTON = "Пост"
_STORY_REGENERATE_BUTTON = "ПЕРЕГЕНЕРИРОВАТЬ"
_SERIES_BUTTON_NEW = "Создать серию"
_SERIES_BUTTON_SKIP = "Без серии"
_SERIES_BUTTON_BACK = "Назад"
_SERIES_SESSION_KEYS = (
    SERIES_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SERIES_VIDEO_INDEX_SESSION_KEY,
    SERIES_TOTAL_VIDEOS_SESSION_KEY,
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_CHOICE_CONFIRMED_SESSION_KEY,
)

# A new /generate or /quick request starts a new piece of content. Keep
# account/project preferences in the chat session, but never carry database
# bindings or generated content from the previous run into the new workflow.
# Continuation/resume paths do not call _start_generation_with_recipe and keep
# their run binding intact.
_NEW_GENERATION_STALE_KEYS = (
    "social_descriptions_sent",
    "social_descriptions_approved",
    "social_descriptions_pending",
    "final_social_descriptions_triggered_at",
    "advertising_queue_product_id",
    "advertising_product_paths",
    "story_character_references",
    "story_character_bible",
    "discipline_narrative_mode",
    "generation_run_id",
    "scene_db_ids_by_index",
    "production",
    "scenes_plan",
    "scenes_text",
    "recipe_steps",
    "selected_concept",
    "selected_concept_name",
    "recipe_title",
    "recipe_name",
    "recipe_full_text",
    "user_input",
    "viral_concept_brief",
    "romantic_drama_subplot",
    "retention_review",
    OVERLAY_TITLE_SESSION_KEY,
    OVERLAY_TITLE_BY_LANG_SESSION_KEY,
    OVERLAY_TITLE_STYLE_SESSION_KEY,
    OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY,
    OVERLAY_SUBTITLE_DECIDED_SESSION_KEY,
    OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY,
    OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY,
    STORY_SOURCE_SESSION_KEY,
    STORY_TITLE_SESSION_KEY,
    STORY_VOICEOVER_SESSION_KEY,
    STORY_ENGLISH_REQUESTED_SESSION_KEY,
    STORY_VISUAL_STYLE_SESSION_KEY,
    VISUALIZATION_SOURCE_MEDIA_SESSION_KEY,
    VISUALIZATION_TRANSCRIPT_SEGMENTS_SESSION_KEY,
    VISUALIZATION_INPUT_KIND_SESSION_KEY,
    DISCIPLINE_INTRO_ENABLED_SESSION_KEY,
    DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY,
    DISCIPLINE_INTRO_DIALOGUE_VERSIONS_SESSION_KEY,
    DISCIPLINE_INTRO_IMAGE_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_IMAGE_VERSIONS_SESSION_KEY,
    DISCIPLINE_INTRO_AUDIO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_VERSIONS_SESSION_KEY,
    DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY,
    ADVERTISING_SOURCE_SESSION_KEY,
    ADVERTISING_PRODUCT_PATH_SESSION_KEY,
    ADVERTISING_PRODUCT_BRIEF_SESSION_KEY,
    ADVERTISING_CONTENT_TYPE_SESSION_KEY,
    ADVERTISING_CAMPAIGNS_SESSION_KEY,
    ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY,
    ADVERTISING_FORMAT_SESSION_KEY,
    ADVERTISING_POST_TEXT_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY,
    ADVERTISING_POST_IMAGE_INDEX_SESSION_KEY,
    ADVERTISING_POST_CURRENT_IMAGE_PATH_SESSION_KEY,
    *_SERIES_SESSION_KEYS,
    _SERIES_CHOICE_IDS_SESSION_KEY,
    _SERIES_TRANSLATION_PENDING_SESSION_KEY,
)


def _session_for_new_generation(existing_session: dict) -> dict:
    session = dict(existing_session)
    for key in _NEW_GENERATION_STALE_KEYS:
        session.pop(key, None)
    return session


async def _start_description_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
) -> None:
    """Generate social descriptions directly from recipe text or URL."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    source = (recipe or "").strip()
    if not source:
        await update.message.reply_text("Пришлите рецепт текстом или ссылкой на страницу с рецептом.")
        return
    source_url = _extract_first_http_url(source)
    if source_url:
        try:
            source_text = _fetch_recipe_text_from_url(source_url)
        except Exception as e:
            await update.message.reply_text(
                f"Не удалось извлечь рецепт по ссылке: {_user_error_message(e)}\n"
                "Пришлите, пожалуйста, текст рецепта сообщением."
            )
            return
        await update.message.reply_text(
            "Нашла ссылку и извлекла текст страницы. Передаю весь текст нейросети, чтобы она выделила рецепт."
        )
        await reply_text_without_keyboard(update.message, "Извлекаю рецепт, ждите.")
        try:
            source = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "extract_recipe_from_source_text_for_description",
                lambda: generate_recipe_from_source_text(source_text, source_url=source_url),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return
        except Exception as e:
            logger.exception("recipe extraction from source text for description failed")
            await update.message.reply_text(f"Ошибка извлечения рецепта из ссылки: {_user_error_message(e)}")
            return
        await update.message.reply_text("Ниже рецепт, который я извлекла из страницы.")
        await _reply_text_chunked(update, format_full_recipe_for_approval(source.strip()))
    await send_social_descriptions_for_recipe(update, source)


async def _reply_text_with_optional_markup(update: Update, text: str, reply_markup=None) -> None:
    """Отправляет сообщение с клавиатурой, но не ломается на тестовых стабах без kwargs."""
    if update.message is None:
        return
    if reply_markup is None:
        await safe_reply_text(update.message, text)
        return
    await safe_reply_text(update.message, text, reply_markup=reply_markup)


def _concept_choice_markup(concepts: list[dict]) -> object | None:
    """Клавиатура выбора концепта по номеру; по 2 кнопки в строке."""
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    if not concepts:
        return None
    buttons = [str(i) for i in range(1, len(concepts) + 1)]
    rows: list[list[str]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.extend([["Добавь"], ["Исправь"], [NAV_BUTTON_START_OVER]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _parse_concept_choice(text: str) -> int | None:
    """Возвращает номер концепта только для явного выбора, а не для любого числа в фидбеке."""
    match = re.fullmatch(r"(?:концепт\s*)?(\d+)", text.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _advertising_campaign_choice_markup() -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [
            ["1", "2", "3"],
            ["4", "5"],
            [_ADVERTISING_CAMPAIGN_OTHER_BUTTON, _ADVERTISING_CAMPAIGN_STRENGTHEN_BUTTON],
            [_ADVERTISING_CAMPAIGN_CUSTOM_BUTTON],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _advertising_format_choice_markup() -> object | None:
    from ..advertising_style import ADVICE_BUTTON
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[_ADVERTISING_FORMAT_CLIP_BUTTON, ADVICE_BUTTON], [_ADVERTISING_FORMAT_POST_BUTTON], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _generation_mode_markup(session: dict | None = None) -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        project_mode_rows((session or {}).get("selected_project_key")),
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _story_voiceover_retry_markup() -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[_STORY_REGENERATE_BUTTON], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _story_english_choice_markup() -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [["Да, нужна EN"], ["Нет, только RU"], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _story_style_choice_markup(session: dict | None = None) -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        page_rows(session) + [[NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _show_story_style_page(update, session, *, show_previews=True):
    delivered = await send_story_style_previews(update.message, session) if show_previews else True
    text = page_description(session)
    if not delivered:
        text += "\nНе удалось загрузить все картинки. Нажмите «Показать примеры», чтобы повторить."
    await _reply_text_with_optional_markup(
        update, text, reply_markup=_story_style_choice_markup(session),
    )



def _story_script_approval_markup() -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [
            ["ОК"],
            ["Версии текста"],
            ["СВОЙ ТЕКСТ"],
            ["Сократить текст"],
            [_STORY_REGENERATE_BUTTON],
            ["Добавь"],
            ["Исправь"],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _discipline_intro_dialogue_approval_markup() -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [
            ["ОК"],
            ["Без интро"],
            [_STORY_REGENERATE_BUTTON],
            ["Добавь"],
            ["Исправь"],
            [DRAFT_BUTTON_SAVE],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _ask_generation_mode(update: Update, session: dict) -> None:
    session["state"] = STATE_WAITING_GENERATION_MODE
    await _reply_text_with_optional_markup(
        update,
        "Выберите режим генерации.",
        reply_markup=_generation_mode_markup(session),
    )


def _series_choice_markup(session: dict) -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    rows = [[str(i)] for i in range(1, len(session.get(_SERIES_CHOICE_IDS_SESSION_KEY) or []) + 1)]
    rows.append([_SERIES_BUTTON_NEW])
    rows.append([_SERIES_BUTTON_SKIP])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _format_series_choice_text(series_items) -> str:
    if not series_items:
        return (
            "Можно привязать это видео к серии.\n"
            "Активных серий для проекта пока нет: создайте новую или выберите «Без серии»."
        )
    lines = ["Можно привязать это видео к серии. Выберите номер:"]
    for idx, item in enumerate(series_items, start=1):
        lines.append(f"{idx}. {format_series_label(item)}")
    return "\n".join(lines)


async def _ask_series_choice(update: Update, session: dict) -> None:
    project_slug = str(session.get(_PROJECT_KEY_SESSION) or "").strip() or None
    series_items = list_active_series(project_slug)
    session[_SERIES_CHOICE_IDS_SESSION_KEY] = [item.id for item in series_items]
    session["state"] = STATE_WAITING_SERIES_CHOICE
    await _reply_text_with_optional_markup(
        update,
        _format_series_choice_text(series_items),
        reply_markup=_series_choice_markup(session),
    )


def _parse_series_create_text(text: str) -> tuple[str, str | None, int] | None:
    raw = " ".join(str(text or "").strip().split())
    if not raw:
        return None
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 2:
        title = parts[0]
        days_text = parts[1]
        if not title or not re.fullmatch(r"\d+", days_text):
            return None
        return title, None, int(days_text)
    if len(parts) < 3:
        return None
    ru_title = parts[0]
    en_title = parts[1]
    days_text = parts[2]
    days_match = re.fullmatch(r"\d+", days_text)
    legacy_match = re.fullmatch(r"\d+\s*(?:/|из|of)\s*(\d+)", days_text, flags=re.IGNORECASE)
    if not ru_title or not en_title or not (days_match or legacy_match):
        return None
    total_videos = int(days_match.group(0) if days_match else legacy_match.group(1))
    return ru_title, en_title, total_videos


def _clean_series_title(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "«", "»"}:
        text = text[1:-1].strip()
    for prefix in ("EN:", "English:", "Английский:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].strip()
            break
    return text


def _series_title_target_languages(session: dict) -> list[str]:
    if normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_SILENCE:
        return ["ru"]
    raw = session.get("target_languages")
    candidates = raw if isinstance(raw, list) and raw else target_languages_from_env()
    out: list[str] = []
    for item in ("ru", "en", *candidates):
        code = normalize_language_code(str(item or ""))
        if code and code not in out:
            out.append(code)
    return out or ["ru", "en"]


async def _translate_series_titles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    ru_title: str,
    languages: list[str],
) -> dict[str, str]:
    target_languages = [lang for lang in languages if lang != "ru"]
    title_by_lang = {"ru": _clean_series_title(ru_title)}
    if not target_languages:
        return title_by_lang
    lang_lines = "\n".join(f"- {lang}: {language_name(lang)}" for lang in target_languages)
    prompt = (
        "Translate this short Russian content-series title for social videos.\n"
        "Keep every translation concise, natural, catchy, and suitable as an on-video series label.\n"
        "Return only a strict JSON object where keys are the requested language codes and values are titles.\n"
        "Do not include Markdown, quotes around the whole object, comments, or extra keys.\n\n"
        f"Russian title: {title_by_lang['ru']}\n\n"
        f"Target languages:\n{lang_lines}"
    )
    raw = await _call_with_503_retries(
        update,
        context,
        chat_id,
        "translate_series_titles",
        lambda: asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT),
    )
    try:
        data = extract_json_object(str(raw or ""))
    except Exception:
        data = {}
    for lang in target_languages:
        translated = _clean_series_title(str(data.get(lang) or ""))
        if translated:
            title_by_lang[lang] = translated
    title_by_lang.setdefault("en", title_by_lang["ru"])
    return title_by_lang


def _format_series_titles_for_approval(title_by_lang: dict[str, str], languages: list[str]) -> str:
    lines = ["Предлагаю названия серии:"]
    for lang in languages:
        title = _clean_series_title(str(title_by_lang.get(lang) or ""))
        if title:
            lines.append(f"{lang.upper()}: {title}")
    extras = [lang for lang in title_by_lang if lang not in languages]
    for lang in extras:
        title = _clean_series_title(str(title_by_lang.get(lang) or ""))
        if title:
            lines.append(f"{lang.upper()}: {title}")
    lines.append("")
    lines.append(
        "Если подходит — нажмите «ОК». Если нужно исправить, "
        "пришлите новый EN-текст или строки вида `en: ... | es: ...`."
    )
    return "\n".join(lines)


def _parse_series_title_corrections(text: str, languages: list[str]) -> dict[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    chunks = [part.strip() for line in raw.splitlines() for part in line.split("|")]
    out: dict[str, str] = {}
    for chunk in chunks:
        match = re.fullmatch(r"([a-z]{2}(?:[-_][a-z]{2})?)\s*:\s*(.+)", chunk, flags=re.IGNORECASE)
        if not match:
            continue
        code = normalize_language_code(match.group(1))
        title = _clean_series_title(match.group(2))
        if code and title:
            out[code] = title
    if out:
        return out
    if "en" in languages:
        corrected = _clean_series_title(raw)
        if corrected:
            return {"en": corrected}
    return {}


def _create_series_from_title_map(session: dict, title_by_lang: dict[str, str], total_videos: int):
    ru_title = _clean_series_title(str(title_by_lang.get("ru") or ""))
    en_title = _clean_series_title(str(title_by_lang.get("en") or "")) or ru_title
    return create_series(
        title_ru=ru_title,
        title_en=en_title,
        title_by_lang=title_by_lang,
        total_videos=total_videos,
        project_slug=str(session.get(_PROJECT_KEY_SESSION) or "").strip() or None,
    )


def _copy_series_context(source: dict, target: dict) -> None:
    for key in _SERIES_SESSION_KEYS:
        if key in source:
            target[key] = source[key]


def _copy_generation_mode_context(source: dict, target: dict) -> None:
    if source.get("directorial_notes"):
        target["directorial_notes"] = list(source["directorial_notes"])
    mode = normalize_generation_mode(source.get(GENERATION_MODE_SESSION_KEY))
    target[GENERATION_MODE_SESSION_KEY] = mode
    target["target_languages"] = target_languages_for_generation_mode(
        mode,
        target.get("target_languages") if isinstance(target.get("target_languages"), list) else target_languages_from_env(),
    )


def _copy_overlay_title_context(source: dict, target: dict) -> None:
    for key in (
        OVERLAY_TITLE_SESSION_KEY,
        OVERLAY_TITLE_BY_LANG_SESSION_KEY,
        OVERLAY_TITLE_STYLE_SESSION_KEY,
        OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY,
        OVERLAY_SUBTITLE_DECIDED_SESSION_KEY,
        OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY,
    ):
        if key in source:
            target[key] = source[key]


def _has_overlay_titles(session: dict) -> bool:
    titles = session.get(OVERLAY_TITLE_BY_LANG_SESSION_KEY)
    return isinstance(titles, dict) and any(str(value or "").strip() for value in titles.values())


async def _ask_overlay_title_before_recipe_plan(update: Update, chat_id: int, session: dict) -> bool:
    session[OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] = OVERLAY_TITLE_NEXT_RECIPE_PLAN
    session["state"] = STATE_WAITING_OVERLAY_TITLE_RU
    SESSIONS[chat_id] = session
    await reply_text_without_keyboard(
        update.message,
        "Рецепт утверждён. До viral plan введите русский заголовок для верхней надписи.",
    )
    return True


async def _ask_overlay_subtitle_before_recipe_plan(update: Update, chat_id: int, session: dict) -> bool:
    session[OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] = OVERLAY_TITLE_NEXT_RECIPE_PLAN
    session["state"] = STATE_WAITING_OVERLAY_SUBTITLE_RU
    SESSIONS[chat_id] = session
    await safe_reply_text(
        update.message,
        "Добавить третью строку под заголовком? Она будет мелкой и спокойной.\n"
        "Например: КБЖУ на 100 гр: 126/10/4/24\n\n"
        "Пришлите текст одним сообщением или нажмите «ОК» / напишите «нет», чтобы пропустить.",
        reply_markup=ok_only_markup(),
    )
    return True


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/generate <текст>``: обычный старт пайплайна (без fastpath)."""
    await _start_generation(update, context, prefer_vertex_api=False)


async def quick_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/quick <текст>``: старт пайплайна с приоритетом VERTEX_API_KEY."""
    await _start_generation(update, context, prefer_vertex_api=True)


async def _announce_generation_mode(
    update: Update,
    *,
    chat_id: int,
    prefer_vertex_api: bool,
) -> None:
    """Отправляет сообщения о режиме запуска и обязательные предупреждения."""
    _bump_debounce_generation(chat_id)
    existing = SESSIONS.get(chat_id) or {}
    warn = None if str(existing.get("project_voice_id") or "").strip() else _elevenlabs_voice_warning()
    if warn:
        await update.message.reply_text(warn)
    if prefer_vertex_api:
        await update.message.reply_text(
            "Приняла режим «быстро»: для Gemini включаю приоритет VERTEX_API_KEY на этом запуске."
        )


async def _start_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prefer_vertex_api: bool,
) -> None:
    """
    Общий обработчик старта пайплайна для `/generate` и `/quick`.
    """
    if update.message is None or update.effective_chat is None:
        return
    recipe = _extract_recipe_from_command_text(update.message.text or "")
    if not recipe.strip():
        cmd = "/quick" if prefer_vertex_api else "/generate"
        await update.message.reply_text(
            f'Пришлите текст рецепта после команды.\nПример: {cmd} "Суп: 1) нарезать... 2) варить..."'
        )
        return
    await _start_generation_with_recipe(
        update,
        context,
        recipe=recipe,
        prefer_vertex_api=prefer_vertex_api,
    )


async def _start_generation_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
    prefer_vertex_api: bool,
) -> None:
    """
    Запускает пайплайн генерации по уже полученному тексту рецепта/идеи.

    Используется как командным входом (`/generate`, `/quick`), так и кнопочным UX первого шага.
    """
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    existing_session = SESSIONS.get(chat_id) or {}
    selected_project_key = str(existing_session.get(_PROJECT_KEY_SESSION) or "").strip()
    await _announce_generation_mode(update, chat_id=chat_id, prefer_vertex_api=prefer_vertex_api)
    session = {
        **_session_for_new_generation(existing_session),
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_NEXT_SESSION_KEY: SERIES_NEXT_GENERATION,
        SERIES_PENDING_RECIPE_SESSION_KEY: recipe,
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: prefer_vertex_api,
    }
    if selected_project_key:
        session[_PROJECT_KEY_SESSION] = selected_project_key
    SESSIONS[chat_id] = session
    selected_mode = session.pop(PENDING_MODE_KEY, None)
    if selected_mode in project_mode_labels(selected_project_key):
        await _handle_waiting_generation_mode_state(
            update, context, chat_id=chat_id, session=session, text=selected_mode,
        )
    elif session.get("force_story_mode") is True and selected_project_key != "discipline":
        await _handle_waiting_generation_mode_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=_GENERATION_MODE_BUTTON_STORY,
        )
    else:
        await _ask_generation_mode(update, session)
    return


async def _continue_generation_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
    prefer_vertex_api: bool,
) -> None:
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    existing_session = SESSIONS.get(chat_id) or {}
    selected_project_key = str(existing_session.get(_PROJECT_KEY_SESSION) or "").strip()
    source_url = _extract_first_http_url(recipe)
    if source_url:
        try:
            source_text = _fetch_recipe_text_from_url(source_url)
        except Exception as e:
            await update.message.reply_text(
                f"Не удалось извлечь рецепт по ссылке: {_user_error_message(e)}\n"
                "Пришлите, пожалуйста, текст рецепта сообщением."
            )
            return
        await update.message.reply_text(
            "Нашла ссылку и извлекла текст страницы. Передаю весь текст нейросети, чтобы она выделила рецепт."
        )
        await reply_text_without_keyboard(update.message, "Извлекаю рецепт, ждите.")
        try:
            recipe = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "extract_recipe_from_source_text",
                lambda: generate_recipe_from_source_text(source_text, source_url=source_url),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return
        except Exception as e:
            logger.exception("recipe extraction from source text failed")
            await update.message.reply_text(f"Ошибка извлечения рецепта из ссылки: {_user_error_message(e)}")
            return
        await update.message.reply_text(
            "Ниже рецепт, который нейросеть извлекла из текста страницы. Подтвердите или исправьте."
        )
    parsed_steps = _try_parse_direct_recipe_from_user_text(recipe)
    # Если пользователь сразу прислал рецепт по шагам — пропускаем этап концептов.
    advice_mode = normalize_generation_mode(existing_session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_ADVICE
    if (parsed_steps or source_url) and not advice_mode:
        new_session = _build_direct_recipe_session(
            recipe,
            parsed_steps or [],
            prefer_vertex_api=prefer_vertex_api,
        )
        if selected_project_key:
            new_session[_PROJECT_KEY_SESSION] = selected_project_key
        _copy_series_context(existing_session, new_session)
        _copy_generation_mode_context(existing_session, new_session)
        _copy_overlay_title_context(existing_session, new_session)
        SESSIONS[chat_id] = new_session
        logger.info(
            "chat=%s state=%s (recipe provided directly%s)",
            chat_id,
            STATE_WAITING_RECIPE_APPROVAL,
            " from link" if source_url else "",
        )
        await update.message.reply_text(
            (
                "Приняла рецепт из ссылки. Ниже текст для подтверждения; после «ОК» сгенерирую план сцен."
                if source_url
                else "Приняла рецепт из сообщения. Ниже тот же текст для подтверждения; после «ОК» сгенерирую план сцен."
            )
        )
        await _reply_text_chunked(update, format_full_recipe_for_approval(recipe.strip()))
        await _reply_text_with_optional_markup(
            update,
            "Если рецепт подходит — нажми «ОК». "
            "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли текст правки.",
            reply_markup=ok_only_markup(),
        )
        return

    try:
        await reply_text_without_keyboard(update.message, "Генерирую варианты рецепта, ждите.")
        result = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_recipe_concepts",
            lambda: generate_recipe_concepts(advice_concept_input(recipe, normalize_generation_mode(existing_session.get(GENERATION_MODE_SESSION_KEY)))),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        logger.exception("generate failed after retries")
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        logger.exception("generate failed")
        await update.message.reply_text(f"Ошибка генерации: {_user_error_message(e)}")
        return
    if result is None:
        await update.message.reply_text("Ошибка генерации: не удалось получить ответ модели.")
        return
    new_session = _build_concepts_session(
        recipe,
        result["concepts"],
        prefer_vertex_api=prefer_vertex_api,
    )
    if selected_project_key:
        new_session[_PROJECT_KEY_SESSION] = selected_project_key
    _copy_series_context(existing_session, new_session)
    _copy_generation_mode_context(existing_session, new_session)
    _copy_overlay_title_context(existing_session, new_session)
    SESSIONS[chat_id] = new_session
    logger.info("chat=%s state=%s", chat_id, STATE_WAITING_CONCEPT)
    await _reply_text_chunked(update, format_advice_choices(result["concepts"]) if normalize_generation_mode(SESSIONS[chat_id].get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_ADVICE else result["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер варианта кнопкой. "
        "Либо нажмите «Добавь»/«Исправь» и отправьте правку для нейросети.",
        reply_markup=_concept_choice_markup(result["concepts"]),
    )


async def _start_advertising_campaign_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    direction: str = "new",
) -> bool:
    source_text = str(session.get(ADVERTISING_SOURCE_SESSION_KEY) or "").strip()
    product_brief = session.get(ADVERTISING_PRODUCT_BRIEF_SESSION_KEY)
    if not source_text or not isinstance(product_brief, dict):
        session["state"] = STATE_WAITING_ADVERTISING_PRODUCT_IMAGE
        SESSIONS[chat_id] = session
        await update.message.reply_text("Не вижу исходную идею или карточку товара. Пришлите фото товара ещё раз.")
        return True
    stronger = str(direction).strip().lower() == "stronger"
    try:
        await reply_text_without_keyboard(
            update.message,
            "Усиливаю пять рекламных концепций, ждите."
            if stronger
            else "Придумываю пять разных рекламных кампаний для товара, ждите.",
        )
        result = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_advertising_campaigns",
            lambda: asyncio.to_thread(
                generate_advertising_campaigns,
                source_text=source_text,
                product_brief=product_brief,
                previous_campaigns=(session.get(ADVERTISING_CAMPAIGNS_SESSION_KEY) or []),
                direction="stronger" if stronger else "new",
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("advertising campaign generation failed")
        await update.message.reply_text(f"Ошибка подготовки рекламных идей: {_user_error_message(e)}")
        return True
    campaigns = result.get("campaigns") if isinstance(result, dict) else None
    if not isinstance(campaigns, list) or len(campaigns) != 5:
        await update.message.reply_text("Не удалось получить пять рекламных вариантов. Нажмите «Другие варианты» и попробуйте ещё раз.")
        return True
    session[ADVERTISING_CAMPAIGNS_SESSION_KEY] = campaigns
    session.pop(ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY, None)
    session.setdefault(ADVERTISING_FORMAT_SESSION_KEY, "clip")
    session["state"] = STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE
    SESSIONS[chat_id] = session
    await _reply_text_chunked(update, str(result.get("text") or ""))
    await _reply_text_with_optional_markup(
        update,
        "Выберите идею кнопкой 1–5. Можно запросить другие или усиленные варианты либо прислать свой сюжет.",
        reply_markup=_advertising_campaign_choice_markup(),
    )
    return True


async def _start_advertising_scene_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
) -> bool:
    source_text = str(
        session.get(ADVERTISING_SOURCE_SESSION_KEY)
        or session.get(SERIES_PENDING_RECIPE_SESSION_KEY)
        or ""
    ).strip()
    product_brief = session.get(ADVERTISING_PRODUCT_BRIEF_SESSION_KEY)
    product_path = str(session.get(ADVERTISING_PRODUCT_PATH_SESSION_KEY) or "").strip()
    if not source_text or not isinstance(product_brief, dict) or not product_path:
        session["state"] = STATE_WAITING_ADVERTISING_PRODUCT_IMAGE
        SESSIONS[chat_id] = session
        await update.message.reply_text("Для рекламы нужен исходный текст и фотография товара. Пришлите фото товара ещё раз.")
        return True
    try:
        await reply_text_without_keyboard(update.message, "Строю рекламный план из 3–6 сцен Veo, ждите.")
        campaign = session.get(ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY)
        result = campaign.get("prepared_scene_plan") if isinstance(campaign, dict) else None
        if isinstance(result, dict) and result.get("video_style", "story") != product_brief.get("video_style", "story"):
            result = None
        if not isinstance(result, dict):
            result = await _call_with_503_retries(
                update, context, chat_id, "generate_advertising_scene_plan",
                lambda: asyncio.to_thread(generate_advertising_scene_plan,
                    source_text=source_text, product_brief=product_brief, selected_campaign=campaign),
            )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("advertising scene plan failed")
        await update.message.reply_text(f"Ошибка подготовки рекламного плана: {_user_error_message(e)}")
        return True
    plan = result.get("json_plan") if isinstance(result, dict) else None
    if not isinstance(plan, list) or not 3 <= len(plan) <= 6:
        await update.message.reply_text("Не удалось получить полный рекламный план из 3–6 сцен. Попробуйте ещё раз.")
        return True
    title = str(result.get("title") or product_brief.get("product_name") or "Реклама товара").strip()
    session.pop(SERIES_PENDING_RECIPE_SESSION_KEY, None)
    session.pop(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY, None)
    session[ADVERTISING_CONTENT_TYPE_SESSION_KEY] = str(result.get("content_type") or "story")
    session["selected_concept"] = title
    session["selected_concept_name"] = title
    session["recipe_full_text"] = source_text
    session["user_input"] = source_text
    session["brand_context"] = json.dumps(product_brief, ensure_ascii=False)
    session["sponsored_content"] = True
    session["viral_concept_brief"] = {
        "main_hook": str(plan[0].get("text") or ""),
        "brand_integration_plan": "Один и тот же товар целиком виден во всех сценах.",
        "content_type": session[ADVERTISING_CONTENT_TYPE_SESSION_KEY],
    }
    session["romantic_drama_subplot"] = {}
    session["retention_review"] = {}
    session["scenes_plan"] = plan
    session["scenes_text"] = str(result.get("text_plan") or "")
    session["recipe_steps"] = [str(item.get("text") or item.get("action") or "") for item in plan]
    session["state"] = STATE_WAITING_SCENE_APPROVAL
    SESSIONS[chat_id] = session
    run_id, _scene_map = ensure_generation_run_for_session(session)
    if run_id:
        persist_generation_artifact(
            session,
            artifact_type="advertising_product_reference",
            label="Mandatory advertising product reference",
            uri=file_uri(product_path),
            mime_type="image/jpeg",
            payload={"product_brief": product_brief, "required_in_every_scene": True},
        )
    await _reply_text_chunked(update, session["scenes_text"])
    await _reply_text_with_optional_markup(
        update,
        "Проверьте рекламный план. Товар обязателен во всех сценах. «ОК» — перейти к заголовку и production; "
        "иначе пришлите правку.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _continue_generation_after_series(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
) -> bool:
    next_step = str(session.pop(SERIES_PENDING_NEXT_SESSION_KEY, "") or "").strip()
    if (
        not next_step
        and normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_ADVERTISING
        and session.get(ADVERTISING_PRODUCT_BRIEF_SESSION_KEY)
    ):
        next_step = SERIES_NEXT_ADVERTISING_PLAN
    session.pop(_SERIES_CHOICE_IDS_SESSION_KEY, None)
    if next_step == SERIES_NEXT_ADVERTISING_PLAN:
        return await _start_advertising_scene_plan(
            update,
            context,
            chat_id=chat_id,
            session=session,
        )
    if next_step == SERIES_NEXT_OVERLAY_TITLE:
        session["state"] = STATE_WAITING_OVERLAY_TITLE_RU
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(
            update.message,
            "Теперь введите русский заголовок для верхней надписи.",
        )
        return True
    recipe = str(session.get(SERIES_PENDING_RECIPE_SESSION_KEY) or "").strip()
    prefer_vertex_api = bool(session.get(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY))
    if not recipe:
        await update.message.reply_text("Не вижу исходный рецепт или идею. Начните генерацию заново.")
        session["state"] = ""
        SESSIONS[chat_id] = session
        return True
    session.pop(SERIES_PENDING_RECIPE_SESSION_KEY, None)
    session.pop(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY, None)
    SESSIONS[chat_id] = session
    await _continue_generation_with_recipe(
        update,
        context,
        recipe=recipe,
        prefer_vertex_api=prefer_vertex_api,
    )
    return True


async def _start_initial_story_voiceover(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
) -> bool:
    source_story = str(
        session.get(SERIES_PENDING_RECIPE_SESSION_KEY)
        or session.get(STORY_SOURCE_SESSION_KEY)
        or session.get("user_input")
        or ""
    ).strip()
    if not source_story:
        session["state"] = ""
        SESSIONS[chat_id] = session
        await update.message.reply_text("Не вижу исходную историю. Начните снова и пришлите её после /generate.")
        return True
    session[STORY_SOURCE_SESSION_KEY] = source_story
    session["user_input"] = source_story
    session["recipe_full_text"] = source_story
    SESSIONS[chat_id] = session
    try:
        await reply_text_without_keyboard(update.message, "Составляю русский текст закадровой озвучки, ждите.")
        story = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_story_voiceover",
            lambda: asyncio.to_thread(generate_story_voiceover, source_story,
                **({"discipline_mode": session["discipline_narrative_mode"]} if session.get("discipline_narrative_mode") else {})),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        session["state"] = STATE_WAITING_GENERATION_MODE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            f"{e}\nИсходная история сохранена. Нажмите «{_STORY_REGENERATE_BUTTON}», чтобы повторить.",
            reply_markup=_story_voiceover_retry_markup(),
        )
        return True
    except Exception as e:
        logger.exception("story voiceover generation failed")
        session["state"] = STATE_WAITING_GENERATION_MODE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            f"Ошибка подготовки истории: {_user_error_message(e)}\n"
            f"Исходная история сохранена. Нажмите «{_STORY_REGENERATE_BUTTON}», чтобы повторить.",
            reply_markup=_story_voiceover_retry_markup(),
        )
        return True
    session.pop(SERIES_PENDING_RECIPE_SESSION_KEY, None)
    session.pop(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY, None)
    archive_story_voiceover_version(session, story, stage="initial")
    session[STORY_SOURCE_SESSION_KEY] = source_story
    session[STORY_TITLE_SESSION_KEY] = story["title"]
    session[STORY_VOICEOVER_SESSION_KEY] = story["voiceover_text"]
    session["user_input"] = source_story
    session["selected_concept"] = story["title"]
    session["selected_concept_name"] = story["title"]
    session["recipe_full_text"] = source_story
    # Kling is generated once in RU and later reused for the optional EN master.
    session["target_languages"] = ["ru"]
    session["state"] = STATE_WAITING_STORY_SCRIPT_APPROVAL
    SESSIONS[chat_id] = session
    await _reply_text_chunked(
        update,
        f"ТЕКСТ RU-ОЗВУЧКИ · {story['title']}\n\n{story['voiceover_text']}",
    )
    length_note = ""
    if not bool(story.get("within_target_limit", True)):
        length_note = (
            f"\n\nТекст длиннее целевого лимита: {story.get('word_count', '?')} слов, "
            f"{story.get('character_count', '?')} знаков. Я сохранила эту версию и ничего не меняю автоматически."
        )
    english_note = (
        " После утверждения RU-аудио я подготовлю английский перевод на отдельный апрув."
        if session.get(STORY_ENGLISH_REQUESTED_SESSION_KEY)
        else ""
    )
    await _reply_text_with_optional_markup(
        update,
        "Если текст подходит — нажмите «ОК», и я создам цельную RU-озвучку ElevenLabs. "
        "«Сократить текст» или «ПЕРЕГЕНЕРИРОВАТЬ» создадут новую текстовую версию, сохранив текущую. "
        "«СВОЙ ТЕКСТ» позволит прислать готовый вариант дословно, без обработки Gemini. "
        "Если нужны другие изменения — нажмите «Добавь» или «Исправь», затем пришлите фидбэк."
        + english_note
        + length_note,
        reply_markup=_story_script_approval_markup(),
    )
    return True


async def _start_discipline_intro_dialogue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    feedback: str = "",
) -> bool:
    """Generate the project-specific intro dialogue before the ordinary Story flow."""
    project_slug = str(session.get(_PROJECT_KEY_SESSION) or "").strip()
    first_name, second_name = intro_character_names(project_slug)
    source_story = str(
        session.get(SERIES_PENDING_RECIPE_SESSION_KEY)
        or session.get(STORY_SOURCE_SESSION_KEY)
        or session.get("user_input")
        or ""
    ).strip()
    if not source_story:
        session["state"] = ""
        SESSIONS[chat_id] = session
        await update.message.reply_text("Не вижу исходную тему для вступительного диалога.")
        return True
    session[STORY_SOURCE_SESSION_KEY] = source_story
    session["user_input"] = source_story
    session["recipe_full_text"] = source_story
    previous = session.get(DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY)
    dialogue = parse_intro_dialogue_text(feedback, project_slug=project_slug)
    if dialogue is not None:
        await reply_text_without_keyboard(
            update.message,
            f"Приняла ваш диалог «{first_name} — {second_name}» дословно.",
        )
    else:
        try:
            await reply_text_without_keyboard(
                update.message,
                f"Готовлю короткий диалог «{first_name} — {second_name}» для интро, ждите.",
            )
            dialogue = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_discipline_intro_dialogue",
                lambda: asyncio.to_thread(
                    generate_discipline_intro_dialogue,
                    source_story,
                    previous=previous if isinstance(previous, dict) else None,
                    feedback=feedback,
                    project_slug=project_slug,
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as exc:
            await update.message.reply_text(str(exc))
            return True
        except Exception as exc:
            logger.exception("discipline intro dialogue generation failed")
            await update.message.reply_text(f"Ошибка подготовки диалога: {_user_error_message(exc)}")
            return True
    versions = session.get(DISCIPLINE_INTRO_DIALOGUE_VERSIONS_SESSION_KEY)
    if not isinstance(versions, list):
        versions = []
    versions.append({"dialogue": dict(dialogue), "feedback": str(feedback or "")})
    session[DISCIPLINE_INTRO_DIALOGUE_VERSIONS_SESSION_KEY] = versions
    session[DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY] = dict(dialogue)
    for key in (
        DISCIPLINE_INTRO_IMAGE_PATH_SESSION_KEY,
        DISCIPLINE_INTRO_AUDIO_PATH_SESSION_KEY,
        DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY,
        DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY,
        DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY,
    ):
        session.pop(key, None)
    session["state"] = STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL
    SESSIONS[chat_id] = session
    await _reply_text_chunked(
        update,
        format_discipline_intro_dialogue(dialogue, project_slug=project_slug),
    )
    await _reply_text_with_optional_markup(
        update,
        "Утвердите диалог до генерации кадра и видео. «ОК» — создать стартовый кадр через Vertex. "
        "«Без интро» — сразу продолжить основную историю. «ПЕРЕГЕНЕРИРОВАТЬ» или текстовая правка создадут новый вариант.",
        reply_markup=_discipline_intro_dialogue_approval_markup(),
    )
    return True


async def _handle_waiting_story_english_choice_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = str(text or "").strip().lower()
    if low in {"да", "yes", "y", "да, нужна en", "нужна en", "en"}:
        session[STORY_ENGLISH_REQUESTED_SESSION_KEY] = True
        await update.message.reply_text(
            "Добавлю английскую версию. Сначала полностью согласуем RU-текст и RU-аудио, затем отдельно EN-текст и EN-аудио."
        )
    elif low in {"нет", "no", "n", "нет, только ru", "только ru", "ru"}:
        session[STORY_ENGLISH_REQUESTED_SESSION_KEY] = False
        await update.message.reply_text("Оставляю только русскую версию.")
    else:
        await _reply_text_with_optional_markup(
            update,
            "Нужна английская версия этой истории?",
            reply_markup=_story_english_choice_markup(),
        )
        return True
    session["state"] = STATE_WAITING_STORY_STYLE_CHOICE
    SESSIONS[chat_id] = session
    session.pop(PAGE_KEY, None)
    await _show_story_style_page(update, session, show_previews=False)
    return True


async def _handle_waiting_story_style_choice_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    action = str(text or "").strip()
    if action in {PREVIEW_BUTTON, NEXT_BUTTON, PREVIOUS_BUTTON}:
        session.pop(PAGE_KEY, None)
        await _show_story_style_page(update, session)
        return True
    text = resolve_choice(text)
    low = str(text or "").strip().lower()
    if low in {"основной", "основная", "primary", "main"}:
        style_key = STORY_STYLE_PRIMARY
        label = "Основной"
    elif low in {"сухая акварель", "акварель", "watercolor"}:
        style_key = STORY_STYLE_WATERCOLOR
        label = "Сухая акварель"
    elif low in {"ретро-мультфильм", "ретро мультфильм", "мультфильм", "retro cartoon"}:
        style_key = STORY_STYLE_RETRO_CARTOON
        label = "Ретро-мультфильм"
    elif low in {"книжная гуашь", "книжная иллюстрация", "storybook gouache"}:
        style_key = STORY_STYLE_STORYBOOK_GOUACHE
        label = "Книжная гуашь"
    elif low in {"пластилиновый мультфильм", "пластилин", "claymation"}:
        style_key = STORY_STYLE_CLAYMATION
        label = "Пластилиновый мультфильм"
    elif low in {"кукольный стоп-моушен", "кукольный стоп моушен", "кукольный", "felt stop motion"}:
        style_key = STORY_STYLE_FELT_STOP_MOTION
        label = "Кукольный стоп-моушен"
    elif low in {"рисованная ретро-сказка", "рисованная ретро сказка", "ретро-сказка", "retro fairytale"}:
        style_key = STORY_STYLE_RETRO_FAIRYTALE
        label = "Рисованная ретро-сказка"
    elif low in {"сумеречная акварель", "вечерняя акварель", "twilight watercolor"}:
        style_key = STORY_STYLE_TWILIGHT_WATERCOLOR
        label = "Сумеречная акварель"
    elif low in {"светлая анимационная пастель", "анимационная пастель", "soft animation"}:
        style_key = STORY_STYLE_SOFT_ANIMATION
        label = "Светлая анимационная пастель"
    elif low in {"классический рисованный мультфильм", "рисованный мультфильм", "classic cel cartoon"}:
        style_key = STORY_STYLE_CLASSIC_CEL_CARTOON
        label = "Классический рисованный мультфильм"
    elif low in {"фетровая аппликация", "аппликация", "felt applique"}:
        style_key = STORY_STYLE_FELT_APPLIQUE
        label = "Фетровая аппликация"
    elif low in {"народная рисованная сказка", "народная сказка", "folk fairytale"}:
        style_key = STORY_STYLE_FOLK_FAIRYTALE
        label = "Народная рисованная сказка"
    elif low in {"пастельные мелки", "мелки", "oil pastel"}:
        style_key = STORY_STYLE_OIL_PASTEL
        label = "Пастельные мелки"
    elif low in {"вязаная иллюстрация", "вязание", "crochet"}:
        style_key = STORY_STYLE_CROCHET
        label = "Вязаная иллюстрация"
    elif low in {"минималистичный ретро-мультфильм", "минималистичный мультфильм", "minimal retro cartoon"}:
        style_key = STORY_STYLE_MINIMAL_RETRO_CARTOON
        label = "Минималистичный ретро-мультфильм"
    else:
        await _reply_text_with_optional_markup(
            update,
            "Выберите стиль кнопкой под последними примерами.",
            reply_markup=_story_style_choice_markup(session),
        )
        return True
    session[STORY_VISUAL_STYLE_SESSION_KEY] = style_key
    SESSIONS[chat_id] = session
    if style_key == STORY_STYLE_PRIMARY:
        await update.message.reply_text(
            "Стиль истории: «Основной». Использую только стилистику референсов персонажей."
        )
    else:
        await update.message.reply_text(f"Стиль истории: «{label}». Применю его ко всем персонажам и всем кадрам.")
    if is_visualization_generation(session):
        from .handlers_state_scene import _approve_story_audio_and_build_scenes

        return await _approve_story_audio_and_build_scenes(
            update,
            context,
            chat_id=chat_id,
            session=session,
        )
    return await _start_initial_story_voiceover(
        update,
        context,
        chat_id=chat_id,
        session=session,
    )


async def _handle_waiting_generation_mode_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = str(text or "").strip().lower()
    retry_story = (
        low in {_STORY_REGENERATE_BUTTON.lower(), "перегенерировать историю"}
        and normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_STORY
    )
    from ..discipline_modes import MODE_KEY, VASYA, REFRAME, LABELS
    requested_format = {"о васе": VASYA, "а что если…": REFRAME, "а что если": REFRAME,
                        "а что если...": REFRAME}.get(low)
    if requested_format and session.get(_PROJECT_KEY_SESSION) != "discipline":
        await update.message.reply_text("Этот режим доступен в проекте «Дисциплина».")
        return True
    if requested_format:
        session[MODE_KEY] = requested_format
        session[DISCIPLINE_INTRO_ENABLED_SESSION_KEY] = False
        mode = GENERATION_MODE_STORY
    elif retry_story:
        mode = GENERATION_MODE_STORY
    elif low in {"совет", "advice"}:
        mode = GENERATION_MODE_ADVICE
    elif low in {_GENERATION_MODE_BUTTON_NORMAL.lower(), "normal", "обычная", "обычный режим"}:
        mode = GENERATION_MODE_NORMAL
    elif low in {_GENERATION_MODE_BUTTON_SILENCE.lower(), GENERATION_MODE_SILENCE, "silent", "тихий"}:
        mode = GENERATION_MODE_SILENCE
    elif low in {_GENERATION_MODE_BUTTON_LONG.lower(), GENERATION_MODE_LONG, "длинный", "долгий", "long horizontal"}:
        mode = GENERATION_MODE_LONG
    elif low in {_GENERATION_MODE_BUTTON_COPY.lower(), GENERATION_MODE_COPY, "copy", "video copy"}:
        mode = GENERATION_MODE_COPY
    elif low in {_GENERATION_MODE_BUTTON_STORY.lower(), GENERATION_MODE_STORY, "story", "narrative"}:
        mode = GENERATION_MODE_STORY
    elif low in {
        _GENERATION_MODE_BUTTON_VISUALIZATION.lower(),
        GENERATION_MODE_VISUALIZATION,
        "visualisation",
        "visualize",
    }:
        mode = GENERATION_MODE_VISUALIZATION
    elif low in {
        _GENERATION_MODE_BUTTON_ADVERTISING.lower(),
        GENERATION_MODE_ADVERTISING,
        "advert",
        "advertising",
        "ad",
    }:
        mode = GENERATION_MODE_ADVERTISING
    else:
        await _reply_text_with_optional_markup(
            update,
            "Выберите формат: " + ", ".join(project_mode_labels(session.get(_PROJECT_KEY_SESSION))) + ".",
            reply_markup=_generation_mode_markup(session),
        )
        return True
    if mode == GENERATION_MODE_ADVICE and str(session.get(_PROJECT_KEY_SESSION) or "grandma_kitchen") != "grandma_kitchen":
        await update.message.reply_text("Формат «Совет» доступен для проекта бабушки. Выберите проект бабушки и начните снова.")
        return True
    if not requested_format and not retry_story:
        session.pop(MODE_KEY, None)
    session[GENERATION_MODE_SESSION_KEY] = normalize_generation_mode(mode)
    session["target_languages"] = target_languages_for_generation_mode(
        mode,
        target_languages_from_env(),
    )
    if mode == GENERATION_MODE_ADVICE:
        message = "Формат «Совет»: предложу вопросы дочери на выбор. В первом клипе дочь задаёт вопрос, бабушка сразу отвечает; дальше показываем решение. Языки: " + ", ".join(lang.upper() for lang in session["target_languages"]) + "."
    elif mode == GENERATION_MODE_SILENCE:
        message = "Ок, запускаю silence-режим: один RU-формат, Kling, короткие динамичные кадры."
    elif mode == GENERATION_MODE_LONG:
        message = "Ок, запускаю длинный режим: RU, горизонталь 16:9, подробный ролик от 3:10."
    elif mode == GENERATION_MODE_COPY:
        message = "Ок, режим «Копия»: пришлите исходное видео, я разберу его по таймингам и подготовлю Seedance-промпты."
    elif mode == GENERATION_MODE_STORY:
        message = (
            "Повторно составляю текст истории с тем же исходником."
            if retry_story
            else "Ок, режим «История»: озвучка ElevenLabs до двух минут и немые 3-секундные сцены Kling. Сначала уточню, нужна ли EN-версия."
        )
    elif mode == GENERATION_MODE_VISUALIZATION:
        message = (
            "Ок, режим «Визуализация»: пришлите аудио или видео. Из видео я извлеку дорожку, "
            "распознаю речь с таймингами и построю по ней видеоряд в выбранной стилистике проекта."
        )
    elif mode == GENERATION_MODE_ADVERTISING:
        message = (
            "Ок, режим «Реклама»: после фото товара предложу 5 рекламных кампаний и дам выбрать формат «Клип» или «Пост». "
            "По умолчанию — клип из 3–6 русскоязычных сцен Veo; можно выбрать пост с одним изображением и готовым текстом."
        )
    else:
        message = "Ок, запускаю обычный режим."
    if requested_format:
        message = (f"Режим «{LABELS[requested_format]}»: раскладываем вашу тему на проблему, поворот и решение. "
                   "Сначала согласуем сценарий, затем озвучку и наглядный видеоряд. Оформление заголовка выберете по карточкам.")
    await update.message.reply_text(message)
    if mode == GENERATION_MODE_VISUALIZATION:
        session["state"] = STATE_WAITING_VISUALIZATION_MEDIA
        session[STORY_ENGLISH_REQUESTED_SESSION_KEY] = False
        session.pop(SERIES_PENDING_RECIPE_SESSION_KEY, None)
        session.pop(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY, None)
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(
            update.message,
            "Жду аудиофайл, голосовое сообщение или видео. Для видео звук будет отделён автоматически.",
        )
        return True
    if mode == GENERATION_MODE_COPY:
        session["state"] = "state_waiting_first_step_copy_video"
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(update.message, "Жду исходное видео файлом или video-сообщением. Можно сначала прислать текст или рецепт для описаний. После финальных видео пришлю описания для публикации по вашему рецепту.")
        return True
    if mode == GENERATION_MODE_ADVERTISING:
        source_text = str(session.get(SERIES_PENDING_RECIPE_SESSION_KEY) or "").strip()
        if not source_text:
            session["state"] = ""
            SESSIONS[chat_id] = session
            await update.message.reply_text("Не вижу исходной идеи, рецепта или истории. Начните генерацию заново.")
            return True
        session[ADVERTISING_SOURCE_SESSION_KEY] = source_text
        session["sponsored_content"] = True
        session["state"] = STATE_WAITING_ADVERTISING_PRODUCT_IMAGE
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(
            update.message,
            "Теперь обязательно пришлите фотографию рекламируемого товара. Можно добавить подпись с названием бренда "
            "или важными деталями. Без фото товара рекламный сценарий не запускается.",
        )
        return True
    if mode == GENERATION_MODE_STORY:
        source_story = str(
            session.get(SERIES_PENDING_RECIPE_SESSION_KEY)
            or session.get(STORY_SOURCE_SESSION_KEY)
            or session.get("user_input")
            or ""
        ).strip()
        if not source_story:
            session["state"] = ""
            SESSIONS[chat_id] = session
            await update.message.reply_text("Не вижу исходную историю. Начните снова и пришлите её после /generate.")
            return True
        session[STORY_SOURCE_SESSION_KEY] = source_story
        session["user_input"] = source_story
        session["recipe_full_text"] = source_story
        session["state"] = STATE_WAITING_STORY_ENGLISH_CHOICE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Нужна английская версия этой истории? Видеоряд будет один и тот же; отдельно согласуем RU и EN тексты и аудио.",
            reply_markup=_story_english_choice_markup(),
        )
        return True
    if mode == GENERATION_MODE_SILENCE:
        session[OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] = OVERLAY_TITLE_NEXT_SERIES_CHOICE
        session["state"] = STATE_WAITING_OVERLAY_TITLE_RU
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(
            update.message,
            "Теперь введите русский заголовок для верхней надписи.",
        )
        return True
    await _ask_series_choice(update, session)
    SESSIONS[chat_id] = session
    return True


async def _handle_waiting_series_choice_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = str(text or "").strip().lower()
    if low == _SERIES_BUTTON_SKIP.lower():
        attach_series_to_session(session, None)
        session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
        await update.message.reply_text("Ок, это видео будет без серии.")
        return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)
    if low == _SERIES_BUTTON_NEW.lower():
        session["state"] = STATE_WAITING_SERIES_CREATE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Пришлите серию в формате:\n"
            "название | дней\n\n"
            "Например: летние напитки | 10\n"
            "Если нужен отдельный английский заголовок: летние напитки | summer drinks | 10",
            reply_markup=ok_only_markup(),
        )
        return True
    choice = _parse_concept_choice(text)
    ids = session.get(_SERIES_CHOICE_IDS_SESSION_KEY) or []
    if choice is not None and 1 <= choice <= len(ids):
        payload = select_series_payload(str(ids[choice - 1]))
        if payload is None:
            await update.message.reply_text(
                "Не нашла активную серию или она уже закончилась. Выберите заново или создайте новую."
            )
            await _ask_series_choice(update, session)
            SESSIONS[chat_id] = session
            return True
        attach_series_to_session(session, payload)
        session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
        await update.message.reply_text(f"Серия выбрана: {format_series_label(payload)}.")
        return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер серии, «Создать серию» или «Без серии».",
        reply_markup=_series_choice_markup(session),
    )
    return True


async def _handle_waiting_series_create_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if str(text or "").strip().lower() == _SERIES_BUTTON_BACK.lower():
        await _ask_series_choice(update, session)
        SESSIONS[chat_id] = session
        return True
    parsed = _parse_series_create_text(text)
    if parsed is None:
        await _reply_text_with_optional_markup(
            update,
            "Не смогла разобрать серию. Формат: название | дней",
            reply_markup=ok_only_markup(),
        )
        return True
    ru_title, en_title, total_videos = parsed
    if en_title is None:
        languages = _series_title_target_languages(session)
        try:
            await reply_text_without_keyboard(update.message, "Перевожу название серии, ждите.")
            title_by_lang = await _translate_series_titles(
                update,
                context,
                chat_id=chat_id,
                ru_title=ru_title,
                languages=languages,
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("content series title translation failed")
            await update.message.reply_text(
                f"Ошибка перевода названия серии: {_user_error_message(e)}"
            )
            return True
        session["state"] = STATE_WAITING_SERIES_TRANSLATION_APPROVAL
        session[_SERIES_TRANSLATION_PENDING_SESSION_KEY] = {
            "title_by_lang": title_by_lang,
            "languages": languages,
            "total_videos": total_videos,
        }
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            _format_series_titles_for_approval(title_by_lang, languages),
            reply_markup=ok_only_markup(),
        )
        return True
    try:
        payload = _create_series_from_title_map(
            session,
            {"ru": ru_title, "en": en_title},
            total_videos,
        )
    except Exception as e:
        logger.exception("content series creation failed")
        await update.message.reply_text(f"Ошибка создания серии: {_user_error_message(e)}")
        return True
    attach_series_to_session(session, payload)
    session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
    await update.message.reply_text(f"Серия создана: {format_series_label(payload)}.")
    return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)


async def _handle_waiting_series_translation_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    pending = session.get(_SERIES_TRANSLATION_PENDING_SESSION_KEY)
    if not isinstance(pending, dict):
        session["state"] = STATE_WAITING_SERIES_CREATE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Не вижу сохранённого перевода. "
            "Пришлите серию ещё раз в формате: название | дней",
            reply_markup=ok_only_markup(),
        )
        return True
    title_by_lang = pending.get("title_by_lang")
    if not isinstance(title_by_lang, dict) or not str(title_by_lang.get("ru") or "").strip():
        session["state"] = STATE_WAITING_SERIES_CREATE
        session.pop(_SERIES_TRANSLATION_PENDING_SESSION_KEY, None)
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Не вижу русского названия серии. "
            "Пришлите серию ещё раз в формате: название | дней",
            reply_markup=ok_only_markup(),
        )
        return True
    languages = [
        normalize_language_code(str(item or ""))
        for item in (pending.get("languages") if isinstance(pending.get("languages"), list) else [])
    ]
    languages = [lang for lang in languages if lang] or _series_title_target_languages(session)
    low = str(text or "").strip().lower()
    if low in {"исправь", "fix"}:
        await _reply_text_with_optional_markup(
            update,
            "Пришлите новый EN-текст или строки вида `en: Summer drinks | es: Bebidas de verano`.",
            reply_markup=ok_only_markup(),
        )
        return True
    if low not in {"ок", "ok", "okay", "approve"}:
        corrections = _parse_series_title_corrections(text, languages)
        if not corrections:
            await _reply_text_with_optional_markup(
                update,
                "Нажмите «ОК» или пришлите правку: новый EN-текст либо `en: ... | es: ...`.",
                reply_markup=ok_only_markup(),
            )
            return True
        title_by_lang = {**title_by_lang, **corrections}
    total_videos = int(pending.get("total_videos") or 10)
    try:
        payload = _create_series_from_title_map(session, title_by_lang, total_videos)
    except Exception as e:
        logger.exception("content series creation after translation failed")
        await update.message.reply_text(f"Ошибка создания серии: {_user_error_message(e)}")
        return True
    session.pop(_SERIES_TRANSLATION_PENDING_SESSION_KEY, None)
    attach_series_to_session(session, payload)
    session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
    await update.message.reply_text(f"Серия создана: {format_series_label(payload)}.")
    return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)


async def _handle_waiting_concept_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """Обрабатывает состояние `STATE_WAITING_CONCEPT`."""
    choice = _parse_concept_choice(text)
    if choice is not None:
        idx = choice - 1
        concepts = session.get("concepts") or []
        if idx < 0 or idx >= len(concepts):
            await update.message.reply_text("Неверный номер концепта. Выбери из списка.")
            return True
        selected = concepts[idx]
        concept_text = f"{selected.get('name', '')}. {selected.get('hook', '')}"
        if normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_ADVICE:
            concept_text += "\nРешение: " + str(selected.get("why") or "") + "\n\n" + ADVICE_SCENE_RULE + "\nИсходный запрос/рецепт:\n" + str(session.get("user_input") or "")
        session["selected_concept"] = concept_text
        session["selected_concept_name"] = str(selected.get("name", "")).strip() or concept_text
        try:
            await reply_text_without_keyboard(update.message, "Генерирую полный рецепт, ждите.")
            recipe_full = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_recipe_full_text",
                lambda: generate_recipe_full_text(
                    concept_text,
                    dish_name=session.get("selected_concept_name"),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("recipe full text generation failed")
            await update.message.reply_text(f"Ошибка генерации рецепта: {_user_error_message(e)}")
            return True
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        session["recipe_full_text"] = recipe_full
        session["precheck_recipe_steps"] = None
        session["recipe_steps"] = None
        session["scenes_plan"] = None
        session["scenes_text"] = None
        session["frame_prompts"] = None
        session["frame_prompts_text"] = None
        session["end_frame_scene_requests"] = []
        session["user_feedback"] = None
        await _reply_text_chunked(update, format_full_recipe_for_approval(recipe_full))
        await _reply_text_with_optional_markup(
            update,
            "Если рецепт подходит — нажми «ОК». "
            "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли текст правки.",
            reply_markup=ok_only_markup(),
        )
        return True

    session["state"] = STATE_REFINING_CONCEPT
    session["user_feedback"] = text
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю концепты, ждите.")
        result = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_recipe_concepts",
            lambda: generate_recipe_concepts(
                advice_concept_input(session["user_input"], normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY))),
                user_feedback=text,
                previous_concepts=session.get("concepts"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        session["state"] = STATE_WAITING_CONCEPT
        return True
    except Exception as e:
        logger.exception("concept refinement failed")
        await update.message.reply_text(f"Ошибка доработки концептов: {_user_error_message(e)}")
        session["state"] = STATE_WAITING_CONCEPT
        return True
    session["concepts"] = result["concepts"]
    session["state"] = STATE_WAITING_CONCEPT
    await update.message.reply_text("Обновила концепты по фидбеку:")
    await _reply_text_chunked(update, format_advice_choices(result["concepts"]) if normalize_generation_mode(SESSIONS[chat_id].get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_ADVICE else result["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер варианта кнопкой. "
        "Либо нажмите «Добавь»/«Исправь» и отправьте правку для нейросети.",
        reply_markup=_concept_choice_markup(result["concepts"]),
    )
    return True


async def _continue_silence_after_recipe_approval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    full_recipe: str,
    on_scene_plan_fail,
) -> bool:
    try:
        await reply_text_without_keyboard(
            update.message,
            "Silence: готовлю разбиение на 5–8 Kling-сцен без речи.",
        )
        scenes = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_silence_scenes_plan",
            lambda: generate_scenes_plan(
                session["selected_concept"],
                full_recipe_text=full_recipe,
                on_attempt_failure=on_scene_plan_fail,
                generation_mode=GENERATION_MODE_SILENCE,
                generation_comment=direction_for_session(session),
            ),
        )
        dish_visual_brief = build_dish_visual_brief(
            str(session.get("selected_concept_name") or session.get("selected_concept") or ""),
            full_recipe,
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("silence scene generation failed")
        await update.message.reply_text(f"Ошибка silence-сцен: {_user_error_message(e)}")
        return True

    session["target_languages"] = ["ru"]
    session["recipe_steps"] = [
        str(s.get("recipe_step") or s.get("action") or "").strip()
        for s in (scenes.get("json_plan") or [])
        if isinstance(s, dict)
    ]
    session["state"] = STATE_WAITING_SCENE_APPROVAL
    session["viral_concept_brief"] = {}
    session["romantic_drama_subplot"] = {}
    session["retention_review"] = {}
    session["scenes_plan"] = scenes["json_plan"]
    session["scenes_text"] = scenes["text_plan"]
    session["frame_prompts"] = None
    session["frame_prompts_text"] = None
    session["dish_visual_brief"] = dish_visual_brief
    SESSIONS[chat_id] = session
    await _reply_text_chunked(update, scenes["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Это сценарий по сценам, который уйдёт в production. "
        "Если подходит — нажми «ОК». Если нужно поменять действия в сценах — пришли правку текстом.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _continue_after_recipe_approval_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    full_recipe: str | None = None,
    on_scene_plan_fail=None,
) -> bool:
    full_recipe = str(full_recipe or session.get("recipe_full_text") or "").strip()
    if not full_recipe:
        await update.message.reply_text("Нет текста рецепта. Сначала согласуйте рецепт.")
        return True

    if on_scene_plan_fail is None:
        async def on_scene_plan_fail(p: dict[str, Any]) -> None:
            if update.message is not None:
                await _reply_text_chunked(update, _format_scenes_plan_attempt_notice(p))

    if normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_SILENCE:
        return await _continue_silence_after_recipe_approval(
            update,
            context,
            chat_id=chat_id,
            session=session,
            full_recipe=full_recipe,
            on_scene_plan_fail=on_scene_plan_fail,
        )

    try:
        await reply_text_without_keyboard(update.message, "Готовлю viral brief и план сцен, ждите.")
        viral_brief = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_viral_concept_brief",
            lambda: generate_viral_concept_brief(
                session["selected_concept"],
                full_recipe_text=full_recipe,
                sponsored_content=bool(session.get("sponsored_content")),
                brand_context=session.get("brand_context"),
                analytics_feedback=session.get("analytics_feedback"),
            ),
        )
        scenes = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_scenes_plan",
            lambda: generate_scenes_plan(
                session["selected_concept"],
                full_recipe_text=full_recipe,
                on_attempt_failure=on_scene_plan_fail,
                viral_concept_brief=viral_brief,
                sponsored_content=bool(session.get("sponsored_content")),
                brand_context=session.get("brand_context"),
                analytics_feedback=session.get("analytics_feedback"),
                generation_mode=session.get(GENERATION_MODE_SESSION_KEY, GENERATION_MODE_NORMAL),
                generation_comment=direction_for_session(session),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("scene plan failed")
        await update.message.reply_text(f"Ошибка генерации сцен: {_user_error_message(e)}")
        return True
    session["recipe_steps"] = [
        str(s.get("text") or "").strip()
        for s in (scenes.get("json_plan") or [])
        if isinstance(s, dict)
    ]
    session["state"] = STATE_WAITING_SCENE_APPROVAL
    session["viral_concept_brief"] = scenes.get("viral_concept_brief") or viral_brief
    session["romantic_drama_subplot"] = scenes.get("romantic_drama_subplot") or {}
    session["retention_review"] = scenes.get("retention_review") or {}
    session["scenes_plan"] = scenes["json_plan"]
    session["scenes_text"] = scenes["text_plan"]
    session["frame_prompts"] = None
    session["frame_prompts_text"] = None
    report = _format_scenes_regeneration_report(scenes.get("regeneration_events") or [])
    if report:
        await _reply_text_chunked(update, report)
    await _reply_text_chunked(
        update,
        format_viral_application_summary(
            session["viral_concept_brief"],
            session["scenes_plan"],
            session.get("retention_review"),
        ),
    )
    await _reply_text_chunked(update, scenes["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Если план подходит — нажми «ОК». "
        "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли текст правки.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _handle_waiting_recipe_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """Обрабатывает состояние `STATE_WAITING_RECIPE_APPROVAL`."""
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        async def _on_scene_plan_fail(p: dict[str, Any]) -> None:
            if update.message is not None:
                await _reply_text_chunked(update, _format_scenes_plan_attempt_notice(p))

        full_recipe = str(session.get("recipe_full_text") or "").strip()
        if not full_recipe:
            await update.message.reply_text("Нет текста рецепта. Сначала согласуйте рецепт.")
            return True
        if normalize_generation_mode(session.get(GENERATION_MODE_SESSION_KEY)) == GENERATION_MODE_SILENCE:
            return await _continue_silence_after_recipe_approval(
                update,
                context,
                chat_id=chat_id,
                session=session,
                full_recipe=full_recipe,
                on_scene_plan_fail=_on_scene_plan_fail,
            )
        if not _has_overlay_titles(session):
            return await _ask_overlay_title_before_recipe_plan(update, chat_id, session)
        if not session.get(OVERLAY_SUBTITLE_DECIDED_SESSION_KEY):
            return await _ask_overlay_subtitle_before_recipe_plan(update, chat_id, session)
        return await _continue_after_recipe_approval_plan(
            update,
            context,
            chat_id=chat_id,
            session=session,
            full_recipe=full_recipe,
            on_scene_plan_fail=_on_scene_plan_fail,
        )

    session["state"] = STATE_REFINING_RECIPE
    session["user_feedback"] = text
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю рецепт, ждите.")
        recipe_full = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_recipe_full_text",
            lambda: generate_recipe_full_text(
                str(session.get("selected_concept") or "").strip(),
                dish_name=session.get("selected_concept_name"),
                user_feedback=text,
                previous_text=session.get("recipe_full_text"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        return True
    except Exception as e:
        logger.exception("recipe regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации рецепта: {_user_error_message(e)}")
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        return True
    session["state"] = STATE_WAITING_RECIPE_APPROVAL
    session["recipe_full_text"] = recipe_full
    session["precheck_recipe_steps"] = None
    session["recipe_steps"] = None
    await update.message.reply_text("Обновила рецепт по правкам:")
    await _reply_text_chunked(update, format_full_recipe_for_approval(recipe_full))
    await _reply_text_with_optional_markup(
        update,
        "Если рецепт подходит — нажми «ОК». "
        "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли правку текстом.",
        reply_markup=ok_only_markup(),
    )
    return True
