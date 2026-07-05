import os
import re
from pathlib import Path

from .handlers_state_deps import (
    Any,
    ContextTypes,
    DEBOUNCE_DELAY_SEC,
    SESSIONS,
    STATE_READY_FOR_PRODUCTION,
    STATE_REFINING_CONCEPT,
    STATE_REFINING_FRAME_PROMPTS,
    STATE_REFINING_PRODUCTION_BUNDLE,
    STATE_REFINING_RECIPE,
    STATE_REFINING_SCENES,
    STATE_WAITING_CONCEPT,
    STATE_WAITING_FRAME_PROMPTS_APPROVAL,
    STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    STATE_WAITING_SCENE_MEDIA,
    Update,
    _DEBOUNCE_SEQ,
    _call_with_503_retries,
    _generate_image_with_lenient_fallback,
    _generate_video_bytes_with_high_load_retry,
    _format_scenes_plan_attempt_notice,
    _format_scenes_regeneration_report,
    _append_reference_style_to_image_prompt,
    _append_scene_identity_to_image_prompt,
    _parse_video_command_args,
    _send_next_scene_media_step,
    _user_error_message,
    apply_veo3_lowercase_format,
    asyncio,
    build_human_readable_plan,
    format_frame_prompts_for_approval,
    format_full_recipe_for_approval,
    format_production_bundle_for_approval,
    format_viral_application_summary,
    format_viral_concept_brief,
    generate_frame_prompts_for_approval,
    generate_full_production,
    generate_scenes_plan,
    get_last_video_result_source,
    get_last_video_result_uri,
    io,
    logger,
    ServiceUnavailableRetriesExhaustedError,
    SCENE_MEDIA_SUBSTEP_END,
    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_START,
    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
    SCENE_MEDIA_STOP_BUTTON,
    SCENE_MEDIA_SUBSTEP_VIDEO,
    _scene_needs_end_frame_step,
    _load_scene_reference_images,
    _normalize_character_key,
    ok_only_markup,
    VideoProviderChoiceRequired,
    encode_video_provider_choice_for_session,
)
from .handlers_state_commands import (
    _extract_video_source_image,
    _handle_video_provider_choice_pending_video_command,
    _handle_syntax_fallback_pending_video_command,
    _reply_text_chunked,
)
from .handlers_state_commands import (
    FIRST_STEP_BUTTON_DESCRIPTION,
    FIRST_STEP_BUTTON_GENERATE,
    FIRST_STEP_BUTTON_IMAGE,
    FIRST_STEP_BUTTON_PROJECT_IMAGE,
    FIRST_STEP_BUTTON_PROJECT,
    FIRST_STEP_BUTTON_SOUND,
    FIRST_STEP_BUTTON_VIDEO,
    NAV_BUTTON_PREVIOUS_STEP,
    NAV_BUTTON_START_OVER,
    PROJECT_BUTTON_BACK,
    PROJECT_KEY_GRANDMA_CAT_COOKING,
    PROJECT_KEY_SESSION,
    PROJECT_LEGACY_LABEL_GRANDMA_CAT_COOKING,
    PROJECT_LABEL_GRANDMA_CAT_COOKING,
    first_step_menu_markup,
    project_choice_markup,
)
from .handlers_keyboards import (
    DRAFT_BUTTON_LIST,
    DRAFT_BUTTON_SAVE,
    remove_keyboard_markup,
    reply_text_without_keyboard,
    safe_reply_photo,
    safe_reply_text,
)
from ..assets import ASSETS
from ..drafts import (
    DRAFTS_PAGE_SIZE,
    clear_scene_db_approvals_for_repair,
    clear_scene_for_repair,
    format_drafts_page,
    format_drafts_list,
    hide_generation_draft,
    list_generation_drafts,
    load_generation_draft,
    parse_draft_open_command,
    save_generation_draft,
)
from ..elevenlabs_tts import generate_elevenlabs_speech
from ..generation_store import set_generation_run_title_color_for_session
from ..llm_utils import gemini_generate_sync
from ..scene_media_candidates import resolve_candidate_by_message
from .handlers_state_start import (
    SERIES_CHOICE_CONFIRMED_SESSION_KEY,
    SERIES_NEXT_OVERLAY_TITLE,
    SERIES_PENDING_NEXT_SESSION_KEY,
    STATE_WAITING_SERIES_CHOICE,
    STATE_WAITING_SERIES_CREATE,
    STATE_WAITING_SERIES_TRANSLATION_APPROVAL,
    _ask_series_choice,
    _concept_choice_markup,
    _handle_waiting_series_choice_state,
    _handle_waiting_series_create_state,
    _handle_waiting_series_translation_approval_state,
    _handle_waiting_concept_state,
    _handle_waiting_recipe_approval_state,
    _series_choice_markup,
    _start_description_with_recipe,
    _start_generation_with_recipe,
)
from .handlers_generation_utils import (
    OVERLAY_TITLE_BY_LANG_SESSION_KEY,
    OVERLAY_TITLE_STYLE_SESSION_KEY,
    OVERLAY_TITLE_SESSION_KEY,
    OVERLAY_TITLE_TRANSLATION_PENDING_SESSION_KEY,
    STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL,
    STATE_WAITING_OVERLAY_TITLE_RU,
    STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL,
)
from ..state import get_loaded_session, save_loaded_session_if_possible
from .handlers_state_media import (
    STATE_REFINING_SOCIAL_DESCRIPTIONS,
    STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL,
    handle_scene_media_reference_photo,
    _handle_waiting_scene_media_state,
    _handle_waiting_social_descriptions_approval_state,
    scene_media_reference_images_pending,
)
from ..character_catalog import format_character_short_descriptions
from ..pipeline_history import record_checkpoint, restore_previous_checkpoint, session_step_signature
from ..scene_media_store import mark_candidate_liked_by_message as persist_mark_candidate_liked_by_message
from ..session_store import load_session_if_possible, save_session_if_possible
from ..series_store import (
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SERIES_TOTAL_VIDEOS_SESSION_KEY,
    SERIES_VIDEO_INDEX_SESSION_KEY,
)


async def _reply_text_with_optional_markup(update: Update, text: str, reply_markup=None) -> None:
    """Отправляет текст с клавиатурой и не падает на тестовых стабах без kwargs."""
    if update.message is None:
        return
    if reply_markup is None:
        await safe_reply_text(update.message, text)
        return
    await safe_reply_text(update.message, text, reply_markup=reply_markup)


_STEP_GUIDED_FEEDBACK_KEY = "guided_step_feedback"
_STEP_ADD_BUTTONS = {"добавь", "add"}
_STEP_FIX_BUTTONS = {"исправь", "fix"}
_STEP_OK_BUTTONS = {"ок", "ok", "okay", "approve"}
_FRAME_PROMPT_REFERENCE_ADD_BUTTON = "Добавить"
_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY = "frame_prompt_reference_images"
_STATE_WAITING_FRAME_PROMPT_REFERENCES = "state_waiting_frame_prompt_references"
_STATE_WAITING_FIRST_STEP_GENERATE_TEXT = "state_waiting_first_step_generate_text"
_STATE_WAITING_FIRST_STEP_DESCRIPTION_TEXT = "state_waiting_first_step_description_text"
_STATE_WAITING_FIRST_STEP_IMAGE_TEXT = "state_waiting_first_step_image_text"
_STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES = "state_waiting_first_step_image_references"
_STATE_WAITING_FIRST_STEP_SOUND_TEXT = "state_waiting_first_step_sound_text"
_STATE_WAITING_FIRST_STEP_VIDEO_TEXT = "state_waiting_first_step_video_text"
_STATE_WAITING_FIRST_STEP_VIDEO_IMAGE = "state_waiting_first_step_video_image"
_STATE_WAITING_FIRST_STEP_PROJECT_SELECTION = "state_waiting_first_step_project_selection"
_STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_CHARACTERS = "state_waiting_first_step_project_image_characters"
_STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_PROMPT = "state_waiting_first_step_project_image_prompt"
_STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES = "state_waiting_first_step_project_image_references"
_STATE_WAITING_DRAFT_SCENES = "state_waiting_draft_scenes"
_STATE_WAITING_DRAFT_LANGUAGE = "state_waiting_draft_language"
_STATE_WAITING_DRAFT_STAGE = "state_waiting_draft_stage"
_DRAFTS_PAGE_SESSION_KEY = "telegram_drafts_page"
_DRAFTS_PAGE_IDS_SESSION_KEY = "telegram_drafts_page_ids"
_DRAFT_REPAIR_ACTIVE_PLAN_KEY = "draft_repair_active_plan"
_DRAFT_REPAIR_ADD_SCENE_BUTTON = "Добавить сцену"
_DRAFT_REPAIR_START_BUTTON = "Начать правку"
_DRAFT_REPAIR_NEXT_BUTTON = "Далее"
_PROJECT_IMAGE_DONE_BUTTON = "больше никто не нужен"
_PROJECT_IMAGE_CHARACTERS_SESSION_KEY = "first_step_project_image_characters"
_PROJECT_IMAGE_BASE_CHARACTERS = ("grandma", "cat")
_CUSTOM_REFERENCES_SESSION_KEY = "first_step_custom_reference_images"
_CUSTOM_REFERENCE_ADD_BUTTON = "Добавить свое"
_CUSTOM_REFERENCE_DONE_BUTTON = "больше файлов не нужно"
_PROJECT_MAIN_CHARACTER_BY_PROJECT = {
    PROJECT_KEY_GRANDMA_CAT_COOKING: "grandma",
}
_DEFAULT_OVERLAY_TITLE_SIDE_MARGIN_RATIO = 0.10
_DEFAULT_OVERLAY_TITLE_TOP_MARGIN_RATIO = 0.05
_TITLE_COLOR_BY_WORD = (
    ("клубнич", "#ff4f7b"),
    ("strawberry", "#ff4f7b"),
    ("лимонад", "#ffd84a"),
    ("lemonade", "#ffd84a"),
    ("борщ", "#c72638"),
    ("borscht", "#c72638"),
    ("щи", "#7dbb5d"),
    ("сырник", "#f2b84b"),
    ("морожен", "#ff9fc2"),
)
_OVERLAY_PREVIEW_WIDTH = 1080
_OVERLAY_PREVIEW_HEIGHT = 1920
_OVERLAY_PREVIEW_SAFE_TOP_HEIGHT = int(_OVERLAY_PREVIEW_HEIGHT * 0.30)
_OVERLAY_PREVIEW_FONTS_DIR = Path(__file__).resolve().parents[2] / "fonts"
_OVERLAY_PREVIEW_FONT_BY_LANGUAGE = {
    "ru": _OVERLAY_PREVIEW_FONTS_DIR / "TD Afisha.otf",
    "en": _OVERLAY_PREVIEW_FONTS_DIR / "Taller.otf",
}
_OVERLAY_PREVIEW_SERIES_FONT_BY_LANGUAGE = {
    "ru": _OVERLAY_PREVIEW_FONTS_DIR / "Berezka.ttf",
    "en": _OVERLAY_PREVIEW_FONTS_DIR / "Ravsten Demo.otf",
}
_OVERLAY_PREVIEW_SERIES_DAY_FONT = _OVERLAY_PREVIEW_FONTS_DIR / "Marianna.ttf"
_OVERLAY_PREVIEW_TITLE_STROKE_WIDTH = 2
_OVERLAY_PREVIEW_TITLE_TRACKING = 0
_OVERLAY_PREVIEW_TITLE_SHADOW_OFFSET = 7
_OVERLAY_PREVIEW_TITLE_SPACE_WIDTH_SCALE = 0.24
_OVERLAY_PREVIEW_SERIES_STROKE_WIDTH = 1
_OVERLAY_PREVIEW_SERIES_SHADOW_OFFSET = 4


def _apply_overlay_title_to_production(session: dict, production: dict) -> None:
    """Attach user-provided title metadata to the production payload."""
    if not isinstance(production, dict):
        return
    title = str(session.get(OVERLAY_TITLE_SESSION_KEY) or "").strip()
    title_map = session.get(OVERLAY_TITLE_BY_LANG_SESSION_KEY)
    if title:
        production[OVERLAY_TITLE_SESSION_KEY] = title
    if isinstance(title_map, dict) and title_map:
        cleaned_titles = {
            str(key).strip(): str(value).strip()
            for key, value in title_map.items()
            if str(key).strip() and str(value).strip()
        }
        if cleaned_titles:
            production[OVERLAY_TITLE_BY_LANG_SESSION_KEY] = cleaned_titles
    style = session.get(OVERLAY_TITLE_STYLE_SESSION_KEY)
    if isinstance(style, dict) and style:
        cleaned_style = {str(key).strip(): value for key, value in style.items() if str(key).strip()}
        if cleaned_style:
            production[OVERLAY_TITLE_STYLE_SESSION_KEY] = cleaned_style
    for key in (
        SERIES_SESSION_KEY,
        SERIES_TITLE_BY_LANG_SESSION_KEY,
        SERIES_VIDEO_INDEX_SESSION_KEY,
        SERIES_TOTAL_VIDEOS_SESSION_KEY,
        SERIES_COUNTER_BY_LANG_SESSION_KEY,
    ):
        value = session.get(key)
        if value not in (None, "", {}, []):
            production[key] = value


def _overlay_title_ru_prompt_text() -> str:
    return "Сценарий утверждён. Теперь введите русский заголовок для верхней надписи."


def _clean_translated_title(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "«", "»"}:
        text = text[1:-1].strip()
    prefixes = ("EN:", "English:", "Английский:")
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].strip()
    return text


async def _translate_overlay_title_to_en(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    ru_title: str,
) -> str:
    prompt = (
        "Translate this short social-video title from Russian to natural English.\n"
        "Keep it concise, catchy, and suitable as an on-video title.\n"
        "Return only the English title, no quotes and no comments.\n\n"
        f"Russian title:\n{ru_title.strip()}"
    )
    raw = await _call_with_503_retries(
        update,
        context,
        chat_id,
        "translate_overlay_title_en",
        lambda: asyncio.to_thread(gemini_generate_sync, prompt),
    )
    translated = _clean_translated_title(str(raw or ""))
    return translated or ru_title.strip()


def _store_overlay_titles(session: dict, *, ru_title: str, en_title: str) -> None:
    ru = " ".join(str(ru_title or "").split())
    en = " ".join(str(en_title or "").split())
    session[OVERLAY_TITLE_SESSION_KEY] = ru
    session[OVERLAY_TITLE_BY_LANG_SESSION_KEY] = {"ru": ru, "en": en or ru}
    session.pop(OVERLAY_TITLE_TRANSLATION_PENDING_SESSION_KEY, None)


def _semantic_overlay_title_color(title: str) -> str:
    text = str(title or "").casefold()
    for marker, color in _TITLE_COLOR_BY_WORD:
        if marker in text:
            return color
    return "#fff7e8"


def _default_overlay_title_style(session: dict) -> dict[str, object]:
    title = str(session.get(OVERLAY_TITLE_SESSION_KEY) or "").strip()
    existing = session.get(OVERLAY_TITLE_STYLE_SESSION_KEY)
    if isinstance(existing, dict) and existing:
        return dict(existing)
    return {
        "title_color": _semantic_overlay_title_color(title),
        "top_margin_ratio": _DEFAULT_OVERLAY_TITLE_TOP_MARGIN_RATIO,
        "side_margin_ratio": _DEFAULT_OVERLAY_TITLE_SIDE_MARGIN_RATIO,
    }


