import re

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
from .handlers_keyboards import reply_text_without_keyboard, safe_reply_text
from ..assets import ASSETS
from ..elevenlabs_tts import generate_elevenlabs_speech
from ..llm_utils import gemini_generate_sync
from ..scene_media_candidates import resolve_candidate_by_message
from .handlers_state_start import (
    _concept_choice_markup,
    _handle_waiting_concept_state,
    _handle_waiting_recipe_approval_state,
    _start_description_with_recipe,
    _start_generation_with_recipe,
)
from ..state import get_loaded_session, save_loaded_session_if_possible
from .handlers_state_media import (
    STATE_REFINING_SOCIAL_DESCRIPTIONS,
    STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL,
    _handle_waiting_scene_media_state,
    _handle_waiting_social_descriptions_approval_state,
)
from ..character_catalog import format_character_short_descriptions
from ..pipeline_history import record_checkpoint, restore_previous_checkpoint, session_step_signature
from ..scene_media_store import mark_candidate_liked_by_message as persist_mark_candidate_liked_by_message
from ..session_store import save_session_if_possible


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
_PROJECT_IMAGE_DONE_BUTTON = "больше никто не нужен"
_PROJECT_IMAGE_CHARACTERS_SESSION_KEY = "first_step_project_image_characters"
_PROJECT_IMAGE_BASE_CHARACTERS = ("grandma", "cat")
_CUSTOM_REFERENCES_SESSION_KEY = "first_step_custom_reference_images"
_CUSTOM_REFERENCE_ADD_BUTTON = "Добавить свое"
_CUSTOM_REFERENCE_DONE_BUTTON = "больше файлов не нужно"
_PROJECT_MAIN_CHARACTER_BY_PROJECT = {
    PROJECT_KEY_GRANDMA_CAT_COOKING: "grandma",
}


def keyboard_for_session_state(session: dict):
    """Return the reply keyboard expected for the current visible state."""
    state = str((session or {}).get("state") or "")
    if state == _STATE_WAITING_FIRST_STEP_PROJECT_SELECTION:
        return project_choice_markup()
    if state == "":
        return first_step_menu_markup()
    if state == STATE_WAITING_CONCEPT:
        concepts = session.get("concepts")
        return _concept_choice_markup(concepts if isinstance(concepts, list) else [])
    if state in {
        STATE_WAITING_RECIPE_APPROVAL,
        STATE_WAITING_SCENE_APPROVAL,
        STATE_WAITING_FRAME_PROMPTS_APPROVAL,
    }:
        return ok_only_markup()
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
        project_image_button,
    } or low.startswith(f"{project_image_button} ")


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
        session["production"] = None
        session.pop("image_scene_index", None)
        session.pop("scene_media_substep", None)
        return
    if step == "production":
        session["production"] = None
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
                "Вернула на предыдущий фактический подшаг текущей сцены."
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
            await update.message.reply_text("Вернула к промпту конечного кадра текущей сцены.")
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
            await update.message.reply_text("Вернула к предыдущему этапу стартового кадра текущей сцены.")
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
            await update.message.reply_text("Вернула к промпту стартового кадра текущей сцены.")
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

    restored = restore_previous_checkpoint(chat_id, session)
    if restored is not None:
        state_after_restore = str(session.get("state") or "")
        if state_after_restore == STATE_WAITING_SCENE_MEDIA:
            await update.message.reply_text("Вернула на предыдущий сохранённый шаг.")
            await _send_next_scene_media_step(update, session)
            return True
        await _reply_text_with_optional_markup(
            update,
            "Вернула на предыдущий сохранённый шаг.",
            reply_markup=keyboard_for_session_state(session),
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
        if ("все сцен" in chunk or "кажд" in chunk) and total_scenes > 0:
            numbers.update(range(1, total_scenes + 1))
        numbers = {number for number in numbers if 1 <= number <= total_scenes}
        if not numbers:
            continue
        is_remove = any(marker in chunk for marker in _END_FRAME_REMOVE_MARKERS) or (
            any(action in chunk for action in _END_FRAME_REMOVE_ACTIONS)
            and any(marker in chunk for marker in _END_FRAME_FEEDBACK_MARKERS)
        )
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


async def _handle_waiting_scene_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        try:
            await reply_text_without_keyboard(update.message, "Генерирую production-промпты, ждите.")
            production = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_full_production",
                lambda: generate_full_production(
                    session["scenes_plan"],
                    approved_frame_prompts=None,
                    requested_end_frame_scenes=session.get("end_frame_scene_requests"),
                    viral_concept_brief=session.get("viral_concept_brief"),
                    sponsored_content=bool(session.get("sponsored_content")),
                    brand_context=session.get("brand_context"),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("production failed after scene approval")
            await update.message.reply_text(f"Ошибка генерации промптов (кадры + видео): {_user_error_message(e)}")
            return True
        plan = production.get("json_plan") if isinstance(production, dict) else None
        if not isinstance(plan, list) or not plan:
            await update.message.reply_text("Модель не вернула production-план. Попробуйте ещё раз или начните с /generate.")
            return True
        session["state"] = STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL
        session["production"] = production
        session["frame_prompts"] = _frame_prompts_slice_from_production(plan)
        session["frame_prompts_text"] = format_frame_prompts_for_approval(session["frame_prompts"])
        await update.message.reply_text(
            "Сценарий утверждён. Ниже — все промпты по сценам: START / END кадры и video (одним запросом к модели). "
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
    return False


async def _handle_waiting_frame_prompts_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        try:
            await reply_text_without_keyboard(update.message, "Генерирую production-промпты, ждите.")
            production = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_full_production",
                lambda: generate_full_production(
                    session["scenes_plan"],
                    approved_frame_prompts=session.get("frame_prompts"),
                    requested_end_frame_scenes=session.get("end_frame_scene_requests"),
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
        "Если нужно доработать — нажми «Добавь»/«Исправь» и отправь новую правку.",
        reply_markup=ok_only_markup(),
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
        if state in {
            _STATE_WAITING_FIRST_STEP_IMAGE_REFERENCES,
            _STATE_WAITING_FIRST_STEP_PROJECT_IMAGE_REFERENCES,
        }:
            image_bytes = await _extract_any_image_from_update(update, context)
            if not image_bytes:
                await update.message.reply_text("Не удалось прочитать изображение. Пришлите фото или image-файл еще раз.")
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
