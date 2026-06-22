import asyncio
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import NetworkError, RetryAfter, TimedOut

from ..character_catalog import sanitize_character_names_and_keys
from ..pipeline_llm_steps import RUSSIAN_YO_RULE
from .handlers_state_deps import (
    ContextTypes,
    SESSIONS,
    add_scene_language_cost,
    add_scene_shared_cost,
    SCENE_MEDIA_AI_APPROVE_BUTTON,
    SCENE_MEDIA_APPROVE_MODE_AI,
    SCENE_MEDIA_APPROVE_MODE_AUTO,
    SCENE_MEDIA_APPROVE_MODE_MANUAL,
    SCENE_MEDIA_APPROVE_MODE_SESSION_KEY,
    SCENE_MEDIA_AUTO_APPROVE_BUTTON,
    SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY,
    SCENE_MEDIA_CHANGE_SPEED_BUTTON,
    SCENE_MEDIA_REQUEST_END_FRAME_BUTTON,
    SCENE_MEDIA_SKIP_END_FRAME_BUTTON,
    SCENE_MEDIA_STOP_BUTTON,
    SCENE_MEDIA_SUBSTEP_END,
    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_START,
    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_VIDEO,
    SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON,
    STATE_READY_FOR_PRODUCTION,
    SyntaxFallbackPendingConfirmation,
    VideoProviderChoiceRequired,
    Update,
    _append_reference_style_to_image_prompt,
    _append_scene_identity_to_image_prompt,
    _attempt_syntax_video_fallback_batch,
    _call_with_503_retries,
    _decode_b64,
    _encode_b64,
    _end_frame_delta_instruction,
    _extract_format_or_feedback,
    _extract_prompt_override,
    enforce_video_prompt_voice_requirements,
    encode_video_provider_choice_for_session,
    encode_syntax_fallback_pending_for_session,
    _generate_image_with_lenient_fallback,
    _generate_image_with_lenient_fallback_result,
    _generate_video_bytes_with_high_load_retry,
    _image_end_scene_prompt,
    _image_scene_prompt,
    _is_video_policy_error,
    _is_video_prompt_filtered_error,
    _is_video_safety_error,
    _load_scene_reference_images,
    _next_image_quality_model,
    _regenerate_end_frame_prompt_from_feedback,
    _regenerate_video_prompt_from_error,
    _rewrite_video_prompt_by_format_instruction,
    run_vertex_fallback_from_session_payload,
    run_syntax_fallback_from_session_payload,
    get_last_video_provider,
    _scene_needs_end_frame_step,
    _send_next_scene_media_step,
    _set_end_frame_text,
    syntax_fallback_parse_user_confirmation,
    video_provider_choice_parse_user_selection,
    _user_error_message,
    _should_suggest_regenerate_start_frame_after_video_prompt_fails,
    scene_end_approval_markup,
    scene_generated_image_approval_markup,
    scene_image_approval_markup,
    scene_language_approval_markup,
    scene_video_approval_markup,
    video_policy_retry_markup,
    _video_policy_prompt_approval_text,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
    estimated_scene_video_cost,
    estimated_session_total_media_cost,
    estimate_image_generation_cost_usd,
    save_video_result_estimate,
    io,
    gemini_generate_sync,
    logger,
    ServiceUnavailableRetriesExhaustedError,
)
from ..ai_approval_service import (
    AI_APPROVAL_STATUS_APPROVE,
    AI_APPROVAL_STATUS_HUMAN_REVIEW,
    AI_APPROVAL_STATUS_REGENERATE_IMAGE,
    AI_APPROVAL_STATUS_REGENERATE_VIDEO,
    _video_duration_seconds,
    ai_approval_history_entry,
    check_scene_prompts,
    check_start_frame,
    check_video,
    format_ai_approval_summary,
    get_ai_approval_config,
)
from ..artifact_store import persist_post_production_artifacts
from .handlers_state_commands import _regenerate_image_prompt_from_feedback
from .handlers_keyboards import first_step_menu_markup, reply_text_without_keyboard, safe_send_message
from ..llm_utils import extract_json_object
from ..multilingual import normalize_language_code
from ..post_production import build_post_production, probe_video_bytes_dimensions
from ..publications import publication_jobs_summary, queue_publication_bundle
from ..scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    image_candidates_for_kind,
    latest_scene_video_candidate,
    mark_image_candidate_approved,
    mark_video_candidate_approved,
    parse_speed_coef,
    register_image_candidate,
    register_media_message_candidate,
    register_video_candidate,
    resolve_scene_image_for_video,
    resolve_scene_video_for_production,
    selected_image_bytes_for_video,
)
from ..scene_media_store import (
    persist_image_candidate,
    persist_image_candidate_state,
    persist_message_link,
    persist_video_candidate,
    persist_video_candidate_state,
)
from ..scene_speech_budget import (
    append_prompt_timing_hints,
    build_default_speech_plan,
    build_speech_cues_from_plan,
    max_speech_chars_for_duration,
    normalize_speech_phrase_for_voice,
    speech_budget_for_duration,
    speech_chars_excluding_pauses,
)
from ..scene_speech_shorten import shorten_scene_speech_and_sync_video_prompt_sync
from ..syntax_video_fallback import BatchFallbackRequest
from ..db import SessionLocal
from ..vertex_images import primary_image_generation_model

SCENE_SPEECH_WAIT_TIMEOUT_SEC = 600
SCENE_SPEECH_OPTION_RETRY = "Перегенерировать ещё раз"
SCENE_SPEECH_OPTION_WAIT = "Жду фразу от пользователя"
SCENE_SPEECH_OPTION_APPROVE = "ОК, запускай видео"
SCENE_SPEECH_OPTION_EDIT = "Изменить фразу"
SCENE_SPEECH_OPTION_KEEP_AS_IS = "Оставить как есть"
SCENE_SPEECH_OPTION_SHORTEN_AI = "Укоротить нейросетью"
SCENE_SPEECH_PENDING_KEY = "scene_speech_shorten_pending"
SCENE_PHRASE_INPUT_PENDING_KEY = "scene_phrase_input_pending"
SCENE_PHRASE_TRANSLATION_PENDING_KEY = "scene_phrase_translation_pending"
SCENE_PHRASE_ORIGIN_KEY = "speech_origin_by_lang"
SCENE_SPEECH_BUDGET_OVERRIDE_KEY = "speech_budget_override_by_lang"
SOCIAL_DESCRIPTIONS_SENT_KEY = "social_descriptions_sent"
STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL = "waiting_social_descriptions_approval"
STATE_REFINING_SOCIAL_DESCRIPTIONS = "refining_social_descriptions"
SOCIAL_DESCRIPTIONS_PENDING_KEY = "social_descriptions_pending"
SCENE_IMAGE_EDIT_PENDING_KEY = "scene_image_edit_pending"
SCENE_VIDEO_FEEDBACK_PENDING_KEY = "scene_video_feedback_pending"
SCENE_VIDEO_SPEED_PENDING_KEY = "scene_video_speed_pending"
SCENE_PROMPT_INPUT_PENDING_KEY = "scene_prompt_input_pending"
SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY = "image_model_choice_pending"
SCENE_IMAGE_MODEL_RETRY_BUTTON = "ПОВТОРИТЬ СИЛЬНУЮ"
SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON = "ПОНИЗИТЬ МОДЕЛЬ"
SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY = "scene_media_ai_human_review"
START_IMAGE_MODEL_KEY = "start_frame_image_model"
END_IMAGE_MODEL_KEY = "end_frame_image_model"
EDITED_IMAGE_MODEL_KEY = "edited_image_model"
START_PROMPT_RAW_MODE_KEY = "start_frame_prompt_raw_mode"
END_PROMPT_RAW_MODE_KEY = "end_frame_prompt_raw_mode"
VIDEO_PROMPT_RAW_BY_LANG_KEY = "video_prompt_raw_by_lang"
END_FRAME_REQUESTED_KEY = "end_frame_requested"
_SHOT_CLOSEUP_MARKERS = ("крупный план", "крупно", "close-up", "close up")
_SHOT_MEDIUM_MARKERS = ("средний план", "medium shot", "medium")
SOCIAL_FIXED_HASHTAGS_RU = ["#убабушкивкуснее", "#бабушкинырецепты", "#рецепты"]
SOCIAL_FIXED_HASHTAGS_EN = ["#grandmatastesbetter", "#grandmasrecipes", "#recipes"]
SOCIAL_COPY_RU_KEYS = {
    "youtube_ru_description",
    "rutube_ru_description",
    "instagram_ru",
    "vk_ru",
    "tiktok_ru",
}
SOCIAL_COPY_EN_KEYS = {
    "instagram_en",
    "youtube_en_description",
}
_SCENE_SPEECH_TIMEOUT_TASKS: dict[int, asyncio.Task[None]] = {}
NAV_BUTTON_PREVIOUS_STEP = "Предыдущий шаг"
NAV_BUTTON_START_OVER = "В начало"
_SEND_RETRY_ATTEMPTS = 3
_SEND_RETRY_BASE_DELAY_SEC = 1.0
_POST_PRODUCTION_RETRY_ATTEMPTS = 2
_POST_PRODUCTION_RETRY_DELAY_SEC = 2.0


@dataclass(frozen=True)
class SceneMediaKeyboardSnapshot:
    """Declarative description of the keyboard required by the current scene-media state."""

    state: str
    rows: tuple[tuple[str, ...], ...]
    markup: object


SCENE_MEDIA_KEYBOARD_STATE_NAMES = frozenset(
    {
        "scene_media.start.prompt",
        "scene_media.start.image_approval",
        "scene_media.end.prompt",
        "scene_media.end.image_approval",
        "scene_media.video.prompt",
        "scene_media.video.provider_choice",
        "scene_media.video.syntax_confirmation",
        "scene_media.image.model_choice",
        "scene_media.video.speech.choice",
        "scene_media.video.speech.manual_phrase",
        "scene_media.video.speech.approval",
        "scene_media.video.speech.approval_manual_phrase",
        "scene_media.video.speech.user_long_phrase_choice",
        "scene_media.video.translation.confirm",
        "scene_media.video.translation.approval",
        "scene_media.video.translation.revision",
        "scene_media.input.phrase_input",
        "scene_media.input.feedback_input",
        "scene_media.input.speed_input",
        "scene_media.input.prompt_input",
        "scene_media.input.image_edit_input",
        "scene_media.video.policy_retry",
        "scene_media.video.language_approval",
        "scene_media.complete",
    }
)


def _keyboard_rows(markup: object) -> tuple[tuple[str, ...], ...]:
    keyboard = getattr(markup, "keyboard", None)
    if not isinstance(keyboard, (list, tuple)):
        return ()
    return tuple(
        tuple(str(getattr(button, "text", button)) for button in row)
        for row in keyboard
    )


def _keyboard_snapshot(state: str, markup: object) -> SceneMediaKeyboardSnapshot:
    if state not in SCENE_MEDIA_KEYBOARD_STATE_NAMES:
        raise ValueError(
            f"Unknown scene-media keyboard state {state!r}. "
            "Register it in SCENE_MEDIA_KEYBOARD_STATE_NAMES and review the keyboard schema."
        )
    return SceneMediaKeyboardSnapshot(state=state, rows=_keyboard_rows(markup), markup=markup)


def _syntax_recovery_markup(languages: list[str] | None = None) -> ReplyKeyboardMarkup:
    rows: list[list[str]] = [["Syntax"], ["Vertex"], ["ФИДБЕК:"]]
    langs = [str(lang).strip().lower() for lang in (languages or []) if str(lang).strip()]
    if langs:
        for lang in langs:
            rows.append([f"ПРОМПТ {lang}:"])
        for lang in langs:
            rows.append([f"ФРАЗА {lang}:"])
    else:
        rows.append(["ПРОМПТ:"])
        rows.append(["ФРАЗА:"])
    rows.append([NAV_BUTTON_PREVIOUS_STEP])
    rows.append([NAV_BUTTON_START_OVER])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def scene_media_keyboard_snapshot(session: dict) -> SceneMediaKeyboardSnapshot:
    """
    Resolve the one keyboard allowed by the current scene-media state.

    Pending decisions have priority over the base substep. This prevents stale
    Syntax/Vertex buttons from surviving after the pending payload is cleared.
    """
    scenes, idx = _scene_media_context(session)
    if not scenes or idx >= len(scenes):
        return _keyboard_snapshot("scene_media.complete", first_step_menu_markup())
    scene = scenes[idx]
    languages = _scene_target_languages(session, scene)

    provider_pending = scene.get("video_provider_choice_pending")
    if isinstance(provider_pending, dict):
        pending_languages = (
            [
                normalize_language_code(str(language)) or str(language)
                for language in provider_pending.get("languages", [])
                if str(language).strip()
            ]
            if provider_pending.get("source") == "scene_batch"
            else [normalize_language_code(str(provider_pending.get("language") or "")) or languages[0]]
        )
        return _keyboard_snapshot(
            "scene_media.video.provider_choice",
            _syntax_recovery_markup(pending_languages),
        )

    if isinstance(scene.get("syntax_fallback_pending"), dict):
        return _keyboard_snapshot(
            "scene_media.video.syntax_confirmation",
            ReplyKeyboardMarkup(
                [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                resize_keyboard=True,
                one_time_keyboard=False,
            ),
        )

    image_model_pending = scene.get(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY)
    if isinstance(image_model_pending, dict):
        return _keyboard_snapshot(
            "scene_media.image.model_choice",
            _scene_image_model_retry_markup(
                lower_model=str(image_model_pending.get("lower_model") or "").strip() or None
            ),
        )

    speech_pending = session.get(SCENE_SPEECH_PENDING_KEY)
    if isinstance(speech_pending, dict) and int(speech_pending.get("scene_idx", -1)) == idx:
        stage = str(speech_pending.get("stage") or "choice")
        if stage == "approval":
            markup = _scene_speech_approval_markup()
        elif stage == "approval_manual_phrase":
            markup = ReplyKeyboardRemove()
        elif stage == "user_long_phrase_choice":
            markup = _scene_speech_user_long_phrase_markup()
        else:
            markup = _scene_speech_options_markup()
        return _keyboard_snapshot(f"scene_media.video.speech.{stage}", markup)

    translation_pending = session.get(SCENE_PHRASE_TRANSLATION_PENDING_KEY)
    if isinstance(translation_pending, dict) and int(translation_pending.get("scene_idx", -1)) == idx:
        stage = str(translation_pending.get("stage") or "confirm")
        if stage == "revision":
            markup = ReplyKeyboardMarkup(
                [["ФИДБЕК:"], ["ФРАЗА:"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                resize_keyboard=True,
                one_time_keyboard=False,
            )
        else:
            markup = ReplyKeyboardMarkup(
                [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                resize_keyboard=True,
                one_time_keyboard=False,
            )
        return _keyboard_snapshot(f"scene_media.video.translation.{stage}", markup)

    text_input_pending_keys = (
        (SCENE_PHRASE_INPUT_PENDING_KEY, "phrase_input"),
        (SCENE_VIDEO_FEEDBACK_PENDING_KEY, "feedback_input"),
        (SCENE_VIDEO_SPEED_PENDING_KEY, "speed_input"),
        (SCENE_PROMPT_INPUT_PENDING_KEY, "prompt_input"),
        (SCENE_IMAGE_EDIT_PENDING_KEY, "image_edit_input"),
    )
    for pending_key, state_suffix in text_input_pending_keys:
        pending = session.get(pending_key)
        if isinstance(pending, dict) and int(pending.get("scene_idx", -1)) == idx:
            return _keyboard_snapshot(
                f"scene_media.input.{state_suffix}",
                ReplyKeyboardRemove(),
            )

    substep = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
    if substep == SCENE_MEDIA_SUBSTEP_START:
        return _keyboard_snapshot(
            "scene_media.start.prompt",
            scene_image_approval_markup(session=session),
        )
    if substep == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        return _keyboard_snapshot(
            "scene_media.start.image_approval",
            scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "start"),
                include_end_frame_choice=_scene_has_available_end_frame_prompt(scene),
                session=session,
            ),
        )
    if substep == SCENE_MEDIA_SUBSTEP_END:
        return _keyboard_snapshot(
            "scene_media.end.prompt",
            scene_end_approval_markup(session=session),
        )
    if substep == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        return _keyboard_snapshot(
            "scene_media.end.image_approval",
            scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "end"),
                session=session,
            ),
        )

    current_language = _scene_current_language(session, scene)
    if _get_scene_policy_candidate(scene, current_language):
        return _keyboard_snapshot(
            "scene_media.video.policy_retry",
            video_policy_retry_markup(
                include_restart_hint=_should_suggest_regenerate_start_frame_after_video_prompt_fails(scene)
            ),
        )
    if _all_scene_languages_have_video(scene, languages):
        return _keyboard_snapshot(
            "scene_media.video.language_approval",
            scene_language_approval_markup(languages, session=session),
        )
    return _keyboard_snapshot(
        "scene_media.video.prompt",
        scene_video_approval_markup(
            include_restart_end=_scene_needs_end_frame_step(scene),
            languages=languages,
            session=session,
        ),
    )


async def _reply_current_scene_media_keyboard(update: Update, session: dict, text: str) -> None:
    snapshot = scene_media_keyboard_snapshot(session)
    await _reply_text_with_optional_markup(update, text, reply_markup=snapshot.markup)


def _store_syntax_retry_context(
    scene: dict,
    *,
    languages: list[str],
    pending_payload: dict,
    error_message: str,
) -> None:
    attempts = scene.get("syntax_attempts_by_lang")
    if not isinstance(attempts, dict):
        attempts = {}
    for language in languages:
        attempts[str(language)] = {
            "prompt": _scene_video_prompt_for_language(scene, str(language)),
            "speech": _scene_speech_for_language(scene, str(language)),
            "error": error_message,
        }
    scene["syntax_attempts_by_lang"] = attempts
    scene["syntax_retry_context"] = {
        "languages": list(languages),
        "error": error_message,
        "pending": pending_payload,
    }


def _persist_image_candidate_best_effort(session: dict | None, *, scene_idx: int, candidate: dict | None) -> None:
    if not isinstance(session, dict) or not isinstance(candidate, dict):
        return
    try:
        persist_image_candidate(session, scene_idx=scene_idx, candidate=candidate)
    except Exception:
        logger.exception("scene_media.persist_image_candidate_failed scene_idx=%s", scene_idx)


def _persist_video_candidate_best_effort(session: dict | None, *, scene_idx: int, candidate: dict | None) -> None:
    if not isinstance(session, dict) or not isinstance(candidate, dict):
        return
    try:
        persist_video_candidate(session, scene_idx=scene_idx, candidate=candidate)
    except Exception:
        logger.exception("scene_media.persist_video_candidate_failed scene_idx=%s", scene_idx)


def _persist_message_link_best_effort(
    session: dict | None,
    *,
    chat_id: int | None,
    message_id: int | None,
    scene_idx: int,
    media_type: str,
    candidate_id: str,
    language: str | None = None,
    kind: str | None = None,
) -> None:
    if chat_id is None or not isinstance(session, dict):
        return
    try:
        persist_message_link(
            session,
            chat_id=chat_id,
            message_id=message_id,
            scene_idx=scene_idx,
            media_type=media_type,
            candidate_id=candidate_id,
            language=language,
            kind=kind,
        )
    except Exception:
        logger.exception("scene_media.persist_message_link_failed scene_idx=%s message_id=%s", scene_idx, message_id)


def _persist_image_state_best_effort(candidate: dict | None) -> None:
    if not isinstance(candidate, dict):
        return
    try:
        persist_image_candidate_state(candidate)
    except Exception:
        logger.exception("scene_media.persist_image_state_failed candidate_id=%s", candidate.get("candidate_id"))


def _persist_video_state_best_effort(candidate: dict | None) -> None:
    if not isinstance(candidate, dict):
        return
    try:
        persist_video_candidate_state(candidate)
    except Exception:
        logger.exception("scene_media.persist_video_state_failed candidate_id=%s", candidate.get("candidate_id"))


def _scene_image_model_key(target: str) -> str:
    return END_IMAGE_MODEL_KEY if target == "end" else START_IMAGE_MODEL_KEY


def _scene_image_model_manual_key(target: str) -> str:
    return f"{_scene_image_model_key(target)}_manual"


def _is_retryable_post_production_timeout(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, TimedOut, subprocess.TimeoutExpired)):
        return True
    low = str(exc or "").strip().lower()
    return "timed out" in low or "timeout" in low or "time out" in low


def _post_production_timeout_user_message() -> str:
    return (
        "Финальная сборка ролика заняла слишком много времени. "
        "Я уже попробовала повторить автоматически; можно запустить сборку ещё раз чуть позже."
    )


def _strip_post_production_inline_video_payloads(value):
    if isinstance(value, dict):
        return {
            str(key): _strip_post_production_inline_video_payloads(item)
            for key, item in value.items()
            if str(key) not in {"video_b64", "video_bytes"}
        }
    if isinstance(value, list):
        return [_strip_post_production_inline_video_payloads(item) for item in value]
    return value


def _is_request_entity_too_large_error(exc: Exception) -> bool:
    low = str(exc or "").lower()
    return (
        "request entity too large" in low
        or "413" in low
        or "payload too large" in low
        or "entity too large" in low
    )


async def _reply_final_master_document(
    message,
    *,
    video_bytes: bytes,
    filename: str,
    caption: str,
    label: str,
    video_path: str | None = None,
):
    document_caption = f"{caption}\nОтправляю как файл: Telegram не принял video payload."
    path = Path(video_path).expanduser() if video_path else None

    if path is not None and path.exists() and path.is_file():
        async def _send_from_path():
            with path.open("rb") as stream:
                return await message.reply_document(document=stream, caption=document_caption, filename=filename)

        return await _safe_send(
            lambda: _send_from_path(),
            label=label.replace("reply_video", "reply_document", 1),
        )

    def _fresh_document():
        stream = io.BytesIO(video_bytes)
        stream.name = filename
        return stream

    return await _safe_send(
        lambda: message.reply_document(document=_fresh_document(), caption=document_caption, filename=filename),
        label=label.replace("reply_video", "reply_document", 1),
    )


async def _reply_final_master_video(
    message,
    *,
    video_bytes: bytes,
    filename: str,
    caption: str,
    label: str,
    video_path: str | None = None,
) -> None:
    stream = io.BytesIO(video_bytes)
    stream.name = filename
    try:
        await _safe_reply_video(
            message,
            video=stream,
            caption=caption,
            label=label,
        )
    except (NetworkError, TimedOut) as exc:
        if not _is_request_entity_too_large_error(exc):
            raise
        logger.warning(
            "telegram.final_master_too_large label=%s filename=%s bytes=%s path=%s",
            label,
            filename,
            len(video_bytes),
            video_path or "",
        )
        try:
            return await _reply_final_master_document(
                message,
                video_bytes=video_bytes,
                filename=filename,
                caption=caption,
                label=label,
                video_path=video_path,
            )
        except Exception:
            logger.exception(
                "telegram.final_master_document_fallback_failed label=%s filename=%s path=%s",
                label,
                filename,
                video_path or "",
            )
        path_note = f"\nПуть к файлу: `{video_path}`" if video_path else ""
        await _safe_reply_text(
            message,
            (
                f"{caption}\n"
                "Файл собран, но Telegram не принял его как video: размер payload слишком большой."
                f"{path_note}"
            ),
            parse_mode="Markdown",
        )


async def _build_post_production_with_retries(
    update: Update,
    *,
    session: dict,
    production: dict,
    scenes: list[dict],
) -> dict:
    project_slug = str(session.get("selected_project_key") or "").strip() or None
    last_exc: Exception | None = None
    for attempt in range(1, _POST_PRODUCTION_RETRY_ATTEMPTS + 1):
        try:
            post = await asyncio.to_thread(
                build_post_production,
                scenes,
                project_slug=project_slug,
            )
            production["post_production"] = _strip_post_production_inline_video_payloads(post)
            persist_post_production_artifacts(session, post)
            return post
        except Exception as exc:
            last_exc = exc
            if _is_retryable_post_production_timeout(exc) and attempt < _POST_PRODUCTION_RETRY_ATTEMPTS:
                await update.message.reply_text(
                    "Финальная сборка заняла слишком много времени. Повторяю автоматически."
                )
                await asyncio.sleep(_POST_PRODUCTION_RETRY_DELAY_SEC)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _set_scene_prompt_input_pending(
    session: dict,
    *,
    scene_idx: int,
    target: str,
    language: str | None = None,
) -> None:
    session[SCENE_PROMPT_INPUT_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "target": str(target or "").strip(),
        "language": normalize_language_code(language or "") if language else "",
    }


def _get_scene_prompt_input_pending(session: dict, *, scene_idx: int, target: str) -> dict | None:
    raw = session.get(SCENE_PROMPT_INPUT_PENDING_KEY)
    if not isinstance(raw, dict):
        return None
    if int(raw.get("scene_idx", -1)) != int(scene_idx):
        return None
    if str(raw.get("target") or "").strip() != str(target or "").strip():
        return None
    return raw


def _pop_scene_prompt_input_pending(session: dict, *, scene_idx: int, target: str) -> dict | None:
    raw = _get_scene_prompt_input_pending(session, scene_idx=scene_idx, target=target)
    if raw is not None:
        session.pop(SCENE_PROMPT_INPUT_PENDING_KEY, None)
    return raw


def _scene_video_prompt_raw(scene: dict, language: str) -> bool:
    mapping = scene.get(VIDEO_PROMPT_RAW_BY_LANG_KEY)
    if not isinstance(mapping, dict):
        return False
    return bool(mapping.get(language))


def _set_scene_video_prompt_raw(scene: dict, language: str, enabled: bool) -> None:
    mapping = scene.get(VIDEO_PROMPT_RAW_BY_LANG_KEY)
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = bool(enabled)
    scene[VIDEO_PROMPT_RAW_BY_LANG_KEY] = mapping


def _set_end_frame_requested(scene: dict, enabled: bool) -> None:
    scene[END_FRAME_REQUESTED_KEY] = bool(enabled)


def _scene_has_available_end_frame_prompt(scene: dict) -> bool:
    end_prompt = str(scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or "").strip()
    return bool(end_prompt and end_prompt.lower() != "пусто")