def _format_overlay_title_style(style: dict[str, object]) -> str:
    color = str(style.get("title_color") or "#fff7e8")
    top = float(style.get("top_margin_ratio") or _DEFAULT_OVERLAY_TITLE_TOP_MARGIN_RATIO) * 100
    side = float(style.get("side_margin_ratio") or _DEFAULT_OVERLAY_TITLE_SIDE_MARGIN_RATIO) * 100
    return (
        f"Цвет заголовка: {color}\n"
        f"Отступ сверху: {top:.0f}%\n"
        f"Боковые поля: {side:.0f}%"
    )


def _normalize_overlay_ratio(value: str, *, default: float) -> float:
    raw_text = str(value or "")
    try:
        raw = float(raw_text.replace(",", ".").replace("%", "").strip())
    except ValueError:
        return default
    ratio = raw / 100.0 if raw > 1 or "%" in raw_text else raw
    return min(max(ratio, 0.04), 0.22)


def _apply_overlay_title_style_feedback(style: dict[str, object], text: str) -> dict[str, object]:
    updated = dict(style)
    raw = str(text or "").strip()
    color_match = re.search(r"#?([0-9a-fA-F]{6})", raw)
    if color_match:
        updated["title_color"] = f"#{color_match.group(1).lower()}"
    top_match = re.search(r"(?:сверху|верх\w*|top)\D{0,16}(\d+(?:[.,]\d+)?\s*%?)", raw, flags=re.IGNORECASE)
    if top_match:
        updated["top_margin_ratio"] = _normalize_overlay_ratio(
            top_match.group(1),
            default=float(updated.get("top_margin_ratio") or _DEFAULT_OVERLAY_TITLE_TOP_MARGIN_RATIO),
        )
    side_match = re.search(
        r"(?:бок\w*|лев\w*|прав\w*|side|margin)\D{0,16}(\d+(?:[.,]\d+)?\s*%?)",
        raw,
        flags=re.IGNORECASE,
    )
    if side_match:
        updated["side_margin_ratio"] = _normalize_overlay_ratio(
            side_match.group(1),
            default=float(updated.get("side_margin_ratio") or _DEFAULT_OVERLAY_TITLE_SIDE_MARGIN_RATIO),
        )
    return updated


def _overlay_preview_ratio(style: dict[str, object], key: str, default: float) -> float:
    try:
        value = float(style.get(key) or default)
    except (TypeError, ValueError):
        return default
    if value > 1:
        value = value / 100.0
    return min(max(value, 0.04), 0.22)


def _overlay_preview_text_bbox(draw, text: str, font):
    left, top, _right, bottom = draw.textbbox(
        (0, 0),
        text,
        font=font,
        stroke_width=_OVERLAY_PREVIEW_TITLE_STROKE_WIDTH,
    )
    width = _overlay_preview_tracked_line_width(draw, text, font)
    return (left, top, left + width, bottom)


def _overlay_preview_tracked_line_width(draw, text: str, font) -> int:
    chars = list(str(text or ""))
    if not chars:
        return 0
    width = 0.0
    for ch in chars:
        char_width = float(draw.textlength(ch, font=font))
        width += char_width * _OVERLAY_PREVIEW_TITLE_SPACE_WIDTH_SCALE if ch == " " else char_width
    width += _OVERLAY_PREVIEW_TITLE_TRACKING * max(0, len(chars) - 1)
    return int(round(width)) + _OVERLAY_PREVIEW_TITLE_STROKE_WIDTH * 2


def _overlay_preview_tracked_multiline_bbox(draw, lines: list[str], font, spacing: int):
    normal = draw.multiline_textbbox(
        (0, 0),
        "\n".join(lines),
        font=font,
        spacing=spacing,
        stroke_width=_OVERLAY_PREVIEW_TITLE_STROKE_WIDTH,
    )
    width = max((_overlay_preview_tracked_line_width(draw, line, font) for line in lines), default=0)
    return (normal[0], normal[1], normal[0] + width, normal[3])


def _overlay_preview_draw_tracked_multiline(
    draw,
    xy: tuple[int, int],
    lines: list[str],
    font,
    *,
    fill: str,
    spacing: int,
    box_width: int,
) -> None:
    x, y = xy
    line_y = y
    for line in lines:
        line_x = x + (box_width - _overlay_preview_tracked_line_width(draw, line, font)) // 2
        shadow_x = line_x + _OVERLAY_PREVIEW_TITLE_SHADOW_OFFSET
        for ch in line:
            advance = int(round(float(draw.textlength(ch, font=font))))
            if ch == " ":
                shadow_x += int(round(advance * _OVERLAY_PREVIEW_TITLE_SPACE_WIDTH_SCALE))
                continue
            draw.text(
                (shadow_x, line_y + _OVERLAY_PREVIEW_TITLE_SHADOW_OFFSET),
                ch,
                font=font,
                fill="#000000",
                stroke_width=0,
            )
            shadow_x += advance + _OVERLAY_PREVIEW_TITLE_TRACKING
        for ch in line:
            advance = int(round(float(draw.textlength(ch, font=font))))
            if ch == " ":
                line_x += int(round(advance * _OVERLAY_PREVIEW_TITLE_SPACE_WIDTH_SCALE))
                continue
            draw.text(
                (line_x, line_y),
                ch,
                font=font,
                fill=fill,
                stroke_width=_OVERLAY_PREVIEW_TITLE_STROKE_WIDTH,
                stroke_fill="#000000",
            )
            line_x += advance + _OVERLAY_PREVIEW_TITLE_TRACKING
        line_bbox = draw.textbbox(
            (0, 0),
            line or " ",
            font=font,
            stroke_width=_OVERLAY_PREVIEW_TITLE_STROKE_WIDTH,
        )
        line_y += (line_bbox[3] - line_bbox[1]) + spacing


def _overlay_preview_wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = [word for word in str(text or "").split() if word]
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        left, _top, right, _bottom = _overlay_preview_text_bbox(draw, candidate, font)
        if right - left <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def _overlay_preview_fit_title(draw, title: str, font_path: Path, *, max_width: int, max_height: int):
    from PIL import ImageFont

    clean = " ".join(str(title or "").split())
    start_size = 260 if len(clean) <= 8 else 170
    for size in range(start_size, 45, -2):
        font = ImageFont.truetype(str(font_path), size=size)
        spacing = max(8, int(size * 0.10))
        lines = _overlay_preview_wrap_text(draw, clean, font, max_width)
        if len(lines) > 2:
            continue
        left, top, right, bottom = _overlay_preview_tracked_multiline_bbox(draw, lines, font, spacing)
        if right - left <= max_width and bottom - top <= max_height:
            return font, lines, spacing, (left, top, right, bottom)
    font = ImageFont.truetype(str(font_path), size=46)
    lines = _overlay_preview_wrap_text(draw, clean, font, max_width)[:2]
    spacing = 8
    bbox = _overlay_preview_tracked_multiline_bbox(draw, lines, font, spacing)
    return font, lines, spacing, bbox


def _overlay_preview_value_for_language(value_by_lang: object, language: str) -> str:
    if not isinstance(value_by_lang, dict):
        return ""
    lang = str(language or "").strip().lower()
    direct = str(value_by_lang.get(lang) or "").strip()
    if direct:
        return direct
    return str(value_by_lang.get("ru") or value_by_lang.get("en") or "").strip()


def _overlay_preview_text_box(draw, text: str, font, *, stroke_width: int) -> tuple[int, int, int, int]:
    if not text:
        return (0, 0, 0, 0)
    return draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)


def _overlay_preview_series_line_metrics(draw, parts: list[tuple[str, object]], *, gap: int) -> tuple[int, int, int]:
    boxes = [
        _overlay_preview_text_box(draw, text, font, stroke_width=_OVERLAY_PREVIEW_SERIES_STROKE_WIDTH)
        for text, font in parts
        if text
    ]
    widths = [right - left for left, _top, right, _bottom in boxes]
    width = sum(widths) + gap * max(0, len(widths) - 1)
    top = min((box[1] for box in boxes), default=0)
    bottom = max((box[3] for box in boxes), default=0)
    return width, top, bottom


def _overlay_preview_fit_series_line(
    draw,
    language: str,
    series_title: str,
    series_counter: str,
    *,
    max_width: int,
):
    from PIL import ImageFont

    clean_title = " ".join(str(series_title or "").split())
    clean_counter = " ".join(str(series_counter or "").split())
    if not clean_title and not clean_counter:
        return None
    title_font_path = _OVERLAY_PREVIEW_SERIES_FONT_BY_LANGUAGE.get(
        language,
        _OVERLAY_PREVIEW_SERIES_FONT_BY_LANGUAGE["ru"],
    )
    for title_size in range(78, 31, -2):
        counter_size = max(36, int(round(title_size * 1.44)))
        title_font = ImageFont.truetype(str(title_font_path), size=title_size)
        counter_font = ImageFont.truetype(str(_OVERLAY_PREVIEW_SERIES_DAY_FONT), size=counter_size)
        parts = [(clean_title, title_font), (clean_counter, counter_font)]
        gap = int(round(counter_size * 0.18)) if clean_title and clean_counter else 0
        width, top, bottom = _overlay_preview_series_line_metrics(draw, parts, gap=gap)
        if width <= max_width:
            return parts, gap, (width, top, bottom)
    title_font = ImageFont.truetype(str(title_font_path), size=32)
    counter_font = ImageFont.truetype(str(_OVERLAY_PREVIEW_SERIES_DAY_FONT), size=46)
    parts = [(clean_title, title_font), (clean_counter, counter_font)]
    gap = 8 if clean_title and clean_counter else 0
    width, top, bottom = _overlay_preview_series_line_metrics(draw, parts, gap=gap)
    return parts, gap, (width, top, bottom)


def _overlay_preview_draw_series_line(draw, x: int, y: int, parts: list[tuple[str, object]], *, gap: int) -> None:
    cursor_x = x
    for text, font in parts:
        if not text:
            continue
        left, top, right, _bottom = _overlay_preview_text_box(
            draw,
            text,
            font,
            stroke_width=_OVERLAY_PREVIEW_SERIES_STROKE_WIDTH,
        )
        text_x = cursor_x - left
        text_y = y - top
        draw.text(
            (text_x + _OVERLAY_PREVIEW_SERIES_SHADOW_OFFSET, text_y + _OVERLAY_PREVIEW_SERIES_SHADOW_OFFSET),
            text,
            font=font,
            fill="#000000",
            stroke_width=0,
        )
        draw.text(
            (text_x, text_y),
            text,
            font=font,
            fill="#ffffff",
            stroke_width=_OVERLAY_PREVIEW_SERIES_STROKE_WIDTH,
            stroke_fill="#000000",
        )
        cursor_x += (right - left) + gap


def _render_overlay_title_preview(
    language: str,
    title: str,
    style: dict[str, object],
    series_title: str = "",
    series_counter: str = "",
) -> bytes:
    from PIL import Image, ImageDraw

    width = _OVERLAY_PREVIEW_WIDTH
    height = _OVERLAY_PREVIEW_HEIGHT
    safe_top = _OVERLAY_PREVIEW_SAFE_TOP_HEIGHT
    side_ratio = _overlay_preview_ratio(style, "side_margin_ratio", _DEFAULT_OVERLAY_TITLE_SIDE_MARGIN_RATIO)
    top_ratio = _overlay_preview_ratio(style, "top_margin_ratio", _DEFAULT_OVERLAY_TITLE_TOP_MARGIN_RATIO)
    margin_x = int(width * side_ratio)
    max_width = width - margin_x * 2
    top_y = int(height * top_ratio)
    max_height = max(64, safe_top - top_y - 28)
    title_color = str(style.get("title_color") or "#fff7e8")

    image = Image.new("RGB", (width, height), "#e5bf94")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        r = int(246 + (198 - 246) * t)
        g = int(215 + (143 - 215) * t)
        b = int(175 + (94 - 175) * t)
        draw.line((0, y, width, y), fill=(r, g, b))
    draw.rectangle((0, int(height * 0.58), width, height), fill="#b87955")
    draw.ellipse((160, 990, 920, 1660), fill="#8d5745")
    draw.ellipse((230, 1050, 850, 1580), fill="#efcf9d")
    draw.ellipse((395, 760, 685, 1055), fill="#f0c7a8")
    draw.rectangle((355, 1055, 725, 1440), fill="#74aaa9")
    draw.rounded_rectangle((105, 1240, 280, 1530), radius=20, fill="#ff6b7e", outline="#7f2e3f", width=6)
    draw.ellipse((780, 1210, 960, 1530), fill="#ffd85a", outline="#8c6722", width=6)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    fade_height = int(height * 0.42)
    for y in range(fade_height):
        t = y / max(1, fade_height - 1)
        smooth = t * t * (3 - 2 * t)
        alpha = int(255 * 0.24 * (1 - smooth))
        if alpha > 0:
            overlay_draw.line((0, y, width, y), fill=(0, 0, 0, alpha))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    clean_series_title = " ".join(str(series_title or "").split())
    clean_series_counter = " ".join(str(series_counter or "").split())
    series_gap = 0
    series_height = 0
    series_parts: list[tuple[str, object]] = []
    series_metrics = None
    if clean_series_title or clean_series_counter:
        series_metrics = _overlay_preview_fit_series_line(
            draw,
            language,
            clean_series_title,
            clean_series_counter,
            max_width=max_width,
        )
    if series_metrics:
        series_parts, series_gap, (series_width, series_top, series_bottom) = series_metrics
        series_height = series_bottom - series_top
        series_x = margin_x + (max_width - series_width) // 2
        _overlay_preview_draw_series_line(draw, series_x, top_y, series_parts, gap=series_gap)

    title_top_y = top_y + (int(round(series_height * 0.95)) if series_height else 0)
    if series_height:
        title_top_y += 12
    max_height = max(64, safe_top - title_top_y - 28)
    font_path = _OVERLAY_PREVIEW_FONT_BY_LANGUAGE.get(language, _OVERLAY_PREVIEW_FONT_BY_LANGUAGE["ru"])
    font, lines, spacing, bbox = _overlay_preview_fit_title(
        draw,
        title,
        font_path,
        max_width=max_width,
        max_height=max_height,
    )
    left, top, right, bottom = bbox
    text_width = right - left
    x = margin_x + (max_width - text_width) // 2 - left
    y = title_top_y - top
    _overlay_preview_draw_tracked_multiline(
        draw,
        (x, y),
        lines,
        font=font,
        fill=title_color,
        spacing=spacing,
        box_width=text_width,
    )
    bio = io.BytesIO()
    image.save(bio, format="PNG")
    return bio.getvalue()


async def _send_overlay_title_previews(update: Update, session: dict, style: dict[str, object]) -> None:
    message = update.message
    if message is None or not hasattr(message, "reply_photo"):
        return
    titles = session.get(OVERLAY_TITLE_BY_LANG_SESSION_KEY)
    if not isinstance(titles, dict):
        return
    series_titles = session.get(SERIES_TITLE_BY_LANG_SESSION_KEY)
    series_counters = session.get(SERIES_COUNTER_BY_LANG_SESSION_KEY)
    for language, label in (("ru", "RU"), ("en", "EN")):
        title = str(titles.get(language) or titles.get("ru") or "").strip()
        if not title:
            continue
        series_title = _overlay_preview_value_for_language(series_titles, language)
        series_counter = _overlay_preview_value_for_language(series_counters, language)
        try:
            image_bytes = _render_overlay_title_preview(language, title, style, series_title, series_counter)
        except Exception:
            logger.exception("overlay title preview render failed language=%s", language)
            continue
        bio = io.BytesIO(image_bytes)
        bio.name = f"overlay_title_preview_{language}.png"
        await safe_reply_photo(message, photo=bio, caption=f"Превью верхнего заголовка {label}.")


async def _ask_overlay_title_style_approval(update: Update, session: dict) -> bool:
    style = _default_overlay_title_style(session)
    session[OVERLAY_TITLE_STYLE_SESSION_KEY] = style
    await _send_overlay_title_previews(update, session, style)
    await _reply_text_with_optional_markup(
        update,
        "Предлагаю настройки верхнего заголовка:\n"
        f"{_format_overlay_title_style(style)}\n\n"
        "Если подходит — нажмите «ОК». Если нужно поправить, напишите, например: "
        "цвет #ff4f7b, сверху 15%, боковые 10%.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _generate_production_after_overlay_title(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
) -> bool:
    try:
        await reply_text_without_keyboard(update.message, "Генерирую production-промпты, ждите.")
        requested_end_frame_scenes = _ensure_end_frame_scene_requests_from_session_text(session)
        production = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_full_production",
            lambda: generate_full_production(
                session["scenes_plan"],
                approved_frame_prompts=None,
                requested_end_frame_scenes=requested_end_frame_scenes,
                viral_concept_brief=session.get("viral_concept_brief"),
                sponsored_content=bool(session.get("sponsored_content")),
                brand_context=session.get("brand_context"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("production failed after overlay title")
        await update.message.reply_text(f"Ошибка генерации промптов (кадры + видео): {_user_error_message(e)}")
        return True
    plan = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(plan, list) or not plan:
        await update.message.reply_text("Модель не вернула production-план. Попробуйте ещё раз или начните с /generate.")
        return True
    session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
    _apply_overlay_title_to_production(session, production)
    session["production"] = production
    session["frame_prompts"] = _frame_prompts_slice_from_production(plan)
    session["frame_prompts_text"] = format_frame_prompts_for_approval(session["frame_prompts"])
    await update.message.reply_text(
        "Заголовок утверждён. Ниже — все промпты по сценам: START / END кадры и video. "
        "После «ОК» пойдём по сценам: старт → картинка → при необходимости конец → video → ролик."
    )
    await _reply_text_chunked(update, format_production_bundle_for_approval(plan))
    await _reply_text_with_optional_markup(
        update,
        "Нажмите «ОК», чтобы перейти к пошаговой генерации. "
        "Либо нажмите «Добавь»/«Исправь» и пришлите правки. "
        "Конечный кадр добавлю только для явно названных в правке сцен.",
        reply_markup=_production_bundle_approval_markup(),
    )
    return True


def keyboard_for_session_state(session: dict):
    """Return the reply keyboard expected for the current visible state."""
    state = str((session or {}).get("state") or "")
    if state == _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION:
        return project_choice_markup()
    if state == "":
        return first_step_menu_markup()
    if state == STATE_WAITING_OVERLAY_TITLE_RU:
        return remove_keyboard_markup()
    if state == STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL:
        return ok_only_markup()
    if state == STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL:
        return ok_only_markup()
    if state == STATE_WAITING_SERIES_CHOICE:
        return _series_choice_markup(session)
    if state == STATE_WAITING_SERIES_CREATE:
        return ok_only_markup()
    if state == STATE_WAITING_CONCEPT:
        concepts = session.get("concepts")
        return _concept_choice_markup(concepts if isinstance(concepts, list) else [])
    if state in {
        STATE_WAITING_RECIPE_APPROVAL,
        STATE_WAITING_SCENE_APPROVAL,
    }:
        return ok_only_markup()
    if state == STATE_WAITING_FRAME_PROMPTS_APPROVAL:
        return _frame_prompts_approval_markup()
    if state == _STATE_WAITING_FRAME_PROMPT_REFERENCES:
        return _frame_prompt_reference_markup()
    if state == STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL:
        return _production_bundle_approval_markup()
    if state == STATE_READY_FOR_PRODUCTION:
        return first_step_menu_markup()
    return None


def _is_explicit_veo3_speech_format_request(text: str) -> bool:
    """
    Распознает только короткие служебные просьбы нормализовать речь Veo3.

    Содержательные правки сцен часто упоминают паузы ("пауза не нужна между...").
    Такие сообщения должны уходить в LLM вместе с previous_scenes, а не в локальный
    пост-фикс реплик.
    """
    low = (text or "").lower().strip()
    if not low or len(low) > 120:
        return False
    if re.search(r"\bсцен[аеуы]?\s*\d|\bв\s+сцен[еуы]\b", low):
        return False
    if any(phrase in low for phrase in ("не нужна", "не нужны", "между", "добавляем слово", "говорим:")):
        return False
    return any(
        re.search(pattern, low)
        for pattern in (
            r"\b(fix_stress|формат\s+veo3|veo3\s+формат)\b",
            r"\b(убери|удали|сними)\s+(все\s+)?ударени",
            r"\b(исправь|поправь|почини|нормализуй|приведи)\b.*\b(ударени|ударн|пауз[ауы]?|реплики|озвучк|veo3)\b",
        )
    )


def _default_sound_character_for_session(session: dict) -> str:
    project_key = str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING)
    return _PROJECT_MAIN_CHARACTER_BY_PROJECT.get(project_key, "grandma")


async def _generate_first_step_sound(update: Update, text: str, session: dict) -> None:
    # Today every character resolves to the single configured voice id. Later this is
    # where project character -> voice id mapping can be applied.
    character = _default_sound_character_for_session(session)
    await reply_text_without_keyboard(update.message, f"Генерируем звук в ElevenLabs, ждите.\nГолос: {character}.")
    result = await asyncio.to_thread(generate_elevenlabs_speech, text)
    bio = io.BytesIO(result.audio_bytes)
    bio.name = "sound.mp3"
    try:
        await update.message.reply_audio(audio=bio, caption="Звук готов.")
    except AttributeError:
        await update.message.reply_voice(voice=bio, caption="Звук готов.")
    await update.message.reply_text(
        "Выберите первый шаг кнопкой.",
        reply_markup=first_step_menu_markup(),
    )


def _custom_reference_images_from_session(session: dict) -> list[bytes]:
    raw = session.get(_CUSTOM_REFERENCES_SESSION_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, bytes) and item]


def _append_custom_reference_image(session: dict, image_bytes: bytes) -> int:
    refs = _custom_reference_images_from_session(session)
    refs.append(image_bytes)
    session[_CUSTOM_REFERENCES_SESSION_KEY] = refs
    return len(refs)


def _custom_reference_count_text(session: dict) -> str:
    count = len(_custom_reference_images_from_session(session))
    return f"Добавлено своих файлов: {count}."


def _frame_prompt_reference_images_from_session(session: dict) -> list[bytes]:
    raw = session.get(_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, bytes) and item]


def _append_frame_prompt_reference_image(session: dict, image_bytes: bytes) -> int:
    refs = _frame_prompt_reference_images_from_session(session)
    refs.append(image_bytes)
    session[_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY] = refs
    return len(refs)


def _frame_prompt_reference_count_text(session: dict) -> str:
    count = len(_frame_prompt_reference_images_from_session(session))
    return f"Прикреплено изображений к frame prompts: {count}."