def _has_explicit_end_frame_request(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return False
    markers = (
        "конечный кадр",
        "финальный кадр",
        "последний кадр",
        "end frame",
        "энд фрейм",
        "нужен конец",
        "нужен финал",
    )
    return any(marker in low for marker in markers)


def _apply_shot_type_hint_from_text(scene: dict, text: str) -> None:
    low = str(text or "").strip().lower()
    if not low:
        return
    if any(marker in low for marker in _SHOT_CLOSEUP_MARKERS):
        scene["shot_type"] = "closeup"
        return
    if any(marker in low for marker in _SHOT_MEDIUM_MARKERS):
        scene["shot_type"] = "medium"


def _enforce_start_prompt_shot_type(scene: dict, prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        return text
    shot = str(scene.get("shot_type") or "").strip().lower()
    cleaned = re.sub(
        r"(?i)\b(крупный план|крупно|средний план|close-up|close up|medium shot)\b[ .,:;-]*",
        "",
        text,
        count=1,
    ).strip()
    if shot == "closeup":
        sep = " " if cleaned and cleaned[:1].islower() else ". "
        return f"Крупный план{sep}{cleaned}".strip()
    if shot == "medium":
        sep = " " if cleaned and cleaned[:1].islower() else ". "
        return f"Средний план{sep}{cleaned}".strip()
    return text


def _scene_image_model_for_generation(scene: dict, target: str) -> str | None:
    if not bool(scene.get(_scene_image_model_manual_key(target))):
        return None
    model = str(scene.get(_scene_image_model_key(target)) or "").strip()
    return model or None


def _store_scene_image_model(scene: dict, target: str, model: str | None) -> None:
    if model:
        scene[_scene_image_model_key(target)] = str(model).strip()


def _clear_scene_image_model_choice_pending(scene: dict) -> None:
    scene.pop(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY, None)


def _next_lower_scene_image_model(scene: dict, target: str) -> str | None:
    return _next_image_quality_model(_scene_image_model_for_generation(scene, target) or primary_image_generation_model())


def _scene_image_model_retry_markup(*, lower_model: str | None) -> ReplyKeyboardMarkup:
    rows = [[SCENE_IMAGE_MODEL_RETRY_BUTTON]]
    if lower_model:
        rows.append([SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON])
    rows.append([NAV_BUTTON_PREVIOUS_STEP])
    rows.append([NAV_BUTTON_START_OVER])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


async def _ask_scene_image_model_choice(
    update: Update,
    *,
    scene: dict,
    target: str,
    failed_model: str,
    error: Exception,
) -> None:
    lower_model = _next_image_quality_model(failed_model)
    retry_model = primary_image_generation_model()
    scene[SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY] = {
        "target": target,
        "retry_model": retry_model,
        "failed_model": failed_model,
        "lower_model": lower_model or "",
        "error": str(error),
    }
    target_label = "стартовый кадр" if target == "start" else "конечный кадр"
    if lower_model:
        tail = (
            f"Можно повторить на сильной модели `{retry_model}` или понизить до `{lower_model}`. "
            "Автоматически на модель ниже не ухожу."
        )
    else:
        tail = f"Модели ниже не настроены, могу только повторить сильную `{retry_model}`."
    await update.message.reply_text(
        f"Не удалось сгенерировать {target_label} на модели `{failed_model}`.\n"
        f"Ошибка: {_user_error_message(error)}\n\n{tail}",
        reply_markup=_scene_image_model_retry_markup(lower_model=lower_model),
    )


def _dedupe_image_reference_bytes(items: list[bytes | None]) -> list[bytes]:
    out: list[bytes] = []
    seen: set[bytes] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _end_frame_reference_images(session: dict, scene: dict, start_bytes: bytes) -> list[bytes]:
    refs: list[bytes | None] = [start_bytes]
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    first_scene = scenes[0] if isinstance(scenes, list) and scenes and isinstance(scenes[0], dict) else None
    if isinstance(first_scene, dict):
        refs.extend(_load_scene_reference_images(first_scene, include_location=False))
    refs.extend(_load_scene_reference_images(scene, include_location=False))
    return _dedupe_image_reference_bytes(refs)


def _escalate_scene_image_model(scene: dict, target: str) -> str | None:
    return None


def _can_escalate_scene_image_model(scene: dict, target: str) -> bool:
    return False


def _prepare_ai_image_retry_model_message(scene: dict, target: str) -> str:
    upgraded_model = _escalate_scene_image_model(scene, target)
    if upgraded_model:
        return f" Повышаю модель до `{upgraded_model}` и перегенерирую."
    current = _scene_image_model_for_generation(scene, target)
    if current:
        return f" Следующая попытка на той же модели `{current}`."
    return " Следующая попытка на текущем профиле генерации."


async def _generate_scene_image_result(
    prompt: str,
    *,
    reference_images: list[bytes] | None,
    model: str | None,
):
    # Tests and a few legacy imports monkeypatch the bytes-only helper. Keep that
    # hook alive while production uses the richer result API.
    if getattr(_generate_image_with_lenient_fallback, "__module__", "") != "app.handlers.handlers_media_scene":
        try:
            data = await _generate_image_with_lenient_fallback(
                prompt,
                reference_images=reference_images,
                model=model,
            )
        except TypeError as e:
            if "model" not in str(e):
                raise
            data = await _generate_image_with_lenient_fallback(prompt, reference_images=reference_images)
        return SimpleNamespace(data=data, model=model)
    return await _generate_image_with_lenient_fallback_result(
        prompt,
        reference_images=reference_images,
        model=model,
    )
_PROMPT_PREVIEW_MAX_LEN = 3500


async def _safe_send(call, *, label: str) -> None:
    """Run Telegram send call with short retries on transient network failures."""
    for attempt in range(1, _SEND_RETRY_ATTEMPTS + 1):
        try:
            return await call()
        except RetryAfter as e:
            if attempt >= _SEND_RETRY_ATTEMPTS:
                raise
            delay = float(
                getattr(e, "retry_after", _SEND_RETRY_BASE_DELAY_SEC)
                or _SEND_RETRY_BASE_DELAY_SEC
            )
            logger.warning(
                "telegram.send retry_after label=%s attempt=%s delay=%ss",
                label,
                attempt,
                delay,
            )
            await asyncio.sleep(delay)
        except (NetworkError, TimedOut) as e:
            if attempt >= _SEND_RETRY_ATTEMPTS:
                raise
            delay = _SEND_RETRY_BASE_DELAY_SEC * attempt
            logger.warning(
                "telegram.send transient_error label=%s attempt=%s err=%s",
                label,
                attempt,
                str(e),
            )
            await asyncio.sleep(delay)


async def _safe_reply_text(message, text: str, **kwargs) -> None:
    raw = str(text or "")
    chunks: list[str] = []
    while len(raw) > 3900:
        cut = raw.rfind("\n", 0, 3900)
        if cut < 1200:
            cut = 3900
        chunks.append(raw[:cut].rstrip())
        raw = raw[cut:].lstrip()
    chunks.append(raw)
    for chunk in chunks[:-1]:
        await _safe_send(lambda chunk=chunk: message.reply_text(chunk), label="reply_text")
    return await _safe_send(lambda: message.reply_text(chunks[-1], **kwargs), label="reply_text")


async def _safe_reply_photo(message, *, photo, caption: str) -> None:
    return await _safe_send(
        lambda: message.reply_photo(photo=photo, caption=caption),
        label="reply_photo",
    )


async def _safe_reply_video(message, *, video, caption: str, label: str = "reply_video") -> None:
    if isinstance(video, (bytes, bytearray)):
        payload = bytes(video)
        filename = "video.mp4"
    else:
        filename = str(getattr(video, "name", "") or "video.mp4")
        if hasattr(video, "seek"):
            try:
                video.seek(0)
            except Exception:
                pass
        payload = video.read() if hasattr(video, "read") else bytes(video)

    def _fresh_video():
        stream = io.BytesIO(payload)
        stream.name = filename
        return stream

    return await _safe_send(
        lambda: message.reply_video(video=_fresh_video(), caption=caption),
        label=label,
    )


def _parse_like_command(text: str) -> int | None:
    raw = str(text or "").strip().lower()
    if not raw.startswith("лайк"):
        return None
    parts = raw.split()
    if len(parts) == 1:
        return 1
    try:
        value = int(parts[1])
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_speed_command(text: str) -> float | None:
    raw = str(text or "").strip()
    low = raw.lower()
    if not low.startswith("скорость"):
        return None
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parse_speed_coef(parts[1])


def _scene_speed_coef(scene: dict, language: str) -> float:
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return DEFAULT_SCENE_VIDEO_SPEED_COEF
    return parse_speed_coef(selected.get("speed_coef")) or DEFAULT_SCENE_VIDEO_SPEED_COEF


def _approve_scene_video_language(scene: dict, language: str, *, speed_coef: float | None = None) -> None:
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    approvals[language] = True
    scene["video_approvals_by_lang"] = approvals
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return
    mark_video_candidate_approved(
        scene,
        language,
        str(selected.get("candidate_id") or ""),
        speed_coef=speed_coef if speed_coef is not None else _scene_speed_coef(scene, language),
    )
    _persist_video_state_best_effort(selected)


def _set_scene_video_speed(scene: dict, language: str, speed_coef: float) -> bool:
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return False
    candidate_id = str(selected.get("candidate_id") or "")
    updated = mark_video_candidate_approved(scene, language, candidate_id, speed_coef=speed_coef)
    if updated is None:
        latest = latest_scene_video_candidate(scene, language)
        if latest is None:
            return False
        latest["speed_coef"] = float(speed_coef)
        _persist_video_state_best_effort(latest)
        return True
    updated["approved_for_production"] = bool(updated.get("approved_for_production"))
    _persist_video_state_best_effort(updated)
    return True


def _apply_scene_like_fallback(session: dict, scene: dict, *, idx: int, text: str, substep: str) -> str | None:
    ordinal = _parse_like_command(text)
    if ordinal is None:
        return None
    if substep == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        candidates = image_candidates_for_kind(scene, "start")
        if len(candidates) < ordinal:
            return "Не нашла такой стартовый кадр в истории сцены."
        candidate = candidates[-ordinal]
        candidate["liked"] = True
        _persist_image_state_best_effort(candidate)
        return f"Лайк поставлен стартовому кадру сцены {idx + 1}."
    if substep == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        candidates = image_candidates_for_kind(scene, "end")
        if len(candidates) < ordinal:
            return "Не нашла такой конечный кадр в истории сцены."
        candidate = candidates[-ordinal]
        candidate["liked"] = True
        _persist_image_state_best_effort(candidate)
        return f"Лайк поставлен конечному кадру сцены {idx + 1}."
    if substep == SCENE_MEDIA_SUBSTEP_VIDEO:
        language = _scene_current_language(session, scene)
        mapping = scene.get("video_candidates_by_lang")
        bucket = mapping.get(language) if isinstance(mapping, dict) else None
        candidates = [item for item in bucket if isinstance(item, dict)] if isinstance(bucket, list) else []
        if len(candidates) < ordinal:
            return f"Не нашла такое видео для языка {language}."
        candidate = candidates[-ordinal]
        candidate["liked"] = True
        _persist_video_state_best_effort(candidate)
        return f"Лайк поставлен видео сцены {idx + 1} для языка {language}."
    return "Лайк сейчас можно ставить только на уже сгенерированное изображение или видео."


def _prompt_preview(prompt: str) -> str:
    """Shorten long prompts for Telegram while preserving the exact prefix sent."""
    p = str(prompt or "").strip()
    if len(p) <= _PROMPT_PREVIEW_MAX_LEN:
        return p
    return f"{p[:_PROMPT_PREVIEW_MAX_LEN]}…"


def _reset_video_prompt_policy_failure_count(scene: dict) -> None:
    """Сбрасывает счётчик неудачных policy-retry для видео (новый стартовый кадр или успешное видео)."""
    scene.pop("video_prompt_policy_failure_count", None)


def _scene_speech_options_markup() -> ReplyKeyboardMarkup:
    """Клавиатура с действиями после неуспешного автосокращения."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_MEDIA_AUTO_APPROVE_BUTTON],
            [SCENE_SPEECH_OPTION_RETRY],
            [SCENE_SPEECH_OPTION_WAIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _scene_speech_approval_markup() -> ReplyKeyboardMarkup:
    """Клавиатура подтверждения сокращённой реплики."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_MEDIA_AUTO_APPROVE_BUTTON],
            [SCENE_SPEECH_OPTION_APPROVE],
            [SCENE_SPEECH_OPTION_EDIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _scene_speech_user_long_phrase_markup() -> ReplyKeyboardMarkup:
    """Клавиатура выбора для длинной пользовательской фразы."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_SPEECH_OPTION_KEEP_AS_IS],
            [SCENE_SPEECH_OPTION_SHORTEN_AI],
            [SCENE_SPEECH_OPTION_EDIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _is_scene_media_auto_approve_enabled(session: dict) -> bool:
    return (
        bool(session.get(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY))
        or str(session.get(SCENE_MEDIA_APPROVE_MODE_SESSION_KEY) or "").strip() == SCENE_MEDIA_APPROVE_MODE_AUTO
    )


def _is_scene_media_ai_approve_enabled(session: dict) -> bool:
    return (
        str(session.get(SCENE_MEDIA_APPROVE_MODE_SESSION_KEY) or "").strip() == SCENE_MEDIA_APPROVE_MODE_AI
        and not bool(session.get(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY))
    )


def _is_scene_media_auto_approve_command(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().replace("-", " ").split())
    return low in {"автоапрув", "авто апрув", "autoapprove", "auto approve", "auto approval"}


def _is_scene_media_ai_approve_command(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().replace("-", " ").split())
    return low in {"🤖 ии апрув", "ии апрув", "ai approve", "ai approval", "aiapprove"}


def _is_scene_media_stop_command(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().replace("-", " ").split())
    return low == SCENE_MEDIA_STOP_BUTTON.lower()


def _enable_scene_media_auto_approve(session: dict) -> None:
    session[SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY] = True
    session[SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] = SCENE_MEDIA_APPROVE_MODE_AUTO
    session.pop(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY, None)


def _enable_scene_media_ai_approve(session: dict) -> None:
    session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
    session[SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] = SCENE_MEDIA_APPROVE_MODE_AI
    session.pop(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY, None)


def _set_scene_media_manual_approval(session: dict) -> None:
    session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
    session[SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] = SCENE_MEDIA_APPROVE_MODE_MANUAL
    session.pop(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY, None)


def _set_ai_human_review_pending(session: dict, *, stage: str, reason: str) -> None:
    session[SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY] = {"stage": stage, "reason": reason}


def _clear_ai_human_review_pending(session: dict) -> None:
    session.pop(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY, None)


def _ai_attempt_count(scene: dict, stage: str) -> int:
    history = scene.get("ai_approval_history")
    if not isinstance(history, list):
        return 0
    return sum(1 for item in history if isinstance(item, dict) and item.get("stage") == stage)


def _append_ai_approval_history(scene: dict, result, *, stage: str) -> None:
    history = scene.get("ai_approval_history")
    if not isinstance(history, list):
        history = []
    history.append(ai_approval_history_entry(result, attempt=_ai_attempt_count(scene, stage) + 1))
    scene["ai_approval_history"] = history


def _scene_recipe_text(session: dict, scene: dict) -> str:
    return str(
        session.get("recipe_full_text")
        or scene.get("recipe_step")
        or scene.get("action")
        or scene.get("text")
        or ""
    ).strip()


def _scene_description(scene: dict) -> str:
    parts = [
        str(scene.get("action") or "").strip(),
        str(scene.get("recipe_step") or "").strip(),
        str(scene.get("text") or "").strip(),
        str(scene.get("romantic_drama_beat") or "").strip(),
    ]
    generic_fragments = (
        "выполняет следующий кулинарный шаг",
        "кот активно взаимодействует с едой",
    )
    return sanitize_character_names_and_keys("\n".join(
        p
        for p in parts
        if p and not any(fragment in p.lower() for fragment in generic_fragments)
    ))


async def _send_ai_approval_summary(update: Update, result) -> None:
    await update.message.reply_text(format_ai_approval_summary(result))


async def _show_manual_scene_media_controls(update: Update, session: dict) -> None:
    """Перерисовывает текущий шаг после выхода из авто/ИИ-апрува."""
    scenes, idx = _scene_media_context(session)
    if not scenes or idx >= len(scenes):
        await _send_next_scene_media_step(update, session)
        return
    scene = scenes[idx]
    substep = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
    languages = _scene_target_languages(session, scene)
    if substep == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        await _reply_text_with_optional_markup(
            update,
            "Автоапрув остановлен. Проверьте стартовый кадр вручную.",
            reply_markup=scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "start"),
                include_end_frame_choice=_scene_has_available_end_frame_prompt(scene),
                session=session,
            ),
        )
        return
    if substep == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        await _reply_text_with_optional_markup(
            update,
            "Автоапрув остановлен. Проверьте конечный кадр вручную.",
            reply_markup=scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "end"),
                session=session,
            ),
        )
        return
    if substep == SCENE_MEDIA_SUBSTEP_VIDEO and len(languages) > 1 and _all_scene_languages_have_video(scene, languages):
        await _reply_text_with_optional_markup(
            update,
            "Автоапрув остановлен. Продолжаем с ручным апрувом видео по языкам.",
            reply_markup=scene_language_approval_markup(languages, session=session),
        )
        return
    await _send_next_scene_media_step(update, session)


async def _run_ai_start_frame_check(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    image_bytes: bytes,
) -> str:
    cfg = get_ai_approval_config()
    result = await asyncio.to_thread(
        check_start_frame,
        image_bytes=image_bytes,
        scene_prompt=str(scene.get("start_frame_prompt_effective") or scene.get("start_frame_prompt") or "").strip(),
        scene_description=_scene_description(scene),
        recipe_text=_scene_recipe_text(session, scene),
        scene_number=scene.get("scene") or idx + 1,
        previous_reference_images=_load_scene_reference_images(scene, include_location=False),
        expected_characters=[str(x) for x in (scene.get("characters") or [])],
        expected_objects=[],
        style_requirements="colorful cartoon style, soft stylized shading, non realistic rendering, warm cozy lighting",
        config=cfg,
    )
    _append_ai_approval_history(scene, result, stage="image")
    await _send_ai_approval_summary(update, result)
    return result.status


async def _run_ai_prompt_check(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    start_frame_prompt: str,
) -> str:
    cfg = get_ai_approval_config()
    language = str(scene.get("video_prompt_current_language") or "ru").strip() or "ru"
    result = await asyncio.to_thread(
        check_scene_prompts,
        start_frame_prompt=start_frame_prompt,
        end_frame_prompt=_image_end_scene_prompt(scene),
        video_prompt=_scene_video_prompt_for_language(scene, language),
        scene_description=_scene_description(scene),
        recipe_text=_scene_recipe_text(session, scene),
        scene_number=scene.get("scene") or idx + 1,
        expected_characters=[str(x) for x in (scene.get("characters") or [])],
        config=cfg,
    )
    _append_ai_approval_history(scene, result, stage="prompt")
    await _send_ai_approval_summary(update, result)
    return result.status


async def _run_ai_end_frame_check(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    image_bytes: bytes,
    start_bytes: bytes,
) -> str:
    cfg = get_ai_approval_config()
    references = _end_frame_reference_images(session, scene, start_bytes)
    result = await asyncio.to_thread(
        check_start_frame,
        image_bytes=image_bytes,
        scene_prompt=str(scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or "").strip(),
        scene_description=_scene_description(scene),
        recipe_text=_scene_recipe_text(session, scene),
        scene_number=scene.get("scene") or idx + 1,
        previous_reference_images=references,
        expected_characters=[str(x) for x in (scene.get("characters") or [])],
        expected_objects=[],
        style_requirements="colorful cartoon style, soft stylized shading, non realistic rendering, warm cozy lighting",
        frame_kind="end",
        config=cfg,
    )
    _append_ai_approval_history(scene, result, stage="end_image")
    await _send_ai_approval_summary(update, result)
    return result.status


async def _run_ai_video_check(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    language: str,
    video_bytes: bytes,
) -> str:
    cfg = get_ai_approval_config()
    start_bytes = _decode_b64(str(scene.get("start_frame_image_b64", "")).strip())
    result = await asyncio.to_thread(
        check_video,
        video_bytes=video_bytes,
        scene_prompt=_scene_video_prompt_for_language(scene, language),
        expected_speech_text=_scene_speech_for_language(scene, language),
        duration=scene.get("duration_sec"),
        recipe_text=_scene_recipe_text(session, scene),
        scene_number=scene.get("scene") or idx + 1,
        previous_reference_images=_load_scene_reference_images(scene, include_location=False),
        generated_start_frame=start_bytes,
        config=cfg,
    )
    _append_ai_approval_history(scene, result, stage="video")
    await _send_ai_approval_summary(update, result)
    return result.status


def _extract_image_edit_instruction(text: str) -> str | None:
    raw = str(text or "").strip()
    low = raw.lower()
    for prefix in ("изменить", "измени", "edit", "change"):
        if low == prefix:
            return ""
        marker = f"{prefix}:"
        if low.startswith(marker):
            return raw.split(":", 1)[1].strip()
    return None


def _is_empty_feedback_command(text: str) -> bool:
    return str(text or "").strip().lower().rstrip(":").strip() in {"фидбек", "feedback", "формат", "format"}


async def _ask_for_feedback_text(update: Update, target: str = "шаге") -> None:
    await update.message.reply_text(
        f"Напишите, что исправить на этом {target} (можно `ФИДБЕК: ...`)."
    )


def _set_scene_image_edit_pending(session: dict, *, scene_idx: int, target: str) -> None:
    session[SCENE_IMAGE_EDIT_PENDING_KEY] = {"scene_idx": scene_idx, "target": target}


def _pop_scene_image_edit_pending(session: dict, *, scene_idx: int) -> dict | None:
    pending = session.get(SCENE_IMAGE_EDIT_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    try:
        pending_idx = int(pending.get("scene_idx", -1))
    except Exception:
        pending_idx = -1
    if pending_idx != scene_idx:
        return None
    session.pop(SCENE_IMAGE_EDIT_PENDING_KEY, None)
    return pending


def _set_scene_video_feedback_pending(session: dict, *, scene_idx: int, language: str | None = None) -> None:
    session[SCENE_VIDEO_FEEDBACK_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "language": normalize_language_code(str(language or "")) or "",
    }


def _pop_scene_video_feedback_pending(session: dict, *, scene_idx: int) -> dict | None:
    pending = session.get(SCENE_VIDEO_FEEDBACK_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    try:
        pending_idx = int(pending.get("scene_idx", -1))
    except Exception:
        pending_idx = -1
    if pending_idx != int(scene_idx):
        return None
    session.pop(SCENE_VIDEO_FEEDBACK_PENDING_KEY, None)
    return pending


def _set_scene_video_speed_pending(session: dict, *, scene_idx: int, language: str | None = None) -> None:
    session[SCENE_VIDEO_SPEED_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "language": normalize_language_code(str(language or "")) or "",
    }


def _pop_scene_video_speed_pending(session: dict, *, scene_idx: int) -> dict | None:
    pending = session.get(SCENE_VIDEO_SPEED_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    try:
        pending_idx = int(pending.get("scene_idx", -1))
    except Exception:
        pending_idx = -1
    if pending_idx != int(scene_idx):
        return None
    session.pop(SCENE_VIDEO_SPEED_PENDING_KEY, None)
    return pending


def _clean_social_copy_message(text: str) -> str:
    clean = sanitize_character_names_and_keys(text).strip()
    clean = clean.replace("—", ", ").replace("–", ", ")
    clean = clean.replace('"', "").replace("«", "").replace("»", "")
    clean = clean.replace("```", "'''")
    clean = "\n".join(line.rstrip() for line in clean.splitlines()).strip()
    return clean


def _dedupe_hashtags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        clean = re.sub(r"[^\w#а-яА-ЯёЁ]", "", str(tag or ""), flags=re.UNICODE).strip()
        if not clean:
            continue
        if not clean.startswith("#"):
            clean = f"#{clean}"
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def _recipe_specific_hashtags_ru(recipe: str, existing_tags: list[str]) -> list[str]:
    fixed = {tag.lower() for tag in SOCIAL_FIXED_HASHTAGS_RU}
    from_existing = [
        tag.lower()
        for tag in existing_tags
        if re.search(r"[а-яё]", tag.lower()) and tag.lower() not in fixed
    ]
    if from_existing:
        return _dedupe_hashtags(from_existing)[:2]

    text = str(recipe or "").lower()
    known = [
        ("красн", "бархат", "#красныйбархат"),
        ("драник", "", "#драники"),
        ("сырник", "", "#сырники"),
        ("блин", "", "#блины"),
        ("олад", "", "#оладьи"),
        ("борщ", "", "#борщ"),
        ("котлет", "", "#котлеты"),
        ("пирог", "", "#пирог"),
        ("торт", "", "#торт"),
        ("суп", "", "#суп"),
        ("паста", "", "#паста"),
        ("салат", "", "#салат"),
    ]
    tags = [tag for first, second, tag in known if first in text and (not second or second in text)]
    if tags:
        return _dedupe_hashtags(tags)[:2]

    before_ingredients = re.split(r"\bингредиенты\b|:", text, maxsplit=1)[0]
    words = re.findall(r"[а-яё]{4,}", before_ingredients)
    stop = {"рецепт", "домашний", "простое", "простая", "вкусный", "вкусная", "готовим"}
    words = [word for word in words if word not in stop]
    if words:
        return _dedupe_hashtags(["#" + "".join(words[:2])])[:1]
    return ["#домашняякухня"]


def _recipe_specific_hashtags_en(recipe: str, body: str, existing_tags: list[str]) -> list[str]:
    fixed = {tag.lower() for tag in SOCIAL_FIXED_HASHTAGS_EN}
    from_existing = [
        tag.lower()
        for tag in existing_tags
        if re.fullmatch(r"#[a-zA-Z0-9_]+", tag) and tag.lower() not in fixed
    ]
    if from_existing:
        return _dedupe_hashtags(from_existing)[:2]

    text = f"{recipe} {body}".lower()
    known = [
        (("red", "velvet"), "#redvelvet"),
        (("potato", "pancake"), "#potatopancakes"),
        (("драник",), "#potatopancakes"),
        (("сырник",), "#syrniki"),
        (("borscht",), "#borscht"),
        (("борщ",), "#borscht"),
        (("pancake",), "#pancakes"),
        (("блин",), "#pancakes"),
        (("cake",), "#cake"),
        (("торт",), "#cake"),
        (("pie",), "#pie"),
        (("пирог",), "#pie"),
        (("soup",), "#soup"),
        (("суп",), "#soup"),
        (("salad",), "#salad"),
        (("салат",), "#salad"),
        (("pasta",), "#pasta"),
        (("паста",), "#pasta"),
    ]
    tags = [tag for needles, tag in known if all(needle in text for needle in needles)]
    return _dedupe_hashtags(tags or ["#homemadecooking"])[:2]


def _with_social_hashtags(text: str, *, recipe: str, language: str) -> str:
    clean = _clean_social_copy_message(text)
    existing_tags = re.findall(r"#[\wа-яА-ЯёЁ]+", clean, flags=re.UNICODE)
    clean = re.sub(r"(?:^|\s)#[\wа-яА-ЯёЁ]+", "", clean, flags=re.UNICODE)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = "\n".join(line.rstrip() for line in clean.splitlines()).strip()
    if language == "en":
        tags = SOCIAL_FIXED_HASHTAGS_EN + _recipe_specific_hashtags_en(recipe, clean, existing_tags)
    else:
        tags = SOCIAL_FIXED_HASHTAGS_RU + _recipe_specific_hashtags_ru(recipe, existing_tags)
    return f"{clean}\n\n{' '.join(_dedupe_hashtags(tags))}".strip()


def _fallback_social_copy(recipe: str) -> dict[str, str]:
    recipe_text = _clean_social_copy_message(recipe)
    ru_description = (
        "Живой домашний рецепт, который легко повторить дома.\n\n"
        f"{recipe_text}"
    ).strip()
    en_description = (
        "A warm homemade recipe with simple ingredients and clear cooking steps.\n\n"
        f"{recipe_text}"
    ).strip()
    return {
        "youtube_ru_title": "Домашний рецепт как у бабушки",
        "youtube_ru_description": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "rutube_ru_title": "Домашний рецепт как у бабушки",
        "rutube_ru_description": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "instagram_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "vk_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "tiktok_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "instagram_en": _with_social_hashtags(en_description, recipe=recipe, language="en"),
        "youtube_en_title": "Homemade grandma style recipe",
        "youtube_en_description": _with_social_hashtags(en_description, recipe=recipe, language="en"),
    }


def _social_copy_prompt(recipe: str) -> str:
    return (
        "Ты эксперт по Reels, YouTube Shorts и продвижению в соцсетях.\n\n"
        "Из утвержденного рецепта ниже сделай публикационные тексты.\n\n"
        "ТРЕБОВАНИЯ:\n"
        "- сделай 5 максимально вирусных русских описаний для YouTube Shorts, RuTube, Instagram, VK и TikTok\n"
        "- для YouTube Shorts отдельно верни заголовок и отдельно описание\n"
        "- для RuTube отдельно верни заголовок и отдельно описание\n"
        "- дополнительно сделай 2 английских текста для иностранного Instagram и YouTube Shorts\n"
        "- для английского YouTube Shorts отдельно верни title и отдельно description\n"
        "- тексты должны быть человечные, теплые и легко копируемые\n"
        "- не используй тире, кавычки и markdown\n"
        "- все нюансы готовки из рецепта сохрани подробно\n"
        "- в каждом описании должны быть живое описание, ингредиенты с сокращениями гр, шт, мл, ст л, ч л, и подробный пошаговый рецепт\n"
        "- в русские описания добавь ровно эти 3 постоянных хэштега: #убабушкивкуснее #бабушкинырецепты #рецепты\n"
        "- в русские описания добавь еще 1-2 хэштега, отражающие конкретный рецепт, например #драники или #красныйбархат\n"
        "- в английские описания добавь переведенные постоянные хэштеги: #grandmatastesbetter #grandmasrecipes #recipes\n"
        "- в английские описания добавь еще 1-2 английских хэштега, отражающие конкретный рецепт, например #potatopancakes или #redvelvet\n"
        "- не выдумывай ингредиенты, если количества нет, оставь без количества\n\n"
        f"{RUSSIAN_YO_RULE}\n\n"
        "Верни строго JSON объект без markdown с ключами:\n"
        "youtube_ru_title, youtube_ru_description, rutube_ru_title, rutube_ru_description, instagram_ru, vk_ru, tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
        "УТВЕРЖДЕННЫЙ РЕЦЕПТ:\n"
        f"{recipe.strip()}"
    )


async def _generate_social_copy_from_recipe(recipe: str) -> dict[str, str]:
    fallback = _fallback_social_copy(recipe)
    prompt = _social_copy_prompt(recipe)
    try:
        raw = await asyncio.to_thread(gemini_generate_sync, prompt)
        obj = extract_json_object(raw or "")
    except Exception:
        logger.exception("social copy generation failed")
        return fallback
    out: dict[str, str] = {}
    for key, fallback_value in fallback.items():
        value = _clean_social_copy_message(str(obj.get(key) or ""))
        value = value or fallback_value
        if key in SOCIAL_COPY_RU_KEYS:
            value = _with_social_hashtags(value, recipe=recipe, language="ru")
        elif key in SOCIAL_COPY_EN_KEYS:
            value = _with_social_hashtags(value, recipe=recipe, language="en")
        out[key] = value
    return out


def _social_descriptions_approval_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            ["ОК"],
            ["ФИДБЕК"],
            ["Исправь"],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _social_copy_messages(copy: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("YouTube Shorts RU заголовок, скопировать отдельно", copy["youtube_ru_title"]),
        ("YouTube Shorts RU описание, скопировать отдельно", copy["youtube_ru_description"]),
        ("RuTube RU заголовок, скопировать отдельно", copy["rutube_ru_title"]),
        ("RuTube RU описание, скопировать отдельно", copy["rutube_ru_description"]),
        ("Instagram RU описание, скопировать отдельно", copy["instagram_ru"]),
        ("VK RU описание, скопировать отдельно", copy["vk_ru"]),
        ("TikTok RU описание, скопировать отдельно", copy["tiktok_ru"]),
        ("Instagram EN description, copy separately", copy["instagram_en"]),
        ("YouTube Shorts EN title, copy separately", copy["youtube_en_title"]),
        ("YouTube Shorts EN description, copy separately", copy["youtube_en_description"]),
    ]


def _social_copy_video_targets(video_targets: list[dict[str, str]] | None) -> str:
    if not video_targets:
        return ""
    lines = ["Привязка описаний к видео:"]
    for item in video_targets:
        language = str(item.get("language") or "").strip() or "unknown"
        video_label = str(item.get("video_label") or "").strip() or f"{language.upper()} video"
        platforms = str(item.get("platforms") or "").strip()
        lines.append(f"{video_label}: {platforms}" if platforms else video_label)
    return "\n".join(lines)


async def _send_social_copy_for_approval(
    update: Update,
    copy: dict[str, str],
    *,
    video_targets: list[dict[str, str]] | None = None,
) -> None:
    mapping = _social_copy_video_targets(video_targets)
    if mapping:
        await _reply_text_with_optional_markup(update, mapping)
    for title, body in _social_copy_messages(copy):
        await _send_copyable_message(update, title, body)
    await _reply_text_with_optional_markup(
        update,
        "Проверьте описания.\n"
        "«ОК» — утвердить.\n"
        "«ФИДБЕК» или «Исправь» — пришлите, что поменять, и я перепишу описания.",
        reply_markup=_social_descriptions_approval_markup(),
    )


async def _refine_social_copy_from_feedback(
    *,
    recipe: str,
    previous_copy: dict[str, str],
    feedback: str,
) -> dict[str, str]:
    fallback = previous_copy or _fallback_social_copy(recipe)
    prompt = (
        "Ты редактируешь публикационные тексты для Reels, YouTube Shorts, VK и TikTok.\n\n"
        "Сохрани структуру JSON и все требования к хэштегам.\n"
        "Учти фидбэк пользователя, но не выдумывай ингредиенты и количества.\n"
        "Русские описания: #убабушкивкуснее #бабушкинырецепты #рецепты + 1-2 хэштега по рецепту.\n"
        "Английские описания: #grandmatastesbetter #grandmasrecipes #recipes + 1-2 английских хэштега по рецепту.\n"
        f"{RUSSIAN_YO_RULE}\n"
        "Верни строго JSON объект без markdown с ключами:\n"
        "youtube_ru_title, youtube_ru_description, rutube_ru_title, rutube_ru_description, instagram_ru, vk_ru, tiktok_ru, "
        "instagram_en, youtube_en_title, youtube_en_description.\n\n"
        f"РЕЦЕПТ:\n{recipe.strip()}\n\n"
        f"ПРЕДЫДУЩИЕ ОПИСАНИЯ JSON:\n{previous_copy}\n\n"
        f"ФИДБЭК ПОЛЬЗОВАТЕЛЯ:\n{feedback.strip()}\n"
    )
    try:
        raw = await asyncio.to_thread(gemini_generate_sync, prompt)
        obj = extract_json_object(raw or "")
    except Exception:
        logger.exception("social copy refinement failed")
        return fallback
    out: dict[str, str] = {}
    for key, fallback_value in fallback.items():
        value = _clean_social_copy_message(str(obj.get(key) or ""))
        value = value or fallback_value
        if key in SOCIAL_COPY_RU_KEYS:
            value = _with_social_hashtags(value, recipe=recipe, language="ru")
        elif key in SOCIAL_COPY_EN_KEYS:
            value = _with_social_hashtags(value, recipe=recipe, language="en")
        out[key] = value
    return out


async def _start_social_descriptions_approval(
    update: Update,
    *,
    session: dict,
    recipe: str,
    copy: dict[str, str],
    origin: str,
    video_targets: list[dict[str, str]] | None = None,
) -> None:
    session["state"] = STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
    session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = {
        "recipe": recipe,
        "copy": copy,
        "origin": origin,
        "video_targets": video_targets or [],
    }
    await _send_social_copy_for_approval(update, copy, video_targets=video_targets)


async def _send_copyable_message(update: Update, title: str, body: str) -> None:
    clean = _clean_social_copy_message(body)
    text = f"{title}\n\n```\n{clean}\n```".strip()
    limit = 3400
    if len(text) <= limit:
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    head = f"{title}\n\n```\n"
    rest = clean
    while rest:
        tail = "\n```"
        chunk = rest[: limit - len(head) - len(tail)].rstrip()
        if not chunk:
            chunk = rest[: limit - len(head) - len(tail)]
        await update.message.reply_text(f"{head}{chunk}{tail}".strip(), parse_mode="Markdown")
        rest = rest[len(chunk):].lstrip()
        head = f"{title} продолжение\n\n```\n"


async def _send_final_social_descriptions(update: Update, session: dict) -> bool:
    recipe = str(session.get("recipe_full_text") or "").strip()
    if not recipe or session.get(SOCIAL_DESCRIPTIONS_SENT_KEY):
        return False
    await reply_text_without_keyboard(update.message, "Готовлю описания для публикаций на основе утверждённого рецепта.")
    copy = await _generate_social_copy_from_recipe(recipe)
    await _start_social_descriptions_approval(
        update,
        session=session,
        recipe=recipe,
        copy=copy,
        origin="production_final",
        video_targets=[
            {
                "language": "ru",
                "video_label": "RU видео / финальный master",
                "platforms": "YouTube Shorts RU, RuTube RU, Instagram RU, VK RU, TikTok RU",
            },
            {
                "language": "en",
                "video_label": "EN видео / финальный master",
                "platforms": "Instagram EN, YouTube Shorts EN",
            },
        ],
    )
    return True


async def send_social_descriptions_for_recipe(update: Update, recipe: str) -> None:
    """Generate and send copyable social descriptions for an arbitrary recipe text."""
    recipe_text = str(recipe or "").strip()
    if not recipe_text or update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    session = SESSIONS.setdefault(chat_id, {})
    await reply_text_without_keyboard(update.message, "Готовлю описания для публикаций на основе рецепта.")
    copy = await _generate_social_copy_from_recipe(recipe_text)
    await _start_social_descriptions_approval(
        update,
        session=session,
        recipe=recipe_text,
        copy=copy,
        origin="description_mode",
    )


async def _finish_scene_media_production(update: Update, session: dict, *, message: str = "Все сцены обработаны. Production завершён.") -> bool:
    session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
    total_media_cost = estimated_session_total_media_cost(session)
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    await update.message.reply_text(
        f"{message}\n"
        f"Оценка суммарной media-side себестоимости: ~${total_media_cost:.4f}.\n\n"
        "Собираю финальные версии ролика и готовлю описания для апрува."
    )
    if isinstance(production, dict) and isinstance(scenes, list) and scenes:
        try:
            post = await _build_post_production_with_retries(
                update,
                session=session,
                production=production,
                scenes=scenes,
            )
            renders = post.get("renders_by_language")
            if isinstance(renders, dict) and renders:
                for language, payload in renders.items():
                    if not isinstance(payload, dict):
                        continue
                    clean_payload = payload.get("clean") or {}
                    clean_b64 = str((clean_payload.get("video_b64")) or "")
                    clean_bytes = _decode_b64(clean_b64)
                    if clean_bytes:
                        await _reply_final_master_video(
                            update.message,
                            video_bytes=clean_bytes,
                            filename=f"final_clean_master_{language}.mp4",
                            caption=f"Финальный clean master готов для языка {language}.",
                            label=f"reply_video:final_clean_master:{language}",
                            video_path=str(clean_payload.get("video_path") or "").strip() or None,
                        )
                    subtitled_payload = payload.get("subtitled_music") or {}
                    subtitled_b64 = str((subtitled_payload.get("video_b64")) or "")
                    subtitled_bytes = _decode_b64(subtitled_b64)
                    if subtitled_bytes:
                        await _reply_final_master_video(
                            update.message,
                            video_bytes=subtitled_bytes,
                            filename=f"final_subtitled_music_master_{language}.mp4",
                            caption=f"Финальный master с субтитрами и музыкой готов для языка {language}.",
                            label=f"reply_video:final_subtitled_master:{language}",
                            video_path=str(subtitled_payload.get("video_path") or "").strip() or None,
                        )
            else:
                clean_payload = post.get("final_clean_video") or {}
                subtitled_payload = post.get("final_subtitled_music_video") or {}
                clean_b64 = str((clean_payload.get("video_b64")) or "")
                subtitled_b64 = str((subtitled_payload.get("video_b64")) or "")
                clean_bytes = _decode_b64(clean_b64)
                if clean_bytes:
                    await _reply_final_master_video(
                        update.message,
                        video_bytes=clean_bytes,
                        filename="final_clean_master.mp4",
                        caption="Финальный clean master готов.",
                        label="reply_video:final_clean_master",
                        video_path=str(clean_payload.get("video_path") or "").strip() or None,
                    )
                subtitled_bytes = _decode_b64(subtitled_b64)
                if subtitled_bytes:
                    await _reply_final_master_video(
                        update.message,
                        video_bytes=subtitled_bytes,
                        filename="final_subtitled_music_master.mp4",
                        caption="Финальный master с субтитрами и музыкой готов.",
                        label="reply_video:final_subtitled_master",
                        video_path=str(subtitled_payload.get("video_path") or "").strip() or None,
                    )
        except Exception as e:
            logger.exception("post production build failed")
            production["post_production"] = {"error": str(e)}
            user_message = (
                _post_production_timeout_user_message()
                if _is_retryable_post_production_timeout(e)
                else f"Финальную сборку ролика пока не удалось завершить: {_user_error_message(e, domain='video')}"
            )
            await update.message.reply_text(
                user_message
            )
    started = await _send_final_social_descriptions(update, session)
    if not started:
        session["state"] = STATE_READY_FOR_PRODUCTION
        await update.message.reply_text(
            "Production завершён. Описания не сформированы: нет текста рецепта.\n\nВыберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
    return True


async def _handle_waiting_social_descriptions_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    pending = session.get(SOCIAL_DESCRIPTIONS_PENDING_KEY)
    if not isinstance(pending, dict):
        session["state"] = ""
        await update.message.reply_text(
            "Нет описаний на апрув. Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        return True
    low = (text or "").strip().lower()
    if low in {"ок", "ok", "okay", "approve", "утвердить"}:
        origin = str(pending.get("origin") or "description_mode")
        copy = pending.get("copy")
        if origin == "production_final" and isinstance(copy, dict):
            production = session.get("production")
            post_production = production.get("post_production") if isinstance(production, dict) else None
            recipe = str(pending.get("recipe") or "").strip()
            if isinstance(post_production, dict) and recipe:
                try:
                    with SessionLocal() as db:
                        jobs = queue_publication_bundle(
                            db,
                            project_slug=str(session.get("selected_project_key") or "").strip() or None,
                            recipe=recipe,
                            copy={str(k): str(v) for k, v in copy.items()},
                            post_production=post_production,
                            origin=origin,
                        )
                        db.commit()
                    if jobs:
                        await update.message.reply_text(publication_jobs_summary(jobs))
                except Exception:
                    logger.exception("publication bundle queue failed")
                    await update.message.reply_text(
                        "Описания утвердила, но публикационные jobs в БД сохранить не удалось. "
                        "Проверьте DATABASE_URL и настройку автопостинга."
                    )
        session.pop(SOCIAL_DESCRIPTIONS_PENDING_KEY, None)
        session[SOCIAL_DESCRIPTIONS_SENT_KEY] = True
        if origin == "production_final":
            session["state"] = STATE_READY_FOR_PRODUCTION
            await update.message.reply_text(
                "Описания утверждены. Production завершён.\n\nВыберите первый шаг кнопкой.",
                reply_markup=first_step_menu_markup(),
            )
        else:
            session["state"] = ""
            await update.message.reply_text(
                "Описания утверждены. Выберите первый шаг кнопкой.",
                reply_markup=first_step_menu_markup(),
            )
        return True
    if low in {"фидбек", "feedback", "исправь", "добавь"}:
        await update.message.reply_text("Пришлите фидбэк текстом: что именно поменять в описаниях.")
        return True
    feedback = (text or "").strip()
    if not feedback:
        await update.message.reply_text("Пришлите фидбэк по описаниям или нажмите «ОК».")
        return True
    recipe = str(pending.get("recipe") or "").strip()
    previous_copy = pending.get("copy")
    if not recipe or not isinstance(previous_copy, dict):
        session.pop(SOCIAL_DESCRIPTIONS_PENDING_KEY, None)
        session["state"] = ""
        await update.message.reply_text(
            "Не нашла черновик описаний для правки. Выберите первый шаг кнопкой.",
            reply_markup=first_step_menu_markup(),
        )
        return True
    session["state"] = STATE_REFINING_SOCIAL_DESCRIPTIONS
    await reply_text_without_keyboard(update.message, "Правлю описания по фидбэку, ждите.")
    copy = await _refine_social_copy_from_feedback(
        recipe=recipe,
        previous_copy={str(k): str(v) for k, v in previous_copy.items()},
        feedback=feedback,
    )
    session["state"] = STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
    session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = {
        **pending,
        "copy": copy,
        "last_feedback": feedback,
    }
    await _send_social_copy_for_approval(update, copy, video_targets=pending.get("video_targets"))
    return True


def _sync_scene_text_into_video_prompt(video_prompt: str, new_text: str) -> str:
    """Синхронизирует секцию `CHARACTERS SPEAKS` с новой ручной репликой."""
    prompt = str(video_prompt or "").strip()
    marker = "CHARACTERS SPEAKS:"
    match = re.search(r"characters\s+speaks\s*:", prompt, flags=re.IGNORECASE)
    if match:
        head = prompt[: match.start()]
        tail = prompt[match.end():]
        tail_lines = tail.splitlines()
        tail_rest = "\n".join(tail_lines[1:]) if len(tail_lines) > 1 else ""
        merged = f"{head}{marker} {new_text}".rstrip()
        if tail_rest:
            merged = f"{merged}\n{tail_rest}"
        return merged
    return f"{prompt}\n{marker} {new_text}".strip() if prompt else f"{marker} {new_text}"


async def _reply_text_with_optional_markup(update: Update, text: str, reply_markup=None) -> None:
    """Отправляет сообщение с `reply_markup`, даже если тестовый стаб не принимает kwargs."""
    if update.message is None:
        return
    try:
        if reply_markup is None:
            await _safe_reply_text(update.message, text)
            return
        await _safe_reply_text(update.message, text, reply_markup=reply_markup)
    except TypeError:
        await _safe_reply_text(update.message, text)


def _cancel_scene_speech_timeout(chat_id: int | None) -> None:
    """Останавливает отложенный таймер ожидания фразы для чата."""
    if chat_id is None:
        return
    pending_task = _SCENE_SPEECH_TIMEOUT_TASKS.pop(chat_id, None)
    if pending_task is not None:
        pending_task.cancel()


def _set_scene_speech_pending(session: dict, *, scene_idx: int, duration_sec: int, stage: str) -> None:
    """Сохраняет контекст ожидания решения по длинной реплике."""
    session[SCENE_SPEECH_PENDING_KEY] = {
        "scene_idx": scene_idx,
        "duration_sec": duration_sec,
        "stage": stage,
    }


def _schedule_scene_speech_timeout(
    *,
    context: ContextTypes.DEFAULT_TYPE | None,
    session: dict,
    chat_id: int | None,
) -> None:
    """Запускает 10-минутный таймер; по таймауту пытается автосокращение снова."""
    _cancel_scene_speech_timeout(chat_id)
    if context is None or chat_id is None:
        return

    async def _job() -> None:
        try:
            await asyncio.sleep(SCENE_SPEECH_WAIT_TIMEOUT_SEC)
            pending = session.get(SCENE_SPEECH_PENDING_KEY)
            if not isinstance(pending, dict):
                return
            production = session.get("production") or {}
            scenes = production.get("json_plan") if isinstance(production, dict) else None
            idx = int(pending.get("scene_idx", -1))
            if not isinstance(scenes, list) or idx < 0 or idx >= len(scenes):
                return
            scene = scenes[idx]
            duration_sec = int(pending.get("duration_sec", 6))
            max_chars = max_speech_chars_for_duration(duration_sec)
            spoken_len = speech_chars_excluding_pauses(str(scene.get("text", "")).strip())
            if spoken_len <= max_chars:
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                return
            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text="Прошло 10 минут без ответа. Пробую сократить реплику автоматически ещё раз…",
            )
            shortened = await _call_with_503_retries(
                None,
                context,
                chat_id,
                "retry_shorten_scene_speech_after_timeout",
                lambda: asyncio.to_thread(
                    shorten_scene_speech_and_sync_video_prompt_sync,
                    scene,
                    duration_sec,
                ),
            )
            if shortened is None:
                await safe_send_message(
                    context.bot,
                    chat_id=chat_id,
                    text=(
                        "Снова не получилось автоматически сократить реплику. "
                        "Нажмите кнопку для повтора или пришлите короткую фразу."
                    ),
                    reply_markup=_scene_speech_options_markup(),
                )
                _schedule_scene_speech_timeout(context=context, session=session, chat_id=chat_id)
                return
            prompt = _compose_locked_video_prompt(
                scene,
                "ru",
                shortened["video_prompt"],
            )
            _set_scene_video_prompt_for_language(scene, "ru", prompt)
            _apply_scene_phrase_update_to_scene(
                session,
                scene,
                scene_idx=idx,
                language="ru",
                phrase_text=shortened["text"],
                origin="auto_shorten",
            )
            updated_langs, failed_langs = await _sync_scene_phrase_translations(
                update=None,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                scene_idx=idx,
                source_language="ru",
                source_text=shortened["text"],
            )
            _set_scene_speech_pending(
                session,
                scene_idx=idx,
                duration_sec=duration_sec,
                stage="approval",
            )
            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=(
                    "Реплика автоматически сокращена.\n"
                    + f"Новая фраза:\n{scene['text']}\n\n"
                    + (
                        f"Обновила переводы для языков: {', '.join(updated_langs)}.\n"
                        if updated_langs
                        else ""
                    )
                    + (
                        f"Не удалось обновить переводы для языков: {', '.join(failed_langs)}.\n\n"
                        if failed_langs
                        else ""
                    )
                    + "Подтвердите запуск видео кнопкой или выберите правку."
                ),
                reply_markup=_scene_speech_approval_markup(),
            )
        except asyncio.CancelledError:
            return
        except ServiceUnavailableRetriesExhaustedError as e:
            logger.warning("scene speech timeout LLM retries exhausted: %s", e)
            try:
                await safe_send_message(context.bot, chat_id=chat_id, text=str(e))
            except Exception:
                logger.exception("failed to notify about exhausted scene speech timeout retries")
        except Exception:
            logger.exception("scene speech timeout handler failed")

    _SCENE_SPEECH_TIMEOUT_TASKS[chat_id] = asyncio.create_task(_job())


def _video_policy_approval_user_message(scene: dict, candidate_prompt: str) -> str:
    """Текст для Telegram после автопереписывания video prompt или ручного правки кандидата."""
    return _video_policy_prompt_approval_text(
        candidate_prompt,
        suggest_regenerate_start_frame=_should_suggest_regenerate_start_frame_after_video_prompt_fails(
            scene
        ),
    )


def _scene_media_context(session: dict) -> tuple[list[dict] | None, int]:
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    idx = int(session.get("image_scene_index", 0))
    return scenes if isinstance(scenes, list) else None, idx


def _scene_target_languages(session: dict, scene: dict) -> list[str]:
    """Resolve active languages for current scene media workflow."""
    raw = session.get("target_languages")
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            code = normalize_language_code(str(item))
            if code and code not in out:
                out.append(code)
    prompt_map = scene.get("video_prompt_by_lang")
    if isinstance(prompt_map, dict):
        for key in prompt_map:
            code = normalize_language_code(str(key))
            if code and code not in out:
                out.append(code)
    return out or ["ru"]


def _scene_current_language(session: dict, scene: dict) -> str:
    """Return current language by scene language index."""
    langs = _scene_target_languages(session, scene)
    idx = int(session.get("scene_media_language_index", 0) or 0)
    if idx < 0 or idx >= len(langs):
        idx = 0
        session["scene_media_language_index"] = 0
    return langs[idx]


def _scene_video_prompt_for_language(scene: dict, language: str) -> str:
    """Fetch effective video prompt for language with compatibility fallback."""
    mapping = scene.get("video_prompt_by_lang")
    if isinstance(mapping, dict):
        value = str(mapping.get(language, "")).strip()
        if value:
            return value
    return str(scene.get("video_prompt", "")).strip()


def _set_scene_video_prompt_for_language(scene: dict, language: str, prompt: str) -> None:
    """Store updated prompt in per-language map and legacy field."""
    mapping = scene.get("video_prompt_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = prompt
    scene["video_prompt_by_lang"] = mapping
    if language == "ru" or not str(scene.get("video_prompt", "")).strip():
        scene["video_prompt"] = prompt


def _set_scene_video_prompt_for_all_languages(session: dict, scene: dict, prompt: str) -> list[str]:
    """Apply one base video prompt to every scene language, preserving each language phrase."""
    languages = _scene_target_languages(session, scene)
    for lang in languages:
        _set_scene_video_prompt_for_language(
            scene,
            lang,
            _compose_locked_video_prompt(scene, lang, prompt),
        )
        _invalidate_scene_language_video(scene, lang)
    return languages


def _set_scene_speech_for_language(scene: dict, language: str, text: str) -> None:
    """Store phrase in multilingual map and legacy text field."""
    text = normalize_speech_phrase_for_voice(text)
    speech_map = scene.get("speech_by_lang")
    if not isinstance(speech_map, dict):
        speech_map = {}
    speech_map[language] = text
    scene["speech_by_lang"] = speech_map
    if language == "ru" or not str(scene.get("text", "")).strip():
        scene["text"] = text


def _scene_speech_for_language(scene: dict, language: str) -> str:
    """Resolve current speech text for language with RU fallback."""
    speech_map = scene.get("speech_by_lang")
    if isinstance(speech_map, dict):
        val = str(speech_map.get(language, "")).strip()
        if val:
            return val
    if language == "ru":
        return str(scene.get("text", "")).strip()
    return ""


_DIALOGUE_SPEAKER_ALIASES = {
    "grandma": "grandma",
    "бабушка": "grandma",
    "grandfather": "grandfather",
    "grandpa": "grandfather",
    "дедушка": "grandfather",
    "grandson": "grandson",
    "внук": "grandson",
    "granddaughter": "granddaughter",
    "внучка": "granddaughter",
}

_DIALOGUE_SPEAKER_PROMPT_LABELS = {
    "grandma": "GRANDMA",
    "grandfather": "GRANDFATHER",
    "grandson": "GRANDSON",
    "granddaughter": "GRANDDAUGHTER",
}


def _normalize_dialogue_speaker(value: str) -> str:
    return _DIALOGUE_SPEAKER_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def _scene_dialogue_for_language(scene: dict, language: str) -> list[dict[str, str]]:
    dialogue_map = scene.get("dialogue_by_lang")
    raw = dialogue_map.get(language) if isinstance(dialogue_map, dict) else None
    if not isinstance(raw, list) and normalize_language_code(language) == "ru":
        raw = scene.get("dialogue")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        speaker = _normalize_dialogue_speaker(item.get("speaker"))
        text = normalize_speech_phrase_for_voice(str(item.get("text") or "").strip())
        if speaker and text:
            out.append({"speaker": speaker, "text": text})
    return out


def _dialogue_voice_lock_full(speaker: str, language: str) -> str:
    lang = normalize_language_code(language) or "ru"
    if speaker == "grandma":
        return (
            "female voice English language elderly grandmother; neutral English accent; warm calm wise tone; medium slow pace."
            if lang == "en"
            else "female voice Russian language elderly grandmother; neutral Russian accent; warm calm wise tone; medium slow pace."
        )
    if speaker == "grandfather":
        return (
            "male voice English language adult around sixty years old; neutral English accent; tone calm friendly; pitch low to medium; speech rate 0.9."
            if lang == "en"
            else "male voice Russian language adult around sixty years old; neutral Russian accent; tone calm friendly; pitch low to medium; speech rate 0.9."
        )
    if speaker == "grandson":
        return (
            "male child voice English language around eight to ten years old; neutral English accent; tone bright excited; pitch medium to high; speech rate 1.0; clear articulation."
            if lang == "en"
            else "male child voice Russian language around eight to ten years old; neutral Russian accent; tone bright excited; pitch medium to high; speech rate 1.0; clear articulation."
        )
    if speaker == "granddaughter":
        return (
            "female child voice English language around eight to ten years old; neutral English accent; tone bright warm; pitch medium to high; speech rate normal 1.0; clear articulation."
            if lang == "en"
            else "female child voice Russian language around eight to ten years old; neutral Russian accent; tone bright warm; pitch medium to high; speech rate normal 1.0; clear articulation."
        )
    return (
        "neutral English voice, medium pitch, calm delivery."
        if lang == "en"
        else "neutral Russian voice, medium pitch, calm delivery."
    )


def _strip_dialogue_prompt_metadata(prompt: str) -> str:
    out = str(prompt or "").strip()
    patterns = (
        r"\n?speaker_dialogue_order:\s*[^\n]*",
        r"\n?character_dialogue_script:\s*(?:\n[^\n:]+:\s*[^\n]*)+",
        r"\n?voice_lock_full_[a-z_]+:\s*[^\n]*",
    )
    for pattern in patterns:
        out = re.sub(pattern, "", out, flags=re.IGNORECASE)
    return out.strip()


def _append_dialogue_prompt_metadata(prompt: str, scene: dict, language: str) -> str:
    dialogue = _scene_dialogue_for_language(scene, language)
    if len(dialogue) < 2:
        return _strip_dialogue_prompt_metadata(prompt)
    order: list[str] = []
    lines: list[str] = []
    voice_locks: list[str] = []
    for item in dialogue:
        speaker = item["speaker"]
        label = _DIALOGUE_SPEAKER_PROMPT_LABELS.get(speaker, speaker.upper())
        if label not in order:
            order.append(label)
        lines.append(f"{label}: {item['text']}")
        voice_locks.append(f"voice_lock_full_{speaker}: {_dialogue_voice_lock_full(speaker, language)}")
    base = _strip_dialogue_prompt_metadata(prompt)
    metadata = [
        f"speaker_dialogue_order: {', '.join(order)}",
        "character_dialogue_script:",
        *lines,
        *voice_locks,
    ]
    return f"{base}\n" + "\n".join(metadata)


def _scene_video_provider_choice_entry(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None,
    quality: str,
    aspect_ratio: str,
    audio_enabled: bool,
    syntax_error_message: str,
) -> dict:
    return {
        "schema": 1,
        "prompt": prompt,
        "model": model,
        "duration_sec": duration_sec,
        "vertex_duration_sec": 8 if str(model or "").strip().lower().startswith("veo") else duration_sec,
        "quality": quality,
        "aspect_ratio": aspect_ratio,
        "audio_enabled": audio_enabled,
        "start_image_b64": _encode_b64(start_image),
        "end_image_b64": _encode_b64(end_image) if end_image else "",
        "syntax_error_message": (syntax_error_message or "")[:8000],
    }


async def _deliver_scene_video_result(
    update: Update,
    *,
    chat_id: int | None,
    session: dict,
    scene: dict,
    idx: int,
    total: int,
    language: str,
    video_bytes: bytes,
    provider: str,
    caption_suffix: str,
) -> None:
    vb = io.BytesIO(video_bytes)
    vb.name = f"scene_{idx + 1}_{language}.mp4"
    sent_message = await _safe_reply_video(
        update.message,
        video=vb,
        caption=f"Сцена {idx + 1}/{total}, язык {language}: видео готово{caption_suffix}",
    )
    cost_summary = _store_scene_video_result(
        scene,
        language,
        video_bytes,
        session=session,
        scene_idx=idx,
        provider=provider,
        source_image_candidate_id=str((resolve_scene_image_for_video(scene, kind='start') or {}).get("candidate_id") or ""),
    )
    latest_candidate = latest_scene_video_candidate(scene, language)
    register_media_message_candidate(
        session,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="video",
        candidate_id=str((latest_candidate or {}).get("candidate_id") or ""),
        language=language,
    )
    _persist_message_link_best_effort(
        session,
        chat_id=chat_id,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="video",
        candidate_id=str((latest_candidate or {}).get("candidate_id") or ""),
        language=language,
    )
    width, height = probe_video_bytes_dimensions(video_bytes)
    if (width, height) != (1080, 1920):
        await update.message.reply_text(
            f"Видео {language} пришло в размере {width or '?'}x{height or '?'}. "
            "На финальной сборке нормализую до 1080x1920."
        )
    _set_scene_policy_candidate(scene, language, "")
    _reset_video_prompt_policy_failure_count(scene)
    await update.message.reply_text(
        f"Оценка себестоимости видео ({language}): ~${float(cost_summary['estimated_total_cost_usd']):.4f}."
    )


def _compose_locked_video_prompt(scene: dict, language: str, prompt: str) -> str:
    """
    Build final prompt while locking phrase in CHARACTERS SPEAKS.

    Prompt edits (`ПРОМПТ`/`ФИДБЕК`) must not alter spoken phrase.
    """
    out = enforce_video_prompt_voice_requirements(sanitize_character_names_and_keys(prompt), scene, target_language=language)
    speech = _scene_speech_for_language(scene, language)
    if speech:
        out = _sync_scene_text_into_video_prompt(out, speech)
    out = _append_dialogue_prompt_metadata(out, scene, language)
    plan_map = scene.get("speech_plan_by_lang")
    plan = plan_map.get(language) if isinstance(plan_map, dict) else build_default_speech_plan(speech, language)
    out = append_prompt_timing_hints(out, plan, duration_sec=float(scene.get("duration_sec") or 0.0))
    return _prefix_video_prompt_with_language(out, language)


def _set_scene_phrase_input_pending(session: dict, *, scene_idx: int, language: str) -> None:
    """Remember that next user message should be treated as phrase text."""
    session[SCENE_PHRASE_INPUT_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "language": normalize_language_code(language) or "ru",
    }


def _pop_scene_phrase_input_pending(session: dict) -> dict | None:
    """Pop pending phrase input marker."""
    raw = session.pop(SCENE_PHRASE_INPUT_PENDING_KEY, None)
    return raw if isinstance(raw, dict) else None


def _get_scene_phrase_input_pending(session: dict, *, scene_idx: int) -> dict | None:
    """Return pending phrase marker for current scene."""
    raw = session.get(SCENE_PHRASE_INPUT_PENDING_KEY)
    if not isinstance(raw, dict):
        return None
    if int(raw.get("scene_idx", -1)) != int(scene_idx):
        return None
    return raw


def _set_scene_phrase_translation_pending(
    session: dict,
    *,
    scene_idx: int,
    source_language: str,
    source_text: str,
    target_language: str,
    previous_target_text: str,
) -> None:
    session[SCENE_PHRASE_TRANSLATION_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "source_language": normalize_language_code(source_language) or "ru",
        "source_text": source_text.strip(),
        "target_language": normalize_language_code(target_language) or "en",
        "previous_target_text": previous_target_text.strip(),
        "stage": "confirm",
    }


def _get_scene_phrase_translation_pending(session: dict, *, scene_idx: int) -> dict | None:
    raw = session.get(SCENE_PHRASE_TRANSLATION_PENDING_KEY)
    if not isinstance(raw, dict):
        return None
    if int(raw.get("scene_idx", -1)) != int(scene_idx):
        return None
    return raw


def _pop_scene_phrase_translation_pending(session: dict) -> dict | None:
    raw = session.pop(SCENE_PHRASE_TRANSLATION_PENDING_KEY, None)
    return raw if isinstance(raw, dict) else None


def _invalidate_scene_language_video(scene: dict, language: str) -> None:
    """Drop generated video/approval for one language after phrase/prompt update."""
    results = scene.get("video_results_by_lang")
    if not isinstance(results, dict):
        results = {}
    results.pop(language, None)
    scene["video_results_by_lang"] = results
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    approvals[language] = False
    scene["video_approvals_by_lang"] = approvals


def _set_scene_phrase_origin(scene: dict, language: str, origin: str) -> None:
    mapping = scene.get(SCENE_PHRASE_ORIGIN_KEY)
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = origin
    scene[SCENE_PHRASE_ORIGIN_KEY] = mapping


def _scene_phrase_origin(scene: dict, language: str) -> str:
    mapping = scene.get(SCENE_PHRASE_ORIGIN_KEY)
    if not isinstance(mapping, dict):
        return ""
    return str(mapping.get(language) or "").strip()


def _set_scene_speech_budget_override(scene: dict, language: str, enabled: bool) -> None:
    mapping = scene.get(SCENE_SPEECH_BUDGET_OVERRIDE_KEY)
    if not isinstance(mapping, dict):
        mapping = {}
    if enabled:
        mapping[language] = True
    else:
        mapping.pop(language, None)
    scene[SCENE_SPEECH_BUDGET_OVERRIDE_KEY] = mapping


def _scene_speech_budget_override_enabled(scene: dict, language: str) -> bool:
    mapping = scene.get(SCENE_SPEECH_BUDGET_OVERRIDE_KEY)
    if not isinstance(mapping, dict):
        return False
    return bool(mapping.get(language))


def _set_scene_speech_plan_for_language(scene: dict, language: str, plan: list[dict]) -> None:
    mapping = scene.get("speech_plan_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = list(plan)
    scene["speech_plan_by_lang"] = mapping


def _set_scene_speech_cues_for_language(scene: dict, language: str, cues: list[dict]) -> None:
    mapping = scene.get("speech_cues_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = list(cues)
    scene["speech_cues_by_lang"] = mapping


def _set_scene_speech_budget_meta(scene: dict, language: str, *, duration_sec: int, text: str, status: str, auto_shortened: bool) -> None:
    mapping = scene.get("speech_budget_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
    meta = dict(speech_budget_for_duration(duration_sec, language))
    meta["current_chars"] = speech_chars_excluding_pauses(text)
    meta["status"] = status
    meta["auto_shortened"] = bool(auto_shortened)
    mapping[language] = meta
    scene["speech_budget_by_lang"] = mapping


def _refresh_scene_language_speech_metadata(
    scene: dict,
    language: str,
    *,
    duration_sec: int | None = None,
    text: str | None = None,
    status: str | None = None,
    auto_shortened: bool = False,
) -> None:
    lang = normalize_language_code(language) or "ru"
    phrase = (text if text is not None else _scene_speech_for_language(scene, lang)).strip()
    effective_duration = int(duration_sec or scene.get("duration_sec") or 6)
    plan = build_default_speech_plan(phrase, lang)
    cues = build_speech_cues_from_plan(plan, duration_sec=float(effective_duration))
    _set_scene_speech_plan_for_language(scene, lang, plan)
    _set_scene_speech_cues_for_language(scene, lang, cues)
    budget = speech_budget_for_duration(effective_duration, lang)
    current_chars = speech_chars_excluding_pauses(phrase)
    final_status = status or ("ok" if current_chars <= int(budget["max_chars"]) else "overflow")
    _set_scene_speech_budget_meta(
        scene,
        lang,
        duration_sec=effective_duration,
        text=phrase,
        status=final_status,
        auto_shortened=auto_shortened,
    )


def _apply_scene_phrase_update_to_scene(
    session: dict,
    scene: dict,
    *,
    scene_idx: int,
    language: str,
    phrase_text: str,
    origin: str | None = None,
) -> str:
    """Update every canonical phrase/prompt field for a scene language."""
    phrase = (phrase_text or "").strip()
    lang = normalize_language_code(language) or "ru"
    _set_scene_speech_for_language(scene, lang, phrase)
    phrase = _scene_speech_for_language(scene, lang)
    _set_scene_speech_budget_override(scene, lang, False)
    _refresh_scene_language_speech_metadata(scene, lang, auto_shortened=origin == "auto_shorten")
    locked = _compose_locked_video_prompt(
        scene,
        lang,
        _scene_video_prompt_for_language(scene, lang),
    )
    _set_scene_video_prompt_for_language(scene, lang, locked)
    _invalidate_scene_language_video(scene, lang)
    if origin:
        _set_scene_phrase_origin(scene, lang, origin)
    languages = _scene_target_languages(session, scene)
    if lang in languages:
        session["scene_media_language_index"] = languages.index(lang)
    return phrase


async def _sync_scene_phrase_translations(
    *,
    update: Update | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    scene_idx: int,
    source_language: str,
    source_text: str,
) -> tuple[list[str], list[str]]:
    """Retranslate changed phrase into other scene languages."""
    source_lang = normalize_language_code(source_language) or "ru"
    updated: list[str] = []
    failed: list[str] = []
    for lang in _scene_target_languages(session, scene):
        if lang == source_lang:
            continue
        previous_text = _scene_speech_for_language(scene, lang)
        try:
            if context is not None and chat_id is not None:
                translated = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "sync_scene_phrase_translation",
                    lambda lang=lang, previous_text=previous_text: _translate_scene_phrase_adaptive(
                        source_text,
                        lang,
                        previous_translation=previous_text,
                    ),
                )
            else:
                translated = await _translate_scene_phrase_adaptive(
                    source_text,
                    lang,
                    previous_translation=previous_text,
                )
        except Exception:
            logger.exception("adaptive phrase sync failed", extra={"source_language": source_lang, "target_language": lang})
            failed.append(lang)
            continue
        _apply_scene_phrase_update_to_scene(
            session,
            scene,
            scene_idx=scene_idx,
            language=lang,
            phrase_text=translated,
            origin="adaptive_translation",
        )
        updated.append(lang)
    return updated, failed


async def _translate_scene_phrase_adaptive(
    source_text: str,
    target_language: str,
    *,
    feedback: str = "",
    previous_translation: str = "",
) -> str:
    """Adaptive phrase translation via LLM for dubbing line."""
    target = normalize_language_code(target_language) or "en"
    if target == "ru":
        return source_text.strip()
    feedback_block = ""
    if previous_translation.strip() or feedback.strip():
        feedback_block = (
            "\nPREVIOUS TRANSLATION CANDIDATE:\n"
            f"{previous_translation.strip() or '(none)'}\n\n"
            "USER FEEDBACK FOR NEW ATTEMPT:\n"
            f"{feedback.strip() or '(none)'}\n"
        )
    prompt = (
        "Translate phrase for short cooking video dubbing.\n"
        f"Target language code: {target}.\n"
        "Translation must be adaptive and natural for spoken voiceover.\n"
        "Keep it concise enough to fit a short clip; if needed, prefer a shorter natural line over a literal long translation.\n"
        "Keep pauses and emotional accents if present.\n"
        "Do not use hyphen-minus, en dash, or em dash in the spoken phrase; use words or [пауза] instead.\n"
        "If user feedback is provided, revise the translation according to that feedback.\n"
        "Return only translated phrase.\n\n"
        f"SOURCE:\n{source_text.strip()}\n"
        f"{feedback_block}"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    out = normalize_speech_phrase_for_voice(raw or "")
    return out or normalize_speech_phrase_for_voice(source_text)


async def _apply_scene_phrase_update(
    update: Update,
    *,
    session: dict,
    scene: dict,
    scene_idx: int,
    language: str,
    phrase_text: str,
    auto_offer_en_translation: bool = True,
) -> bool:
    """Apply phrase, lock prompt, and optionally ask whether to adaptively translate to EN."""
    lang = normalize_language_code(language) or "ru"
    phrase = _apply_scene_phrase_update_to_scene(
        session,
        scene,
        scene_idx=scene_idx,
        language=lang,
        phrase_text=phrase_text,
        origin="user",
    )

    langs = _scene_target_languages(session, scene)
    if auto_offer_en_translation and lang == "ru" and "en" in langs:
        prev_en = _scene_speech_for_language(scene, "en")
        if prev_en and prev_en != phrase:
            _set_scene_phrase_translation_pending(
                session,
                scene_idx=scene_idx,
                source_language="ru",
                source_text=phrase,
                target_language="en",
                previous_target_text=prev_en,
            )
            await _reply_text_with_optional_markup(
                update,
                "Перевести адаптивно на английский и заменить фразу в английском видео?",
                reply_markup=ReplyKeyboardMarkup(
                    [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                ),
            )
            return True
    return False


def _store_scene_video_result(
    scene: dict,
    language: str,
    payload: bytes,
    *,
    session: dict | None = None,
    scene_idx: int | None = None,
    provider: str | None = None,
    source_image_candidate_id: str | None = None,
) -> dict[str, object]:
    """Persist generated video bytes and reset approval for language."""
    languages_count = 1
    if isinstance(scene.get("video_prompt_by_lang"), dict):
        languages_count = max(1, len(scene.get("video_prompt_by_lang") or {}))
    cost_summary = estimated_scene_video_cost(scene, language, languages_count)
    results = scene.get("video_results_by_lang")
    if not isinstance(results, dict):
        results = {}
    results[language] = {
        "video_b64": _encode_b64(payload),
        "estimated_cost_usd": cost_summary["estimated_total_cost_usd"],
        "estimated_cost_breakdown": cost_summary,
    }
    if payload:
        import tempfile

        with tempfile.NamedTemporaryFile(prefix="scene_video_", suffix=".mp4", delete=True) as tmp:
            tmp.write(payload)
            tmp.flush()
            actual_duration = _video_duration_seconds(tmp.name) or float(scene.get("duration_sec") or 0.0)
        plan_map = scene.get("speech_plan_by_lang")
        plan = plan_map.get(language) if isinstance(plan_map, dict) else None
        if not plan:
            plan = build_default_speech_plan(_scene_speech_for_language(scene, language), language)
        _set_scene_speech_plan_for_language(scene, language, list(plan))
        _set_scene_speech_cues_for_language(
            scene,
            language,
            build_speech_cues_from_plan(plan, duration_sec=actual_duration),
        )
        _set_scene_speech_budget_meta(
            scene,
            language,
            duration_sec=int(round(actual_duration or float(scene.get("duration_sec") or 6))),
            text=_scene_speech_for_language(scene, language),
            status="rendered",
            auto_shortened=_scene_phrase_origin(scene, language) == "auto_shorten",
        )
    scene["video_results_by_lang"] = results
    candidate = register_video_candidate(
        scene,
        language=language,
        video_bytes=payload,
        source_image_candidate_id=source_image_candidate_id,
        speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF,
    )
    if session is not None and scene_idx is not None:
        _persist_video_candidate_best_effort(session, scene_idx=scene_idx, candidate=candidate)
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    approvals[language] = False
    scene["video_approvals_by_lang"] = approvals
    _clear_ai_video_approval(scene, language)
    if session is not None and scene_idx is not None:
        try:
            save_video_result_estimate(
                session,
                scene_idx=scene_idx,
                language=language,
                provider=provider or "telegram",
                status="success",
                video_path=None,
                cost_usd=float(cost_summary["estimated_total_cost_usd"]),
                cost_breakdown=cost_summary,
            )
        except Exception:
            logger.exception("video result cost persistence failed")
    return cost_summary


def _all_scene_languages_have_video(scene: dict, languages: list[str]) -> bool:
    """True when each language has generated video result."""
    results = scene.get("video_results_by_lang")
    if not isinstance(results, dict):
        return False
    return all(isinstance(results.get(lang), dict) for lang in languages)


def _all_scene_languages_approved(scene: dict, languages: list[str]) -> bool:
    """True when each language has explicit approval."""
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        return False
    return all(bool(approvals.get(lang)) for lang in languages)


def _set_all_scene_languages_approval(scene: dict, languages: list[str], approved: bool) -> None:
    """Bulk update approval state for all scene languages."""
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    for lang in languages:
        approvals[lang] = approved
    scene["video_approvals_by_lang"] = approvals


def _scene_video_bytes_for_language(scene: dict, language: str) -> bytes | None:
    results = scene.get("video_results_by_lang")
    if not isinstance(results, dict):
        return None
    item = results.get(language)
    if not isinstance(item, dict):
        return None
    return _decode_b64(str(item.get("video_b64") or "").strip())


def _remove_scene_video_for_language(scene: dict, language: str) -> None:
    results = scene.get("video_results_by_lang")
    if isinstance(results, dict):
        results.pop(language, None)
        scene["video_results_by_lang"] = results
    approvals = scene.get("video_approvals_by_lang")
    if isinstance(approvals, dict):
        approvals[language] = False
        scene["video_approvals_by_lang"] = approvals


def _mark_ai_video_approved(scene: dict, language: str) -> None:
    approved = scene.get("ai_video_approved_by_lang")
    if not isinstance(approved, dict):
        approved = {}
    approved[language] = True
    scene["ai_video_approved_by_lang"] = approved


def _is_ai_video_approved(scene: dict, language: str) -> bool:
    approved = scene.get("ai_video_approved_by_lang")
    return isinstance(approved, dict) and bool(approved.get(language))


def _clear_ai_video_approval(scene: dict, language: str | None = None) -> None:
    if language is None:
        scene["ai_video_approved_by_lang"] = {}
        return
    approved = scene.get("ai_video_approved_by_lang")
    if isinstance(approved, dict):
        approved.pop(language, None)
        scene["ai_video_approved_by_lang"] = approved


async def _ai_check_existing_scene_videos(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    languages: list[str],
) -> bool:
    for language in languages:
        if _is_ai_video_approved(scene, language):
            continue
        video_bytes = _scene_video_bytes_for_language(scene, language)
        if not video_bytes:
            return False
        status = await _run_ai_video_check(
            update,
            session=session,
            scene=scene,
            idx=idx,
            language=language,
            video_bytes=video_bytes,
        )
        if status == AI_APPROVAL_STATUS_APPROVE:
            _mark_ai_video_approved(scene, language)
            continue
        if status == AI_APPROVAL_STATUS_REGENERATE_VIDEO:
            if _ai_attempt_count(scene, "video") <= get_ai_approval_config().max_regenerate_attempts:
                _remove_scene_video_for_language(scene, language)
                _clear_ai_video_approval(scene, language)
                session["scene_media_language_index"] = languages.index(language)
                await update.message.reply_text(f"🤖 ИИ апрув просит перегенерировать видео для языка {language}.")
                return False
        _set_ai_human_review_pending(session, stage="video", reason=f"video QA requires manual review for {language}")
        await update.message.reply_text("ИИ апрув не смог уверенно принять видео, нужна ручная проверка.")
        await _reply_text_with_optional_markup(
            update,
            "Проверьте видео вручную.",
            reply_markup=scene_language_approval_markup(languages, session=session),
        )
        return False
    return True


def _get_scene_policy_candidate(scene: dict, language: str) -> str:
    """Get policy-rewrite candidate for specific language."""
    mapping = scene.get("video_prompt_policy_candidate_by_lang")
    if isinstance(mapping, dict):
        return str(mapping.get(language, "")).strip()
    if language == "ru":
        return str(scene.get("video_prompt_policy_candidate", "")).strip()
    return ""


def _set_scene_policy_candidate(scene: dict, language: str, prompt: str) -> None:
    """Store policy candidate for specific language."""
    mapping = scene.get("video_prompt_policy_candidate_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[language] = prompt
    scene["video_prompt_policy_candidate_by_lang"] = mapping
    if language == "ru":
        scene["video_prompt_policy_candidate"] = prompt


def _parse_lang_payload_command(text: str, keyword: str) -> tuple[str, str] | None:
    """Parse commands like 'ПРОМПТ en: ...' or legacy language-specific feedback."""
    raw = (text or "").strip()
    lower = raw.lower()
    if not lower.startswith(keyword):
        return None
    tail = raw[len(keyword) :].strip()
    if ":" not in tail:
        return None
    lang_raw, payload = tail.split(":", 1)
    lang = normalize_language_code(lang_raw.strip())
    if not lang:
        return None
    return lang, payload.strip()


def _parse_ok_language_command(text: str) -> str | None:
    """Parse command 'OK <lang>' / 'ОК <lang>'."""
    raw = (text or "").strip()
    low = raw.lower()
    if not (low.startswith("ок ") or low.startswith("ok ")):
        return None
    lang = normalize_language_code(raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else "")
    return lang or None


def _parse_ok_speed_command(text: str) -> float | None:
    """Parse command 'OK 1.25' / 'ОК 1.25'."""
    raw = (text or "").strip()
    low = raw.lower()
    if not (low.startswith("ок ") or low.startswith("ok ")):
        return None
    parts = raw.split()
    if len(parts) != 2:
        return None
    return parse_speed_coef(parts[1])


def _parse_regenerate_language_command(text: str) -> str | None:
    """Parse command 'ПЕРЕГЕНЕРИРОВАТЬ <lang>'."""
    raw = (text or "").strip()
    low = raw.lower()
    if not low.startswith("перегенерировать "):
        return None
    lang = normalize_language_code(raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else "")
    return lang or None


def _prefix_video_prompt_with_language(prompt: str, language: str) -> str:
    """Force prompt to start with `lang:<code>` token for downstream correlation."""
    code = normalize_language_code(language) or "ru"
    clean = str(prompt or "").strip()
    if not clean:
        return f"lang:{code}"
    first, *rest = clean.split(maxsplit=1)
    first_low = first.lower()
    body = rest[0] if rest else ""
    if first_low in {f"lang:{code}", f"lang={code}"}:
        return clean
    if first_low.startswith("lang:") or first_low.startswith("lang="):
        return f"lang:{code} {body}".strip()
    return f"lang:{code} {clean}".strip()


async def _apply_scene_video_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    idx: int,
    feedback: str,
    language: str | None = None,
) -> bool:
    feedback_text = str(feedback or "").strip()
    if not feedback_text:
        await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий, что изменить.')
        return True
    languages = _scene_target_languages(session, scene)
    lang = normalize_language_code(str(language or "")) or _scene_current_language(session, scene)
    if lang not in languages:
        lang = languages[0] if languages else "ru"
    current = _scene_video_prompt_for_language(scene, lang)
    try:
        rewritten = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "rewrite_video_prompt_feedback",
            lambda: _rewrite_video_prompt_by_format_instruction(current, feedback_text),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("video prompt feedback rewrite failed")
        await update.message.reply_text(f"Ошибка переформатирования video prompt: {_user_error_message(e)}")
        return True
    applied_languages = _set_scene_video_prompt_for_all_languages(session, scene, rewritten)
    session["scene_media_language_index"] = applied_languages.index(lang) if lang in applied_languages else 0
    has_generated_videos = _all_scene_languages_have_video(scene, languages)
    await update.message.reply_text(
        "Подготовила обновлённый video prompt по фидбэку для языков: "
        f"{', '.join(applied_languages)}. "
        + ("Перегенерируйте нужные языки." if has_generated_videos else "Нажмите «ОК», чтобы сгенерировать видео.")
    )
    if not has_generated_videos:
        await _send_next_scene_media_step(update, session)
    return True


async def _handle_scene_media_common_restarts(update: Update, session: dict, scene: dict, text: str) -> bool:
    has_prompt_override = _extract_prompt_override(text) is not None
    has_format_or_feedback = _extract_format_or_feedback(text) is not None
    if has_prompt_override or has_format_or_feedback:
        return False
    low = str(text or "").strip().lower()
    if low == SCENE_MEDIA_REQUEST_END_FRAME_BUTTON.lower():
        if not _scene_has_available_end_frame_prompt(scene):
            await _reply_text_with_optional_markup(
                update,
                "Для этой сцены нет подготовленного текста конечного кадра. Можно задать его через `ПРОМПТ: ...` на шаге конечного кадра после возврата к production plan.",
                reply_markup=scene_image_approval_markup(session=session),
            )
            return True
        _set_end_frame_requested(scene, True)
        scene.pop("end_frame_image_b64", None)
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await update.message.reply_text("Ок, добавляю конечный кадр для этой сцены.")
        await _send_next_scene_media_step(update, session)
        return True
    if low == SCENE_MEDIA_SKIP_END_FRAME_BUTTON.lower():
        _set_end_frame_requested(scene, False)
        scene.pop("end_frame_image_b64", None)
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        await update.message.reply_text("Ок, конечный кадр для этой сцены не используем.")
        await _send_next_scene_media_step(update, session)
        return True
    if _wants_restart_start_frame(text):
        _reset_video_prompt_policy_failure_count(scene)
        scene.pop("start_frame_image_b64", None)
        scene.pop("end_frame_image_b64", None)
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _reply_text_with_optional_markup(
            update,
            "Возвращаемся к стартовому промпту. Используйте кнопки ниже или отправьте текст правки.",
            reply_markup=scene_image_approval_markup(session=session),
        )
        await _send_next_scene_media_step(update, session)
        return True
    if _wants_restart_end_frame(text):
        if not _scene_needs_end_frame_step(scene):
            await _reply_text_with_optional_markup(
                update,
                "Для этой сцены нет отдельного конечного кадра. Нажмите «старт заново», чтобы пересогласовать стартовый кадр.",
                reply_markup=scene_video_approval_markup(include_restart_end=False, session=session),
            )
            return True
        scene.pop("end_frame_image_b64", None)
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await _reply_text_with_optional_markup(
            update,
            "Возвращаемся к дельте конечного кадра. Используйте кнопки ниже или отправьте текст правки.",
            reply_markup=scene_end_approval_markup(session=session),
        )
        await _send_next_scene_media_step(update, session)
        return True
    return False


async def _scene_media_start_not_ok(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    text: str,
) -> bool:
    pending_prompt = _pop_scene_prompt_input_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        target="start_pre",
    )
    if pending_prompt is not None and _extract_prompt_override(text) is None:
        text = f"ПРОМПТ: {text}"
    manual_prompt = _extract_prompt_override(text)
    if manual_prompt is not None:
        if not manual_prompt:
            _set_scene_prompt_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                target="start_pre",
            )
            await update.message.reply_text('После "ПРОМПТ:" пришлите новый текст промпта.')
            return True
        _apply_shot_type_hint_from_text(scene, manual_prompt)
        scene["start_frame_prompt"] = manual_prompt
        if "start_frame_prompt_ru" in scene:
            scene["start_frame_prompt_ru"] = manual_prompt
        scene[START_PROMPT_RAW_MODE_KEY] = True
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await update.message.reply_text("Обновила стартовый промпт вручную. Сразу генерирую стартовый кадр.")
        return await _scene_media_start_ok(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=int(session.get("image_scene_index", 0) or 0),
        )
    if _is_empty_feedback_command(text):
        await _ask_for_feedback_text(update, "стартовом промпте")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий, что изменить.')
        return True
    _apply_shot_type_hint_from_text(scene, feedback)
    await update.message.reply_text("Перегенерирую стартовый промпт (нейросеть).")
    try:
        new_prompt = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_image_prompt",
            lambda: _regenerate_image_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("image prompt regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации: {_user_error_message(e)}")
        return True
    new_prompt = _enforce_start_prompt_shot_type(scene, new_prompt)
    scene["start_frame_prompt"] = new_prompt
    if "start_frame_prompt_ru" in scene:
        scene["start_frame_prompt_ru"] = new_prompt
    scene[START_PROMPT_RAW_MODE_KEY] = False
    await update.message.reply_text("Готово. Проверь обновлённый стартовый промпт.")
    await _send_next_scene_media_step(update, session)
    return True


async def _scene_media_start_ok(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    idx: int,
) -> bool:
    auto_approve = _is_scene_media_auto_approve_enabled(session)
    try:
        base_prompt = _image_scene_prompt(scene)
    except Exception as e:
        await update.message.reply_text(f"Ошибка подготовки промпта: {e}")
        return True
    scene_refs = _load_scene_reference_images(scene, include_location=False)
    raw_mode = bool(scene.get(START_PROMPT_RAW_MODE_KEY))
    if raw_mode:
        prompt = str(base_prompt or "").strip()
    else:
        prompt = _append_scene_identity_to_image_prompt(
            _append_reference_style_to_image_prompt(base_prompt, bool(scene_refs)),
            scene,
            include_location=False,
        )
    scene["start_frame_prompt_effective"] = prompt
    scene["start_frame_ref_count"] = len(scene_refs)
    logger.info("scene=%s start_image_generation prompt_len=%s refs=%s", idx + 1, len(prompt), len(scene_refs))
    prefix = (
        "Автоапрув: промпт перед генерацией стартового кадра:\n"
        if auto_approve
        else "ИИ апрув: промпт перед генерацией стартового кадра:\n"
        if _is_scene_media_ai_approve_enabled(session)
        else "Промпт перед генерацией стартового кадра:\n"
    )
    await update.message.reply_text(f"{prefix}{_prompt_preview(prompt)}")
    refs_cfg = scene.get("references")
    if isinstance(refs_cfg, dict) and refs_cfg and not scene_refs:
        await update.message.reply_text("Внимание: референсы для сцены указаны, но ни один файл не загрузился. Проверь пути в `references`.")
    if not auto_approve:
        await reply_text_without_keyboard(update.message, "Генерируем стартовый кадр, ждите.")
    image_model = _scene_image_model_for_generation(scene, "start") or primary_image_generation_model()
    try:
        image_result = await _generate_scene_image_result(
            prompt,
            reference_images=scene_refs if scene_refs else None,
            model=image_model,
        )
        image_bytes = image_result.data
        _store_scene_image_model(scene, "start", image_result.model)
        _clear_scene_image_model_choice_pending(scene)
        add_scene_shared_cost(scene, estimate_image_generation_cost_usd(image_result.model))
    except Exception as e:
        logger.exception("vertex image generation failed")
        await _ask_scene_image_model_choice(update, scene=scene, target="start", failed_model=image_model, error=e)
        return True
    bio = io.BytesIO(image_bytes)
    bio.name = f"scene_{idx + 1}.png"
    sent_message = await _safe_reply_photo(
        update.message,
        photo=bio,
        caption=f"Сцена {idx + 1}: стартовый кадр готов.",
    )
    scene["start_frame_image_b64"] = _encode_b64(image_bytes)
    image_candidate = register_image_candidate(scene, kind="start", image_bytes=image_bytes)
    _persist_image_candidate_best_effort(session, scene_idx=idx, candidate=image_candidate)
    register_media_message_candidate(
        session,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="start",
    )
    _persist_message_link_best_effort(
        session,
        chat_id=chat_id,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="start",
    )
    _reset_video_prompt_policy_failure_count(scene)
    scene.pop("end_frame_image_b64", None)
    scene["video_results_by_lang"] = {}
    _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
    session["scene_media_language_index"] = 0
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
    if _is_scene_media_ai_approve_enabled(session):
        image_status = await _run_ai_start_frame_check(
            update,
            session=session,
            scene=scene,
            idx=idx,
            image_bytes=image_bytes,
        )
        if image_status == AI_APPROVAL_STATUS_APPROVE:
            session["scene_media_substep"] = (
                SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
            )
            return True
        if image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
            if _ai_attempt_count(scene, "image") <= get_ai_approval_config().max_regenerate_attempts:
                scene.pop("start_frame_image_b64", None)
                retry_model_message = _prepare_ai_image_retry_model_message(scene, "start")
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                await update.message.reply_text(
                    f"🤖 ИИ апрув просит перегенерировать стартовый фрейм.{retry_model_message}"
                )
                return True
        _set_ai_human_review_pending(session, stage="image", reason="image QA requires manual review")
        await update.message.reply_text("ИИ апрув не смог уверенно принять стартовый фрейм, нужна ручная проверка.")
        await _reply_text_with_optional_markup(
            update,
            "Проверьте стартовый кадр вручную.",
            reply_markup=scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "start"),
                session=session,
            ),
        )
        return True
    if auto_approve:
        return True
    await _reply_text_with_optional_markup(
        update,
        "Проверьте стартовый кадр.\n"
        "«ОК» — перейти дальше к видео/конечному кадру.\n"
        "«ИЗМЕНИТЬ» — поправить текущее изображение по вашему описанию.\n"
        "«ФИДБЕК» — пришлите, что исправить, и перегенерирую промпт + изображение.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — запущу снова с тем же промптом, входными данными и моделью.",
        reply_markup=scene_generated_image_approval_markup(
            include_model_upgrade=_can_escalate_scene_image_model(scene, "start"),
            include_end_frame_choice=_scene_has_available_end_frame_prompt(scene),
            session=session,
        ),
    )
    return True


async def _scene_media_end_not_ok(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    text: str,
) -> bool:
    pending_prompt = _pop_scene_prompt_input_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        target="end_pre",
    )
    if pending_prompt is not None and _extract_prompt_override(text) is None:
        text = f"ПРОМПТ: {text}"
    manual = _extract_prompt_override(text)
    if manual is not None:
        if not manual:
            _set_scene_prompt_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                target="end_pre",
            )
            await update.message.reply_text('После "ПРОМПТ:" пришлите текст дельты конечного кадра.')
            return True
        _set_end_frame_text(scene, manual)
        _set_end_frame_requested(scene, manual.strip().lower() != "пусто")
        scene[END_PROMPT_RAW_MODE_KEY] = True
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await update.message.reply_text("Обновила текст конечного кадра вручную. Сразу генерирую конечный кадр.")
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=int(session.get("image_scene_index", 0) or 0))
    if _is_empty_feedback_command(text):
        await _ask_for_feedback_text(update, "конечном кадре")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий.')
        return True
    await update.message.reply_text("Перегенерирую дельту конечного кадра (нейросеть).")
    try:
        new_end = await _call_with_503_retries(
            update, context, chat_id, "regenerate_end_frame_prompt", lambda: _regenerate_end_frame_prompt_from_feedback(scene, feedback)
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("end frame prompt regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации: {_user_error_message(e)}")
        return True
    _set_end_frame_text(scene, new_end)
    _set_end_frame_requested(scene, new_end.strip().lower() != "пусто")
    scene[END_PROMPT_RAW_MODE_KEY] = False
    await update.message.reply_text("Готово. Проверь обновлённую дельту конечного кадра.")
    await _send_next_scene_media_step(update, session)
    return True


async def _scene_media_edit_generated_image(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
    target: str,
    instruction: str,
) -> bool:
    instruction = str(instruction or "").strip()
    if not instruction:
        _set_scene_image_edit_pending(session, scene_idx=idx, target=target)
        await update.message.reply_text(
            "Напишите, что именно поменять в текущем изображении. Можно одной фразой."
        )
        return True

    is_end = target == "end"
    image_key = "end_frame_image_b64" if is_end else "start_frame_image_b64"
    current_bytes = _decode_b64(str(scene.get(image_key, "")).strip())
    if not current_bytes:
        await update.message.reply_text(
            "Не нашла последнее сгенерированное изображение для правки. Сначала сгенерируйте кадр."
        )
        return True

    base_prompt = (
        str(scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or "").strip()
        if is_end
        else str(scene.get("start_frame_prompt_effective") or scene.get("start_frame_prompt") or scene.get("start_frame_prompt_ru") or "").strip()
    )
    edit_prompt = (
        "Измени приложенное текущее изображение, сохранив композицию, персонажей, стиль, посуду, "
        "предметы и состояние еды максимально близко к исходному кадру.\n"
        "Внеси только эту правку:\n"
        f"{instruction}\n\n"
        "Не добавляй текст, логотипы, подписи и новые лишние объекты.\n\n"
        f"Исходный промпт кадра:\n{base_prompt or '-'}"
    )
    await update.message.reply_text("Изменяю текущий кадр по вашему описанию.")
    try:
        edited_bytes = await _generate_image_with_lenient_fallback(
            edit_prompt,
            reference_images=[current_bytes],
        )
    except Exception as e:
        logger.exception("generated image edit failed")
        await update.message.reply_text(f"Ошибка изменения изображения: {_user_error_message(e)}")
        return True

    bio = io.BytesIO(edited_bytes)
    bio.name = f"scene_{idx + 1}_{'end_' if is_end else ''}edited.png"
    sent_message = await _safe_reply_photo(
        update.message,
        photo=bio,
        caption=f"Сцена {idx + 1}: {'конечный' if is_end else 'стартовый'} кадр изменён.",
    )
    scene[image_key] = _encode_b64(edited_bytes)
    image_candidate = register_image_candidate(scene, kind="end" if is_end else "start", image_bytes=edited_bytes)
    _persist_image_candidate_best_effort(session, scene_idx=idx, candidate=image_candidate)
    register_media_message_candidate(
        session,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="end" if is_end else "start",
    )
    effective_chat = getattr(update, "effective_chat", None)
    _persist_message_link_best_effort(
        session,
        chat_id=getattr(effective_chat, "id", None),
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="end" if is_end else "start",
    )
    if is_end:
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
    else:
        _reset_video_prompt_policy_failure_count(scene)
        scene.pop("end_frame_image_b64", None)
        scene["video_results_by_lang"] = {}
        _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
    session["scene_media_language_index"] = 0
    await _reply_text_with_optional_markup(
        update,
        "Проверьте изменённый кадр.\n"
        "«ОК» — перейти дальше.\n"
        "«ИЗМЕНИТЬ» — снова поправить текущее изображение.\n"
        "«ФИДБЕК» — переписать prompt и сгенерировать заново.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — запустить заново с тем же prompt.",
        reply_markup=scene_generated_image_approval_markup(
            include_model_upgrade=_can_escalate_scene_image_model(scene, target),
            session=session,
        ),
    )
    return True


async def _scene_media_end_ok(update: Update, *, session: dict, scene: dict, idx: int) -> bool:
    auto_approve = _is_scene_media_auto_approve_enabled(session)
    raw_end_mode = bool(scene.get(END_PROMPT_RAW_MODE_KEY))
    end_prompt = str(scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or "").strip() if raw_end_mode else _image_end_scene_prompt(scene)
    if not end_prompt:
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        await _send_next_scene_media_step(update, session)
        return True
    start_bytes = _decode_b64(str(scene.get("start_frame_image_b64", "")).strip())
    if not start_bytes:
        await update.message.reply_text("Нет стартового изображения. Сначала согласуй стартовый кадр.")
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _send_next_scene_media_step(update, session)
        return True
    end_references = _end_frame_reference_images(session, scene, start_bytes)
    end_full = str(end_prompt or "").strip() if raw_end_mode else _append_scene_identity_to_image_prompt(
        _append_reference_style_to_image_prompt(end_prompt, True),
        scene,
        include_location=False,
    )
    if not auto_approve:
        await reply_text_without_keyboard(update.message, "Генерируем конечный кадр (референс — стартовый), ждите.")
    image_model = _scene_image_model_for_generation(scene, "end") or primary_image_generation_model()
    try:
        end_result = await _generate_scene_image_result(
            end_full if raw_end_mode else f"{end_full}\n\n{_end_frame_delta_instruction(scene)}",
            reference_images=end_references,
            model=image_model,
        )
        end_bytes = end_result.data
        _store_scene_image_model(scene, "end", end_result.model)
        _clear_scene_image_model_choice_pending(scene)
        add_scene_shared_cost(scene, estimate_image_generation_cost_usd(end_result.model))
    except Exception as e:
        logger.exception("vertex end-frame generation failed")
        await _ask_scene_image_model_choice(update, scene=scene, target="end", failed_model=image_model, error=e)
        return True
    end_bio = io.BytesIO(end_bytes)
    end_bio.name = f"scene_{idx + 1}_end.png"
    sent_message = await _safe_reply_photo(
        update.message,
        photo=end_bio,
        caption=f"Сцена {idx + 1}: конечный кадр готов.",
    )
    scene["end_frame_image_b64"] = _encode_b64(end_bytes)
    image_candidate = register_image_candidate(scene, kind="end", image_bytes=end_bytes)
    _persist_image_candidate_best_effort(session, scene_idx=idx, candidate=image_candidate)
    register_media_message_candidate(
        session,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="end",
    )
    effective_chat = getattr(update, "effective_chat", None)
    _persist_message_link_best_effort(
        session,
        chat_id=getattr(effective_chat, "id", None),
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="image",
        candidate_id=str(image_candidate["candidate_id"]),
        kind="end",
    )
    scene["video_results_by_lang"] = {}
    _set_all_scene_languages_approval(scene, _scene_target_languages(session, scene), False)
    session["scene_media_language_index"] = 0
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
    if _is_scene_media_ai_approve_enabled(session):
        image_status = await _run_ai_end_frame_check(
            update,
            session=session,
            scene=scene,
            idx=idx,
            image_bytes=end_bytes,
            start_bytes=start_bytes,
        )
        if image_status == AI_APPROVAL_STATUS_APPROVE:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
            return True
        if image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
            if _ai_attempt_count(scene, "end_image") <= get_ai_approval_config().max_regenerate_attempts:
                scene.pop("end_frame_image_b64", None)
                retry_model_message = _prepare_ai_image_retry_model_message(scene, "end")
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
                await update.message.reply_text(
                    f"🤖 ИИ апрув просит перегенерировать конечный фрейм.{retry_model_message}"
                )
                return True
        _set_ai_human_review_pending(session, stage="end_image", reason="end image QA requires manual review")
        await update.message.reply_text("ИИ апрув не смог уверенно принять конечный фрейм, нужна ручная проверка.")
        await _reply_text_with_optional_markup(
            update,
            "Проверьте конечный кадр вручную.",
            reply_markup=scene_generated_image_approval_markup(
                include_model_upgrade=_can_escalate_scene_image_model(scene, "end"),
                session=session,
            ),
        )
        return True
    if auto_approve:
        return True
    await _reply_text_with_optional_markup(
        update,
        "Проверьте конечный кадр.\n"
        "«ОК» — перейти к видео.\n"
        "«ИЗМЕНИТЬ» — поправить текущее изображение по вашему описанию.\n"
        "«ФИДБЕК» — пришлите, что исправить, и перегенерирую промпт + изображение.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — запущу снова с тем же промптом, входными данными и моделью.",
        reply_markup=scene_generated_image_approval_markup(
            include_model_upgrade=_can_escalate_scene_image_model(scene, "end"),
            session=session,
        ),
    )
    return True


async def _scene_media_start_image_approval_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    idx: int,
    text: str,
) -> bool:
    """Handle post-generation approval for start image."""
    pending_prompt = _pop_scene_prompt_input_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        target="start_post",
    )
    if pending_prompt is not None and _extract_prompt_override(text) is None:
        text = f"ПРОМПТ: {text}"
    low = (text or "").strip().lower()
    manual_prompt = _extract_prompt_override(text)
    if manual_prompt is not None:
        if not manual_prompt:
            _set_scene_prompt_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                target="start_post",
            )
            await update.message.reply_text('После "ПРОМПТ:" пришлите новый текст промпта.')
            return True
        _apply_shot_type_hint_from_text(scene, manual_prompt)
        scene["start_frame_prompt"] = manual_prompt
        if "start_frame_prompt_ru" in scene:
            scene["start_frame_prompt_ru"] = manual_prompt
        scene[START_PROMPT_RAW_MODE_KEY] = True
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await update.message.reply_text("Обновила стартовый промпт. Сразу перегенерирую стартовый кадр.")
        return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
    if low in {"ок", "ok", "okay", "approve"}:
        selected = resolve_scene_image_for_video(scene, kind="start")
        if selected is not None:
            approved = mark_image_candidate_approved(scene, str(selected.get("candidate_id") or ""))
            _persist_image_state_best_effort(approved)
        session["scene_media_substep"] = (
            SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
        )
        await _send_next_scene_media_step(update, session)
        return True
    if low in {"перегенерировать", "regen", "regenerate"}:
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
    if low in {SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON.lower(), "upgrade", "upgrade model", "повысить качество"}:
        upgraded_model = _escalate_scene_image_model(scene, "start")
        if not upgraded_model:
            await update.message.reply_text("Модель уже максимальная для текущего профиля. Могу перегенерировать на ней же.")
            return True
        await update.message.reply_text(f"Повышаю модель до `{upgraded_model}` и сразу перегенерирую стартовый кадр.")
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
    edit_instruction = _extract_image_edit_instruction(text)
    if edit_instruction is not None:
        return await _scene_media_edit_generated_image(
            update,
            session=session,
            scene=scene,
            idx=idx,
            target="start",
            instruction=edit_instruction,
        )
    if _is_empty_feedback_command(text):
        await _ask_for_feedback_text(update, "изображении")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text("Пришлите комментарий, что изменить в изображении.")
        return True
    await update.message.reply_text("Перегенерирую стартовый промпт и кадр по фидбеку.")
    try:
        new_prompt = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_image_prompt",
            lambda: _regenerate_image_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("image prompt regeneration from result feedback failed")
        await update.message.reply_text(f"Ошибка перегенерации: {_user_error_message(e)}")
        return True
    scene["start_frame_prompt"] = new_prompt
    if "start_frame_prompt_ru" in scene:
        scene["start_frame_prompt_ru"] = new_prompt
    return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)


async def _scene_media_end_image_approval_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    idx: int,
    text: str,
) -> bool:
    """Handle post-generation approval for end image."""
    pending_prompt = _pop_scene_prompt_input_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        target="end_post",
    )
    if pending_prompt is not None and _extract_prompt_override(text) is None:
        text = f"ПРОМПТ: {text}"
    low = (text or "").strip().lower()
    manual_prompt = _extract_prompt_override(text)
    if manual_prompt is not None:
        if not manual_prompt:
            _set_scene_prompt_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                target="end_post",
            )
            await update.message.reply_text('После "ПРОМПТ:" пришлите новый текст промпта.')
            return True
        _set_end_frame_text(scene, manual_prompt)
        _set_end_frame_requested(scene, manual_prompt.strip().lower() != "пусто")
        scene[END_PROMPT_RAW_MODE_KEY] = True
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await update.message.reply_text("Обновила промпт конечного кадра. Сразу перегенерирую конечный кадр.")
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    if low in {"ок", "ok", "okay", "approve"}:
        selected = resolve_scene_image_for_video(scene, kind="end")
        if selected is not None:
            approved = mark_image_candidate_approved(scene, str(selected.get("candidate_id") or ""))
            _persist_image_state_best_effort(approved)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        await _send_next_scene_media_step(update, session)
        return True
    if low in {"перегенерировать", "regen", "regenerate"}:
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    if low in {SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON.lower(), "upgrade", "upgrade model", "повысить качество"}:
        upgraded_model = _escalate_scene_image_model(scene, "end")
        if not upgraded_model:
            await update.message.reply_text("Модель уже максимальная для текущего профиля. Могу перегенерировать на ней же.")
            return True
        await update.message.reply_text(f"Повышаю модель до `{upgraded_model}` и сразу перегенерирую конечный кадр.")
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    edit_instruction = _extract_image_edit_instruction(text)
    if edit_instruction is not None:
        return await _scene_media_edit_generated_image(
            update,
            session=session,
            scene=scene,
            idx=idx,
            target="end",
            instruction=edit_instruction,
        )
    if _is_empty_feedback_command(text):
        await _ask_for_feedback_text(update, "конечном кадре")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text("Пришлите комментарий, что изменить в конечном кадре.")
        return True
    await update.message.reply_text("Перегенерирую дельту и конечный кадр по фидбеку.")
    try:
        new_end = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_end_frame_prompt",
            lambda: _regenerate_end_frame_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("end frame regeneration from result feedback failed")
        await update.message.reply_text(f"Ошибка перегенерации: {_user_error_message(e)}")
        return True
    _set_end_frame_text(scene, new_end)
    _set_end_frame_requested(scene, new_end.strip().lower() != "пусто")
    return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)


async def _scene_media_video_not_ok(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    session: dict,
    scene: dict,
    text: str,
    language: str,
) -> bool:
    scene["video_prompt_current_language"] = language
    pending_prompt = _pop_scene_prompt_input_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        target=f"video:{language}",
    )
    if pending_prompt is not None and _extract_prompt_override(text) is None:
        text = f"ПРОМПТ: {text}"
    pending = _get_scene_policy_candidate(scene, language)
    raw_text = (text or "").strip()
    languages = _scene_target_languages(session, scene)
    if raw_text.lower().startswith("фраза:"):
        payload = raw_text.split(":", 1)[1].strip()
        if not payload:
            _set_scene_phrase_input_pending(session, scene_idx=int(session.get("image_scene_index", 0) or 0), language=language)
            await update.message.reply_text('После "ФРАЗА:" пришлите текст фразы.')
            return True
        asked = await _apply_scene_phrase_update(
            update,
            session=session,
            scene=scene,
            scene_idx=int(session.get("image_scene_index", 0) or 0),
            language=language,
            phrase_text=payload,
        )
        if asked:
            return True
        await update.message.reply_text(
            f"Фразу для {language} обновила отдельно. Нажмите «ОК», чтобы сгенерировать видео."
        )
        await _send_next_scene_media_step(update, session)
        return True
    prompt_cmd = _parse_lang_payload_command(text, "промпт")
    if prompt_cmd:
        lang, payload = prompt_cmd
        if lang in languages and payload:
            _set_scene_video_prompt_for_language(
                scene,
                lang,
                _compose_locked_video_prompt(scene, lang, payload),
            )
            session["scene_media_language_index"] = languages.index(lang)
            await update.message.reply_text(f"Обновила video prompt для {lang}.")
            await _send_next_scene_media_step(update, session)
            return True
        if lang in languages and not payload:
            await update.message.reply_text(f'После "ПРОМПТ {lang}:" пришлите новый video prompt.')
            return True
    if _parse_lang_payload_command(text, "фидбек"):
        lang, _payload = _parse_lang_payload_command(text, "фидбек") or ("", "")
        if lang in languages:
            session["scene_media_language_index"] = languages.index(lang)
            await update.message.reply_text('Используйте общий формат без языка: "ФИДБЕК: комментарий".')
            return True
    feedback_cmd = _parse_lang_payload_command(text, "фидбек") or _parse_lang_payload_command(text, "исправь")
    if feedback_cmd:
        lang, payload = feedback_cmd
        if lang in languages and payload:
            current = _scene_video_prompt_for_language(scene, lang)
            try:
                rewritten = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "rewrite_video_prompt_language_feedback",
                    lambda: _rewrite_video_prompt_by_format_instruction(current, payload),
                )
            except ServiceUnavailableRetriesExhaustedError as e:
                await update.message.reply_text(str(e))
                return True
            except Exception as e:
                logger.exception("video prompt language-specific rewrite failed")
                await update.message.reply_text(
                    f"Ошибка переформатирования video prompt ({lang}): {_user_error_message(e)}"
                )
                return True
            _set_scene_video_prompt_for_language(
                scene,
                lang,
                _compose_locked_video_prompt(scene, lang, rewritten),
            )
            session["scene_media_language_index"] = languages.index(lang)
            await update.message.reply_text(f"Обновила video prompt для {lang} по фидбеку.")
            await _send_next_scene_media_step(update, session)
            return True
        if lang in languages and not payload:
            session["scene_media_language_index"] = languages.index(lang)
            await update.message.reply_text('Используйте общий формат без языка: "ФИДБЕК: комментарий".')
            return True
    phrase_cmd = _parse_lang_payload_command(text, "фраза")
    if phrase_cmd:
        lang, payload = phrase_cmd
        if lang in languages and payload:
            asked = await _apply_scene_phrase_update(
                update,
                session=session,
                scene=scene,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                language=lang,
                phrase_text=payload,
            )
            if asked:
                return True
            await update.message.reply_text(
                f"Фразу для {lang} обновила отдельно. Нажмите «ОК», чтобы сгенерировать видео."
            )
            session["scene_media_language_index"] = languages.index(lang)
            await _send_next_scene_media_step(update, session)
            return True
        if lang in languages and not payload:
            _set_scene_phrase_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                language=lang,
            )
            await update.message.reply_text('После "ФРАЗА <lang>:" пришлите текст фразы.')
            return True
    if text.strip().lower() in {"фраза", "phrase"}:
        await update.message.reply_text(
            f"Для отдельного апрува фразы используйте команду `ФРАЗА {language}: <текст>`."
        )
        return True
    manual = _extract_prompt_override(text)
    if manual is not None:
        if not manual:
            _set_scene_prompt_input_pending(
                session,
                scene_idx=int(session.get("image_scene_index", 0) or 0),
                target=f"video:{language}",
                language=language,
            )
            await update.message.reply_text('После "ПРОМПТ:" пришлите новый video prompt.')
            return True
        _set_scene_video_prompt_for_language(scene, language, manual)
        _set_scene_video_prompt_raw(scene, language, True)
        _invalidate_scene_language_video(scene, language)
        if pending:
            _set_scene_policy_candidate(scene, language, manual)
        session["scene_media_language_index"] = languages.index(language)
        await update.message.reply_text(f"Обновила video prompt вручную для {language}. Сразу генерирую видео.")
        return await _scene_media_video_ok(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=int(session.get("image_scene_index", 0) or 0),
            total=len(session.get("production", {}).get("json_plan", []) or [scene]),
        )
    if _is_empty_feedback_command(text):
        _set_scene_video_feedback_pending(
            session,
            scene_idx=int(session.get("image_scene_index", 0) or 0),
            language=language,
        )
        await _ask_for_feedback_text(update, "video prompt")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий, что изменить.')
        return True
    if pending and _extract_format_or_feedback(text) is not None:
        try:
            rewritten = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "rewrite_video_prompt_policy_candidate",
                lambda: _rewrite_video_prompt_by_format_instruction(pending, feedback),
            )
            _set_scene_policy_candidate(
                scene,
                language,
                _compose_locked_video_prompt(scene, language, rewritten),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("video prompt format rewrite failed")
            await update.message.reply_text(f"Ошибка переформатирования video prompt: {_user_error_message(e)}")
            return True
        await _reply_text_with_optional_markup(
            update,
            _video_policy_approval_user_message(scene, _get_scene_policy_candidate(scene, language)),
            reply_markup=video_policy_retry_markup(
                include_restart_hint=_should_suggest_regenerate_start_frame_after_video_prompt_fails(scene),
            ),
        )
        return True
    try:
        regenerated = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_video_prompt_from_feedback",
            lambda: _regenerate_video_prompt_from_error(scene, feedback),
        )
        applied_languages = _set_scene_video_prompt_for_all_languages(session, scene, regenerated)
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("video prompt regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации video prompt: {_user_error_message(e)}")
        return True
    await update.message.reply_text(
        f"Обновила video prompt по фидбеку для всех языков: {', '.join(applied_languages)}."
    )
    await _send_next_scene_media_step(update, session)
    return True


async def _maybe_shorten_scene_speech_for_video_budget(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None,
    session: dict,
    chat_id: int | None,
    scene_idx: int,
    scene: dict,
    duration_sec: int,
) -> bool:
    """
    Если реплика длиннее лимита (символы без ``[пауза]``), зовёт Gemini и обновляет ``text`` / ``video_prompt``.

    Returns:
        ``True`` — остановить шаг (пользователь уведомлён об ошибке).
        ``False`` — можно продолжать генерацию видео.
    """
    speech_text = str(scene.get("text", "")).strip()
    if not speech_text:
        return False
    spoken_len = speech_chars_excluding_pauses(speech_text)
    current_max_chars = max_speech_chars_for_duration(duration_sec, "ru")
    if spoken_len <= current_max_chars:
        _refresh_scene_language_speech_metadata(scene, "ru")
        return False
    if duration_sec < 8:
        scene["duration_sec"] = 8
        duration_sec = 8
        max_chars_for_8 = max_speech_chars_for_duration(8, "ru")
        if spoken_len <= max_chars_for_8:
            _refresh_scene_language_speech_metadata(scene, "ru")
            await update.message.reply_text(
                "Реплика длиннее лимита для текущей длительности — увеличила видео до 8 сек вместо сокращения фразы."
            )
            return False
        await update.message.reply_text(
            "Реплика не помещается в текущую длительность — сначала увеличила видео до 8 сек, затем сокращаю фразу под новый лимит."
        )
    max_chars = max_speech_chars_for_duration(duration_sec, "ru")
    if _scene_speech_budget_override_enabled(scene, "ru"):
        return False
    auto_approve = _is_scene_media_auto_approve_enabled(session)
    if _scene_phrase_origin(scene, "ru") == "user":
        _set_scene_speech_pending(
            session,
            scene_idx=scene_idx,
            duration_sec=duration_sec,
            stage="user_long_phrase_choice",
        )
        await _reply_text_with_optional_markup(
            update,
            "Пользовательская фраза всё ещё длиннее лимита даже после увеличения сцены. "
            f"Сейчас {spoken_len} символов речи без учёта [пауза] при лимите {max_chars}. "
            "Выберите: оставить фразу как есть для генерации видео или укоротить её через нейросеть.",
            reply_markup=_scene_speech_user_long_phrase_markup(),
        )
        return True
    if not auto_approve:
        await update.message.reply_text(
            "Реплика длиннее лимита озвучки — отправляю описание нейросети для сокращения…"
        )
    try:
        shortened = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "shorten_scene_speech_for_video_budget",
            lambda: asyncio.to_thread(
                shorten_scene_speech_and_sync_video_prompt_sync,
                scene,
                duration_sec,
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("scene speech auto-shorten failed")
        await update.message.reply_text(f"Не удалось автоматически сократить реплику: {_user_error_message(e)}")
        return True
    if shortened is None:
        _refresh_scene_language_speech_metadata(scene, "ru", status="overflow")
        _set_scene_speech_pending(
            session,
            scene_idx=scene_idx,
            duration_sec=duration_sec,
            stage="choice",
        )
        _schedule_scene_speech_timeout(context=context, session=session, chat_id=chat_id)
        await _reply_text_with_optional_markup(
            update,
            "Не удалось автоматически сократить реплику. "
            f"Сейчас {spoken_len} символов речи без учёта [пауза] при лимите {max_chars}. "
            "Выберите действие: перегенерировать ещё раз или дождаться ручной фразы пользователя.",
            reply_markup=_scene_speech_options_markup(),
        )
        return True
    prompt = _compose_locked_video_prompt(
        scene,
        "ru",
        shortened["video_prompt"],
    )
    cost_event = shortened.get("cost_event")
    if isinstance(cost_event, dict):
        add_scene_language_cost(scene, "ru", cost_event)
    _set_scene_video_prompt_for_language(scene, "ru", prompt)
    _apply_scene_phrase_update_to_scene(
        session,
        scene,
        scene_idx=scene_idx,
        language="ru",
        phrase_text=shortened["text"],
        origin="auto_shorten",
    )
    updated_langs, failed_langs = await _sync_scene_phrase_translations(
        update=update,
        context=context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        scene_idx=scene_idx,
        source_language="ru",
        source_text=shortened["text"],
    )
    spoken_after = speech_chars_excluding_pauses(str(scene.get("text", "")).strip())
    if spoken_after > max_chars:
        _refresh_scene_language_speech_metadata(scene, "ru", status="overflow", auto_shortened=True)
        _set_scene_speech_pending(
            session,
            scene_idx=scene_idx,
            duration_sec=duration_sec,
            stage="choice",
        )
        _schedule_scene_speech_timeout(context=context, session=session, chat_id=chat_id)
        await _reply_text_with_optional_markup(
            update,
            "После автосокращения реплика всё ещё длиннее лимита. "
            f"{spoken_after} символов без [пауза] при лимите {max_chars}. "
            "Можно повторить попытку или прислать короткую фразу вручную.",
            reply_markup=_scene_speech_options_markup(),
        )
        return True
    _refresh_scene_language_speech_metadata(scene, "ru", status="auto_shortened", auto_shortened=True)
    _cancel_scene_speech_timeout(chat_id)
    if auto_approve:
        session.pop(SCENE_SPEECH_PENDING_KEY, None)
        await update.message.reply_text(
            f"Автоапрув: реплика сокращена ({spoken_len} → {spoken_after}), запускаю видео."
        )
        return False
    _set_scene_speech_pending(
        session,
        scene_idx=scene_idx,
        duration_sec=duration_sec,
        stage="approval",
    )
    await _reply_text_with_optional_markup(
        update,
        (
            f"Нейросеть сократила реплику ({spoken_len} → {spoken_after} символов речи без [пауза], лимит {max_chars}).\n"
            + f"Новая фраза:\n{scene['text']}\n\n"
            + (
                f"Переводы обновила для языков: {', '.join(updated_langs)}.\n"
                if updated_langs
                else ""
            )
            + (
                f"Не удалось обновить переводы для языков: {', '.join(failed_langs)}.\n\n"
                if failed_langs
                else ""
            )
            + "Подтвердите запуск видео кнопкой или выберите «Изменить фразу»."
        ),
        reply_markup=_scene_speech_approval_markup(),
    )
    return True


def _scene_prompt_has_fast_delivery_hint(prompt: str) -> bool:
    low = str(prompt or "").lower()
    return "finish the spoken line slightly before the clip ends" in low or "natural-fast spoken delivery" in low


def _boost_scene_prompt_delivery(prompt: str, language: str) -> str:
    text = str(prompt or "").strip()
    lang = normalize_language_code(language) or "ru"
    if not text:
        return text
    if lang != "en" or _scene_prompt_has_fast_delivery_hint(text):
        return text
    hint = (
        " natural-fast spoken delivery; finish the spoken line slightly before the clip ends; "
        "keep articulation clear and avoid draggy pauses."
    )
    return f"{text.rstrip()} {hint}".strip()


async def _maybe_adjust_english_speech_for_video_budget(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    duration_sec: int,
) -> bool:
    """Apply an EN-only prompt-speedup, then shorten only EN if still over budget."""
    text_en = _scene_speech_for_language(scene, "en")
    if not text_en:
        return False
    max_chars = max_speech_chars_for_duration(duration_sec, "en")
    current_chars = speech_chars_excluding_pauses(text_en)
    if current_chars <= max_chars or _scene_speech_budget_override_enabled(scene, "en"):
        _refresh_scene_language_speech_metadata(scene, "en")
        return False
    boosted_prompt = _boost_scene_prompt_delivery(_scene_video_prompt_for_language(scene, "en"), "en")
    _set_scene_video_prompt_for_language(scene, "en", boosted_prompt)
    _set_scene_speech_budget_meta(
        scene,
        "en",
        duration_sec=duration_sec,
        text=text_en,
        status="prompt_tempo_boost",
        auto_shortened=False,
    )
    boosted_limit = max_chars + max(6, duration_sec)
    if current_chars <= boosted_limit:
        await update.message.reply_text(
            "English line is near the timing limit, so I boosted delivery speed in the EN video prompt."
        )
        return False
    try:
        shortened = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "shorten_english_scene_speech_for_video_budget",
            lambda: asyncio.to_thread(
                shorten_scene_speech_and_sync_video_prompt_sync,
                scene,
                duration_sec,
                language="en",
                current_text=text_en,
                current_video_prompt=boosted_prompt,
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("english scene speech auto-shorten failed")
        await update.message.reply_text(f"Не удалось автоматически сократить EN-реплику: {_user_error_message(e)}")
        return True
    if shortened is None:
        _refresh_scene_language_speech_metadata(scene, "en", status="overflow")
        await update.message.reply_text(
            "English line still looks too long for the scene timing. Please shorten the EN phrase or regenerate EN."
        )
        return True
    _apply_scene_phrase_update_to_scene(
        session,
        scene,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        language="en",
        phrase_text=shortened["text"],
        origin="auto_shorten",
    )
    _set_scene_video_prompt_for_language(
        scene,
        "en",
        _compose_locked_video_prompt(scene, "en", _boost_scene_prompt_delivery(shortened["video_prompt"], "en")),
    )
    _refresh_scene_language_speech_metadata(scene, "en", status="auto_shortened", auto_shortened=True)
    await update.message.reply_text(
        "English line did not fit the timing, so I rebuilt only the EN phrase and synced the EN video prompt."
    )
    return False


async def _scene_media_video_ok(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    idx: int,
    total: int,
) -> bool:
    languages = _scene_target_languages(session, scene)
    selected_start_image = resolve_scene_image_for_video(scene, kind="start")
    start_bytes = selected_image_bytes_for_video(scene, kind="start")
    if not start_bytes:
        await update.message.reply_text("Нет стартового кадра для видео. Сначала утверди и сгенерируй стартовый кадр.")
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _send_next_scene_media_step(update, session)
        return True
    end_bytes = selected_image_bytes_for_video(scene, kind="end")

    model = str(scene.get("model", "veo3_fast")).strip() or "veo3_fast"
    try:
        duration_sec = int(scene.get("duration_sec", 6))
    except Exception:
        duration_sec = 6
    if "ru" in languages and not any(_scene_video_prompt_raw(scene, lang) for lang in languages) and await _maybe_shorten_scene_speech_for_video_budget(
        update,
        context=context,
        session=session,
        chat_id=chat_id,
        scene_idx=idx,
        scene=scene,
        duration_sec=duration_sec,
    ):
        return True
    try:
        duration_sec = int(scene.get("duration_sec", duration_sec))
    except Exception:
        pass
    if "en" in languages and not any(_scene_video_prompt_raw(scene, lang) for lang in languages):
        should_stop = await _maybe_adjust_english_speech_for_video_budget(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            duration_sec=duration_sec,
        )
        if should_stop:
            return True

    results_map = scene.get("video_results_by_lang")
    if not isinstance(results_map, dict):
        results_map = {}
    pending_languages = [lang for lang in languages if not isinstance(results_map.get(lang), dict)]
    if not pending_languages:
        pending_languages = [lang for lang in languages if _scene_video_prompt_for_language(scene, lang).strip()]
    if not pending_languages:
        pending_languages = [normalize_language_code(_scene_current_language(session, scene)) or "ru"]

    prompts_by_language: dict[str, str] = {}
    for language in pending_languages:
        scene["video_prompt_current_language"] = language
        pending = _get_scene_policy_candidate(scene, language)
        if pending:
            _set_scene_video_prompt_for_language(
                scene,
                language,
                pending if _scene_video_prompt_raw(scene, language) else _compose_locked_video_prompt(scene, language, pending),
            )
            _set_scene_policy_candidate(scene, language, "")
        prompt_local = _scene_video_prompt_for_language(scene, language) if _scene_video_prompt_raw(scene, language) else _compose_locked_video_prompt(
            scene,
            language,
            _scene_video_prompt_for_language(scene, language),
        )
        speech_budget_map = scene.get("speech_budget_by_lang")
        budget_meta = speech_budget_map.get(language) if isinstance(speech_budget_map, dict) else None
        if language == "en" and isinstance(budget_meta, dict) and str(budget_meta.get("status") or "") == "prompt_tempo_boost":
            prompt_local = _boost_scene_prompt_delivery(prompt_local, language)
        _set_scene_video_prompt_for_language(scene, language, prompt_local)
        prompts_by_language[language] = prompt_local

    async def _generate_for_language(
        language: str,
        *,
        syntax_video: bytes | None = None,
        skip_initial_syntax: bool = False,
        disable_syntax_fallback: bool = False,
    ) -> tuple[str, bytes | None, Exception | None]:
        scene["video_prompt_current_language"] = language
        prompt_local = prompts_by_language.get(language, "")
        if not prompt_local:
            return language, None, RuntimeError("empty video prompt")
        await update.message.reply_text(
            f"Промпт перед генерацией видео — сцена {idx + 1}/{total}, язык {language}:\n"
            f"{_prompt_preview(prompt_local)}"
        )
        if syntax_video is not None:
            return language, syntax_video, None
        try:
            video = await _generate_video_bytes_with_high_load_retry(
                update,
                prompt=prompt_local,
                model=model,
                duration_sec=duration_sec,
                start_image=start_bytes,
                end_image=end_bytes,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
                skip_initial_syntax=skip_initial_syntax,
                disable_syntax_fallback=disable_syntax_fallback,
            )
            return language, video, None
        except VideoProviderChoiceRequired as e:
            return language, None, e
        except Exception as e:
            return language, None, e

    await reply_text_without_keyboard(
        update.message,
        "Генерируем видео параллельно для языков: " + ", ".join(pending_languages),
    )
    syntax_batch_results: dict[str, bytes] = {}
    if len(pending_languages) > 1:
        batch_requests = [
            BatchFallbackRequest(
                key=language,
                prompt=prompts_by_language.get(language, ""),
                start_image=start_bytes,
                end_image=end_bytes,
            )
            for language in pending_languages
            if prompts_by_language.get(language, "")
        ]
        if batch_requests:
            await reply_text_without_keyboard(
                update.message,
                "Сначала отправляю все языки в Syntax одним обработчиком: " + ", ".join(req.key for req in batch_requests),
            )
            syntax_batch_results = await _attempt_syntax_video_fallback_batch(
                requests=batch_requests,
                model=model,
                duration_sec=duration_sec,
            )
            expected_syntax_keys = {str(req.key) for req in batch_requests}
            returned_syntax_keys = set(syntax_batch_results)
            missing_syntax_keys = sorted(expected_syntax_keys - returned_syntax_keys)
            if missing_syntax_keys:
                for language in sorted(returned_syntax_keys):
                    await _deliver_scene_video_result(
                        update,
                        chat_id=int(chat_id) if chat_id is not None else 0,
                        session=session,
                        scene=scene,
                        idx=idx,
                        total=total,
                        language=language,
                        video_bytes=syntax_batch_results[language],
                        provider="telegram",
                        caption_suffix=". Источник: Syntax.",
                    )
                scene["video_provider_choice_pending"] = {
                    "source": "scene_batch",
                    "languages": missing_syntax_keys,
                    "missing_languages": missing_syntax_keys,
                    "entries_by_lang": {
                        language: _scene_video_provider_choice_entry(
                            prompt=prompts_by_language.get(language, ""),
                            model=model,
                            duration_sec=duration_sec,
                            start_image=start_bytes,
                            end_image=end_bytes,
                            quality="1080",
                            aspect_ratio="9:16",
                            audio_enabled=True,
                            syntax_error_message=(
                                "Syntax вернул не весь набор языков. "
                                f"Не пришли: {', '.join(missing_syntax_keys)}."
                            ),
                        )
                        for language in missing_syntax_keys
                        if prompts_by_language.get(language, "")
                    },
                }
                _store_syntax_retry_context(
                    scene,
                    languages=missing_syntax_keys,
                    pending_payload=scene["video_provider_choice_pending"],
                    error_message="Syntax вернул не весь набор языков.",
                )
                ready_syntax_keys = sorted(returned_syntax_keys)
                await _reply_text_with_optional_markup(
                    update,
                    (
                        "Syntax вернул не весь набор языков. Автоматически в Vertex не ухожу.\n\n"
                        f"Языки сцены: {', '.join(sorted(expected_syntax_keys))}\n"
                        f"Готово из Syntax: {', '.join(ready_syntax_keys) or 'ничего'}\n"
                        f"Не пришли из Syntax: {', '.join(missing_syntax_keys)}\n\n"
                        "Напишите `Syntax`, чтобы отправить недостающие языки ещё раз через Syntax, "
                        "или `Vertex`, чтобы запустить Vertex/Veo для недостающих языков."
                    ),
                    reply_markup=_syntax_recovery_markup(missing_syntax_keys),
                )
                return True
            elif syntax_batch_results:
                await reply_text_without_keyboard(
                    update.message,
                    "Syntax already returned: " + ", ".join(sorted(syntax_batch_results)),
                )

    generated = await asyncio.gather(
        *[
            _generate_for_language(
                lang,
                syntax_video=syntax_batch_results.get(lang),
                skip_initial_syntax=bool(syntax_batch_results),
                disable_syntax_fallback=False,
            )
            for lang in pending_languages
        ]
    )

    for language, video_bytes, err in generated:
        if video_bytes is not None:
            provider = get_last_video_provider() or ("telegram" if syntax_batch_results.get(language) is not None else "veo")
            await _deliver_scene_video_result(
                update,
                chat_id=int(chat_id) if chat_id is not None else 0,
                session=session,
                scene=scene,
                idx=idx,
                total=total,
                language=language,
                video_bytes=video_bytes,
                provider=provider,
                caption_suffix=".",
            )
            continue
        if err is None:
            continue
        logger.exception("scene video generation failed for language=%s", language, exc_info=err)
        if isinstance(err, VideoProviderChoiceRequired):
            payload = encode_video_provider_choice_for_session(err)
            payload["source"] = "scene"
            payload["language"] = language
            scene["video_provider_choice_pending"] = payload
            _store_syntax_retry_context(
                scene,
                languages=[language],
                pending_payload=payload,
                error_message=str(getattr(err, "syntax_error_message", "") or err),
            )
            await _reply_text_with_optional_markup(
                update,
                (
                    f"Сцена {idx + 1}/{total}, язык {language}: Syntax не вернул видео.\n\n"
                    "Напишите `Syntax`, чтобы запустить эту сцену ещё раз через Syntax, "
                    "или `Vertex`, чтобы попробовать резервный канал Vertex/Veo."
                ),
                reply_markup=_syntax_recovery_markup([language]),
            )
            continue
        if _is_video_prompt_filtered_error(err) or _is_video_policy_error(err) or _is_video_safety_error(err):
            try:
                candidate = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "regenerate_video_prompt_policy_error",
                    lambda: _regenerate_video_prompt_from_error(scene, str(err)),
                )
                _set_scene_policy_candidate(
                    scene,
                    language,
                    _compose_locked_video_prompt(scene, language, candidate),
                )
                try:
                    prev_fails = int(scene.get("video_prompt_policy_failure_count", 0) or 0)
                except (TypeError, ValueError):
                    prev_fails = 0
                scene["video_prompt_policy_failure_count"] = prev_fails + 1
                await _reply_text_with_optional_markup(
                    update,
                    _video_policy_approval_user_message(scene, _get_scene_policy_candidate(scene, language)),
                    reply_markup=video_policy_retry_markup(
                        include_restart_hint=_should_suggest_regenerate_start_frame_after_video_prompt_fails(scene),
                    ),
                )
            except ServiceUnavailableRetriesExhaustedError as rewrite_err:
                await update.message.reply_text(
                    f"Видео ({language}) отклонено политиками, а AI временно не смог переписать prompt: {rewrite_err}"
                )
            except Exception as rewrite_err:
                logger.exception("video prompt policy rewrite failed")
                await update.message.reply_text(
                    f"Видео ({language}) отклонено политиками, и не удалось переписать prompt: {_user_error_message(rewrite_err, domain='video')}"
                )
        else:
            await update.message.reply_text(
                f"Ошибка генерации видео ({language}): {_user_error_message(err, domain='video')}"
            )

    session["scene_media_language_index"] = 0
    requires_multilingual_approval = (
        len(languages) > 1 or isinstance(scene.get("video_prompt_by_lang"), dict)
    )
    auto_approve = _is_scene_media_auto_approve_enabled(session)
    if not _all_scene_languages_have_video(scene, languages):
        await _reply_text_with_optional_markup(
            update,
            "Не все языки имеют сгенерированные видео. Можно исправить prompt по языку или перегенерировать конкретный язык.",
            reply_markup=scene_language_approval_markup(languages, session=session),
        )
        return True

    if _is_scene_media_ai_approve_enabled(session):
        approved_by_ai = await _ai_check_existing_scene_videos(
            update,
            session=session,
            scene=scene,
            idx=idx,
            languages=languages,
        )
        if not approved_by_ai:
            return True

    if not requires_multilingual_approval:
        for language in languages:
            _approve_scene_video_language(scene, language, speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF)
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            return await _finish_scene_media_production(update, session)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _send_next_scene_media_step(update, session)
        return True

    if auto_approve:
        for language in languages:
            _approve_scene_video_language(scene, language, speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF)
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            return await _finish_scene_media_production(update, session)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        return True

    await _reply_text_with_optional_markup(
        update,
        "Видео по всем языкам для сцены готовы.\n"
        "Доступные команды: `ОК все`, `ОК <lang>`, `ПРОМПТ <lang>: ...`, "
        "`ФИДБЕК: ...`, `ФРАЗА <lang>: ...`, `ПЕРЕГЕНЕРИРОВАТЬ <lang>`, `СКОРОСТЬ 1.25`.\n"
        "Или нажмите `ИЗМЕНИТЬ СКОРОСТЬ`, пришлите коэффициент и текущее видео сразу заапрувится.\n"
        "Для одной языковой версии можно сразу написать `ОК 1.25`.\n"
        "Переход к следующей сцене произойдёт после апрува всех языков.",
        reply_markup=scene_language_approval_markup(languages, session=session),
    )
    if _all_scene_languages_approved(scene, languages):
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            return await _finish_scene_media_production(update, session)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _send_next_scene_media_step(update, session)
        return True

    return True


async def _run_scene_media_auto_approve(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: int | None,
    session: dict,
) -> bool:
    """
    Runs scene media generation without waiting for frame/video approvals.

    Stops only when production is finished or a generation function leaves the
    state unchanged, which means it already asked for human input because of an
    error or fallback.
    """
    max_steps = 500
    for _ in range(max_steps):
        scenes, idx = _scene_media_context(session)
        if not scenes:
            await update.message.reply_text("Нет сцен. Начни заново через /generate.")
            return True
        if idx >= len(scenes):
            return await _finish_scene_media_production(update, session)
        scene = scenes[idx]
        sub = session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START)
        before = (
            idx,
            sub,
            session.get("scene_media_language_index", 0),
            bool(scene.get("start_frame_image_b64")),
            bool(scene.get("end_frame_image_b64")),
            len(scene.get("video_results_by_lang") or {}),
            session.get("state"),
        )

        if sub == SCENE_MEDIA_SUBSTEP_START:
            await _scene_media_start_ok(update, session=session, scene=scene, idx=idx)
        elif sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
            session["scene_media_substep"] = (
                SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
            )
        elif sub == SCENE_MEDIA_SUBSTEP_END:
            await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
        elif sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        elif sub == SCENE_MEDIA_SUBSTEP_VIDEO:
            languages = _scene_target_languages(session, scene)
            if _all_scene_languages_have_video(scene, languages):
                for language in languages:
                    _approve_scene_video_language(scene, language, speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF)
                session["scene_media_language_index"] = 0
                session["image_scene_index"] = idx + 1
                if session["image_scene_index"] >= len(scenes):
                    return await _finish_scene_media_production(update, session)
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            else:
                await _scene_media_video_ok(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=len(scenes),
                )
        else:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START

        if session.get("state") == STATE_READY_FOR_PRODUCTION:
            session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
            await _send_final_social_descriptions(update, session)
            return True

        scenes_after, idx_after = _scene_media_context(session)
        if not scenes_after or idx_after >= len(scenes_after):
            continue
        scene_after = scenes_after[idx_after]
        after = (
            idx_after,
            session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START),
            session.get("scene_media_language_index", 0),
            bool(scene_after.get("start_frame_image_b64")),
            bool(scene_after.get("end_frame_image_b64")),
            len(scene_after.get("video_results_by_lang") or {}),
            session.get("state"),
        )
        if after == before:
            return True

    await update.message.reply_text("Автоапрув остановлен: слишком много внутренних шагов подряд.")
    return True


async def _run_scene_media_ai_approve(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: int | None,
    session: dict,
) -> bool:
    """
    Runs scene media generation with QA checks at prompt/image/video boundaries.

    Any uncertain or failed QA result sets a human-review flag and returns to the
    regular manual approval controls for the current scene.
    """
    max_steps = 500
    for _ in range(max_steps):
        if not _is_scene_media_ai_approve_enabled(session):
            return True
        scenes, idx = _scene_media_context(session)
        if not scenes:
            await update.message.reply_text("Нет сцен. Начни заново через /generate.")
            return True
        if idx >= len(scenes):
            return await _finish_scene_media_production(update, session)
        scene = scenes[idx]
        sub = session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START)
        before = (
            idx,
            sub,
            session.get("scene_media_language_index", 0),
            bool(scene.get("start_frame_image_b64")),
            bool(scene.get("end_frame_image_b64")),
            len(scene.get("video_results_by_lang") or {}),
            len(scene.get("ai_approval_history") or []),
            session.get("state"),
        )

        if sub == SCENE_MEDIA_SUBSTEP_START:
            await _scene_media_start_ok(
                update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
            )
        elif sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
            image_bytes = _decode_b64(str(scene.get("start_frame_image_b64", "")).strip())
            if not image_bytes:
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            else:
                image_status = await _run_ai_start_frame_check(
                    update,
                    session=session,
                    scene=scene,
                    idx=idx,
                    image_bytes=image_bytes,
                )
                if image_status == AI_APPROVAL_STATUS_APPROVE:
                    session["scene_media_substep"] = (
                        SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
                    )
                elif image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE and _ai_attempt_count(scene, "image") <= get_ai_approval_config().max_regenerate_attempts:
                    scene.pop("start_frame_image_b64", None)
                    retry_model_message = _prepare_ai_image_retry_model_message(scene, "start")
                    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                    await update.message.reply_text(
                        f"🤖 ИИ апрув просит перегенерировать стартовый фрейм.{retry_model_message}"
                    )
                else:
                    _set_ai_human_review_pending(session, stage="image", reason="image QA requires manual review")
                    await update.message.reply_text("ИИ апрув отправил стартовый фрейм на ручную проверку.")
                    await _reply_text_with_optional_markup(
                        update,
                        "Проверьте стартовый кадр вручную.",
                        reply_markup=scene_generated_image_approval_markup(
                            include_model_upgrade=_can_escalate_scene_image_model(scene, "start"),
                            session=session,
                        ),
                    )
                    return True
        elif sub == SCENE_MEDIA_SUBSTEP_END:
            await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
        elif sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        elif sub == SCENE_MEDIA_SUBSTEP_VIDEO:
            languages = _scene_target_languages(session, scene)
            if _all_scene_languages_have_video(scene, languages):
                approved = await _ai_check_existing_scene_videos(
                    update,
                    session=session,
                    scene=scene,
                    idx=idx,
                    languages=languages,
                )
                if not approved:
                    return True
                for language in languages:
                    _approve_scene_video_language(scene, language, speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF)
                session["scene_media_language_index"] = 0
                session["image_scene_index"] = idx + 1
                if session["image_scene_index"] >= len(scenes):
                    return await _finish_scene_media_production(update, session)
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            else:
                await _scene_media_video_ok(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=len(scenes),
                )
        else:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START

        if session.get("state") == STATE_READY_FOR_PRODUCTION:
            await _send_final_social_descriptions(update, session)
            return True

        scenes_after, idx_after = _scene_media_context(session)
        if not scenes_after or idx_after >= len(scenes_after):
            continue
        scene_after = scenes_after[idx_after]
        after = (
            idx_after,
            session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START),
            session.get("scene_media_language_index", 0),
            bool(scene_after.get("start_frame_image_b64")),
            bool(scene_after.get("end_frame_image_b64")),
            len(scene_after.get("video_results_by_lang") or {}),
            len(scene_after.get("ai_approval_history") or []),
            session.get("state"),
        )
        if after == before:
            return True

    await update.message.reply_text("ИИ апрув остановлен: слишком много внутренних шагов подряд.")
    return True


async def _handle_scene_syntax_fallback_pending(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """
    Подтверждение human-in-the-loop для Syntax fallback на шаге video в пошаговых сценах.
    """
    scenes, idx = _scene_media_context(session)
    if not scenes or idx >= len(scenes):
        return False
    scene = scenes[idx]
    pending = scene.get("syntax_fallback_pending")
    if not isinstance(pending, dict) or pending.get("source") != "scene":
        return False
    if session.get("scene_media_substep") != SCENE_MEDIA_SUBSTEP_VIDEO:
        return False
    if update.message is None:
        return True
    choice = syntax_fallback_parse_user_confirmation(text)
    if choice is None:
        await _reply_text_with_optional_markup(
            update,
            "Нажмите «да» (запустить Syntax) или «нет» (переписать video prompt, как после отклонения Vertex).",
            reply_markup=ReplyKeyboardMarkup(
                [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                resize_keyboard=True,
                one_time_keyboard=False,
            ),
        )
        return True
    total = len(scenes)
    language = normalize_language_code(str(pending.get("language") or _scene_current_language(session, scene))) or _scene_current_language(session, scene)
    languages = _scene_target_languages(session, scene)
    err_text = str(pending.get("vertex_error_message") or "")

    async def _offer_policy_rewrite_after_syntax_skip() -> None:
        try:
            candidate = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "regenerate_video_prompt_after_syntax_skip",
                lambda: _regenerate_video_prompt_from_error(scene, err_text),
            )
            _set_scene_policy_candidate(
                scene,
                language,
                _compose_locked_video_prompt(scene, language, candidate),
            )
            try:
                prev_fails = int(scene.get("video_prompt_policy_failure_count", 0) or 0)
            except (TypeError, ValueError):
                prev_fails = 0
            scene["video_prompt_policy_failure_count"] = prev_fails + 1
            await _reply_text_with_optional_markup(
                update,
                _video_policy_approval_user_message(scene, _get_scene_policy_candidate(scene, language)),
                reply_markup=video_policy_retry_markup(
                    include_restart_hint=_should_suggest_regenerate_start_frame_after_video_prompt_fails(scene),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as rewrite_err:
            await update.message.reply_text(f"AI временно не смог переписать video prompt: {rewrite_err}")
        except Exception as rewrite_err:
            logger.exception("scene policy rewrite after syntax path failed")
            await update.message.reply_text(
                f"Не удалось переписать video prompt: {_user_error_message(rewrite_err, domain='video')}"
            )

    if choice is False:
        scene.pop("syntax_fallback_pending", None)
        await update.message.reply_text("Ок, Syntax не запускаю.")
        await _offer_policy_rewrite_after_syntax_skip()
        return True

    payload = dict(pending)
    scene.pop("syntax_fallback_pending", None)
    await update.message.reply_text("Запускаю Syntax fallback по вашему подтверждению…")
    try:
        video_bytes = await run_syntax_fallback_from_session_payload(payload)
    except Exception as fb_err:
        logger.exception("scene Syntax fallback execution failed")
        await update.message.reply_text(
            f"Ошибка Syntax fallback: {_user_error_message(fb_err, domain='video')}"
        )
        await _offer_policy_rewrite_after_syntax_skip()
        return True
    if not video_bytes:
        await update.message.reply_text("Syntax не вернул видео. Переходим к переписыванию prompt.")
        await _offer_policy_rewrite_after_syntax_skip()
        return True

    vb = io.BytesIO(video_bytes)
    vb.name = f"scene_{idx + 1}_{language}.mp4"
    sent_message = await _safe_reply_video(
        update.message,
        video=vb,
        caption=(
            f"Сцена {idx + 1}/{total}, язык {language}: видео готово. "
            "Источник: Syntax fallback."
        ),
    )
    cost_summary = _store_scene_video_result(
        scene,
        language,
        video_bytes,
        session=session,
        scene_idx=idx,
        provider="telegram",
        source_image_candidate_id=str((resolve_scene_image_for_video(scene, kind='start') or {}).get("candidate_id") or ""),
    )
    latest_candidate = latest_scene_video_candidate(scene, language)
    register_media_message_candidate(
        session,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="video",
        candidate_id=str((latest_candidate or {}).get("candidate_id") or ""),
        language=language,
    )
    _persist_message_link_best_effort(
        session,
        chat_id=chat_id,
        message_id=getattr(sent_message, "message_id", None),
        scene_idx=idx,
        media_type="video",
        candidate_id=str((latest_candidate or {}).get("candidate_id") or ""),
        language=language,
    )
    _set_scene_policy_candidate(scene, language, "")
    _reset_video_prompt_policy_failure_count(scene)
    await update.message.reply_text(
        f"Оценка себестоимости видео ({language}): ~${float(cost_summary['estimated_total_cost_usd']):.4f}."
    )
    requires_multilingual_approval = (
        len(languages) > 1 or isinstance(scene.get("video_prompt_by_lang"), dict)
    )
    current_idx = int(session.get("scene_media_language_index", 0) or 0)
    if current_idx + 1 < len(languages):
        session["scene_media_language_index"] = current_idx + 1
    else:
        session["scene_media_language_index"] = 0
    if not requires_multilingual_approval and session["scene_media_language_index"] == 0:
        for lang in languages:
            _approve_scene_video_language(scene, lang, speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF)
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            return await _finish_scene_media_production(update, session)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
    await _send_next_scene_media_step(update, session)
    if _is_scene_media_auto_approve_enabled(session):
        return await _run_scene_media_auto_approve(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
        )
    return True


async def _handle_scene_image_model_choice_pending(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    scenes, idx = _scene_media_context(session)
    if not scenes or idx >= len(scenes):
        return False
    scene = scenes[idx]
    pending = scene.get(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY)
    if not isinstance(pending, dict):
        return False
    if update.message is None:
        return True
    target = "end" if str(pending.get("target") or "").strip() == "end" else "start"
    low = str(text or "").strip().lower()
    retry_model = str(pending.get("retry_model") or primary_image_generation_model()).strip() or primary_image_generation_model()
    lower_model = str(pending.get("lower_model") or "").strip()
    retry_commands = {
        SCENE_IMAGE_MODEL_RETRY_BUTTON.lower(),
        "повторить",
        "повторить сильную",
        "retry",
        "retry strong",
        "strong",
    }
    downgrade_commands = {
        SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON.lower(),
        "понизить",
        "понизить модель",
        "downgrade",
        "lower",
    }
    if low not in retry_commands and low not in downgrade_commands:
        await update.message.reply_text(
            "Выберите, что делать с неудачной генерацией изображения: повторить сильную модель или понизить модель.",
            reply_markup=_scene_image_model_retry_markup(lower_model=lower_model),
        )
        return True
    if low in downgrade_commands:
        if not lower_model:
            await update.message.reply_text(
                f"Модель ниже не настроена. Могу повторить сильную `{retry_model}`.",
                reply_markup=_scene_image_model_retry_markup(lower_model=None),
            )
            return True
        selected_model = lower_model
        await update.message.reply_text(f"Понижаю модель до `{selected_model}` и перегенерирую кадр.")
        scene[_scene_image_model_manual_key(target)] = True
    else:
        selected_model = retry_model
        await update.message.reply_text(f"Повторяю генерацию на сильной модели `{selected_model}`.")
        scene.pop(_scene_image_model_manual_key(target), None)
    scene[_scene_image_model_key(target)] = selected_model
    _clear_scene_image_model_choice_pending(scene)
    if target == "end":
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
    return await _scene_media_start_ok(
        update,
        context=context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        idx=idx,
    )


def _refresh_video_provider_choice_payloads(scene: dict, pending: dict, languages: list[str]) -> None:
    """Keep retry payload prompts aligned with the editable scene prompts."""
    if pending.get("source") == "scene_batch":
        entries_by_lang = pending.get("entries_by_lang")
        if not isinstance(entries_by_lang, dict):
            return
        for language in languages:
            entry = entries_by_lang.get(language)
            if isinstance(entry, dict):
                entry["prompt"] = _scene_video_prompt_for_language(scene, language)
        return
    language = normalize_language_code(str(pending.get("language") or "")) or (languages[0] if languages else "ru")
    pending["prompt"] = _scene_video_prompt_for_language(scene, language)


async def _handle_scene_video_provider_choice_edit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    pending: dict,
    text: str,
    languages: list[str],
) -> bool:
    """Handle prompt/feedback/phrase edits while Syntax/Vertex choice is pending."""
    prompt_cmd = _parse_lang_payload_command(text, "промпт")
    phrase_cmd = _parse_lang_payload_command(text, "фраза")
    feedback = _extract_format_or_feedback(text)
    generic_prompt = _extract_prompt_override(text)

    if prompt_cmd:
        lang, payload = prompt_cmd
        if lang not in languages:
            await update.message.reply_text(
                f"Язык `{lang}` не входит в текущий набор: {', '.join(languages) or 'ru'}."
            )
            return True
        if not payload:
            await update.message.reply_text(f'После "ПРОМПТ {lang}:" пришлите новый prompt.')
            return True
        _set_scene_video_prompt_for_language(scene, lang, _compose_locked_video_prompt(scene, lang, payload))
        session["scene_media_language_index"] = languages.index(lang)
        _refresh_video_provider_choice_payloads(scene, pending, languages)
        await _reply_text_with_optional_markup(
            update,
            f"Обновила retry prompt для {lang}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(languages),
        )
        return True

    if phrase_cmd:
        lang, payload = phrase_cmd
        if lang not in languages:
            await update.message.reply_text(
                f"Язык `{lang}` не входит в текущий набор: {', '.join(languages) or 'ru'}."
            )
            return True
        if not payload:
            await update.message.reply_text(f'После "ФРАЗА {lang}:" пришлите текст фразы.')
            return True
        asked = await _apply_scene_phrase_update(
            update,
            session=session,
            scene=scene,
            scene_idx=int(session.get("image_scene_index", 0) or 0),
            language=lang,
            phrase_text=payload,
        )
        session["scene_media_language_index"] = languages.index(lang)
        _refresh_video_provider_choice_payloads(scene, pending, languages)
        if asked:
            return True
        await _reply_text_with_optional_markup(
            update,
            f"Фразу для {lang} обновила. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(languages),
        )
        return True

    if generic_prompt is not None:
        lang = _scene_current_language(session, scene)
        if lang not in languages and languages:
            lang = languages[0]
        if not generic_prompt:
            await update.message.reply_text('После "ПРОМПТ:" пришлите новый prompt.')
            return True
        _set_scene_video_prompt_for_language(scene, lang, _compose_locked_video_prompt(scene, lang, generic_prompt))
        if lang in languages:
            session["scene_media_language_index"] = languages.index(lang)
        _refresh_video_provider_choice_payloads(scene, pending, languages)
        await _reply_text_with_optional_markup(
            update,
            f"Обновила retry prompt для {lang}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(languages),
        )
        return True

    if feedback is not None:
        if not feedback.strip():
            await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий.')
            return True
        current_lang = _scene_current_language(session, scene)
        current = _scene_video_prompt_for_language(scene, current_lang)
        try:
            rewritten = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "rewrite_video_prompt_feedback_pending_provider",
                lambda: _rewrite_video_prompt_by_format_instruction(current, feedback),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("pending provider video prompt feedback rewrite failed")
            await update.message.reply_text(f"Ошибка переформатирования video prompt: {_user_error_message(e)}")
            return True
        applied_languages = _set_scene_video_prompt_for_all_languages(session, scene, rewritten)
        _refresh_video_provider_choice_payloads(scene, pending, applied_languages)
        await _reply_text_with_optional_markup(
            update,
            "Обновила retry prompt по фидбэку для языков: "
            f"{', '.join(applied_languages)}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(applied_languages),
        )
        return True

    return False


async def _continue_after_scene_video_provider_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    scene: dict,
    idx: int,
    total: int,
) -> bool:
    """Advance from provider recovery and redraw the keyboard from the resulting state."""
    languages = _scene_target_languages(session, scene)
    session["scene_media_language_index"] = 0

    if not _all_scene_languages_have_video(scene, languages):
        await _reply_current_scene_media_keyboard(
            update,
            session,
            "Не все языки имеют готовое видео. Выберите провайдера для оставшихся языков.",
        )
        return True

    if _is_scene_media_ai_approve_enabled(session):
        approved_by_ai = await _ai_check_existing_scene_videos(
            update,
            session=session,
            scene=scene,
            idx=idx,
            languages=languages,
        )
        if not approved_by_ai:
            return True

    requires_multilingual_approval = (
        len(languages) > 1 or isinstance(scene.get("video_prompt_by_lang"), dict)
    )
    automatic = (
        _is_scene_media_auto_approve_enabled(session)
        or _is_scene_media_ai_approve_enabled(session)
    )
    if not requires_multilingual_approval or automatic:
        for language in languages:
            _approve_scene_video_language(
                scene,
                language,
                speed_coef=DEFAULT_SCENE_VIDEO_SPEED_COEF,
            )
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            return await _finish_scene_media_production(update, session)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        if _is_scene_media_auto_approve_enabled(session):
            return await _run_scene_media_auto_approve(
                update,
                context=context,
                chat_id=chat_id,
                session=session,
            )
        if _is_scene_media_ai_approve_enabled(session):
            return await _run_scene_media_ai_approve(
                update,
                context=context,
                chat_id=chat_id,
                session=session,
            )
        await _send_next_scene_media_step(update, session)
        return True

    await _reply_current_scene_media_keyboard(
        update,
        session,
        "Видео по всем языкам готовы. Подтвердите нужные версии или внесите правки.",
    )
    return True


async def _handle_scene_video_provider_choice_pending(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """
    Human-in-the-loop choice after automatic Syntax attempt failed for a scene video.
    """
    scenes, idx = _scene_media_context(session)
    if not scenes or idx >= len(scenes):
        return False
    scene = scenes[idx]
    pending = scene.get("video_provider_choice_pending")
    if not isinstance(pending, dict) or pending.get("source") not in {"scene", "scene_batch"}:
        return False
    if session.get("scene_media_substep") != SCENE_MEDIA_SUBSTEP_VIDEO:
        return False
    if update.message is None:
        return True
    pending_languages = (
        [
            normalize_language_code(str(lang)) or str(lang)
            for lang in pending.get("languages", [])
            if str(lang).strip()
        ]
        if pending.get("source") == "scene_batch"
        else _scene_target_languages(session, scene)
    )
    if await _handle_scene_video_provider_choice_edit_command(
        update,
        context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        pending=pending,
        text=text,
        languages=pending_languages,
    ):
        return True
    choice = video_provider_choice_parse_user_selection(text)
    if choice is None:
        await _reply_text_with_optional_markup(
            update,
            "Нажмите `Syntax` для повтора через Syntax или `Vertex` для резервного Vertex/Veo.",
            reply_markup=_syntax_recovery_markup(pending_languages),
        )
        return True

    total = len(scenes)
    if pending.get("source") == "scene_batch":
        entries_by_lang = pending.get("entries_by_lang")
        languages = [
            normalize_language_code(str(lang)) or str(lang)
            for lang in pending.get("languages", [])
            if str(lang).strip()
        ]
        if not isinstance(entries_by_lang, dict) or not languages:
            scene.pop("video_provider_choice_pending", None)
            await _reply_current_scene_media_keyboard(
                update,
                session,
                "Не нашла корректные данные для повторного выбора провайдера видео.",
            )
            return True
        if choice == "syntax":
            await update.message.reply_text(
                "Повторяю эту сцену через Syntax сразу для всех языков…"
            )
            requests = [
                BatchFallbackRequest(
                    key=language,
                    prompt=str(entries_by_lang.get(language, {}).get("prompt") or ""),
                    start_image=_decode_b64(str(entries_by_lang.get(language, {}).get("start_image_b64") or "")),
                    end_image=(
                        _decode_b64(str(entries_by_lang.get(language, {}).get("end_image_b64") or ""))
                        if str(entries_by_lang.get(language, {}).get("end_image_b64") or "").strip()
                        else None
                    ),
                )
                for language in languages
                if isinstance(entries_by_lang.get(language), dict) and str(entries_by_lang.get(language, {}).get("prompt") or "").strip()
            ]
            try:
                syntax_batch_results = await _attempt_syntax_video_fallback_batch(
                    requests=requests,
                    model=str(next(iter(entries_by_lang.values())).get("model") or scene.get("model") or "veo3_fast"),
                    duration_sec=int(next(iter(entries_by_lang.values())).get("duration_sec") or scene.get("duration_sec") or 6),
                )
            except Exception as fb_err:
                logger.exception("scene Syntax batch retry execution failed")
                await _reply_current_scene_media_keyboard(
                    update,
                    session,
                    f"Ошибка Syntax: {_user_error_message(fb_err, domain='video')}",
                )
                return True
            missing = [language for language in languages if language not in syntax_batch_results]
            if missing:
                for language in sorted(set(syntax_batch_results) & set(languages)):
                    await _deliver_scene_video_result(
                        update,
                        chat_id=chat_id,
                        session=session,
                        scene=scene,
                        idx=idx,
                        total=total,
                        language=language,
                        video_bytes=syntax_batch_results[language],
                        provider="telegram",
                        caption_suffix=". Источник: Syntax.",
                    )
                entries_by_lang = {
                    language: entries_by_lang[language]
                    for language in missing
                    if isinstance(entries_by_lang.get(language), dict)
                }
                pending["missing_languages"] = missing
                pending["languages"] = missing
                pending["entries_by_lang"] = entries_by_lang
                _store_syntax_retry_context(
                    scene,
                    languages=missing,
                    pending_payload=pending,
                    error_message="Syntax снова вернул не все видео.",
                )
                await _reply_text_with_optional_markup(
                    update,
                    (
                        "Syntax снова вернул не все видео.\n\n"
                        f"Готово: {', '.join(sorted(syntax_batch_results)) or 'ничего'}\n"
                        f"Не пришли: {', '.join(missing)}\n\n"
                        "Напишите `Syntax`, чтобы ещё раз отправить недостающие языки в Syntax, "
                        "или `Vertex`, чтобы переключить недостающие языки сцены на Vertex/Veo."
                    ),
                    reply_markup=_syntax_recovery_markup(missing),
                )
                return True
            scene.pop("video_provider_choice_pending", None)
            for language in languages:
                await _deliver_scene_video_result(
                    update,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=total,
                    language=language,
                    video_bytes=syntax_batch_results[language],
                    provider="telegram",
                    caption_suffix=". Источник: Syntax.",
                )
            return await _continue_after_scene_video_provider_result(
                update,
                context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
                total=total,
            )

        await update.message.reply_text("Пробую резервный канал Vertex/Veo для всех языков сцены…")
        async def _vertex_for_language(language: str) -> tuple[str, bytes | None, Exception | None]:
            payload = entries_by_lang.get(language, {})
            try:
                return language, await run_vertex_fallback_from_session_payload(payload), None
            except Exception as err:
                return language, None, err

        results = await asyncio.gather(*[_vertex_for_language(language) for language in languages])
        failed_languages: list[str] = []
        for language, video_bytes, err in results:
            if err is not None:
                logger.exception("scene Vertex batch fallback execution failed for language=%s", language, exc_info=err)
                await update.message.reply_text(
                    f"Ошибка резервного Vertex для {language}: {_user_error_message(err, domain='video')}"
                )
                failed_languages.append(language)
                continue
            if video_bytes is None:
                failed_languages.append(language)
                continue
            payload = entries_by_lang.get(language, {})
            vertex_duration_sec = int(payload.get("vertex_duration_sec") or payload.get("duration_sec") or 6)
            await _deliver_scene_video_result(
                update,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
                total=total,
                language=language,
                video_bytes=video_bytes,
                provider="veo",
                caption_suffix=f" ({vertex_duration_sec} сек). Источник: Vertex fallback.",
            )
        if failed_languages:
            pending["languages"] = failed_languages
            pending["missing_languages"] = failed_languages
            pending["entries_by_lang"] = {
                language: entries_by_lang[language]
                for language in failed_languages
                if isinstance(entries_by_lang.get(language), dict)
            }
            _store_syntax_retry_context(
                scene,
                languages=failed_languages,
                pending_payload=pending,
                error_message="Vertex не вернул все видео.",
            )
            await _reply_current_scene_media_keyboard(
                update,
                session,
                "Vertex не вернул все видео. Можно повторить оставшиеся языки через Syntax или Vertex.",
            )
            return True
        scene.pop("video_provider_choice_pending", None)
        return await _continue_after_scene_video_provider_result(
            update,
            context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            total=total,
        )

    language = normalize_language_code(
        str(pending.get("language") or _scene_current_language(session, scene))
    ) or _scene_current_language(session, scene)
    languages = _scene_target_languages(session, scene)
    if choice == "syntax":
        await update.message.reply_text("Готовлю повтор этой сцены через Syntax…")
        try:
            video_bytes = await run_syntax_fallback_from_session_payload(pending)
        except Exception as fb_err:
            logger.exception("scene Syntax retry execution failed")
            await _reply_text_with_optional_markup(
                update,
                f"Не удалось отправить запрос в Syntax: {_user_error_message(fb_err, domain='video')}",
                reply_markup=_syntax_recovery_markup(languages),
            )
            return True
        if not video_bytes:
            await _reply_text_with_optional_markup(
                update,
                "Syntax снова не вернул видео. Можно поправить prompt/фразу, ещё раз нажать `Syntax` или переключиться на `Vertex`.",
                reply_markup=_syntax_recovery_markup(languages),
            )
            return True
        scene.pop("video_provider_choice_pending", None)
        await _deliver_scene_video_result(
            update,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            total=total,
            language=language,
            video_bytes=video_bytes,
            provider="telegram",
            caption_suffix=". Источник: Syntax.",
        )
    else:
        await update.message.reply_text("Пробую резервный канал Vertex/Veo…")
        try:
            video_bytes = await run_vertex_fallback_from_session_payload(pending)
        except Exception as err:
            logger.exception("scene Vertex fallback execution failed")
            await _reply_current_scene_media_keyboard(
                update,
                session,
                f"Ошибка резервного Vertex: {_user_error_message(err, domain='video')}",
            )
            return True
        if not video_bytes:
            await _reply_current_scene_media_keyboard(
                update,
                session,
                "Vertex не вернул видео. Можно повторить попытку через Syntax или Vertex.",
            )
            return True
        scene.pop("video_provider_choice_pending", None)
        vertex_duration_sec = int(pending.get("vertex_duration_sec") or pending.get("duration_sec") or 6)
        await _deliver_scene_video_result(
            update,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            total=total,
            language=language,
            video_bytes=video_bytes,
            provider="veo",
            caption_suffix=f" ({vertex_duration_sec} сек). Источник: Vertex fallback.",
        )

    return await _continue_after_scene_video_provider_result(
        update,
        context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        idx=idx,
        total=total,
    )


async def _handle_waiting_scene_media_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if _is_scene_media_stop_command(text):
        _set_scene_media_manual_approval(session)
        pending = session.get(SCENE_SPEECH_PENDING_KEY)
        if isinstance(pending, dict):
            _cancel_scene_speech_timeout(chat_id)
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
        await update.message.reply_text("Автоапрув остановлен. Возвращаю ручной режим.")
        await _show_manual_scene_media_controls(update, session)
        return True
    if _is_scene_media_ai_approve_command(text):
        _enable_scene_media_ai_approve(session)
        pending = session.get(SCENE_SPEECH_PENDING_KEY)
        if isinstance(pending, dict):
            _cancel_scene_speech_timeout(chat_id)
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
        scenes_for_pending, idx_for_pending = _scene_media_context(session)
        if (
            scenes_for_pending
            and idx_for_pending < len(scenes_for_pending)
            and isinstance(scenes_for_pending[idx_for_pending].get(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY), dict)
        ):
            await update.message.reply_text(
                "ИИ апрув включён, но текущий кадр требует решения по модели. "
                f"Выберите `{SCENE_IMAGE_MODEL_RETRY_BUTTON}` или `{SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON}`."
            )
            return True
        await update.message.reply_text(
            "Включён 🤖 ИИ апрув. Теперь промты, стартовые фреймы и видео будут проверяться ИИ по чек-листу. "
            "Хорошие результаты будут проходить автоматически, спорные будут отправляться на ручную проверку. "
            "Кнопка `СТОП` вернёт ручной режим."
        )
        return await _run_scene_media_ai_approve(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
        )
    if _is_scene_media_auto_approve_command(text):
        _enable_scene_media_auto_approve(session)
        pending = session.get(SCENE_SPEECH_PENDING_KEY)
        if isinstance(pending, dict):
            _cancel_scene_speech_timeout(chat_id)
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
        scenes_for_pending, idx_for_pending = _scene_media_context(session)
        if (
            scenes_for_pending
            and idx_for_pending < len(scenes_for_pending)
            and (
                isinstance(scenes_for_pending[idx_for_pending].get("syntax_fallback_pending"), dict)
                or isinstance(scenes_for_pending[idx_for_pending].get("video_provider_choice_pending"), dict)
                or isinstance(scenes_for_pending[idx_for_pending].get(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY), dict)
            )
        ):
            await update.message.reply_text(
                "Автоапрув включён, но текущий шаг требует отдельного решения. "
                "Сначала выберите вариант на кнопках, после этого продолжу без апрувов."
            )
            return True
        await update.message.reply_text(
            "Автоапрув включён: дальше генерирую кадры и видео без подтверждений. "
            "Кнопка `СТОП` вернёт ручной режим."
        )
        return await _run_scene_media_auto_approve(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
        )
    if await _handle_scene_video_provider_choice_pending(
        update, context, chat_id=chat_id, session=session, text=text
    ):
        return True
    if await _handle_scene_syntax_fallback_pending(
        update, context, chat_id=chat_id, session=session, text=text
    ):
        return True
    if await _handle_scene_image_model_choice_pending(
        update, context, chat_id=chat_id, session=session, text=text
    ):
        return True
    if session.get(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY):
        _set_scene_media_manual_approval(session)
        _clear_ai_human_review_pending(session)
    if _is_scene_media_ai_approve_enabled(session):
        return await _run_scene_media_ai_approve(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
        )
    if _is_scene_media_auto_approve_enabled(session):
        return await _run_scene_media_auto_approve(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
        )
    scenes, idx = _scene_media_context(session)
    if not scenes:
        await update.message.reply_text("Нет сцен. Начни заново через /generate.")
        return True
    if idx >= len(scenes):
        return await _finish_scene_media_production(update, session, message="Все сцены обработаны.")
    scene = scenes[idx]
    image_edit_pending = _pop_scene_image_edit_pending(session, scene_idx=idx)
    if image_edit_pending is not None:
        target = str(image_edit_pending.get("target") or "start")
        return await _scene_media_edit_generated_image(
            update,
            session=session,
            scene=scene,
            idx=idx,
            target="end" if target == "end" else "start",
            instruction=text,
        )
    video_feedback_pending = _pop_scene_video_feedback_pending(session, scene_idx=idx)
    if video_feedback_pending is not None:
        low_pending = (text or "").strip().lower()
        if low_pending in {"отмена", "cancel", NAV_BUTTON_PREVIOUS_STEP.lower(), NAV_BUTTON_START_OVER.lower()}:
            await update.message.reply_text("Ок, отменяю ввод фидбэка для video prompt.")
            await _send_next_scene_media_step(update, session)
            return True
        feedback_text = _extract_format_or_feedback(text)
        if feedback_text is None:
            feedback_text = str(text or "").strip()
        return await _apply_scene_video_feedback(
            update,
            context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            feedback=feedback_text,
            language=str(video_feedback_pending.get("language") or ""),
        )
    video_speed_pending = _pop_scene_video_speed_pending(session, scene_idx=idx)
    if video_speed_pending is not None:
        low_pending = (text or "").strip().lower()
        if low_pending in {"отмена", "cancel", NAV_BUTTON_PREVIOUS_STEP.lower(), NAV_BUTTON_START_OVER.lower()}:
            await update.message.reply_text("Ок, отменяю изменение скорости.")
            await _send_next_scene_media_step(update, session)
            return True
        speed_coef = parse_speed_coef(text)
        if speed_coef is None:
            _set_scene_video_speed_pending(
                session,
                scene_idx=idx,
                language=str(video_speed_pending.get("language") or ""),
            )
            await update.message.reply_text("Пришлите коэффициент числом, например `1.2` или `1.35`.")
            return True
        languages = _scene_target_languages(session, scene)
        target_lang = normalize_language_code(str(video_speed_pending.get("language") or "")) or _scene_current_language(session, scene)
        if target_lang not in languages and languages:
            target_lang = languages[0]
        if not _set_scene_video_speed(scene, target_lang, speed_coef):
            await update.message.reply_text("Не нашла видео, которому можно поставить скорость.")
            await _send_next_scene_media_step(update, session)
            return True
        _approve_scene_video_language(scene, target_lang, speed_coef=speed_coef)
        await update.message.reply_text(
            f"Скорость {speed_coef:.2f}x сохранена, видео {target_lang} автоматически утверждено."
        )
        if _all_scene_languages_approved(scene, languages):
            session["image_scene_index"] = idx + 1
            session["scene_media_language_index"] = 0
            if session["image_scene_index"] >= len(scenes):
                return await _finish_scene_media_production(update, session)
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            await _send_next_scene_media_step(update, session)
            return True
        await _reply_text_with_optional_markup(
            update,
            "Скорость применена. Осталось утвердить остальные языки.",
            reply_markup=scene_language_approval_markup(languages, session=session),
        )
        return True
    phrase_translation_pending = _get_scene_phrase_translation_pending(session, scene_idx=idx)
    if phrase_translation_pending is not None:
        stage = str(phrase_translation_pending.get("stage") or "confirm")
        target_lang = normalize_language_code(str(phrase_translation_pending.get("target_language") or "en")) or "en"
        source_lang = normalize_language_code(str(phrase_translation_pending.get("source_language") or "ru")) or "ru"
        source_text = str(phrase_translation_pending.get("source_text") or "").strip()
        if stage == "revision":
            raw_revision = (text or "").strip()
            if raw_revision.lower() in {"отмена", "cancel", NAV_BUTTON_PREVIOUS_STEP.lower(), NAV_BUTTON_START_OVER.lower()}:
                _pop_scene_phrase_translation_pending(session)
                await update.message.reply_text("Ок, оставляю старую английскую фразу.")
                await _send_next_scene_media_step(update, session)
                return True
            if raw_revision.lower() in {"фидбек", "фидбек:", "формат", "формат:", "фраза", "фраза:"}:
                await update.message.reply_text(
                    "Пришлите текст после команды: `ФИДБЕК: что изменить` или `ФРАЗА: новая исходная фраза`."
                )
                return True
            feedback = _extract_format_or_feedback(raw_revision)
            if feedback is None:
                feedback = ""
            new_source_text = ""
            if raw_revision.lower().startswith("фраза:"):
                new_source_text = raw_revision.split(":", 1)[1].strip()
            phrase_cmd = _parse_lang_payload_command(raw_revision, "фраза")
            if phrase_cmd:
                lang, payload = phrase_cmd
                if lang == source_lang:
                    new_source_text = payload.strip()
                elif lang == target_lang and payload.strip():
                    _apply_scene_phrase_update_to_scene(
                        session,
                        scene,
                        scene_idx=idx,
                        language=target_lang,
                        phrase_text=payload.strip(),
                    )
                    _pop_scene_phrase_translation_pending(session)
                    await update.message.reply_text(
                        f"Фразу для {target_lang} обновила вручную. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {target_lang}»."
                    )
                    await _send_next_scene_media_step(update, session)
                    return True
            if new_source_text:
                source_text = new_source_text
                phrase_translation_pending["source_text"] = source_text
            elif not feedback:
                feedback = raw_revision
            if new_source_text:
                _apply_scene_phrase_update_to_scene(
                    session,
                    scene,
                    scene_idx=idx,
                    language=source_lang,
                    phrase_text=source_text,
                    origin="user",
                )
            if not source_text.strip() and not feedback.strip():
                await update.message.reply_text(
                    "Пришлите фидбэк к переводу или новую исходную фразу в формате `ФРАЗА: ...`."
                )
                return True
            try:
                translated = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "translate_scene_phrase_adaptive",
                    lambda: _translate_scene_phrase_adaptive(
                        source_text,
                        target_lang,
                        feedback=feedback,
                        previous_translation=str(phrase_translation_pending.get("candidate_text") or ""),
                    ),
                )
            except ServiceUnavailableRetriesExhaustedError as e:
                _pop_scene_phrase_translation_pending(session)
                await update.message.reply_text(str(e))
                return True
            except Exception as e:
                _pop_scene_phrase_translation_pending(session)
                logger.exception("adaptive phrase translation retry failed")
                await update.message.reply_text(
                    f"Не удалось перевести фразу адаптивно: {_user_error_message(e)}"
                )
                return True
            phrase_translation_pending["candidate_text"] = translated.strip()
            phrase_translation_pending["stage"] = "approval"
            session[SCENE_PHRASE_TRANSLATION_PENDING_KEY] = phrase_translation_pending
            await _reply_text_with_optional_markup(
                update,
                "Обновлённый адаптивный перевод на английский:\n"
                f"{translated}\n\n"
                "Применить этот перевод?",
                reply_markup=ReplyKeyboardMarkup(
                    [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                ),
            )
            return True
        choice = syntax_fallback_parse_user_confirmation(text)
        if choice is None:
            await _reply_text_with_optional_markup(
                update,
                "Нажмите «да» или «нет»: переводить адаптивно фразу на английский?",
                reply_markup=ReplyKeyboardMarkup(
                    [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                ),
            )
            return True
        if stage == "confirm":
            if choice is False:
                _pop_scene_phrase_translation_pending(session)
                await update.message.reply_text("Ок, оставляю старую английскую фразу без изменений.")
                await _send_next_scene_media_step(update, session)
                return True
            try:
                translated = await _call_with_503_retries(
                    update,
                    context,
                    chat_id,
                    "translate_scene_phrase_adaptive",
                    lambda: _translate_scene_phrase_adaptive(source_text, target_lang),
                )
            except ServiceUnavailableRetriesExhaustedError as e:
                _pop_scene_phrase_translation_pending(session)
                await update.message.reply_text(str(e))
                return True
            except Exception as e:
                _pop_scene_phrase_translation_pending(session)
                logger.exception("adaptive phrase translation failed")
                await update.message.reply_text(
                    f"Не удалось перевести фразу адаптивно: {_user_error_message(e)}"
                )
                return True
            phrase_translation_pending["candidate_text"] = translated.strip()
            phrase_translation_pending["stage"] = "approval"
            session[SCENE_PHRASE_TRANSLATION_PENDING_KEY] = phrase_translation_pending
            await _reply_text_with_optional_markup(
                update,
                "Предложенный адаптивный перевод на английский:\n"
                f"{translated}\n\n"
                "Применить этот перевод?",
                reply_markup=ReplyKeyboardMarkup(
                    [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                ),
            )
            return True
        if choice is False:
            phrase_translation_pending["stage"] = "revision"
            session[SCENE_PHRASE_TRANSLATION_PENDING_KEY] = phrase_translation_pending
            await _reply_text_with_optional_markup(
                update,
                "Ок, не применяю этот перевод.\n"
                "Пришлите фидбэк к переводу или новую исходную фразу в формате `ФРАЗА: ...` — "
                "после этого снова запущу адаптивный перевод.",
                reply_markup=ReplyKeyboardMarkup(
                    [["ФИДБЕК:"], ["ФРАЗА:"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                ),
            )
            return True
        candidate = str(phrase_translation_pending.get("candidate_text") or "").strip()
        if not candidate:
            _pop_scene_phrase_translation_pending(session)
            await update.message.reply_text(
                "Перевод не готов. Повторите команду `ФРАЗА:` при необходимости."
            )
            return True
        _apply_scene_phrase_update_to_scene(
            session,
            scene,
            scene_idx=idx,
            language=target_lang,
            phrase_text=candidate,
            origin="adaptive_translation",
        )
        languages = _scene_target_languages(session, scene)
        if target_lang in languages:
            session["scene_media_language_index"] = languages.index(target_lang)
        _pop_scene_phrase_translation_pending(session)
        await update.message.reply_text(
            f"Адаптивный перевод применён для {target_lang}. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {target_lang}»."
        )
        await _send_next_scene_media_step(update, session)
        return True
    phrase_pending = _get_scene_phrase_input_pending(session, scene_idx=idx)
    if phrase_pending is not None:
        language = normalize_language_code(str(phrase_pending.get("language", ""))) or _scene_current_language(session, scene)
        low = (text or "").strip().lower()
        if low in {"отмена", "cancel", NAV_BUTTON_PREVIOUS_STEP.lower(), NAV_BUTTON_START_OVER.lower()}:
            _pop_scene_phrase_input_pending(session)
            await update.message.reply_text("Ок, отменяю ввод фразы.")
            await _send_next_scene_media_step(update, session)
            return True
        if _extract_prompt_override(text) is not None or _extract_format_or_feedback(text) is not None:
            _pop_scene_phrase_input_pending(session)
            # Continue regular flow for prompt/feedback commands.
        else:
            payload = (text or "").strip()
            if not payload:
                await update.message.reply_text("Пришлите текст фразы.")
                return True
            _pop_scene_phrase_input_pending(session)
            asked = await _apply_scene_phrase_update(
                update,
                session=session,
                scene=scene,
                scene_idx=idx,
                language=language,
                phrase_text=payload,
            )
            languages = _scene_target_languages(session, scene)
            if language in languages:
                session["scene_media_language_index"] = languages.index(language)
            if asked:
                return True
            await update.message.reply_text(
                f"Фразу для {language} обновила. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {language}» или «ОК»."
            )
            await _send_next_scene_media_step(update, session)
            return True
    pending = session.get(SCENE_SPEECH_PENDING_KEY)
    if isinstance(pending, dict) and int(pending.get("scene_idx", -1)) == idx:
        stage = str(pending.get("stage") or "choice")
        is_ok_pending = text.lower() in {"ок", "ok", "okay", "approve"}
        if text == SCENE_SPEECH_OPTION_APPROVE:
            is_ok_pending = True
        if stage == "approval":
            if is_ok_pending:
                _cancel_scene_speech_timeout(chat_id)
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                await reply_text_without_keyboard(update.message, "Фраза подтверждена. Генерирую видео…")
                return await _scene_media_video_ok(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=len(scenes),
                )
            if text == SCENE_SPEECH_OPTION_EDIT:
                _set_scene_speech_pending(
                    session,
                    scene_idx=idx,
                    duration_sec=int(pending.get("duration_sec", 6)),
                    stage="approval_manual_phrase",
                )
                await _reply_text_with_optional_markup(
                    update,
                    "Пришлите вашу фразу для озвучки. После этого покажу её на подтверждение.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return True
            manual_phrase = text.strip()
            if not manual_phrase:
                await update.message.reply_text("Пришлите фразу текстом или подтвердите «ОК».")
                return True
            manual_phrase = _apply_scene_phrase_update_to_scene(
                session,
                scene,
                scene_idx=idx,
                language="ru",
                phrase_text=manual_phrase,
                origin="user",
            )
            updated_langs, failed_langs = await _sync_scene_phrase_translations(
                update=update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                scene_idx=idx,
                source_language="ru",
                source_text=manual_phrase,
            )
            _set_scene_speech_pending(
                session,
                scene_idx=idx,
                duration_sec=int(pending.get("duration_sec", 6)),
                stage="approval",
            )
            await _reply_text_with_optional_markup(
                update,
                f"Обновила фразу:\n{manual_phrase}\n\n"
                + (
                    f"Переводы обновила для языков: {', '.join(updated_langs)}.\n"
                    if updated_langs
                    else ""
                )
                + (
                    f"Не удалось обновить переводы для языков: {', '.join(failed_langs)}.\n\n"
                    if failed_langs
                    else ""
                )
                + "Подтвердите запуск видео кнопкой.",
                reply_markup=_scene_speech_approval_markup(),
            )
            return True
        if stage == "approval_manual_phrase":
            manual_phrase = text.strip()
            if not manual_phrase:
                await update.message.reply_text("Пришлите фразу текстом.")
                return True
            manual_phrase = _apply_scene_phrase_update_to_scene(
                session,
                scene,
                scene_idx=idx,
                language="ru",
                phrase_text=manual_phrase,
                origin="user",
            )
            updated_langs, failed_langs = await _sync_scene_phrase_translations(
                update=update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                scene_idx=idx,
                source_language="ru",
                source_text=manual_phrase,
            )
            _set_scene_speech_pending(
                session,
                scene_idx=idx,
                duration_sec=int(pending.get("duration_sec", 6)),
                stage="approval",
            )
            await _reply_text_with_optional_markup(
                update,
                f"Обновила фразу:\n{manual_phrase}\n\n"
                + (
                    f"Переводы обновила для языков: {', '.join(updated_langs)}.\n"
                    if updated_langs
                    else ""
                )
                + (
                    f"Не удалось обновить переводы для языков: {', '.join(failed_langs)}.\n\n"
                    if failed_langs
                    else ""
                )
                + "Подтвердите запуск видео кнопкой.",
                reply_markup=_scene_speech_approval_markup(),
            )
            return True
        if text == SCENE_SPEECH_OPTION_RETRY:
            _cancel_scene_speech_timeout(chat_id)
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
            await _reply_text_with_optional_markup(
                update,
                "Запускаю повторное автосокращение…",
                reply_markup=ReplyKeyboardRemove(),
            )
            return await _scene_media_video_ok(
                update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
                total=len(scenes),
            )
        if text == SCENE_SPEECH_OPTION_WAIT and stage == "choice":
            _set_scene_speech_pending(
                session,
                scene_idx=idx,
                duration_sec=int(pending.get("duration_sec", 6)),
                stage="manual_phrase",
            )
            _schedule_scene_speech_timeout(context=context, session=session, chat_id=chat_id)
            await _reply_text_with_optional_markup(
                update,
                "Жду короткую фразу до 10 минут. Если ответа не будет, попробую автосокращение снова.",
                reply_markup=_scene_speech_options_markup(),
            )
            return True
        if stage == "manual_phrase":
            manual_phrase = text.strip()
            if not manual_phrase:
                await update.message.reply_text("Пришлите фразу текстом.")
                return True
            _cancel_scene_speech_timeout(chat_id)
            manual_phrase = _apply_scene_phrase_update_to_scene(
                session,
                scene,
                scene_idx=idx,
                language="ru",
                phrase_text=manual_phrase,
                origin="user",
            )
            updated_langs, failed_langs = await _sync_scene_phrase_translations(
                update=update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                scene_idx=idx,
                source_language="ru",
                source_text=manual_phrase,
            )
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
            await _reply_text_with_optional_markup(
                update,
                f"Фразу обновила:\n{manual_phrase}\n\n"
                + (
                    f"Переводы обновила для языков: {', '.join(updated_langs)}.\n"
                    if updated_langs
                    else ""
                )
                + (
                    f"Не удалось обновить переводы для языков: {', '.join(failed_langs)}.\n\n"
                    if failed_langs
                    else ""
                )
                + "Подтвердите запуск видео кнопкой.",
                reply_markup=_scene_speech_approval_markup(),
            )
            _set_scene_speech_pending(
                session,
                scene_idx=idx,
                duration_sec=int(pending.get("duration_sec", 6)),
                stage="approval",
            )
            return True
        if stage == "user_long_phrase_choice":
            if text == SCENE_SPEECH_OPTION_EDIT:
                _set_scene_speech_pending(
                    session,
                    scene_idx=idx,
                    duration_sec=int(pending.get("duration_sec", 6)),
                    stage="approval_manual_phrase",
                )
                await _reply_text_with_optional_markup(
                    update,
                    "Пришлите новую фразу для озвучки.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return True
            if text == SCENE_SPEECH_OPTION_KEEP_AS_IS:
                _cancel_scene_speech_timeout(chat_id)
                _set_scene_speech_budget_override(scene, "ru", True)
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                await reply_text_without_keyboard(
                    update.message,
                    "Оставляю фразу как есть и запускаю видео без автосокращения.",
                )
                return await _scene_media_video_ok(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=len(scenes),
                )
            if text == SCENE_SPEECH_OPTION_SHORTEN_AI:
                _cancel_scene_speech_timeout(chat_id)
                _set_scene_speech_budget_override(scene, "ru", False)
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                await reply_text_without_keyboard(
                    update.message,
                    "Запускаю сокращение фразы через нейросеть.",
                )
                return await _scene_media_video_ok(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    total=len(scenes),
                )
            await _reply_text_with_optional_markup(
                update,
                "Выберите один вариант: оставить фразу как есть, укоротить её нейросетью или изменить вручную.",
                reply_markup=_scene_speech_user_long_phrase_markup(),
            )
            return True
    like_feedback = _apply_scene_like_fallback(
        session,
        scene,
        idx=idx,
        text=text,
        substep=str(session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START)),
    )
    if like_feedback is not None:
        await update.message.reply_text(like_feedback)
        return True
    sub = session.get("scene_media_substep", SCENE_MEDIA_SUBSTEP_START)
    is_ok = text.lower() in {"ок", "ok", "okay", "approve"}
    if await _handle_scene_media_common_restarts(update, session, scene, text):
        return True
    if sub == SCENE_MEDIA_SUBSTEP_VIDEO:
        languages = _scene_target_languages(session, scene)
        if _all_scene_languages_have_video(scene, languages):
            low = text.lower().strip()
            if low == SCENE_MEDIA_CHANGE_SPEED_BUTTON.lower():
                target_lang = _scene_current_language(session, scene)
                _set_scene_video_speed_pending(session, scene_idx=idx, language=target_lang)
                await update.message.reply_text(
                    f"Пришлите коэффициент скорости для языка {target_lang}, например `1.2`. "
                    "После этого видео автоматически заапрувится."
                )
                return True
            speed_coef = _parse_speed_command(text)
            if speed_coef is not None:
                target_lang = _scene_current_language(session, scene)
                if _set_scene_video_speed(scene, target_lang, speed_coef):
                    await update.message.reply_text(
                        f"Скорость для выбранного видео {target_lang} сохранена: {speed_coef:.2f}x."
                    )
                else:
                    await update.message.reply_text("Не нашла видео, которому можно поставить скорость.")
                return True
            ok_speed = _parse_ok_speed_command(text)
            if ok_speed is not None and len(languages) == 1:
                only_lang = languages[0]
                _approve_scene_video_language(scene, only_lang, speed_coef=ok_speed)
                await update.message.reply_text(
                    f"Апрув принят для языка {only_lang} со скоростью {ok_speed:.2f}x."
                )
                session["image_scene_index"] = idx + 1
                session["scene_media_language_index"] = 0
                if session["image_scene_index"] >= len(scenes):
                    return await _finish_scene_media_production(update, session)
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                await _send_next_scene_media_step(update, session)
                return True
            if low in {"ок все", "ok all", "approve all"}:
                for language in languages:
                    _approve_scene_video_language(scene, language, speed_coef=_scene_speed_coef(scene, language))
            else:
                ok_lang = _parse_ok_language_command(text)
                if ok_lang and ok_lang in languages:
                    _approve_scene_video_language(scene, ok_lang, speed_coef=_scene_speed_coef(scene, ok_lang))
                    await update.message.reply_text(f"Апрув принят для языка {ok_lang}.")
                    if not _all_scene_languages_approved(scene, languages):
                        return True
                else:
                    regen_lang = _parse_regenerate_language_command(text)
                    if regen_lang and regen_lang in languages:
                        results = scene.get("video_results_by_lang")
                        if not isinstance(results, dict):
                            results = {}
                        results.pop(regen_lang, None)
                        scene["video_results_by_lang"] = results
                        approvals = scene.get("video_approvals_by_lang")
                        if not isinstance(approvals, dict):
                            approvals = {}
                        approvals[regen_lang] = False
                        scene["video_approvals_by_lang"] = approvals
                        session["scene_media_language_index"] = languages.index(regen_lang)
                        await update.message.reply_text(
                            f"Перегенерирую видео для языка {regen_lang} с текущим prompt."
                        )
                        return await _scene_media_video_ok(
                            update,
                            context=context,
                            chat_id=chat_id,
                            session=session,
                            scene=scene,
                            idx=idx,
                            total=len(scenes),
                        )
                    prompt_cmd = _parse_lang_payload_command(text, "промпт")
                    lang_feedback_cmd = _parse_lang_payload_command(text, "фидбек")
                    feedback_cmd = lang_feedback_cmd or _parse_lang_payload_command(text, "исправь")
                    phrase_cmd = _parse_lang_payload_command(text, "фраза")
                    if prompt_cmd:
                        lang, payload = prompt_cmd
                        if lang in languages and payload:
                            _set_scene_video_prompt_for_language(
                                scene,
                                lang,
                                _compose_locked_video_prompt(scene, lang, payload),
                            )
                            session["scene_media_language_index"] = languages.index(lang)
                            await update.message.reply_text(
                                f"Обновила prompt для {lang}. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {lang}»."
                            )
                            return True
                    if _is_empty_feedback_command(text):
                        _set_scene_video_feedback_pending(
                            session,
                            scene_idx=idx,
                            language=_scene_current_language(session, scene),
                        )
                        await _ask_for_feedback_text(update, "video prompt")
                        return True
                    generic_feedback = _extract_format_or_feedback(text)
                    if generic_feedback is not None:
                        if not generic_feedback:
                            await update.message.reply_text('После "ФИДБЕК:" пришлите комментарий.')
                            return True
                        return await _apply_scene_video_feedback(
                            update,
                            context,
                            chat_id=chat_id,
                            session=session,
                            scene=scene,
                            idx=idx,
                            feedback=generic_feedback,
                            language=_scene_current_language(session, scene),
                        )
                    if feedback_cmd:
                        lang, payload = feedback_cmd
                        if lang_feedback_cmd and lang in languages:
                            session["scene_media_language_index"] = languages.index(lang)
                            await update.message.reply_text('Используйте общий формат без языка: "ФИДБЕК: комментарий".')
                            return True
                        if lang in languages and not payload:
                            session["scene_media_language_index"] = languages.index(lang)
                            await update.message.reply_text('Используйте общий формат без языка: "ФИДБЕК: комментарий".')
                            return True
                        if lang in languages and payload:
                            current = _scene_video_prompt_for_language(scene, lang)
                            try:
                                rewritten = await _call_with_503_retries(
                                    update,
                                    context,
                                    chat_id,
                                    "rewrite_video_prompt_generated_language_feedback",
                                    lambda: _rewrite_video_prompt_by_format_instruction(current, payload),
                                )
                            except ServiceUnavailableRetriesExhaustedError as e:
                                await update.message.reply_text(str(e))
                                return True
                            _set_scene_video_prompt_for_language(
                                scene,
                                lang,
                                _compose_locked_video_prompt(scene, lang, rewritten),
                            )
                            session["scene_media_language_index"] = languages.index(lang)
                            await update.message.reply_text(
                                f"Подготовила обновлённый prompt для {lang}. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {lang}»."
                            )
                            return True
                        if lang in languages and not payload:
                            session["scene_media_language_index"] = languages.index(lang)
                            await update.message.reply_text(
                                'Используйте общий формат без языка: "ФИДБЕК: комментарий".'
                            )
                            return True
                    if phrase_cmd:
                        lang, payload = phrase_cmd
                        if lang in languages and payload:
                            asked = await _apply_scene_phrase_update(
                                update,
                                session=session,
                                scene=scene,
                                scene_idx=idx,
                                language=lang,
                                phrase_text=payload,
                            )
                            session["scene_media_language_index"] = languages.index(lang)
                            if asked:
                                return True
                            await update.message.reply_text(
                                f"Фразу для {lang} обновила отдельно. Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {lang}»."
                            )
                            return True
                        if lang in languages and not payload:
                            _set_scene_phrase_input_pending(session, scene_idx=idx, language=lang)
                            await update.message.reply_text(
                                f'После "ФРАЗА {lang}:" пришлите текст фразы.'
                            )
                            return True
                    await _reply_text_with_optional_markup(
                        update,
                        "Ожидаю апрув языков: используйте `ОК все`, `ОК <lang>`, "
                        "`ПРОМПТ <lang>: ...`, `ФИДБЕК: ...`, `ФРАЗА <lang>: ...`, `СКОРОСТЬ 1.25` "
                        "или `ПЕРЕГЕНЕРИРОВАТЬ <lang>`.",
                        reply_markup=scene_language_approval_markup(languages, session=session),
                    )
                    return True
            if _all_scene_languages_approved(scene, languages):
                session["image_scene_index"] = idx + 1
                session["scene_media_language_index"] = 0
                if session["image_scene_index"] >= len(scenes):
                    return await _finish_scene_media_production(update, session)
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                await _send_next_scene_media_step(update, session)
                return True
            await _reply_text_with_optional_markup(
                update,
                "Не все языки апрувнуты. Завершите апрув командами `ОК <lang>` или `ОК все`. "
                "Коэффициент можно указать заранее через `СКОРОСТЬ 1.25`.",
                reply_markup=scene_language_approval_markup(languages, session=session),
            )
            return True
    if sub == SCENE_MEDIA_SUBSTEP_START:
        return await (
            _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
            if is_ok
            else _scene_media_start_not_ok(update, context, chat_id=chat_id, session=session, scene=scene, text=text)
        )
    if sub == SCENE_MEDIA_SUBSTEP_END:
        return await (_scene_media_end_ok(update, session=session, scene=scene, idx=idx) if is_ok else _scene_media_end_not_ok(update, context, chat_id=chat_id, session=session, scene=scene, text=text))
    if sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
        return await _scene_media_start_image_approval_step(
            update,
            context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            text=text,
        )
    if sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
        return await _scene_media_end_image_approval_step(
            update,
            context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            idx=idx,
            text=text,
        )
    if not is_ok:
        return await _scene_media_video_not_ok(
            update,
            context=context,
            chat_id=chat_id,
            session=session,
            scene=scene,
            text=text,
            language=_scene_current_language(session, scene),
        )
    return await _scene_media_video_ok(
        update,
        context=context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        idx=idx,
        total=len(scenes),
    )