def _custom_reference_markup():
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[_CUSTOM_REFERENCE_DONE_BUTTON], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _frame_prompt_reference_markup():
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[_CUSTOM_REFERENCE_DONE_BUTTON], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _frame_prompts_approval_markup():
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [
            ["ОК"],
            [_FRAME_PROMPT_REFERENCE_ADD_BUTTON],
            ["Добавь"],
            ["Исправь"],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def image_text_markup():
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        [[_CUSTOM_REFERENCE_ADD_BUTTON], [NAV_BUTTON_START_OVER]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _project_image_available_character_keys() -> list[str]:
    chars = ASSETS.get("characters") if isinstance(ASSETS, dict) else {}
    keys = [str(k).strip() for k, v in (chars or {}).items() if str(k).strip() and isinstance(v, dict)]
    ordered: list[str] = []
    for key in _PROJECT_IMAGE_BASE_CHARACTERS:
        if key not in ordered:
            ordered.append(key)
    for key in sorted(keys):
        if key not in ordered:
            ordered.append(key)
    return ordered


def _project_image_selected_characters(session: dict) -> list[str]:
    raw = session.get(_PROJECT_IMAGE_CHARACTERS_SESSION_KEY)
    if not isinstance(raw, list):
        raw = []
    selected: list[str] = []
    for fixed in _PROJECT_IMAGE_BASE_CHARACTERS:
        selected.append(fixed)
    for item in raw:
        key = _normalize_character_key(str(item))
        if key and key not in selected:
            selected.append(key)
    return selected


def _project_image_character_keys_from_text(text: str) -> list[str]:
    raw = (text or "").lower()
    tokens = [
        p.strip(".,;:!?()[]{}\"'«»")
        for p in raw.replace("\n", " ").split()
        if p.strip(".,;:!?()[]{}\"'«»")
    ]
    candidates = tokens + [raw.strip()]
    available = set(_project_image_available_character_keys())
    out: list[str] = []
    for candidate in candidates:
        key = _normalize_character_key(candidate)
        if key in available and key not in out:
            out.append(key)
    return out


def project_image_character_markup(selected: list[str]):
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    selected_set = set(selected)
    buttons: list[str] = []
    for key in _project_image_available_character_keys():
        buttons.append(f"✓ {key}" if key in selected_set else key)
    rows: list[list[str]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append([_CUSTOM_REFERENCE_ADD_BUTTON])
    rows.append([_PROJECT_IMAGE_DONE_BUTTON])
    rows.append([NAV_BUTTON_START_OVER])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _project_image_character_summary(characters: list[str]) -> str:
    source = characters if characters else list(_PROJECT_IMAGE_BASE_CHARACTERS)
    return format_character_short_descriptions([str(x) for x in source]) or ", ".join(source)


async def _generate_project_image(
    update: Update,
    prompt: str,
    characters: list[str],
    *,
    custom_reference_images: list[bytes] | None = None,
) -> None:
    effective_characters = list(characters or list(_PROJECT_IMAGE_BASE_CHARACTERS))
    for key in _project_image_character_keys_from_text(prompt):
        if key not in effective_characters:
            effective_characters.append(key)
    scene = {
        "characters": effective_characters,
        "shot_type": "medium",
        "start_frame_prompt": prompt,
    }
    refs = [*_load_scene_reference_images(scene, include_location=False), *(custom_reference_images or [])]
    effective_prompt = _append_reference_style_to_image_prompt(prompt, bool(refs))
    effective_prompt = _append_scene_identity_to_image_prompt(effective_prompt, scene, include_location=False)
    await reply_text_without_keyboard(
        update.message,
        "Генерируем изображение проекта, ждите.\n"
        f"Персонажи: {_project_image_character_summary(scene['characters'])}."
    )
    image_bytes = await _generate_image_with_lenient_fallback(
        effective_prompt,
        reference_images=refs if refs else None,
    )
    bio = io.BytesIO(image_bytes)
    bio.name = "project_image.png"
    await update.message.reply_photo(
        photo=bio,
        caption=f"Изображение проекта готово. Персонажи: {_project_image_character_summary(scene['characters'])}.",
    )


def _reset_first_step_session(chat_id: int, session: dict) -> None:
    """Сбрасывает временное состояние первого шага, сохраняя выбранный проект."""
    project_key = str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING)
    target_languages = session.get("target_languages")
    if not isinstance(target_languages, list) or not target_languages:
        target_languages = ["ru", "en"]
    SESSIONS[chat_id] = {
        PROJECT_KEY_SESSION: project_key,
        "state": "",
        "target_languages": target_languages,
    }


def _is_first_step_menu_button(text: str) -> bool:
    """Проверяет, что текст пришёл из меню первого шага."""
    low = (text or "").strip().lower()
    project_image_button = FIRST_STEP_BUTTON_PROJECT_IMAGE.lower()
    return low in {
        FIRST_STEP_BUTTON_GENERATE.lower(),
        FIRST_STEP_BUTTON_DESCRIPTION.lower(),
        FIRST_STEP_BUTTON_IMAGE.lower(),
        FIRST_STEP_BUTTON_SOUND.lower(),
        FIRST_STEP_BUTTON_VIDEO.lower(),
        FIRST_STEP_BUTTON_PROJECT.lower(),
        DRAFT_BUTTON_SAVE.lower(),
        DRAFT_BUTTON_LIST.lower(),
        project_image_button,
    } or low.startswith(f"{project_image_button} ")


def _draft_admin_allowed(chat_id: int | None) -> bool:
    raw = str(os.getenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID") or "").strip()
    if not raw:
        return True
    try:
        return chat_id is not None and int(raw) == int(chat_id)
    except ValueError:
        return True


def _parse_scene_indexes(text: str, total: int) -> list[int]:
    low = str(text or "").strip().lower()
    if low in {"все", "all"}:
        return list(range(total))
    out: list[int] = []
    for match in re.finditer(r"\d+", low):
        idx = int(match.group(0)) - 1
        if 0 <= idx < total and idx not in out:
            out.append(idx)
    return out


def _parse_draft_languages(text: str) -> list[str]:
    low = str(text or "").strip().lower().replace(" ", "")
    if low in {"ru+en", "en+ru", "оба", "обе", "both"}:
        return ["ru", "en"]
    if low in {"ru", "рус", "русский"}:
        return ["ru"]
    if low in {"en", "англ", "английский"}:
        return ["en"]
    return []


def _parse_draft_stage(text: str) -> str | None:
    low = str(text or "").strip().lower()
    if low in {"промпт", "prompt"}:
        return "prompt"
    if low in {"изображение", "картинка", "image"}:
        return "image"
    if low in {"видео", "video"}:
        return "video"
    return None


def _simple_keyboard(rows: list[list[str]]):
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _drafts_page_markup(*, page: int, has_next: bool, count: int):
    rows: list[list[str]] = []
    for idx in range(1, count + 1):
        rows.append([f"Открыть {idx}", f"Убрать {idx}"])
    nav: list[str] = []
    if page > 0:
        nav.append("Назад")
    if has_next:
        nav.append("Дальше")
    if nav:
        rows.append(nav)
    rows.append([NAV_BUTTON_START_OVER])
    return _simple_keyboard(rows)


def _draft_scene_markup(total: int, *, has_plan: bool, selected: list[int] | None = None):
    selected_set = {int(idx) for idx in (selected or [])}
    rows: list[list[str]] = []
    labels = [f"{'✓ ' if idx - 1 in selected_set else ''}Сцена {idx}" for idx in range(1, total + 1)]
    for idx in range(0, len(labels), 3):
        rows.append(labels[idx : idx + 3])
    if total > 1:
        rows.append(["Все"])
    if selected_set:
        rows.append([_DRAFT_REPAIR_NEXT_BUTTON])
    if has_plan:
        rows.append([_DRAFT_REPAIR_ADD_SCENE_BUTTON, _DRAFT_REPAIR_START_BUTTON])
    rows.append([NAV_BUTTON_START_OVER])
    return _simple_keyboard(rows)


def _draft_language_markup():
    return _simple_keyboard([["RU", "EN", "RU+EN"], [NAV_BUTTON_START_OVER]])


def _draft_stage_markup():
    return _simple_keyboard([["Промпт", "Изображение", "Видео"], [NAV_BUTTON_START_OVER]])


def _draft_after_scene_markup():
    return _simple_keyboard([[_DRAFT_REPAIR_ADD_SCENE_BUTTON, _DRAFT_REPAIR_START_BUTTON], [NAV_BUTTON_START_OVER]])


def _disable_scene_media_ai_auto(session: dict) -> None:
    session.pop("scene_media_auto_approve", None)
    session.pop("scene_media_ai_human_review", None)
    session.pop("scene_media_ai_technical_failure", None)
    session["scene_media_approve_mode"] = "manual"


def _parse_numbered_button(text: str, prefix: str) -> int | None:
    raw = str(text or "").strip().lower()
    prefix_low = prefix.lower()
    if not raw.startswith(prefix_low):
        return None
    match = re.search(r"\d+", raw[len(prefix_low):])
    if not match:
        return None
    return int(match.group(0)) - 1


def _draft_pending_scene_label(repair: dict) -> str:
    try:
        return f"сцены {int(repair.get('pending_scene_index')) + 1}"
    except (TypeError, ValueError):
        return "выбранной сцены"


def _draft_plan_summary(plan: list[dict]) -> str:
    parts: list[str] = []
    for item in plan:
        try:
            scene_num = int(item.get("scene_index")) + 1
        except (TypeError, ValueError):
            continue
        languages = "/".join(str(lang) for lang in (item.get("languages") or []))
        stage = str(item.get("stage") or "")
        parts.append(f"{scene_num}:{languages}:{stage}")
    return ", ".join(parts) or "-"


async def _ask_next_draft_scene_language(update: Update, session: dict, scenes: list[dict]) -> bool:
    repair = dict(session.get("draft_repair") or {})
    queue = repair.get("pending_scene_queue")
    if not isinstance(queue, list) or not queue:
        selected = repair.get("selected_scene_indexes")
        queue = [int(idx) for idx in (selected or []) if 0 <= int(idx) < len(scenes)]
    if not queue:
        await update.message.reply_text(
            "Сначала выберите одну или несколько сцен.",
            reply_markup=_draft_scene_markup(
                len(scenes),
                has_plan=bool(repair.get("plan")),
                selected=repair.get("selected_scene_indexes") if isinstance(repair.get("selected_scene_indexes"), list) else [],
            ),
        )
        return True
    current = int(queue.pop(0))
    repair["pending_scene_index"] = current
    repair["pending_scene_queue"] = queue
    repair["selected_scene_indexes"] = queue
    session["draft_repair"] = repair
    session["state"] = _STATE_WAITING_DRAFT_LANGUAGE
    await update.message.reply_text(
        f"Сцена {current + 1}: выберите язык.",
        reply_markup=_draft_language_markup(),
    )
    return True


async def _send_drafts_page(update: Update, chat_id: int, session: dict | None, *, page: int) -> None:
    if not isinstance(session, dict):
        session = {}
    SESSIONS[chat_id] = session
    page = max(0, int(page))
    rows = list_generation_drafts(None, limit=DRAFTS_PAGE_SIZE + 1, offset=page * DRAFTS_PAGE_SIZE)
    drafts = rows[:DRAFTS_PAGE_SIZE]
    has_next = len(rows) > DRAFTS_PAGE_SIZE
    session[_DRAFTS_PAGE_SESSION_KEY] = page
    session[_DRAFTS_PAGE_IDS_SESSION_KEY] = [str(draft.id) for draft in drafts]
    save_session_if_possible(chat_id, session)
    await update.message.reply_text(
        format_drafts_page(drafts, page=page, has_next=has_next),
        reply_markup=_drafts_page_markup(page=page, has_next=has_next, count=len(drafts)),
    )


async def _open_generation_draft(update: Update, chat_id: int, draft_id: str) -> bool:
    loaded = load_generation_draft(draft_id)
    if loaded is None:
        await update.message.reply_text("Не нашла такой черновик.")
        return True
    draft, snapshot = loaded
    _disable_scene_media_ai_auto(snapshot)
    snapshot["draft_repair"] = {"draft_id": str(draft.id), "plan": []}
    snapshot.pop(_DRAFT_REPAIR_ACTIVE_PLAN_KEY, None)
    snapshot["state"] = _STATE_WAITING_DRAFT_SCENES
    snapshot["is_draft"] = True
    SESSIONS[chat_id] = snapshot
    production = snapshot.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    total = len(scenes) if isinstance(scenes, list) else 0
    await update.message.reply_text(
        f"Открыла черновик: {draft.title}\n"
        f"Сцен: {total}.\n"
        "Выберите одну или несколько сцен для правки, затем нажмите `Далее`.",
        reply_markup=_draft_scene_markup(total, has_plan=False),
    )
    return True


async def _start_draft_repair(update: Update, session: dict) -> bool:
    repair = dict(session.get("draft_repair") or {})
    plan = repair.get("plan") if isinstance(repair.get("plan"), list) else []
    if not plan:
        await update.message.reply_text("Сначала выберите хотя бы одну сцену для правки.")
        return True
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        await update.message.reply_text("В этом черновике нет production-сцен.")
        session["state"] = ""
        return True
    normalized_plan: list[dict] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        idx = _safe_draft_scene_index(item.get("scene_index"), len(scenes))
        languages = [str(lang).strip().lower() for lang in (item.get("languages") or []) if str(lang).strip()]
        stage = _parse_draft_stage(str(item.get("stage") or ""))
        if idx is None or not languages or stage is None:
            continue
        normalized_plan.append({"scene_index": idx, "languages": languages, "stage": stage})
        clear_scene_for_repair(scenes[idx], stage=stage, languages=languages)
        scene_db_ids = session.get("scene_db_ids_by_index")
        scene_db_id = None
        if isinstance(scene_db_ids, dict):
            scene_db_id = scene_db_ids.get(idx) or scene_db_ids.get(str(idx))
        clear_scene_db_approvals_for_repair(scene_db_id, stage=stage, languages=languages)
    if not normalized_plan:
        await update.message.reply_text("Не вижу корректного плана правки.")
        return True
    normalized_plan.sort(key=lambda row: int(row["scene_index"]))
    first = normalized_plan[0]
    session[_DRAFT_REPAIR_ACTIVE_PLAN_KEY] = normalized_plan
    session["image_scene_index"] = int(first["scene_index"])
    session["scene_media_language_index"] = 0
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO if first["stage"] == "video" else SCENE_MEDIA_SUBSTEP_START
    session["state"] = STATE_WAITING_SCENE_MEDIA
    session["is_draft"] = True
    session.pop("draft_repair", None)
    _disable_scene_media_ai_auto(session)
    await update.message.reply_text(
        "Черновик подготовлен к правке. Пройду только выбранные сцены; остальные останутся в финальной сборке как были."
    )
    await _send_next_scene_media_step(update, session)
    return True


def _safe_draft_scene_index(value, total: int) -> int | None:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < total:
        return idx
    return None


async def _handle_draft_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict | None,
    text: str,
) -> bool:
    if update.message is None:
        return False
    low = str(text or "").strip().lower()
    if low == DRAFT_BUTTON_SAVE.lower() or low == "/save_draft":
        if not isinstance(session, dict) or not session:
            await update.message.reply_text("Пока нечего сохранять: активной генерации нет.")
            return True
        draft = save_generation_draft(chat_id, session)
        await update.message.reply_text(f"Черновик сохранён: {draft.title}\nid: {draft.id}")
        return True
    if low == DRAFT_BUTTON_LIST.lower() or low == "/drafts":
        if not _draft_admin_allowed(chat_id):
            await update.message.reply_text("Список черновиков доступен только админскому Telegram chat id.")
            return True
        await _send_drafts_page(update, chat_id, session, page=0)
        return True
    if low in {"дальше", "next"} and _draft_admin_allowed(chat_id):
        page = int((session or {}).get(_DRAFTS_PAGE_SESSION_KEY, 0) or 0) + 1
        await _send_drafts_page(update, chat_id, session, page=page)
        return True
    if low in {"назад", "back"} and _draft_admin_allowed(chat_id):
        page = max(0, int((session or {}).get(_DRAFTS_PAGE_SESSION_KEY, 0) or 0) - 1)
        await _send_drafts_page(update, chat_id, session, page=page)
        return True
    open_idx = _parse_numbered_button(text, "Открыть")
    if open_idx is not None and _draft_admin_allowed(chat_id):
        ids = (session or {}).get(_DRAFTS_PAGE_IDS_SESSION_KEY)
        if not isinstance(ids, list):
            page = int((session or {}).get(_DRAFTS_PAGE_SESSION_KEY, 0) or 0)
            drafts = list_generation_drafts(None, limit=DRAFTS_PAGE_SIZE, offset=page * DRAFTS_PAGE_SIZE)
            ids = [str(draft.id) for draft in drafts]
        if isinstance(ids, list) and 0 <= open_idx < len(ids):
            return await _open_generation_draft(update, chat_id, str(ids[open_idx]))
        await update.message.reply_text("Не вижу такого номера на текущей странице черновиков.")
        return True
    hide_idx = _parse_numbered_button(text, "Убрать")
    if hide_idx is not None and _draft_admin_allowed(chat_id):
        ids = (session or {}).get(_DRAFTS_PAGE_IDS_SESSION_KEY)
        if not isinstance(ids, list):
            page = int((session or {}).get(_DRAFTS_PAGE_SESSION_KEY, 0) or 0)
            drafts = list_generation_drafts(None, limit=DRAFTS_PAGE_SIZE, offset=page * DRAFTS_PAGE_SIZE)
            ids = [str(draft.id) for draft in drafts]
        if isinstance(ids, list) and 0 <= hide_idx < len(ids):
            hidden = hide_generation_draft(str(ids[hide_idx]))
            if hidden:
                page = int((session or {}).get(_DRAFTS_PAGE_SESSION_KEY, 0) or 0)
                await update.message.reply_text("Убрала черновик из списка.")
                await _send_drafts_page(update, chat_id, session, page=page)
                return True
        await update.message.reply_text("Не вижу такого номера на текущей странице черновиков.")
        return True
    draft_id = parse_draft_open_command(text)
    if draft_id:
        return await _open_generation_draft(update, chat_id, draft_id)
    if isinstance(session, dict) and session.get("state") in {
        _STATE_WAITING_DRAFT_SCENES,
        _STATE_WAITING_DRAFT_LANGUAGE,
        _STATE_WAITING_DRAFT_STAGE,
    } and low == SCENE_MEDIA_STOP_BUTTON.lower():
        _disable_scene_media_ai_auto(session)
        session["state"] = STATE_WAITING_SCENE_MEDIA if session.get("production") else ""
        session.pop("draft_repair", None)
        await update.message.reply_text("Остановила правку черновика и вернула ручной режим без удаления медиа.")
        return True
    if isinstance(session, dict) and session.get("state") == _STATE_WAITING_DRAFT_SCENES:
        production = session.get("production") or {}
        scenes = production.get("json_plan") if isinstance(production, dict) else None
        if not isinstance(scenes, list) or not scenes:
            await update.message.reply_text("В этом черновике нет production-сцен.")
            session["state"] = ""
            return True
        repair = dict(session.get("draft_repair") or {})
        plan = repair.get("plan") if isinstance(repair.get("plan"), list) else []
        if low in {_DRAFT_REPAIR_START_BUTTON.lower(), "start repair", "начать"}:
            return await _start_draft_repair(update, session)
        if low in {_DRAFT_REPAIR_ADD_SCENE_BUTTON.lower(), "add scene"}:
            repair["selected_scene_indexes"] = []
            repair["pending_scene_queue"] = []
            session["draft_repair"] = repair
            await update.message.reply_text(
                "Выберите одну или несколько следующих сцен.",
                reply_markup=_draft_scene_markup(len(scenes), has_plan=bool(plan), selected=[]),
            )
            return True
        if low in {_DRAFT_REPAIR_NEXT_BUTTON.lower(), "next", "далее"}:
            return await _ask_next_draft_scene_language(update, session, scenes)
        indexes = _parse_scene_indexes(text, len(scenes))
        if not indexes:
            await update.message.reply_text(
                "Не поняла сцену. Нажмите кнопку сцены или пришлите, например: `1`, `1, 3` или `все`.",
                reply_markup=_draft_scene_markup(
                    len(scenes),
                    has_plan=bool(plan),
                    selected=repair.get("selected_scene_indexes") if isinstance(repair.get("selected_scene_indexes"), list) else [],
                ),
            )
            return True
        selected = repair.get("selected_scene_indexes") if isinstance(repair.get("selected_scene_indexes"), list) else []
        if low.startswith("сцена") or low.startswith("✓ сцена"):
            idx = indexes[0]
            if idx in selected:
                selected = [item for item in selected if int(item) != idx]
            else:
                selected = [*selected, idx]
            selected = sorted({int(item) for item in selected})
            repair["selected_scene_indexes"] = selected
            session["draft_repair"] = repair
            selected_text = ", ".join(str(int(idx) + 1) for idx in selected) or "нет"
            await update.message.reply_text(
                f"Выбрано сцен: {selected_text}.",
                reply_markup=_draft_scene_markup(len(scenes), has_plan=bool(plan), selected=selected),
            )
            return True
        repair["selected_scene_indexes"] = indexes
        repair["pending_scene_queue"] = list(indexes)
        session["draft_repair"] = repair
        return await _ask_next_draft_scene_language(update, session, scenes)
    if isinstance(session, dict) and session.get("state") == _STATE_WAITING_DRAFT_LANGUAGE:
        languages = _parse_draft_languages(text)
        if not languages:
            await update.message.reply_text("Не поняла язык. Выберите `RU`, `EN` или `RU+EN`.", reply_markup=_draft_language_markup())
            return True
        repair = dict(session.get("draft_repair") or {})
        repair["languages"] = languages
        session["draft_repair"] = repair
        session["state"] = _STATE_WAITING_DRAFT_STAGE
        await update.message.reply_text(
            f"{_draft_pending_scene_label(repair).capitalize()}: откуда перезапустить?",
            reply_markup=_draft_stage_markup(),
        )
        return True
    if isinstance(session, dict) and session.get("state") == _STATE_WAITING_DRAFT_STAGE:
        stage = _parse_draft_stage(text)
        if stage is None:
            await update.message.reply_text("Не поняла этап. Выберите `Промпт`, `Изображение` или `Видео`.", reply_markup=_draft_stage_markup())
            return True
        repair = dict(session.get("draft_repair") or {})
        try:
            pending_idx = int(repair.get("pending_scene_index"))
        except (TypeError, ValueError):
            pending_idx = -1
        indexes = [pending_idx] if pending_idx >= 0 else []
        languages = [str(lang) for lang in (repair.get("languages") or ["ru"])]
        production = session.get("production") or {}
        scenes = production.get("json_plan") if isinstance(production, dict) else None
        if not isinstance(scenes, list) or not indexes:
            await update.message.reply_text("Не вижу выбранных сцен в черновике.")
            session["state"] = ""
            return True
        plan = repair.get("plan") if isinstance(repair.get("plan"), list) else []
        for idx in indexes:
            if 0 <= idx < len(scenes):
                plan.append({"scene_index": idx, "languages": languages, "stage": stage})
        repair["plan"] = plan
        repair.pop("pending_scene_index", None)
        repair.pop("languages", None)
        session["draft_repair"] = repair
        queue = repair.get("pending_scene_queue") if isinstance(repair.get("pending_scene_queue"), list) else []
        if queue:
            return await _ask_next_draft_scene_language(update, session, scenes)
        session["state"] = _STATE_WAITING_DRAFT_SCENES
        selected = _draft_plan_summary(plan)
        await update.message.reply_text(
            f"Добавила в план: {selected}.\nМожно добавить ещё сцену или начать правку.",
            reply_markup=_draft_after_scene_markup(),
        )
        return True
    return False


def _drop_step_outputs_from(session: dict, step: str) -> None:
    """Удаляет результаты указанного шага и всех следующих."""
    if step == "recipe":
        session["recipe_full_text"] = None
        session["precheck_recipe_steps"] = None
        session["recipe_steps"] = None
        session["scenes_plan"] = None
        session["scenes_text"] = None
        session["frame_prompts"] = None
        session["frame_prompts_text"] = None
        session.pop(_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY, None)
        session["production"] = None
        session.pop("image_scene_index", None)
        session.pop("scene_media_substep", None)
        return
    if step == "scenes":
        session["scenes_plan"] = None
        session["scenes_text"] = None
        session["recipe_steps"] = None
        session["frame_prompts"] = None
        session["frame_prompts_text"] = None
        session.pop(_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY, None)
        session["production"] = None
        session.pop("image_scene_index", None)
        session.pop("scene_media_substep", None)
        return
    if step == "production":
        session["production"] = None
        session.pop(_FRAME_PROMPT_REFERENCE_IMAGES_SESSION_KEY, None)
        session.pop("image_scene_index", None)
        session.pop("scene_media_substep", None)
        return
    if step == "media":
        production = session.get("production")
        if isinstance(production, dict):
            plan = production.get("json_plan")
            if isinstance(plan, list):
                for scene in plan:
                    if not isinstance(scene, dict):
                        continue
                    for key in (
                        "start_frame_image_b64",
                        "end_frame_image_b64",
                        "video_prompt_policy_candidate",
                        "video_prompt_policy_candidate_by_lang",
                        "syntax_fallback_pending",
                        "video_prompt_policy_failure_count",
                        "video_results_by_lang",
                        "video_approvals_by_lang",
                    ):
                        scene.pop(key, None)
        session["image_scene_index"] = 0
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START


def _last_available_scene_media_substep(scene: dict) -> str:
    """Return the latest meaningful media substep for a previous scene."""
    video_results = scene.get("video_results_by_lang")
    video_prompts = scene.get("video_prompt_by_lang")
    if (
        (isinstance(video_results, dict) and video_results)
        or (isinstance(video_prompts, dict) and video_prompts)
        or str(scene.get("video_prompt") or "").strip()
    ):
        return SCENE_MEDIA_SUBSTEP_VIDEO
    if _scene_needs_end_frame_step(scene):
        return (
            SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
            if scene.get("end_frame_image_b64")
            else SCENE_MEDIA_SUBSTEP_END
        )
    if scene.get("start_frame_image_b64"):
        return SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
    return SCENE_MEDIA_SUBSTEP_START


def _clear_scene_media_navigation_pending(session: dict, scene: dict | None = None) -> None:
    """Drop transient media prompts so Back lands on the visible target step."""
    for key in (
        "scene_speech_shorten_pending",
        "scene_phrase_translation_pending",
        "scene_phrase_input_pending",
        "scene_image_edit_pending",
        "scene_image_reference_pending",
        "scene_video_feedback_pending",
        "scene_video_speed_pending",
        "scene_prompt_input_pending",
    ):
        session.pop(key, None)
    if isinstance(scene, dict):
        for key in (
            "video_provider_choice_pending",
            "syntax_fallback_pending",
            "image_model_choice_pending",
        ):
            scene.pop(key, None)


def _scene_media_nav_label(substep: str) -> str:
    if substep == SCENE_MEDIA_SUBSTEP_START:
        return "промпт стартового кадра"
    if substep == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        return "апрув стартового изображения"
    if substep == SCENE_MEDIA_SUBSTEP_END:
        return "промпт конечного кадра"
    if substep == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        return "апрув конечного изображения"
    if substep == SCENE_MEDIA_SUBSTEP_VIDEO:
        return "апрув video prompt"
    return substep


async def _handle_navigation_buttons(
    update: Update,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """Глобальная навигация: предыдущий шаг и возврат в начало."""
    low = (text or "").strip().lower()
    if low == NAV_BUTTON_START_OVER.lower():
        SESSIONS.pop(chat_id, None)
        await _reply_text_with_optional_markup(
            update,
            "Начинаем сначала. Выберите действие на первом шаге.",
            reply_markup=first_step_menu_markup(),
        )
        return True
    if low != NAV_BUTTON_PREVIOUS_STEP.lower():
        return False

    state = str(session.get("state") or "")
    if state == _STATE_WAITING_FRAME_PROMPT_REFERENCES:
        session["state"] = STATE_WAITING_FRAME_PROMPTS_APPROVAL
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Вернула к апруву frame prompts.",
            reply_markup=_frame_prompts_approval_markup(),
        )
        return True
    if state == STATE_WAITING_SCENE_MEDIA:
        production = session.get("production") or {}
        scenes = production.get("json_plan") if isinstance(production, dict) else None
        idx = int(session.get("image_scene_index", 0) or 0)
        sub = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
        if not isinstance(scenes, list) or not (0 <= idx < len(scenes)):
            _drop_step_outputs_from(session, "media")
            session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
            await _reply_text_with_optional_markup(
                update,
                "Не нашла текущую сцену медиа-шага. Возвращаю к апруву production-промптов.",
                reply_markup=_production_bundle_approval_markup(),
            )
            return True
        scene = scenes[idx]
        _clear_scene_media_navigation_pending(session, scene)
        total = len(scenes)
        if sub == SCENE_MEDIA_SUBSTEP_VIDEO:
            if _scene_needs_end_frame_step(scene):
                target_substep = (
                    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
                    if scene.get("end_frame_image_b64")
                    else SCENE_MEDIA_SUBSTEP_END
                )
            else:
                target_substep = (
                    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
                    if scene.get("start_frame_image_b64")
                    else SCENE_MEDIA_SUBSTEP_START
                )
            session["scene_media_substep"] = target_substep
            logger.info(
                "chat=%s navigation.previous scene=%s from_substep=%s to_substep=%s",
                chat_id,
                idx + 1,
                sub,
                target_substep,
            )
            await update.message.reply_text(
                f"Назад: сцена {idx + 1}/{total}, {_scene_media_nav_label(target_substep)}."
            )
            await _send_next_scene_media_step(update, session)
            return True
        if sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
            logger.info(
                "chat=%s navigation.previous scene=%s from_substep=%s to_substep=%s",
                chat_id,
                idx + 1,
                sub,
                SCENE_MEDIA_SUBSTEP_END,
            )
            await update.message.reply_text(
                f"Назад: сцена {idx + 1}/{total}, {_scene_media_nav_label(SCENE_MEDIA_SUBSTEP_END)}."
            )
            await _send_next_scene_media_step(update, session)
            return True
        if sub == SCENE_MEDIA_SUBSTEP_END:
            scene.pop("end_frame_image_b64", None)
            target_substep = (
                SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
                if scene.get("start_frame_image_b64")
                else SCENE_MEDIA_SUBSTEP_START
            )
            session["scene_media_substep"] = target_substep
            logger.info(
                "chat=%s navigation.previous scene=%s from_substep=%s to_substep=%s",
                chat_id,
                idx + 1,
                sub,
                target_substep,
            )
            await update.message.reply_text(
                f"Назад: сцена {idx + 1}/{total}, {_scene_media_nav_label(target_substep)}."
            )
            await _send_next_scene_media_step(update, session)
            return True
        if sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            logger.info(
                "chat=%s navigation.previous scene=%s from_substep=%s to_substep=%s",
                chat_id,
                idx + 1,
                sub,
                SCENE_MEDIA_SUBSTEP_START,
            )
            await update.message.reply_text(
                f"Назад: сцена {idx + 1}/{total}, {_scene_media_nav_label(SCENE_MEDIA_SUBSTEP_START)}."
            )
            await _send_next_scene_media_step(update, session)
            return True
        if sub == SCENE_MEDIA_SUBSTEP_START and idx > 0:
            prev_idx = idx - 1
            prev_scene = scenes[prev_idx]
            _clear_scene_media_navigation_pending(session, prev_scene if isinstance(prev_scene, dict) else None)
            target_substep = (
                _last_available_scene_media_substep(prev_scene)
                if isinstance(prev_scene, dict)
                else SCENE_MEDIA_SUBSTEP_START
            )
            session["image_scene_index"] = prev_idx
            session["scene_media_language_index"] = 0
            session["scene_media_substep"] = target_substep
            logger.info(
                "chat=%s navigation.previous scene=%s from_substep=%s to_scene=%s to_substep=%s",
                chat_id,
                idx + 1,
                sub,
                prev_idx + 1,
                target_substep,
            )
            await update.message.reply_text(
                f"Назад: сцена {prev_idx + 1}/{total}, {_scene_media_nav_label(target_substep)}."
            )
            await _send_next_scene_media_step(update, session)
            return True
        _drop_step_outputs_from(session, "media")
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        logger.info(
            "chat=%s navigation.previous scene=%s from_substep=%s to_state=%s",
            chat_id,
            idx + 1,
            sub,
            STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
        )
        await _reply_text_with_optional_markup(
            update,
            "Это первый медиа-подшаг сцены. Возвращаю к апруву production-промптов.",
            reply_markup=_production_bundle_approval_markup(),
        )
        return True

    if state in {"", _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION}:
        await _reply_text_with_optional_markup(
            update,
            "Это первый шаг. Доступна кнопка «В начало».",
            reply_markup=first_step_menu_markup(),
        )
        return True
    if state in {
        _STATE_WAITING_FIRST_STEP_GENERATE_TEXT,
        _STATE_WAITING_FIRST_STEP_DESCRIPTION_TEXT,
        _STATE_WAITING_FIRST_STEP_IMAGE_TEXT,
        _STATE_WAITING_FIRST_STEP_VIDEO_TEXT,
        _STATE_WAITING_FIRST_STEP_VIDEO_IMAGE,
    }:
        _reset_first_step_session(chat_id, session)
        await _reply_text_with_optional_markup(
            update,
            "Вернула на первый шаг.",
            reply_markup=first_step_menu_markup(),
        )
        return True
    if state in {STATE_WAITING_CONCEPT, STATE_REFINING_CONCEPT}:
        await update.message.reply_text("Это первый шаг сценарного пайплайна. Выберите «В начало» для полного сброса.")
        return True
    if state in {STATE_WAITING_RECIPE_APPROVAL, STATE_REFINING_RECIPE}:
        _drop_step_outputs_from(session, "recipe")
        session["state"] = STATE_WAITING_CONCEPT
        await _reply_text_with_optional_markup(
            update,
            "Откатила шаг рецепта. Возвращаю к выбору концепта.",
            reply_markup=ok_only_markup(),
        )
        return True
    if state in {STATE_WAITING_SCENE_APPROVAL, STATE_REFINING_SCENES}:
        _drop_step_outputs_from(session, "scenes")
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        await _reply_text_with_optional_markup(
            update,
            "Откатила шаг сценария. Возвращаю к апруву рецепта.",
            reply_markup=ok_only_markup(),
        )
        return True
    if state in {
        STATE_WAITING_FRAME_PROMPTS_APPROVAL,
        STATE_REFINING_FRAME_PROMPTS,
        STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
        STATE_REFINING_PRODUCTION_BUNDLE,
    }:
        _drop_step_outputs_from(session, "production")
        session["state"] = STATE_WAITING_SCENE_APPROVAL
        await _reply_text_with_optional_markup(
            update,
            "Откатила шаг production-промптов. Возвращаю к апруву сценария.",
            reply_markup=ok_only_markup(),
        )
        return True
    if state == STATE_READY_FOR_PRODUCTION:
        _drop_step_outputs_from(session, "media")
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        await _reply_text_with_optional_markup(
            update,
            "Откатила шаг генерации медиа. Возвращаю к апруву production-промптов.",
            reply_markup=_production_bundle_approval_markup(),
        )
        return True
    restored = restore_previous_checkpoint(chat_id, session)
    if restored is not None:
        state_after_restore = str(session.get("state") or "")
        if state_after_restore == STATE_WAITING_SCENE_MEDIA:
            production = session.get("production") or {}
            scenes = production.get("json_plan") if isinstance(production, dict) else None
            idx = int(session.get("image_scene_index", 0) or 0)
            sub = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
            total = len(scenes) if isinstance(scenes, list) else 0
            await update.message.reply_text(
                f"Назад: сцена {idx + 1}/{total}, {_scene_media_nav_label(sub)}."
                if total
                else "Назад: предыдущий сохранённый медиа-шаг."
            )
            await _send_next_scene_media_step(update, session)
            return True
        await _reply_text_with_optional_markup(
            update,
            "Вернула на предыдущий сохранённый шаг.",
            reply_markup=keyboard_for_session_state(session),
        )
        return True
    return False


def _state_supports_guided_feedback(state: str | None) -> bool:
    """Возвращает True для waiting-состояний, где возможны кнопки «Добавь/Исправь»."""
    return state in {
        STATE_WAITING_CONCEPT,
        STATE_WAITING_RECIPE_APPROVAL,
        STATE_WAITING_SCENE_APPROVAL,
        STATE_WAITING_FRAME_PROMPTS_APPROVAL,
        STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
        STATE_WAITING_SCENE_MEDIA,
    }


def _guided_feedback_prompt(mode: str) -> str:
    """Текст-подсказка после нажатия кнопки направленной правки."""
    if mode == "add":
        return (
            "Что добавить в промпт к нейросети на этом шаге?\n"
            "Например: стартовую фразу, конкретный ингредиент, стиль подачи."
        )
    return "Что нужно исправить на этом шаге? Опишите правку текстом — отправлю как фидбэк в нейросеть."


async def _handle_guided_feedback_buttons(
    update: Update,
    *,
    session: dict,
    state: str | None,
    text: str,
) -> tuple[bool, str]:
    """
    Обрабатывает UX-кнопки «Добавь/Исправь» для всех waiting-шагов.

    Returns:
        (handled, normalized_text)
        - handled=True: событие полностью обработано (дальше в state-handler не идём);
        - normalized_text: текст, который нужно передать дальше в основной обработчик.
    """
    low = text.lower().strip()
    pending = session.get(_STEP_GUIDED_FEEDBACK_KEY)
    if low in _STEP_ADD_BUTTONS and _state_supports_guided_feedback(state):
        session[_STEP_GUIDED_FEEDBACK_KEY] = {"mode": "add", "state": state}
        await update.message.reply_text(_guided_feedback_prompt("add"))
        return True, text
    if low in _STEP_FIX_BUTTONS and _state_supports_guided_feedback(state):
        session[_STEP_GUIDED_FEEDBACK_KEY] = {"mode": "fix", "state": state}
        await update.message.reply_text(_guided_feedback_prompt("fix"))
        return True, text
    if not isinstance(pending, dict):
        return False, text
    pending_state = pending.get("state")
    pending_mode = str(pending.get("mode") or "fix")
    if pending_state != state:
        session.pop(_STEP_GUIDED_FEEDBACK_KEY, None)
        return False, text
    if low in _STEP_OK_BUTTONS:
        await update.message.reply_text("Сначала пришлите текст правки для кнопки «Добавь/Исправь».")
        return True, text
    if not text.strip():
        await update.message.reply_text("Пришлите правку текстом.")
        return True, text
    session.pop(_STEP_GUIDED_FEEDBACK_KEY, None)
    if pending_mode == "add":
        return False, f"Добавь в промпт: {text}"
    return False, f"Исправь: {text}"


def _production_bundle_approval_markup():
    """Кнопки выбора на этапе апрува production-бандла."""
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        keyboard=[
            ["ОК"],
            ["Добавь"],
            ["Исправь"],
            ["ФИДБЕК:"],
            ["ПРОМПТ:"],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _frame_prompts_slice_from_production(enriched_scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Восстанавливает форму «утверждённых frame prompts» из production-плана.

    Нужна для ``generate_full_production(..., approved_frame_prompts=...)`` при правках только video:
    старт/конец кадра фиксируются, в LLM уходит только переписывание ``video_prompt_en``.
    """
    out: list[dict[str, Any]] = []
    for s in enriched_scenes:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "scene": s.get("scene"),
                "shot_type": s.get("shot_type", "medium"),
                "model": s.get("model", "veo3_fast"),
                "duration_sec": s.get("duration_sec", 4),
                "continuity_anchors": s.get("continuity_anchors") or [],
                "continuity_state": s.get("continuity_state") or "",
                "start_frame_prompt_ru": str(s.get("start_frame_prompt") or "пусто").strip() or "пусто",
                "end_frame_prompt_ru": str(s.get("end_frame_prompt") or "пусто").strip() or "пусто",
            }
        )
    return out


_END_FRAME_FEEDBACK_MARKERS = (
    "конечн",
    "финальн",
    "end frame",
    "end-frame",
)
_END_FRAME_REMOVE_MARKERS = (
    "без конечн",
    "без финальн",
    "не нужен конечн",
    "не нужен финальн",
    "не нужны конечн",
    "не нужны финальн",
    "убрать конечн",
    "убрать финальн",
    "удалить конечн",
    "удалить финальн",
    "отменить конечн",
    "отменить финальн",
)
_END_FRAME_REMOVE_ACTIONS = ("без", "не нужен", "не нужны", "убрать", "удалить", "отменить")
_SCENE_ORDINAL_WORDS_RU = {
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четверт": 4,
    "пят": 5,
    "шест": 6,
    "седьм": 7,
    "восьм": 8,
    "девят": 9,
    "десят": 10,
}


def _end_frame_chunk_requests_removal(chunk: str) -> bool:
    """Return True only for explicit remove/cancel wording, not accidental cross-word substrings."""
    if not any(marker in chunk for marker in _END_FRAME_FEEDBACK_MARKERS):
        return False
    if re.search(r"\bне\s+нуж\w*\b", chunk):
        return True
    if re.search(r"\b(?:без|убрать|удалить|отменить)\w*\b", chunk):
        return True
    return False


def _scene_numbers_from_end_frame_feedback(text: str, *, total_scenes: int) -> tuple[set[int], set[int]]:
    """Extract explicit scene-scoped end-frame additions/removals from user feedback."""
    low = " ".join(str(text or "").lower().replace("ё", "е").split())
    if not low or not any(marker in low for marker in _END_FRAME_FEEDBACK_MARKERS):
        return set(), set()

    add: set[int] = set()
    remove: set[int] = set()
    chunks = [chunk.strip() for chunk in re.split(r"[\n.;]+", low) if chunk.strip()]
    for chunk in chunks:
        if not any(marker in chunk for marker in _END_FRAME_FEEDBACK_MARKERS):
            continue
        numbers: set[int] = set()
        for match in re.finditer(r"сцен(?:а|е|ы|у|ой|ах)?\s*№?\s*([0-9,\sи-]+)", chunk):
            numbers.update(int(value) for value in re.findall(r"\d+", match.group(1)))
        numbers.update(
            int(match.group(1))
            for match in re.finditer(r"\b(\d+)(?:-?(?:я|й|ая|ой))?\s+сцен(?:а|е|ы|у|ой|ах)?\b", chunk)
        )
        for word_prefix, number in _SCENE_ORDINAL_WORDS_RU.items():
            if re.search(rf"\b{word_prefix}\w*\s+сцен(?:а|е|ы|у|ой|ах)?\b", chunk):
                numbers.add(number)
        if ("все сцен" in chunk or "кажд" in chunk) and total_scenes > 0:
            numbers.update(range(1, total_scenes + 1))
        numbers = {number for number in numbers if 1 <= number <= total_scenes}
        if not numbers:
            continue
        is_remove = _end_frame_chunk_requests_removal(chunk)
        if is_remove:
            remove.update(numbers)
        else:
            add.update(numbers)
    return add, remove


def _update_end_frame_scene_requests(session: dict, feedback: str) -> list[int]:
    """Apply explicit end-frame feedback and persist the requested scene numbers."""
    scenes = session.get("scenes_plan")
    total_scenes = len(scenes) if isinstance(scenes, list) else 0
    current = {
        int(value)
        for value in (session.get("end_frame_scene_requests") or [])
        if str(value).isdigit() and 1 <= int(value) <= total_scenes
    }
    add, remove = _scene_numbers_from_end_frame_feedback(feedback, total_scenes=total_scenes)
    current.update(add)
    current.difference_update(remove)
    result = sorted(current)
    session["end_frame_scene_requests"] = result
    return result


def _ensure_end_frame_scene_requests_from_session_text(session: dict) -> list[int]:
    """Infer explicit end-frame requests from the original description once scenes are known."""
    scenes = session.get("scenes_plan")
    total_scenes = len(scenes) if isinstance(scenes, list) else 0
    if total_scenes <= 0:
        return []
    current = [
        int(value)
        for value in (session.get("end_frame_scene_requests") or [])
        if str(value).isdigit() and 1 <= int(value) <= total_scenes
    ]
    if current:
        session["end_frame_scene_requests"] = sorted(set(current))
        return session["end_frame_scene_requests"]
    for key in ("user_input", "recipe_full_text", "selected_concept", "user_feedback"):
        value = session.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        add, _remove = _scene_numbers_from_end_frame_feedback(value, total_scenes=total_scenes)
        if add:
            session["end_frame_scene_requests"] = sorted(add)
            return session["end_frame_scene_requests"]
    session["end_frame_scene_requests"] = []
    return []


async def _handle_waiting_scene_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        if not session.get(SERIES_CHOICE_CONFIRMED_SESSION_KEY) and not session.get(SERIES_SESSION_KEY):
            session[SERIES_PENDING_NEXT_SESSION_KEY] = SERIES_NEXT_OVERLAY_TITLE
            SESSIONS[chat_id] = session
            await _ask_series_choice(update, session)
            return True
        session["state"] = STATE_WAITING_OVERLAY_TITLE_RU
        session.pop(OVERLAY_TITLE_TRANSLATION_PENDING_SESSION_KEY, None)
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(update.message, _overlay_title_ru_prompt_text())
        return True
    return False


async def _handle_waiting_frame_prompts_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = text.lower().strip()
    if low == _FRAME_PROMPT_REFERENCE_ADD_BUTTON.lower():
        session["state"] = _STATE_WAITING_FRAME_PROMPT_REFERENCES
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Пришлите одно или несколько изображений для frame prompts. "
            f"Когда закончите, нажмите «{_CUSTOM_REFERENCE_DONE_BUTTON}».\n"
            f"{_frame_prompt_reference_count_text(session)}",
            reply_markup=_frame_prompt_reference_markup(),
        )
        return True
    if low in {"ок", "ok", "okay", "approve"}:
        try:
            await reply_text_without_keyboard(update.message, "Генерирую production-промпты, ждите.")
            requested_end_frame_scenes = _ensure_end_frame_scene_requests_from_session_text(session)
            production = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_full_production",
                lambda: generate_full_production(
                    session["scenes_plan"],
                    approved_frame_prompts=session.get("frame_prompts"),
                    requested_end_frame_scenes=requested_end_frame_scenes,
                    viral_concept_brief=session.get("viral_concept_brief"),
                    sponsored_content=bool(session.get("sponsored_content")),
                    brand_context=session.get("brand_context"),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("production failed")
            await update.message.reply_text(f"Ошибка production-генерации: {_user_error_message(e)}")
            return True
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        _apply_overlay_title_to_production(session, production)
        session["production"] = production
        session["image_scene_index"] = 0
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await update.message.reply_text(
            "Ниже — все промпты (start / end / video) для ознакомления. "
            "После «ОК» пойдём по сценам: старт → картинка → при необходимости конец → video → ролик."
        )
        await _reply_text_chunked(update, format_production_bundle_for_approval(production["json_plan"]))
        await _reply_text_with_optional_markup(
            update,
            "Нажмите «ОК», чтобы перейти к пошаговой генерации. "
            "Либо нажмите «Добавь»/«Исправь» и пришлите правки. "
            "Конечные кадры используются только для явно названных сцен.",
            reply_markup=_production_bundle_approval_markup(),
        )
        return True
    return False


async def _handle_waiting_frame_prompt_references_state(
    update: Update,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = text.lower().strip()
    if low == _CUSTOM_REFERENCE_DONE_BUTTON.lower():
        session["state"] = STATE_WAITING_FRAME_PROMPTS_APPROVAL
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Готово. "
            f"{_frame_prompt_reference_count_text(session)} "
            "Кнопки апрува и фидбэка снова доступны.",
            reply_markup=_frame_prompts_approval_markup(),
        )
        return True
    await _reply_text_with_optional_markup(
        update,
        "Жду изображения для frame prompts. Можно прислать ещё файл или завершить добавление.",
        reply_markup=_frame_prompt_reference_markup(),
    )
    return True


async def _handle_waiting_production_bundle_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        await update.message.reply_text("Нет данных production. Начни заново через /generate.")
        return True
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        session["state"] = STATE_WAITING_SCENE_MEDIA
        session["image_scene_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        session["scene_media_language_index"] = 0
        await update.message.reply_text("Общий блок промптов утверждён. Сцена 1 — стартовый кадр.")
        await _send_next_scene_media_step(update, session)
        return True
    session["state"] = STATE_REFINING_PRODUCTION_BUNDLE
    requested_end_frame_scenes = _update_end_frame_scene_requests(session, text)
    try:
        await reply_text_without_keyboard(update.message, "Перегенерирую production-промпты, ждите.")
        production_new = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_production_bundle",
            lambda: generate_full_production(
                session["scenes_plan"],
                approved_frame_prompts=session.get("frame_prompts"),
                user_feedback=text,
                previous_enriched_scenes=scenes,
                requested_end_frame_scenes=requested_end_frame_scenes,
                viral_concept_brief=session.get("viral_concept_brief"),
                sponsored_content=bool(session.get("sponsored_content")),
                brand_context=session.get("brand_context"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        return True
    except Exception as e:
        logger.exception("production bundle regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации video prompts: {_user_error_message(e)}")
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        return True
    _apply_overlay_title_to_production(session, production_new)
    session["production"] = production_new
    new_plan = production_new.get("json_plan") if isinstance(production_new, dict) else None
    if isinstance(new_plan, list):
        session["frame_prompts"] = _frame_prompts_slice_from_production(new_plan)
        session["frame_prompts_text"] = format_frame_prompts_for_approval(session["frame_prompts"])
    session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
    await update.message.reply_text("Обновила production-промпты по правкам:")
    await _reply_text_chunked(update, format_production_bundle_for_approval(production_new["json_plan"]))
    await _reply_text_with_optional_markup(
        update,
        "«ОК» — перейти к пошаговой генерации по сценам. "
        "Или нажмите «Добавь»/«Исправь» и отправьте новую правку.",
        reply_markup=_production_bundle_approval_markup(),
    )
    return True


async def _handle_waiting_overlay_title_ru_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    ru_title = " ".join(str(text or "").split())
    if not ru_title or ru_title.lower() in {"ок", "ok", "okay", "approve"}:
        await _reply_text_with_optional_markup(
            update,
            "Введите русский заголовок текстом.",
            reply_markup=ok_only_markup(),
        )
        return True
    try:
        await reply_text_without_keyboard(update.message, "Перевожу заголовок на английский, ждите.")
        en_title = await _translate_overlay_title_to_en(
            update,
            context,
            chat_id=chat_id,
            ru_title=ru_title,
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("overlay title translation failed")
        await update.message.reply_text(f"Ошибка перевода заголовка: {_user_error_message(e)}")
        return True
    session["state"] = STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL
    session[OVERLAY_TITLE_TRANSLATION_PENDING_SESSION_KEY] = {"ru": ru_title, "en": en_title}
    SESSIONS[chat_id] = session
    await _reply_text_with_optional_markup(
        update,
        "Предлагаю английский заголовок:\n"
        f"RU: {ru_title}\n"
        f"EN: {en_title}\n\n"
        "Если подходит — нажмите «ОК». Если нужно исправить английский, пришлите правильный EN-текст.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _handle_waiting_overlay_title_en_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    pending = session.get(OVERLAY_TITLE_TRANSLATION_PENDING_SESSION_KEY)
    if not isinstance(pending, dict) or not str(pending.get("ru") or "").strip():
        session["state"] = STATE_WAITING_OVERLAY_TITLE_RU
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Не вижу сохранённого русского заголовка. Введите русский заголовок ещё раз.",
            reply_markup=ok_only_markup(),
        )
        return True

    low = str(text or "").strip().lower()
    ru_title = " ".join(str(pending.get("ru") or "").split())
    en_title = " ".join(str(pending.get("en") or "").split())
    if low in {"ок", "ok", "okay", "approve"}:
        _store_overlay_titles(session, ru_title=ru_title, en_title=en_title)
        session["state"] = STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL
        SESSIONS[chat_id] = session
        return await _ask_overlay_title_style_approval(update, session)
    if low in {"исправь", "fix"}:
        await _reply_text_with_optional_markup(
            update,
            "Пришлите правильный английский заголовок одним сообщением.",
            reply_markup=ok_only_markup(),
        )
        return True

    corrected = str(text or "").strip()
    for prefix in ("EN:", "English:", "Английский:"):
        if corrected.lower().startswith(prefix.lower()):
            corrected = corrected[len(prefix) :].strip()
            break
    corrected = " ".join(corrected.split())
    if not corrected:
        await _reply_text_with_optional_markup(
            update,
            "Пришлите непустой английский заголовок или нажмите «ОК».",
            reply_markup=ok_only_markup(),
        )
        return True
    _store_overlay_titles(session, ru_title=ru_title, en_title=corrected)
    await update.message.reply_text("Английский заголовок исправила.")
    session["state"] = STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL
    SESSIONS[chat_id] = session
    return await _ask_overlay_title_style_approval(update, session)


async def _handle_waiting_overlay_title_style_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    style = _default_overlay_title_style(session)
    low = str(text or "").strip().lower()
    if low in {"ок", "ok", "okay", "approve"}:
        session[OVERLAY_TITLE_STYLE_SESSION_KEY] = style
        set_generation_run_title_color_for_session(session, style.get("title_color"))
        SESSIONS[chat_id] = session
        return await _generate_production_after_overlay_title(
            update,
            context,
            chat_id=chat_id,
            session=session,
        )
    updated = _apply_overlay_title_style_feedback(style, text)
    session[OVERLAY_TITLE_STYLE_SESSION_KEY] = updated
    set_generation_run_title_color_for_session(session, updated.get("title_color"))
    session["state"] = STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL
    SESSIONS[chat_id] = session
    await _send_overlay_title_previews(update, session, updated)
    await _reply_text_with_optional_markup(
        update,
        "Обновила настройки верхнего заголовка:\n"
        f"{_format_overlay_title_style(updated)}\n\n"
        "Если теперь подходит — нажмите «ОК». Или пришлите ещё правку цвет/отступов.",
        reply_markup=ok_only_markup(),
    )
    _ = context
    return True


async def _debounced_session(update: Update) -> tuple[int | None, dict | None, str]:
    """Возвращает актуальную сессию после debounce, либо `(None, None, text)`."""
    if update.message is None or update.effective_chat is None:
        return None, None, ""
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    text = (update.message.text or "").strip()
    if not session:
        return chat_id, None, text
    seq = _DEBOUNCE_SEQ.get(chat_id, 0) + 1
    _DEBOUNCE_SEQ[chat_id] = seq
    await asyncio.sleep(DEBOUNCE_DELAY_SEC)
    if _DEBOUNCE_SEQ.get(chat_id) != seq:
        return chat_id, None, text
    return chat_id, SESSIONS.get(chat_id), text


async def _handle_refining_state(update: Update, state: str) -> bool:
    mapping = {
        STATE_REFINING_CONCEPT: "Дорабатываю концепты. Отправь сообщение еще раз через пару секунд.",
        STATE_REFINING_RECIPE: "Дорабатываю рецепт. Отправь сообщение еще раз через пару секунд.",
        STATE_REFINING_SCENES: "Дорабатываю сцены. Отправь сообщение еще раз через пару секунд.",
        STATE_REFINING_FRAME_PROMPTS: "Дорабатываю frame prompts. Отправь сообщение еще раз через пару секунд.",
        STATE_REFINING_PRODUCTION_BUNDLE: "Перегенерирую промпты. Отправь сообщение ещё раз через пару секунд.",
        STATE_REFINING_SOCIAL_DESCRIPTIONS: "Дорабатываю описания. Отправь сообщение ещё раз через пару секунд.",
    }
    text = mapping.get(state)
    if not text:
        return False
    await update.message.reply_text(text)
    return True


async def _handle_scene_approval_refine(update: Update, context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, session: dict, text: str) -> bool:
    session["state"] = STATE_REFINING_SCENES
    session["user_feedback"] = text
    _update_end_frame_scene_requests(session, text)
    low_text = text.lower()
    if _is_explicit_veo3_speech_format_request(text):
        current = session.get("scenes_plan") or []
        if isinstance(current, list) and current:
            fixed = apply_veo3_lowercase_format(current)
            session["scenes_plan"] = fixed
            session["scenes_text"] = build_human_readable_plan(fixed)
            session["state"] = STATE_WAITING_SCENE_APPROVAL
            await update.message.reply_text("Убрала ударения и привела реплики Veo3 к единому формату с [пауза].")
            await _reply_text_chunked(update, session.get("scenes_text") or "")
            await _reply_text_with_optional_markup(
                update,
                "Если всё ок — нажми «ОК». "
                "Если нужно доработать — нажми «Добавь»/«Исправь» и отправь новую правку.",
                reply_markup=ok_only_markup(),
            )
            return True
    if "добавь сцен" in low_text:
        await update.message.reply_text("Сейчас работает правило 1 шаг рецепта = 1 сцена, поэтому добавить дополнительную сцену нельзя. Могу изменить последнюю сцену на подачу блюда — пришлите правку в этом формате.")
    async def _on_scene_plan_fail_refine(p: dict[str, Any]) -> None:
        if update.message is not None:
            await _reply_text_chunked(update, _format_scenes_plan_attempt_notice(p))
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю план сцен, ждите.")
        scenes = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_scenes_plan",
            lambda: generate_scenes_plan(
                session["selected_concept"],
                full_recipe_text=str(session.get("recipe_full_text") or "").strip(),
                user_feedback=text,
                previous_scenes=session.get("scenes_plan"),
                on_attempt_failure=_on_scene_plan_fail_refine,
                viral_concept_brief=session.get("viral_concept_brief"),
                sponsored_content=bool(session.get("sponsored_content")),
                brand_context=session.get("brand_context"),
                analytics_feedback=session.get("analytics_feedback"),
                romantic_drama_subplot=session.get("romantic_drama_subplot"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e)); session["state"] = STATE_WAITING_SCENE_APPROVAL; return True
    except Exception as e:
        logger.exception("scene regeneration failed"); await update.message.reply_text(f"Ошибка перегенерации сцен: {_user_error_message(e)}"); session["state"] = STATE_WAITING_SCENE_APPROVAL; return True
    session["state"] = STATE_WAITING_SCENE_APPROVAL
    session["viral_concept_brief"] = scenes.get("viral_concept_brief") or session.get("viral_concept_brief") or {}
    session["romantic_drama_subplot"] = scenes.get("romantic_drama_subplot") or {}
    session["retention_review"] = scenes.get("retention_review") or {}
    session["scenes_plan"] = scenes["json_plan"]
    session["scenes_text"] = scenes["text_plan"]
    session["recipe_steps"] = [
        str(s.get("text") or "").strip()
        for s in (scenes.get("json_plan") or [])
        if isinstance(s, dict)
    ]
    report = _format_scenes_regeneration_report(scenes.get("regeneration_events") or [])
    if report: await _reply_text_chunked(update, report)
    await update.message.reply_text("Обновила план по правкам:")
    rft = session.get("recipe_full_text")
    if isinstance(rft, str) and rft.strip(): await _reply_text_chunked(update, format_full_recipe_for_approval(rft))
    if session.get("viral_concept_brief"):
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
        "Если всё ок — нажми «ОК». "
        "Если нужно доработать — нажми «Добавь»/«Исправь» и отправь новую правку.",
        reply_markup=ok_only_markup(),
    )
    return True


async def _handle_frame_prompts_refine(update: Update, context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, session: dict, text: str) -> bool:
    session["state"] = STATE_REFINING_FRAME_PROMPTS
    session["user_feedback"] = text
    _update_end_frame_scene_requests(session, text)
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю frame prompts, ждите.")
        frames = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_frame_prompts",
            lambda: generate_frame_prompts_for_approval(
                session["scenes_plan"],
                user_feedback=text,
                previous_frame_prompts=session.get("frame_prompts"),
                requested_end_frame_scenes=session.get("end_frame_scene_requests"),
                reference_images=_frame_prompt_reference_images_from_session(session),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e)); session["state"] = STATE_WAITING_FRAME_PROMPTS_APPROVAL; return True
    except Exception as e:
        logger.exception("frame prompts refinement failed"); await update.message.reply_text(f"Ошибка доработки frame prompts: {_user_error_message(e)}"); session["state"] = STATE_WAITING_FRAME_PROMPTS_APPROVAL; return True
    session["state"] = STATE_WAITING_FRAME_PROMPTS_APPROVAL
    session["frame_prompts"] = frames["json_plan"]; session["frame_prompts_text"] = frames["text_plan"]
    await update.message.reply_text("Обновила frame prompts по правкам:")
    await _reply_text_chunked(update, format_frame_prompts_for_approval(session["frame_prompts"]))
    await _reply_text_with_optional_markup(
        update,
        "Если всё ок — нажми «ОК». "
        "Если нужно прикрепить изображения — нажми «Добавить». "
        "Если нужно доработать текстом — нажми «Добавь»/«Исправь» и отправь новую правку.",
        reply_markup=_frame_prompts_approval_markup(),
    )
    return True


async def _extract_any_image_from_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    """Возвращает bytes фото или image-документа из сообщения/ответа."""
    image = await _extract_video_source_image(update, context)
    if image:
        return image
    msg = getattr(update, "message", None)
    if msg is None:
        return None
    document = getattr(msg, "document", None)
    if document is None and getattr(msg, "reply_to_message", None) is not None:
        document = getattr(msg.reply_to_message, "document", None)
    if document is None:
        return None
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    if mime_type and not mime_type.startswith("image/"):
        return None
    file_id = getattr(document, "file_id", None)
    if not file_id:
        return None
    tg_file = await context.bot.get_file(file_id)
    data = await tg_file.download_as_bytearray()
    return bytes(data)


async def _generation_source_from_image(image_bytes: bytes, caption: str = "") -> str:
    """Преобразует фото блюда/рецепта в текстовый источник для обычного generate-пайплайна."""
    prompt = (
        "Проанализируй изображение как вход для генерации кулинарного ролика.\n"
        "Если на фото есть рецепт, список ингредиентов, меню, заметка или скриншот, "
        "точно извлеки полезный текст и шаги. Если показано только блюдо, опиши блюдо "
        "и предложи наиболее вероятную идею рецепта без выдумывания точных граммовок.\n"
        "Верни только один связный текст на русском, пригодный как идея или исходный рецепт "
        "для следующего шага генерации, без пояснений о том, что ты анализировал изображение."
    )
    if caption.strip():
        prompt += f"\n\nПодпись пользователя к фото:\n{caption.strip()}"
    return (
        await asyncio.to_thread(
            gemini_generate_sync,
            prompt,
            images=[image_bytes],
        )
    ).strip()


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает изображения для generate, refs image/project image или source video."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id) or {}
    try:
        state = str(session.get("state") or "")
        if state == STATE_WAITING_SCENE_MEDIA and scene_media_reference_images_pending(session):
            image_bytes = await _extract_any_image_from_update(update, context)
            handled = await handle_scene_media_reference_photo(
                update,
                session=session,
                image_bytes=image_bytes,
            )
            if handled:
                SESSIONS[chat_id] = session
                return
        if state in {
            _STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES,
            _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES,
            _STATE_WAITING_FRAME_PROMPT_REFERENCES,
        }:
            image_bytes = await _extract_any_image_from_update(update, context)
            if not image_bytes:
                await update.message.reply_text("Не удалось прочитать изображение. Пришлите фото или image-файл еще раз.")
                return
            if state == _STATE_WAITING_FRAME_PROMPT_REFERENCES:
                count = _append_frame_prompt_reference_image(session, image_bytes)
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    f"Добавила изображение #{count} к frame prompts. "
                    f"Можно прислать ещё или нажать «{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                    reply_markup=_frame_prompt_reference_markup(),
                )
                return
            count = _append_custom_reference_image(session, image_bytes)
            SESSIONS[chat_id] = session
            await _reply_text_with_optional_markup(
                update,
                f"Добавила файл #{count}. Можно прислать ещё или нажать «{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                reply_markup=_custom_reference_markup(),
            )
            return
        if state in {"", _STATE_WAITING_FIRST_STEP_GENERATE_TEXT, STATE_READY_FOR_PRODUCTION}:
            image_bytes = await _extract_any_image_from_update(update, context)
            if not image_bytes:
                await update.message.reply_text("Не удалось прочитать изображение. Пришлите фото ещё раз.")
                return
            await reply_text_without_keyboard(
                update.message,
                "Распознаю идею или рецепт по фото, затем запущу генерацию.",
            )
            try:
                source = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "extract_generation_source_from_image",
                    lambda: _generation_source_from_image(
                        image_bytes,
                        str(getattr(update.message, "caption", "") or ""),
                    ),
                )
            except ServiceUnavailableRetriesExhaustedError as e:
                await update.message.reply_text(str(e))
                return
            except Exception as e:
                logger.exception("generation source extraction from image failed")
                await update.message.reply_text(
                    f"Ошибка распознавания фото: {_user_error_message(e)}"
                )
                return
            if not str(source or "").strip():
                await update.message.reply_text(
                    "Не удалось получить идею или рецепт из фото. Добавьте подпись или пришлите текстом."
                )
                return
            await _start_generation_with_recipe(
                update,
                context,
                recipe=str(source).strip(),
                prefer_vertex_api=False,
            )
            return
        if session.get("state") != _STATE_WAITING_FIRST_STEP_VIDEO_IMAGE:
            return
        duration_sec = int(session.get("first_step_video_duration_sec") or 6)
        prompt = str(session.get("first_step_video_prompt") or "").strip()
        if not prompt:
            await _reply_text_with_optional_markup(
                update,
                "Не вижу сохраненного prompt. Нажмите «Видео» и пришлите сначала текст формата: 6 ваш prompt.",
                reply_markup=first_step_menu_markup(),
            )
            _reset_first_step_session(chat_id, session)
            return
        source_image = await _extract_video_source_image(update, context)
        if not source_image:
            await update.message.reply_text("Не удалось прочитать фото. Пришлите изображение еще раз.")
            return
        await reply_text_without_keyboard(update.message, "Генерируем видео, это может занять некоторое время.")
        try:
            video_bytes = await _generate_video_bytes_with_high_load_retry(
                update,
                prompt=prompt,
                model="veo3_fast",
                duration_sec=duration_sec,
                start_image=source_image,
                end_image=None,
                quality="720",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        except VideoProviderChoiceRequired as pending_choice:
            payload = encode_video_provider_choice_for_session(pending_choice)
            payload["source"] = "video_command"
            payload["duration_for_caption"] = duration_sec
            payload["original_prompt"] = prompt
            session["video_provider_choice_pending"] = payload
            await update.message.reply_text(
                "Syntax не вернул видео.\n\n"
                "Напишите `Syntax`, чтобы повторить через Syntax, или `Vertex`, чтобы попробовать резервный Vertex/Veo."
            )
            return
        except Exception as e:
            logger.exception("first-step video generation failed")
            await update.message.reply_text(f"Ошибка генерации видео: {_user_error_message(e, domain='video')}")
            return
        vb = io.BytesIO(video_bytes)
        vb.name = "video.mp4"
        await update.message.reply_video(video=vb, caption=f"Видео готово ({duration_sec} сек, 9:16, 720, audio).")
        if get_last_video_result_source() == "uri":
            uri = get_last_video_result_uri() or "gs://..."
            await update.message.reply_text(f"Источник результата: video URI ({uri}).")
        await update.message.reply_text(
            "Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        _reset_first_step_session(chat_id, session)
    finally:
        save_session_if_possible(chat_id, SESSIONS.get(chat_id) or session)


async def on_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction_update = getattr(update, "message_reaction", None)
    if reaction_update is None:
        return
    chat = getattr(reaction_update, "chat", None)
    if chat is None:
        return
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return
    session = SESSIONS.get(chat_id)
    if not isinstance(session, dict):
        return
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return
    message_id = getattr(reaction_update, "message_id", None)
    new_reaction = getattr(reaction_update, "new_reaction", None)
    liked = bool(new_reaction)
    resolved = resolve_candidate_by_message(
        session,
        scenes,
        message_id=message_id,
        liked=liked,
    )
    if resolved is None:
        return
    try:
        persist_mark_candidate_liked_by_message(chat_id, message_id, liked)
    except Exception:
        logger.exception(
            "scene_media.reaction_like_persist_failed chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )
    save_session_if_possible(chat_id, session)
    logger.info(
        "scene_media.reaction_like chat_id=%s message_id=%s liked=%s",
        chat_id,
        message_id,
        liked,
    )


async def _on_text_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, session, text = await _debounced_session(update)
    if chat_id is None:
        return
    if update.message is None:
        return
    session = session or SESSIONS.get(chat_id) or {}
    if await _handle_navigation_buttons(update, chat_id=chat_id, session=session, text=text):
        return
    if await _handle_draft_buttons(update, context, chat_id=chat_id, session=session, text=text):
        return
    if str(session.get("state") or "") != STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL:
        persisted = load_session_if_possible(chat_id)
        if (
            isinstance(persisted, dict)
            and str(persisted.get("state") or "") == STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
        ):
            SESSIONS[chat_id] = persisted
            session = persisted
    state = str(session.get("state") or "")
    low = text.lower().strip()
    if state == STATE_READY_FOR_PRODUCTION and _is_first_step_menu_button(text):
        _reset_first_step_session(chat_id, session)
        session = SESSIONS.get(chat_id) or {}
        state = str(session.get("state") or "")

    first_step_states = {
        "",
        _STATE_WAITING_FIRST_STEP_GENERATE_TEXT,
        _STATE_WAITING_FIRST_STEP_DESCRIPTION_TEXT,
        _STATE_WAITING_FIRST_STEP_IMAGE_TEXT,
        _STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES,
        _STATE_WAITING_FIRST_STEP_SOUND_TEXT,
        _STATE_WAITING_FIRST_STEP_VIDEO_TEXT,
        _STATE_WAITING_FIRST_STEP_VIDEO_IMAGE,
        _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION,
        _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_CHARACTERS,
        _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES,
    }
    if state in first_step_states:
        if low == FIRST_STEP_BUTTON_GENERATE.lower():
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_GENERATE_TEXT,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Генерировать». Пришлите идею или рецепт обычным текстом либо отправьте фото — "
                "запущу генерацию без команды.",
                reply_markup=first_step_menu_markup(),
            )
            return
        if low == FIRST_STEP_BUTTON_DESCRIPTION.lower():
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_DESCRIPTION_TEXT,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Описание». Пришлите рецепт или ссылку на страницу с рецептом — "
                "подготовлю готовые описания для публикаций.",
                reply_markup=first_step_menu_markup(),
            )
            return
        if low == FIRST_STEP_BUTTON_IMAGE.lower():
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_IMAGE_TEXT,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Изображение». Пришлите prompt обычным текстом — "
                "сразу сгенерирую картинку без команды.\n"
                "Если нужны свои референсы — нажмите «Добавить свое».",
                reply_markup=image_text_markup(),
            )
            return
        project_image_button = FIRST_STEP_BUTTON_PROJECT_IMAGE.lower()
        if low == project_image_button or low.startswith(f"{project_image_button} "):
            extra = text.strip()[len(FIRST_STEP_BUTTON_PROJECT_IMAGE) :].strip()
            selected = list(_PROJECT_IMAGE_BASE_CHARACTERS)
            for key in _project_image_character_keys_from_text(extra):
                if key not in selected:
                    selected.append(key)
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_CHARACTERS,
                _PROJECT_IMAGE_CHARACTERS_SESSION_KEY: selected,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Изображение проект». Выберите персонажей кнопками.\n"
                f"Сейчас выбраны: {_project_image_character_summary(selected)}.\n"
                f"Когда состав готов — нажмите «{_PROJECT_IMAGE_DONE_BUTTON}».",
                reply_markup=project_image_character_markup(selected),
            )
            return
        if low == FIRST_STEP_BUTTON_SOUND.lower():
            character = _default_sound_character_for_session(session)
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_SOUND_TEXT,
                "first_step_sound_character": character,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Звук». Пришлите текст для озвучки обычным сообщением.\n"
                f"Сейчас используется основной персонаж проекта: {character}.",
                reply_markup=first_step_menu_markup(),
            )
            return
        if low == FIRST_STEP_BUTTON_VIDEO.lower():
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_VIDEO_TEXT,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Режим «Видео». Шаг 1/2: пришлите текст формата:\n"
                "6 Крупный план бабушки и кота с готовым блюдом\n"
                "(первое число — секунды, дальше prompt).",
                reply_markup=first_step_menu_markup(),
            )
            return
        if low == FIRST_STEP_BUTTON_PROJECT.lower():
            SESSIONS[chat_id] = {
                **session,
                "state": _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION,
                PROJECT_KEY_SESSION: str(session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING),
            }
            await _reply_text_with_optional_markup(
                update,
                "Выберите проект кнопкой. Это каркас — позже добавим больше проектов.",
                reply_markup=project_choice_markup(),
            )
            return
        if state == _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_CHARACTERS:
            selected = _project_image_selected_characters(session)
            if low == _CUSTOM_REFERENCE_ADD_BUTTON.lower():
                session["state"] = _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES
                session[_PROJECT_IMAGE_CHARACTERS_SESSION_KEY] = selected
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    "Пришлите одно или несколько фото/image-файлов.\n"
                    f"{_custom_reference_count_text(session)}\n"
                    f"Когда закончите — нажмите «{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                    reply_markup=_custom_reference_markup(),
                )
                return
            if low == _PROJECT_IMAGE_DONE_BUTTON:
                session["state"] = _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_PROMPT
                session[_PROJECT_IMAGE_CHARACTERS_SESSION_KEY] = selected
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    "Состав зафиксирован.\n"
                    f"Персонажи: {_project_image_character_summary(selected)}.\n"
                    "Теперь пришлите prompt для изображения.",
                    reply_markup=first_step_menu_markup(),
                )
                return
            normalized_text = text.strip()
            if normalized_text.startswith("✓ "):
                normalized_text = normalized_text[2:].strip()
            added = False
            for key in _project_image_character_keys_from_text(normalized_text):
                if key not in selected:
                    selected.append(key)
                    added = True
            session[_PROJECT_IMAGE_CHARACTERS_SESSION_KEY] = selected
            SESSIONS[chat_id] = session
            if added:
                message = f"Добавила. Сейчас выбраны: {_project_image_character_summary(selected)}."
            else:
                message = (
                    f"Сейчас выбраны: {_project_image_character_summary(selected)}.\n"
                    f"Выберите ещё персонажей или нажмите «{_PROJECT_IMAGE_DONE_BUTTON}»."
                )
            await _reply_text_with_optional_markup(
                update,
                message,
                reply_markup=project_image_character_markup(selected),
            )
            return
        if state == _STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES:
            if low == _CUSTOM_REFERENCE_DONE_BUTTON.lower():
                session["state"] = _STATE_WAITING_FIRST_STEP_IMAGE_TEXT
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    f"{_custom_reference_count_text(session)} Теперь пришлите prompt для изображения.",
                    reply_markup=image_text_markup(),
                )
                return
            await _reply_text_with_optional_markup(
                update,
                "Жду фото или image-файл. Можно прислать несколько файлов, затем нажать "
                f"«{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                reply_markup=_custom_reference_markup(),
            )
            return
        if state == _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES:
            if low == _CUSTOM_REFERENCE_DONE_BUTTON.lower():
                selected = _project_image_selected_characters(session)
                session["state"] = _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_CHARACTERS
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    f"{_custom_reference_count_text(session)}\n"
                    f"Персонажи: {_project_image_character_summary(selected)}.\n"
                    f"Выберите ещё персонажей или нажмите «{_PROJECT_IMAGE_DONE_BUTTON}».",
                    reply_markup=project_image_character_markup(selected),
                )
                return
            await _reply_text_with_optional_markup(
                update,
                "Жду фото или image-файл. Можно прислать несколько файлов, затем нажать "
                f"«{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                reply_markup=_custom_reference_markup(),
            )
            return
        if state == _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION:
            if text.strip() in {PROJECT_LABEL_GRANDMA_CAT_COOKING, PROJECT_LEGACY_LABEL_GRANDMA_CAT_COOKING}:
                session[PROJECT_KEY_SESSION] = PROJECT_KEY_GRANDMA_CAT_COOKING
                session["state"] = ""
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    f"Проект выбран: {PROJECT_LABEL_GRANDMA_CAT_COOKING}",
                    reply_markup=first_step_menu_markup(),
                )
                return
            if text.strip().lower() == PROJECT_BUTTON_BACK.lower():
                session["state"] = ""
                session[PROJECT_KEY_SESSION] = str(
                    session.get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING
                )
                SESSIONS[chat_id] = session
                await _reply_text_with_optional_markup(
                    update,
                    "Вернулась в меню первого шага.",
                    reply_markup=first_step_menu_markup(),
                )
                return
            await _reply_text_with_optional_markup(
                update,
                "Нажмите кнопку проекта или «Назад».",
                reply_markup=project_choice_markup(),
            )
            return
        if state == "":
            await _start_generation_with_recipe(
                update,
                context,
                recipe=text.strip(),
                prefer_vertex_api=False,
            )
            return
    if not session:
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_GENERATE_TEXT:
        if not text.strip():
            await _reply_text_with_optional_markup(
                update,
                "Пришлите идею или рецепт текстом для запуска генерации.",
                reply_markup=first_step_menu_markup(),
            )
            return
        await _start_generation_with_recipe(
            update,
            context,
            recipe=text.strip(),
            prefer_vertex_api=False,
        )
        return
    if session.get("state") == STATE_WAITING_SERIES_CHOICE:
        await _handle_waiting_series_choice_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == STATE_WAITING_SERIES_CREATE:
        await _handle_waiting_series_create_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == STATE_WAITING_SERIES_TRANSLATION_APPROVAL:
        await _handle_waiting_series_translation_approval_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == STATE_WAITING_OVERLAY_TITLE_RU:
        await _handle_waiting_overlay_title_ru_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL:
        await _handle_waiting_overlay_title_en_approval_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL:
        await _handle_waiting_overlay_title_style_approval_state(
            update,
            context,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_DESCRIPTION_TEXT:
        if not text.strip():
            await _reply_text_with_optional_markup(
                update,
                "Пришлите рецепт текстом или ссылку на страницу с рецептом.",
                reply_markup=first_step_menu_markup(),
            )
            return
        await _start_description_with_recipe(
            update,
            context,
            recipe=text.strip(),
        )
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_IMAGE_TEXT:
        prompt = text.strip()
        if prompt.lower() == _CUSTOM_REFERENCE_ADD_BUTTON.lower():
            session["state"] = _STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES
            SESSIONS[chat_id] = session
            await _reply_text_with_optional_markup(
                update,
                "Пришлите одно или несколько фото/image-файлов.\n"
                f"{_custom_reference_count_text(session)}\n"
                f"Когда закончите — нажмите «{_CUSTOM_REFERENCE_DONE_BUTTON}».",
                reply_markup=_custom_reference_markup(),
            )
            return
        if not prompt:
            await _reply_text_with_optional_markup(
                update,
                "Пришлите prompt текстом для генерации изображения.",
                reply_markup=image_text_markup(),
            )
            return
        await reply_text_without_keyboard(update.message, "Генерируем изображение, ждите.")
        try:
            refs = _custom_reference_images_from_session(session)
            if refs:
                image_bytes = await _generate_image_with_lenient_fallback(prompt, reference_images=refs)
            else:
                image_bytes = await _generate_image_with_lenient_fallback(prompt)
        except Exception as e:
            logger.exception("first-step image generation failed")
            await update.message.reply_text(f"Ошибка генерации изображения: {_user_error_message(e)}")
            return
        bio = io.BytesIO(image_bytes)
        bio.name = "image.png"
        await update.message.reply_photo(photo=bio, caption="Изображение готово.")
        await update.message.reply_text(
            "Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        _reset_first_step_session(chat_id, session)
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_SOUND_TEXT:
        phrase = text.strip()
        if not phrase:
            await _reply_text_with_optional_markup(
                update,
                "Пришлите текст для озвучки.",
                reply_markup=first_step_menu_markup(),
            )
            return
        try:
            await _generate_first_step_sound(update, phrase, session)
        except Exception as e:
            logger.exception("first-step sound generation failed")
            await update.message.reply_text(f"Ошибка генерации звука: {_user_error_message(e)}")
            return
        _reset_first_step_session(chat_id, session)
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_PROMPT:
        prompt = text.strip()
        if not prompt:
            await _reply_text_with_optional_markup(
                update,
                "Пришлите prompt текстом для генерации изображения проекта.",
                reply_markup=first_step_menu_markup(),
            )
            return
        characters = _project_image_selected_characters(session)
        try:
            await _generate_project_image(
                update,
                prompt,
                characters,
                custom_reference_images=_custom_reference_images_from_session(session),
            )
        except Exception as e:
            logger.exception("first-step project image generation failed")
            await update.message.reply_text(f"Ошибка генерации изображения проекта: {_user_error_message(e)}")
            return
        await update.message.reply_text(
            "Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        _reset_first_step_session(chat_id, session)
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_VIDEO_TEXT:
        raw = text.strip()
        if not raw:
            await _reply_text_with_optional_markup(
                update,
                "Пришлите строку формата: 6 ваш prompt для видео.",
                reply_markup=first_step_menu_markup(),
            )
            return
        try:
            duration_sec, prompt = _parse_video_command_args(f"/video {raw}")
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        session["state"] = _STATE_WAITING_FIRST_STEP_VIDEO_IMAGE
        session["first_step_video_duration_sec"] = duration_sec
        session["first_step_video_prompt"] = prompt
        await _reply_text_with_optional_markup(
            update,
            "Шаг 2/2: пришлите фото отдельным сообщением "
            "(или reply на существующее фото) — сразу запущу генерацию видео.",
            reply_markup=first_step_menu_markup(),
        )
        return
    if session.get("state") == _STATE_WAITING_FIRST_STEP_VIDEO_IMAGE:
        raw = text.strip()
        try:
            duration_sec, prompt = _parse_video_command_args(f"/video {raw}")
        except ValueError:
            duration_sec = None
            prompt = ""
        if duration_sec is not None and prompt:
            session["first_step_video_duration_sec"] = duration_sec
            session["first_step_video_prompt"] = prompt
            await _reply_text_with_optional_markup(
                update,
                "Обновила параметры. Теперь пришлите фото для шага 2/2.",
                reply_markup=first_step_menu_markup(),
            )
            return
        await _reply_text_with_optional_markup(
            update,
            "Жду фото для шага 2/2. Если хотите поменять параметры, "
            "пришлите новую строку формата: 6 ваш prompt.",
            reply_markup=first_step_menu_markup(),
        )
        return
    if await _handle_video_provider_choice_pending_video_command(
        update, context, chat_id=chat_id, session=session, text=text
    ):
        return
    if await _handle_syntax_fallback_pending_video_command(
        update, context, chat_id=chat_id, session=session, text=text
    ):
        return
    state = session.get("state")
    logger.info("chat=%s incoming_state=%s text=%s", chat_id, state, text[:80])
    if state == _STATE_WAITING_FRAME_PROMPT_REFERENCES:
        await _handle_waiting_frame_prompt_references_state(
            update,
            chat_id=chat_id,
            session=session,
            text=text,
        )
        return
    handled, normalized_text = await _handle_guided_feedback_buttons(
        update,
        session=session,
        state=state,
        text=text,
    )
    if handled:
        return
    text = normalized_text
    if state == STATE_WAITING_CONCEPT and await _handle_waiting_concept_state(update, context, chat_id=chat_id, session=session, text=text):
        return
    if state == STATE_WAITING_RECIPE_APPROVAL and await _handle_waiting_recipe_approval_state(update, context, chat_id=chat_id, session=session, text=text):
        return
    if await _handle_refining_state(update, state):
        return
    if state == STATE_WAITING_SCENE_APPROVAL:
        if await _handle_waiting_scene_approval_state(update, context, chat_id=chat_id, session=session, text=text):
            return
        await _handle_scene_approval_refine(update, context, chat_id=chat_id, session=session, text=text)
        return
    if state == STATE_WAITING_FRAME_PROMPTS_APPROVAL:
        if await _handle_waiting_frame_prompts_approval_state(update, context, chat_id=chat_id, session=session, text=text):
            return
        await _handle_frame_prompts_refine(update, context, chat_id=chat_id, session=session, text=text)
        return
    if state == STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL and await _handle_waiting_production_bundle_approval_state(update, context, chat_id=chat_id, session=session, text=text):
        return
    if state == STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL and await _handle_waiting_social_descriptions_approval_state(update, context, chat_id=chat_id, session=session, text=text):
        return
    if state == STATE_WAITING_SCENE_MEDIA and await _handle_waiting_scene_media_state(update, context, chat_id=chat_id, session=session, text=text):
        return
    if state == STATE_READY_FOR_PRODUCTION:
        await update.message.reply_text(
            "Production уже готов. Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        return

    # keep legacy no-op for unknown states
    return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    message = getattr(update, "message", None)
    message_text = str(getattr(message, "text", "") or "").strip().lower()
    navigating_previous = message_text == NAV_BUTTON_PREVIOUS_STEP.lower()
    session_before = get_loaded_session(chat_id)
    signature_before = session_step_signature(session_before) if isinstance(session_before, dict) else None
    try:
        await _on_text_impl(update, context)
    finally:
        session_after = get_loaded_session(chat_id)
        if isinstance(session_after, dict):
            signature_after = session_step_signature(session_after)
            if signature_after != signature_before and not navigating_previous:
                record_checkpoint(chat_id, session_after)
            save_loaded_session_if_possible(chat_id)
