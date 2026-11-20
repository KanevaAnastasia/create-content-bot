import asyncio
import hashlib
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import NetworkError, RetryAfter, TimedOut

from ..character_catalog import sanitize_character_names_and_keys
from ..advertising_generation import (
    advertising_product_reference_bytes,
    advertising_video_continuity_en,
    clean_advertising_video_prompt,
)
from ..dish_visuals import append_dish_visual_brief_to_prompt, build_dish_visual_brief
from ..generation_modes import (
    SYNTAX_PROVIDER_VEO,
    SYNTAX_PROVIDER_KLING,
    aspect_ratio_for_generation_mode,
    is_long_generation,
    is_silence_generation,
    is_story_generation,
    is_advertising_generation,
    syntax_provider_for_scene_generation,
)
from ..llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, GEMINI_MODEL_PROFILE_EDIT
from ..pipeline_llm_steps import FINAL_SCENE_SUBSCRIBE_CTA
from ..pipeline_history import restore_scene_prompt_fields_from_history
from ..story_image_postprocess import finish_story_historical_frame
from ..story_generation import ensure_story_video_camera_safety
from ..scheduled_comments import scheduled_generation_every_start_prompt_comment
from .handlers_state_deps import (
    ContextTypes,
    SESSIONS,
    add_scene_language_cost,
    add_scene_shared_cost,
    SCENE_MEDIA_AI_APPROVE_BUTTON,
    SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_APPROVE_MODE_AI,
    SCENE_MEDIA_APPROVE_MODE_AUTO,
    SCENE_MEDIA_APPROVE_MODE_MANUAL,
    SCENE_MEDIA_APPROVE_MODE_SESSION_KEY,
    SCENE_MEDIA_AUTO_APPROVE_BUTTON,
    SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY,
    SCENE_MEDIA_CHANGE_SPEED_BUTTON,
    SCENE_MEDIA_CONTINUE_PREVIOUS_VIDEO_BUTTON,
    SCENE_MEDIA_CLEAR_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_REQUEST_END_FRAME_BUTTON,
    SCENE_MEDIA_SKIP_END_FRAME_BUTTON,
    SCENE_MEDIA_STOP_BUTTON,
    SCENE_MEDIA_SUBSTEP_END,
    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_START,
    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_VIDEO,
    SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON,
    SCENE_USER_REFERENCE_IMAGES_KEY,
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
    _video_prompt_by_lang_with_candidate_fallback,
    _should_suggest_regenerate_start_frame_after_video_prompt_fails,
    scene_end_approval_markup,
    scene_generated_image_approval_markup,
    scene_image_approval_markup,
    scene_reference_images_markup,
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
    AIApprovalResult,
    AI_APPROVAL_STATUS_APPROVE,
    AI_APPROVAL_STATUS_FIX_PROMPT,
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
from ..final_build_queue import queue_final_builds_for_session
from ..final_video_delivery import all_final_artifacts_delivered
from ..generation_store import mark_generation_run_ready
from .handlers_generation_utils import fetch_recipe_text_from_url
from .handlers_state_commands import _regenerate_image_prompt_from_feedback
from .handlers_keyboards import first_step_menu_markup, reply_text_without_keyboard, safe_send_message
from ..llm_utils import extract_json_object
from ..multilingual import normalize_language_code
from ..post_production import (
    build_post_production,
    build_word_synced_subtitle_cues,
    probe_video_bytes_dimensions,
)
from ..project_profiles import (
    DISCIPLINE_HASHTAGS_FIXED_EN,
    DISCIPLINE_HASHTAGS_FIXED_RU,
    DISCIPLINE_PROJECT_SLUG,
)
from ..publications import (
    DEFAULT_PUBLICATION_VIDEO_VARIANT,
    PUBLICATION_VIDEO_VARIANT_LABELS,
    build_publication_targets,
    normalize_publication_scheduled_at,
    publication_jobs_summary,
    publication_target_label,
    queue_publication_bundle,
)
from ..scene_media_candidates import (
    continuation_source_for_scene,
    default_scene_video_speed_coef,
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
    selected_video_bytes_for_production,
    video_candidate_bytes,
)
from ..scene_media_store import (
    persist_image_candidate,
    persist_image_candidate_state,
    persist_message_link,
    persist_video_candidate,
    persist_video_candidate_state,
)
from ..video_frame_extraction import extract_last_video_frame
from ..scene_speech_budget import (
    append_prompt_timing_hints,
    build_default_speech_plan,
    build_speech_cues_from_plan,
    max_speech_chars_for_duration,
    normalize_speech_phrase_for_voice,
    speech_budget_for_duration,
    speech_chars_excluding_pauses,
)
from ..state import save_loaded_session_if_possible
from ..scene_speech_shorten import shorten_scene_speech_and_sync_video_prompt_sync
from ..series_store import (
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    advance_series_after_successful_render,
)
from ..syntax_video_fallback import BatchFallbackRequest
from ..syntax_telethon_fallback import _prepare_prompt_for_syntax
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
SYNTAX_PROMPT_BY_LANG_KEY = "syntax_prompt_by_lang"
SCENE_PHRASE_INPUT_PENDING_KEY = "scene_phrase_input_pending"
SCENE_PHRASE_TRANSLATION_PENDING_KEY = "scene_phrase_translation_pending"
SCENE_PHRASE_ORIGIN_KEY = "speech_origin_by_lang"
SCENE_SPEECH_BUDGET_OVERRIDE_KEY = "speech_budget_override_by_lang"
SOCIAL_DESCRIPTIONS_SENT_KEY = "social_descriptions_sent"
SOCIAL_DESCRIPTIONS_APPROVED_KEY = "social_descriptions_approved"
STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL = "waiting_social_descriptions_approval"
STATE_REFINING_SOCIAL_DESCRIPTIONS = "refining_social_descriptions"
SOCIAL_DESCRIPTIONS_PENDING_KEY = "social_descriptions_pending"
SOCIAL_PUBLISH_TARGETS_KEY = "selected_target_keys"
SOCIAL_PUBLISH_VIDEO_VARIANT_KEY = "video_variant"
SOCIAL_PUBLISH_SCHEDULED_AT_KEY = "scheduled_at"
SCENE_IMAGE_EDIT_PENDING_KEY = "scene_image_edit_pending"
SCENE_IMAGE_REFERENCE_PENDING_KEY = "scene_image_reference_pending"
SCENE_VIDEO_FEEDBACK_PENDING_KEY = "scene_video_feedback_pending"
SCENE_VIDEO_SPEED_PENDING_KEY = "scene_video_speed_pending"
SCENE_VIDEO_REGEN_REQUESTED_KEY = "video_regeneration_requested_by_lang"
SCENE_PROMPT_INPUT_PENDING_KEY = "scene_prompt_input_pending"
SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY = "image_model_choice_pending"
SCENE_IMAGE_REGEN_REFERENCE_KEY = "image_regeneration_reference"
SCENE_IMAGE_MODEL_RETRY_BUTTON = "ПОВТОРИТЬ СИЛЬНУЮ"
SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON = "ПОНИЗИТЬ МОДЕЛЬ"
SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY = "scene_media_ai_human_review"
SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY = "scene_media_ai_technical_failure"
DRAFT_REPAIR_ACTIVE_PLAN_KEY = "draft_repair_active_plan"
SCHEDULED_AI_APPROVAL_MAX_ATTEMPTS_ENV_NAME = "SCHEDULED_AI_APPROVAL_MAX_ATTEMPTS"
AI_APPROVAL_RETRY_COUNT_ENV_NAME = "AI_APPROVAL_RETRY_COUNT"
AI_APPROVAL_RETRY_DELAY_SEC_ENV_NAME = "AI_APPROVAL_RETRY_DELAY_SEC"
SCHEDULED_AI_APPROVAL_RETRY_COUNT_ENV_NAME = "SCHEDULED_AI_APPROVAL_RETRY_COUNT"
SCHEDULED_AI_APPROVAL_RETRY_DELAY_SEC_ENV_NAME = "SCHEDULED_AI_APPROVAL_RETRY_DELAY_SEC"
SCHEDULED_AI_APPROVAL_RETRY_INITIAL_DELAY_SEC_ENV_NAME = "SCHEDULED_AI_APPROVAL_RETRY_INITIAL_DELAY_SEC"
SCHEDULED_AI_APPROVAL_RETRY_MAX_DELAY_SEC_ENV_NAME = "SCHEDULED_AI_APPROVAL_RETRY_MAX_DELAY_SEC"
SCHEDULED_SYNTAX_VIDEO_RETRY_COUNT_ENV_NAME = "SCHEDULED_SYNTAX_VIDEO_RETRY_COUNT"
SCHEDULED_SYNTAX_VIDEO_RETRY_DELAY_SEC_ENV_NAME = "SCHEDULED_SYNTAX_VIDEO_RETRY_DELAY_SEC"
SCHEDULED_SYNTAX_VIDEO_RETRY_MAX_DELAY_SEC_ENV_NAME = "SCHEDULED_SYNTAX_VIDEO_RETRY_MAX_DELAY_SEC"
SCHEDULED_SYNTAX_FALLBACK_TIMEOUT_SEC_ENV_NAME = "SCHEDULED_SYNTAX_FALLBACK_TIMEOUT_SEC"
SCHEDULED_SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC_ENV_NAME = "SCHEDULED_SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC"
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
_SEND_RETRY_ATTEMPTS = 6
_SEND_RETRY_BASE_DELAY_SEC = 2.0
_SEND_RETRY_MAX_DELAY_SEC = 30.0
_SEND_VIDEO_TIMEOUT_SEC = 180.0
_SEND_PHOTO_TIMEOUT_SEC = 120.0
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
        "scene_media.input.reference_images",
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


def _scene_media_stop_visible(session: dict | None) -> bool:
    if not isinstance(session, dict):
        return False
    approve_mode = str(session.get(SCENE_MEDIA_APPROVE_MODE_SESSION_KEY) or "").strip()
    return bool(session.get(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY)) or (
        approve_mode == SCENE_MEDIA_APPROVE_MODE_AI
        and not bool(session.get(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY))
    )


def _append_stop_if_needed(rows: list[list[str]], session: dict | None) -> None:
    if _scene_media_stop_visible(session):
        rows.append([SCENE_MEDIA_STOP_BUTTON])


def _syntax_recovery_markup(languages: list[str] | None = None, *, session: dict | None = None) -> ReplyKeyboardMarkup:
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
    _append_stop_if_needed(rows, session)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _stop_or_start_over_markup(session: dict | None = None) -> ReplyKeyboardMarkup:
    rows = [[NAV_BUTTON_START_OVER]]
    if _scene_media_stop_visible(session):
        rows.insert(0, [SCENE_MEDIA_STOP_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _syntax_confirmation_markup(*, session: dict | None = None) -> ReplyKeyboardMarkup:
    rows = [["да"], ["нет"], [NAV_BUTTON_PREVIOUS_STEP], [NAV_BUTTON_START_OVER]]
    _append_stop_if_needed(rows, session)
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
            _syntax_recovery_markup(pending_languages, session=session),
        )

    if isinstance(scene.get("syntax_fallback_pending"), dict):
        return _keyboard_snapshot(
            "scene_media.video.syntax_confirmation",
            _syntax_confirmation_markup(session=session),
        )

    image_model_pending = scene.get(SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY)
    if isinstance(image_model_pending, dict):
        return _keyboard_snapshot(
            "scene_media.image.model_choice",
            _scene_image_model_retry_markup(
                lower_model=str(image_model_pending.get("lower_model") or "").strip() or None,
                session=session,
            ),
        )

    speech_pending = session.get(SCENE_SPEECH_PENDING_KEY)
    if isinstance(speech_pending, dict) and int(speech_pending.get("scene_idx", -1)) == idx:
        stage = str(speech_pending.get("stage") or "choice")
        if stage == "approval":
            markup = _scene_speech_approval_markup(session=session)
        elif stage == "approval_manual_phrase":
            markup = _stop_or_start_over_markup(session)
        elif stage == "user_long_phrase_choice":
            markup = _scene_speech_user_long_phrase_markup(session=session)
        else:
            markup = _scene_speech_options_markup(session=session)
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

    reference_pending = session.get(SCENE_IMAGE_REFERENCE_PENDING_KEY)
    if isinstance(reference_pending, dict) and int(reference_pending.get("scene_idx", -1)) == idx:
        return _keyboard_snapshot(
            "scene_media.input.reference_images",
            scene_reference_images_markup(),
        )

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
                _stop_or_start_over_markup(session),
            )

    substep = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
    if substep == SCENE_MEDIA_SUBSTEP_START:
        return _keyboard_snapshot(
            "scene_media.start.prompt",
            scene_image_approval_markup(
                include_continue_previous=continuation_source_for_scene(session, scenes, idx) is not None,
                session=session,
            ),
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
    if _all_scene_languages_have_video(scene, languages):
        return _keyboard_snapshot(
            "scene_media.video.language_approval",
            scene_language_approval_markup(languages, session=session),
        )
    if _get_scene_policy_candidate(scene, current_language):
        return _keyboard_snapshot(
            "scene_media.video.policy_retry",
            video_policy_retry_markup(
                include_restart_hint=_should_suggest_regenerate_start_frame_after_video_prompt_fails(scene),
                session=session,
            ),
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


def _syntax_prompt_hash(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _scene_syntax_prompt_for_language(scene: dict, language: str, prompt: str) -> str:
    lang = normalize_language_code(str(language or "")) or "ru"
    source = str(prompt or "").strip()
    if not source:
        return source
    source_hash = _syntax_prompt_hash(source)
    cache = scene.get(SYNTAX_PROMPT_BY_LANG_KEY)
    if not isinstance(cache, dict):
        cache = {}
    entry = cache.get(lang)
    if isinstance(entry, dict) and entry.get("source_hash") == source_hash and str(entry.get("prompt") or "").strip():
        return str(entry["prompt"]).strip()
    prepared = _prepare_prompt_for_syntax(source)
    compact = prepared[0] if prepared is not None else source
    shortened = bool(prepared[1]) if prepared is not None else False
    cache[lang] = {
        "source_hash": source_hash,
        "prompt": compact,
        "shortened": shortened,
    }
    scene[SYNTAX_PROMPT_BY_LANG_KEY] = cache
    return compact


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


async def _reply_final_master_document(
    message,
    *,
    video_bytes: bytes,
    filename: str,
    caption: str,
    label: str,
    video_path: str | None = None,
):
    document_caption = f"{caption}\nОтправляю как файл, чтобы сохранить качество без Telegram video-compression."
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
            "telegram.final_master_document_send_failed label=%s filename=%s path=%s",
            label,
            filename,
            video_path or "",
        )
        path_note = f"\nПуть к файлу: `{video_path}`" if video_path else ""
        await _safe_reply_text(
            message,
            (
                f"{caption}\n"
                "Файл собран, но Telegram не принял отправку документа."
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
    overlay_title_by_lang = production.get("overlay_title_by_lang")
    if not isinstance(overlay_title_by_lang, dict) or not overlay_title_by_lang:
        overlay_title_by_lang = session.get("overlay_title_by_lang")
    if not isinstance(overlay_title_by_lang, dict) or not overlay_title_by_lang:
        overlay_title_by_lang = None
    overlay_title_style = production.get("overlay_title_style")
    if not isinstance(overlay_title_style, dict) or not overlay_title_style:
        overlay_title_style = session.get("overlay_title_style")
    if not isinstance(overlay_title_style, dict) or not overlay_title_style:
        overlay_title_style = None
    overlay_subtitle_by_lang = production.get("overlay_subtitle_by_lang")
    if not isinstance(overlay_subtitle_by_lang, dict) or not overlay_subtitle_by_lang:
        overlay_subtitle_by_lang = session.get("overlay_subtitle_by_lang")
    if not isinstance(overlay_subtitle_by_lang, dict) or not overlay_subtitle_by_lang:
        overlay_subtitle_by_lang = None
    overlay_series_title_by_lang = production.get(SERIES_TITLE_BY_LANG_SESSION_KEY)
    if not isinstance(overlay_series_title_by_lang, dict) or not overlay_series_title_by_lang:
        overlay_series_title_by_lang = session.get(SERIES_TITLE_BY_LANG_SESSION_KEY)
    if not isinstance(overlay_series_title_by_lang, dict) or not overlay_series_title_by_lang:
        overlay_series_title_by_lang = None
    overlay_series_counter_by_lang = production.get(SERIES_COUNTER_BY_LANG_SESSION_KEY)
    if not isinstance(overlay_series_counter_by_lang, dict) or not overlay_series_counter_by_lang:
        overlay_series_counter_by_lang = session.get(SERIES_COUNTER_BY_LANG_SESSION_KEY)
    if not isinstance(overlay_series_counter_by_lang, dict) or not overlay_series_counter_by_lang:
        overlay_series_counter_by_lang = None
    from ..post_production import apply_story_word_synced_subtitles
    from ..story_alignment import load_story_audio_metadata, valid_alignment
    narration_paths = {}
    narration_alignments = {}
    narration_texts = {}
    narration_durations = {}
    for language, suffix in (("ru", ""), ("en", "_en")):
        audio_path = str(session.get("story_audio_path" + suffix) or "")
        if not audio_path or not Path(audio_path).is_file():
            continue
        saved = load_story_audio_metadata(audio_path)
        narration_paths[language] = audio_path
        alignment = session.get("story_audio_alignment" + suffix)
        narration_alignments[language] = alignment if valid_alignment(alignment) else saved.get("alignment", {})
        narration_texts[language] = str(session.get("story_voiceover_text" + suffix) or "")
        narration_durations[language] = float(session.get("story_audio_duration_sec" + suffix) or saved.get("duration_sec") or 0)
    if narration_paths:
        apply_story_word_synced_subtitles(
            scenes, project_slug=project_slug,
            voiceover_by_lang=narration_texts, alignment_by_lang=narration_alignments,
            duration_by_lang=narration_durations,
        )
    last_exc: Exception | None = None
    for attempt in range(1, _POST_PRODUCTION_RETRY_ATTEMPTS + 1):
        try:
            post_kwargs = {"project_slug": project_slug}
            if narration_paths:
                post_kwargs["narration_audio_path_by_lang"] = narration_paths
                post_kwargs["include_background_music"] = False
            if overlay_title_by_lang:
                post_kwargs["overlay_title_by_lang"] = overlay_title_by_lang
            if overlay_title_style:
                post_kwargs["overlay_title_style"] = overlay_title_style
            if overlay_subtitle_by_lang:
                post_kwargs["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
            if overlay_series_title_by_lang:
                post_kwargs["overlay_series_title_by_lang"] = overlay_series_title_by_lang
            if overlay_series_counter_by_lang:
                post_kwargs["overlay_series_counter_by_lang"] = overlay_series_counter_by_lang
            discipline_intro_path = Path(
                str(session.get("discipline_intro_approved_video_path") or "")
            )
            if (
                project_slug in {"discipline", "grandma_kitchen", "grandma_cat_cooking"}
                and session.get("discipline_intro_enabled") is not False
                and discipline_intro_path.is_file()
            ):
                post_kwargs["intro_video_path_by_lang"] = {"ru": str(discipline_intro_path)}
            post = await asyncio.to_thread(
                build_post_production,
                scenes,
                **post_kwargs,
            )
            production["post_production"] = _strip_post_production_inline_video_payloads(post)
            persist_post_production_artifacts(session, post)
            advance_series_after_successful_render(session)
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


def _scene_image_model_retry_markup(*, lower_model: str | None, session: dict | None = None) -> ReplyKeyboardMarkup:
    rows = [[SCENE_IMAGE_MODEL_RETRY_BUTTON]]
    if lower_model:
        rows.append([SCENE_IMAGE_MODEL_DOWNGRADE_BUTTON])
    rows.append([NAV_BUTTON_PREVIOUS_STEP])
    rows.append([NAV_BUTTON_START_OVER])
    _append_stop_if_needed(rows, session)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


async def _ask_scene_image_model_choice(
    update: Update,
    *,
    session: dict | None = None,
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
    await _safe_reply_text(
        update.message,
        f"Не удалось сгенерировать {target_label} на модели `{failed_model}`.\n"
        f"Ошибка: {_user_error_message(error)}\n\n{tail}",
        reply_markup=_scene_image_model_retry_markup(lower_model=lower_model, session=session),
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


def _image_reference_debug_text(label: str, refs: list[bytes], *, source_counts: dict[str, int] | None = None) -> str:
    source_bits = []
    for key, value in (source_counts or {}).items():
        if value:
            source_bits.append(f"{key}={value}")
    items = []
    for index, ref in enumerate(refs, start=1):
        digest = hashlib.sha1(ref).hexdigest()[:10]
        items.append(f"{index}: {len(ref)}b sha1={digest}")
    source_text = ", ".join(source_bits) or "source=n/a"
    item_text = "; ".join(items[:8]) or "-"
    return f"{label} refs: total={len(refs)} ({source_text})\n{item_text}"


async def _send_image_reference_debug(update: Update, label: str, refs: list[bytes], *, source_counts: dict[str, int] | None = None) -> None:
    await _safe_reply_text(update.message, _image_reference_debug_text(label, refs, source_counts=source_counts))


def _image_reference_target_for_substep(substep: str) -> str | None:
    if substep in {SCENE_MEDIA_SUBSTEP_END, SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL}:
        return "end"
    if substep in {SCENE_MEDIA_SUBSTEP_START, SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL}:
        return "start"
    return None


def _scene_user_reference_images(scene: dict, target: str) -> list[bytes]:
    mapping = scene.get(SCENE_USER_REFERENCE_IMAGES_KEY)
    if not isinstance(mapping, dict):
        return []
    raw_items = mapping.get(str(target or "start"))
    if not isinstance(raw_items, list):
        return []
    out: list[bytes] = []
    for item in raw_items:
        data = _decode_b64(str(item or "").strip())
        if data:
            out.append(data)
    return out


def _scene_user_reference_count(scene: dict, target: str) -> int:
    return len(_scene_user_reference_images(scene, target))


def _append_scene_user_reference_image(scene: dict, target: str, image_bytes: bytes) -> int:
    key = "end" if target == "end" else "start"
    mapping = scene.get(SCENE_USER_REFERENCE_IMAGES_KEY)
    if not isinstance(mapping, dict):
        mapping = {}
        scene[SCENE_USER_REFERENCE_IMAGES_KEY] = mapping
    items = mapping.get(key)
    if not isinstance(items, list):
        items = []
        mapping[key] = items
    items.append(_encode_b64(image_bytes))
    return len(items)


def _clear_scene_user_reference_images(scene: dict, target: str) -> None:
    mapping = scene.get(SCENE_USER_REFERENCE_IMAGES_KEY)
    if isinstance(mapping, dict):
        mapping["end" if target == "end" else "start"] = []


def scene_media_reference_images_pending(session: dict) -> bool:
    return isinstance(session.get(SCENE_IMAGE_REFERENCE_PENDING_KEY), dict)


def _set_scene_reference_images_pending(session: dict, *, scene_idx: int, target: str) -> None:
    session[SCENE_IMAGE_REFERENCE_PENDING_KEY] = {
        "scene_idx": int(scene_idx),
        "target": "end" if target == "end" else "start",
    }


def _get_scene_reference_images_pending(session: dict, *, scene_idx: int) -> dict | None:
    pending = session.get(SCENE_IMAGE_REFERENCE_PENDING_KEY)
    if isinstance(pending, dict) and int(pending.get("scene_idx", -1)) == int(scene_idx):
        return pending
    return None


def _pop_scene_reference_images_pending(session: dict) -> dict | None:
    pending = session.get(SCENE_IMAGE_REFERENCE_PENDING_KEY)
    if isinstance(pending, dict):
        session.pop(SCENE_IMAGE_REFERENCE_PENDING_KEY, None)
        return pending
    return None


async def _start_scene_reference_images_collection(
    update: Update,
    session: dict,
    scene: dict,
    *,
    idx: int,
    target: str,
) -> bool:
    _set_scene_reference_images_pending(session, scene_idx=idx, target=target)
    count = _scene_user_reference_count(scene, target)
    label = "конечного" if target == "end" else "стартового"
    await _reply_text_with_optional_markup(
        update,
        f"Пришлите одно или несколько изображений для {label} кадра. "
        f"Сейчас добавлено: {count}. Когда закончите — нажмите «{SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON}».",
        reply_markup=scene_reference_images_markup(),
    )
    return True


async def _handle_scene_reference_images_pending_text(
    update: Update,
    session: dict,
    scene: dict,
    *,
    idx: int,
    text: str,
) -> bool:
    pending = _get_scene_reference_images_pending(session, scene_idx=idx)
    if pending is None:
        return False
    target = "end" if str(pending.get("target") or "start") == "end" else "start"
    low = str(text or "").strip().lower()
    if low == SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON.lower():
        _pop_scene_reference_images_pending(session)
        await _reply_text_with_optional_markup(
            update,
            f"Готово, добавлено изображений: {_scene_user_reference_count(scene, target)}. "
            "Они уйдут в следующую генерацию этого кадра.",
            reply_markup=scene_media_keyboard_snapshot(session).markup,
        )
        return True
    if low == SCENE_MEDIA_CLEAR_REFERENCE_IMAGES_BUTTON.lower():
        _clear_scene_user_reference_images(scene, target)
        await _reply_text_with_optional_markup(
            update,
            "Очистила добавленные изображения для этого кадра. Можно прислать новые или нажать «Готово».",
            reply_markup=scene_reference_images_markup(),
        )
        return True
    await _reply_text_with_optional_markup(
        update,
        "Жду фото или image-файл. Можно прислать несколько изображений, затем нажать «Готово».",
        reply_markup=scene_reference_images_markup(),
    )
    return True


async def handle_scene_media_reference_photo(
    update: Update,
    *,
    session: dict,
    image_bytes: bytes | None,
) -> bool:
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    idx = int(session.get("image_scene_index", 0) or 0) if isinstance(session, dict) else 0
    if not isinstance(scenes, list) or idx >= len(scenes) or not isinstance(scenes[idx], dict):
        return False
    pending = _get_scene_reference_images_pending(session, scene_idx=idx)
    if pending is None:
        return False
    if not image_bytes:
        await update.message.reply_text("Не удалось прочитать изображение. Пришлите фото или image-файл ещё раз.")
        return True
    target = "end" if str(pending.get("target") or "start") == "end" else "start"
    if pending.get("use_as_frame"):
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
        scene = scenes[idx]
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
        scene[f"{target}_frame_image_b64"] = _encode_b64(image_bytes)
        candidate = register_image_candidate(scene, kind=target, image_bytes=image_bytes)
        _persist_image_candidate_best_effort(session, scene_idx=idx, candidate=candidate)
        if target == "start":
            scene.pop("end_frame_image_b64", None)
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = (
            SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL if target == "end"
            else SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
        )
        _pop_scene_reference_images_pending(session)
        await _reply_text_with_optional_markup(
            update,
            "Ваше изображение сохранено как кадр для видео. Нажмите «ОК», чтобы продолжить.",
            reply_markup=scene_generated_image_approval_markup(session=session, include_end_frame_choice=target == "start"),
        )
        return True
    count = _append_scene_user_reference_image(scenes[idx], target, image_bytes)
    label = "конечного" if target == "end" else "стартового"
    await _reply_text_with_optional_markup(
        update,
        f"Добавила изображение #{count} для {label} кадра. Можно прислать ещё или нажать «Готово».",
        reply_markup=scene_reference_images_markup(),
    )
    return True


def _end_frame_reference_images(session: dict, scene: dict, start_bytes: bytes) -> list[bytes]:
    refs: list[bytes | None] = [start_bytes]
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    first_scene = scenes[0] if isinstance(scenes, list) and scenes and isinstance(scenes[0], dict) else None
    if isinstance(first_scene, dict):
        refs.extend(
            _load_scene_reference_images(
                first_scene,
                include_location=False,
                prompt_keys=("end_frame_prompt", "end_frame_prompt_ru", "video_prompt"),
            )
        )
    refs.extend(
        _load_scene_reference_images(
            scene,
            include_location=False,
            prompt_keys=("end_frame_prompt", "end_frame_prompt_ru", "video_prompt"),
        )
    )
    refs.extend(_scene_user_reference_images(scene, "end"))
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
    aspect_ratio: str = "9:16",
):
    # Tests and a few legacy imports monkeypatch the bytes-only helper. Keep that
    # hook alive while production uses the richer result API.
    if getattr(_generate_image_with_lenient_fallback, "__module__", "") != "app.handlers.handlers_media_scene":
        try:
            data = await _generate_image_with_lenient_fallback(
                prompt,
                reference_images=reference_images,
                model=model,
                aspect_ratio=aspect_ratio,
            )
        except TypeError as e:
            if "aspect_ratio" in str(e):
                data = await _generate_image_with_lenient_fallback(
                    prompt,
                    reference_images=reference_images,
                    model=model,
                )
            elif "model" not in str(e):
                raise
            else:
                data = await _generate_image_with_lenient_fallback(prompt, reference_images=reference_images)
        return SimpleNamespace(data=data, model=model)
    return await _generate_image_with_lenient_fallback_result(
        prompt,
        reference_images=reference_images,
        model=model,
        aspect_ratio=aspect_ratio,
    )
_PROMPT_PREVIEW_MAX_LEN = 3500


async def _safe_send(call, *, label: str) -> None:
    """Run Telegram send call with patient retries on transient network failures."""
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
            delay = min(_SEND_RETRY_MAX_DELAY_SEC, _SEND_RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)))
            logger.warning(
                "telegram.send transient_error label=%s attempt=%s/%s delay=%ss err=%s",
                label,
                attempt,
                _SEND_RETRY_ATTEMPTS,
                delay,
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
    if isinstance(photo, (bytes, bytearray)):
        payload = bytes(photo)
        filename = "photo.png"
    else:
        filename = str(getattr(photo, "name", "") or "photo.png")
        if hasattr(photo, "seek"):
            try:
                photo.seek(0)
            except Exception:
                pass
        payload = photo.read() if hasattr(photo, "read") else bytes(photo)

    def _fresh_photo():
        stream = io.BytesIO(payload)
        stream.name = filename
        return stream

    async def _send_once():
        try:
            return await message.reply_photo(
                photo=_fresh_photo(),
                caption=caption,
                read_timeout=_SEND_PHOTO_TIMEOUT_SEC,
                write_timeout=_SEND_PHOTO_TIMEOUT_SEC,
                connect_timeout=30.0,
                pool_timeout=30.0,
            )
        except TypeError as exc:
            # Lightweight test doubles and older telegram clients may not expose timeout kwargs.
            if "timeout" not in str(exc):
                raise
            return await message.reply_photo(photo=_fresh_photo(), caption=caption)

    return await _safe_send(_send_once, label="reply_photo")


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

    send_kwargs = {
        "read_timeout": _SEND_VIDEO_TIMEOUT_SEC,
        "write_timeout": _SEND_VIDEO_TIMEOUT_SEC,
        "connect_timeout": 30.0,
        "pool_timeout": 30.0,
    }
    try:
        return await _safe_send(
            lambda: message.reply_video(video=_fresh_video(), caption=caption, **send_kwargs),
            label=label,
        )
    except (NetworkError, TimedOut):
        if not hasattr(message, "reply_document"):
            raise
        logger.warning(
            "telegram.send video_failed_after_retries label=%s fallback=reply_document filename=%s",
            label,
            filename,
        )
        document_caption = f"{caption}\nTelegram не принял отправку как video, отправляю mp4 как файл."
        return await _safe_send(
            lambda: message.reply_document(
                document=_fresh_video(),
                caption=document_caption,
                filename=filename,
                **send_kwargs,
            ),
            label=label.replace("reply_video", "reply_document", 1),
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


def _parse_video_duration_feedback(text: str) -> int | None:
    low = str(text or "").strip().lower().replace(",", ".")
    if not low:
        return None
    for pattern in (
        r"(?:длительн\w*|хронометраж|тайминг|видео)\D{0,24}([3-8])\s*(?:сек\w*|sec|s)\b",
        r"\b(?:на|до)\s*([3-8])\s*(?:сек\w*|sec|s)\b",
        r"\b([3-8])\s*(?:сек\w*|sec|s)\b",
    ):
        match = re.search(pattern, low, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _apply_video_duration_feedback(scene: dict, feedback: str, *, speechless: bool) -> int | None:
    duration = _parse_video_duration_feedback(feedback)
    if duration is None:
        return None
    if speechless:
        duration = max(3, min(4, duration))
    scene["duration_sec"] = duration
    return duration


def _scene_speed_coef(scene: dict, language: str) -> float:
    if _is_speechless_video_scene(scene):
        return 1.0
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return default_scene_video_speed_coef(scene)
    return parse_speed_coef(selected.get("speed_coef")) or default_scene_video_speed_coef(scene)


def _candidate_speech_qa(scene: dict, language: str, candidate: dict | None) -> dict:
    if not isinstance(candidate, dict):
        return {}
    mapping = candidate.get("video_speech_qa_by_lang")
    value = mapping.get(language) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def _snapshot_candidate_speech_source(scene: dict, language: str, candidate: dict | None) -> None:
    if not isinstance(candidate, dict):
        return
    lang = normalize_language_code(language) or "ru"
    qa = _candidate_speech_qa(scene, lang, candidate)
    recognized = str(qa.get("recognized_text") or "").strip()
    speech = recognized or _scene_speech_for_language(scene, lang)
    speech_map = dict(candidate.get("speech_by_lang") or {}) if isinstance(candidate.get("speech_by_lang"), dict) else {}
    if speech:
        speech_map[lang] = speech
        candidate["speech_by_lang"] = speech_map
    prompt = _scene_video_prompt_for_language(scene, lang)
    prompt_map = dict(candidate.get("video_prompt_by_lang") or {}) if isinstance(candidate.get("video_prompt_by_lang"), dict) else {}
    if prompt:
        prompt_map[lang] = prompt
        candidate["video_prompt_by_lang"] = prompt_map
    cues_map = scene.get("speech_cues_by_lang")
    if isinstance(cues_map, dict) and cues_map.get(lang) and not recognized:
        candidate_cues = (
            dict(candidate.get("speech_cues_by_lang") or {})
            if isinstance(candidate.get("speech_cues_by_lang"), dict)
            else {}
        )
        candidate_cues[lang] = cues_map.get(lang)
        candidate["speech_cues_by_lang"] = candidate_cues
    plan_map = scene.get("speech_plan_by_lang")
    if isinstance(plan_map, dict) and plan_map.get(lang) and not recognized:
        candidate_plan = (
            dict(candidate.get("speech_plan_by_lang") or {})
            if isinstance(candidate.get("speech_plan_by_lang"), dict)
            else {}
        )
        candidate_plan[lang] = plan_map.get(lang)
        candidate["speech_plan_by_lang"] = candidate_plan


def _store_ai_video_speech_qa(scene: dict, language: str, result: AIApprovalResult) -> None:
    candidate = resolve_scene_video_for_production(scene, language)
    if candidate is None:
        return
    lang = normalize_language_code(language) or "ru"
    qa_map = (
        dict(candidate.get("video_speech_qa_by_lang") or {})
        if isinstance(candidate.get("video_speech_qa_by_lang"), dict)
        else {}
    )
    qa_map[lang] = {
        "status": result.status,
        "speech_completion_score": result.speech_completion_score,
        "recognized_text": result.speech_recognized_text,
        "word_timings": list(result.speech_word_timings),
        "missing_words": list(result.speech_missing_words),
        "extra_words": list(result.speech_extra_words),
        "timing_provider": "gemini_multimodal_approximate",
    }
    candidate["video_speech_qa_by_lang"] = qa_map
    if result.speech_recognized_text.strip():
        speech_map = (
            dict(candidate.get("speech_by_lang") or {})
            if isinstance(candidate.get("speech_by_lang"), dict)
            else {}
        )
        speech_map[lang] = result.speech_recognized_text.strip()
        candidate["speech_by_lang"] = speech_map
    timed_cues = build_word_synced_subtitle_cues(
        result.speech_word_timings,
        timing_source="video_audio_transcription",
    )
    if timed_cues:
        cues_map = (
            dict(candidate.get("speech_cues_by_lang") or {})
            if isinstance(candidate.get("speech_cues_by_lang"), dict)
            else {}
        )
        cues_map[lang] = timed_cues
        candidate["speech_cues_by_lang"] = cues_map
    _persist_video_state_best_effort(candidate)


def _approve_scene_video_language(scene: dict, language: str, *, speed_coef: float | None = None) -> None:
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    approvals[language] = True
    scene["video_approvals_by_lang"] = approvals
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return
    _snapshot_candidate_speech_source(scene, language, selected)
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


def _scene_media_mode_rows_for_tail(session: dict | None = None) -> list[list[str]]:
    """Mode buttons live at the bottom so they are not tapped instead of approval choices."""
    if isinstance(session, dict) and (
        _is_scene_media_auto_approve_enabled(session) or _is_scene_media_ai_approve_enabled(session)
    ):
        return [[SCENE_MEDIA_STOP_BUTTON]]
    return [[SCENE_MEDIA_AUTO_APPROVE_BUTTON], [SCENE_MEDIA_AI_APPROVE_BUTTON]]


def _scene_speech_options_markup(*, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура с действиями после неуспешного автосокращения."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_SPEECH_OPTION_RETRY],
            [SCENE_SPEECH_OPTION_WAIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
            *_scene_media_mode_rows_for_tail(session),
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _scene_speech_approval_markup(*, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура подтверждения сокращённой реплики."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_SPEECH_OPTION_APPROVE],
            [SCENE_SPEECH_OPTION_EDIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
            *_scene_media_mode_rows_for_tail(session),
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _scene_speech_user_long_phrase_markup(*, session: dict | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура выбора для длинной пользовательской фразы."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [SCENE_SPEECH_OPTION_KEEP_AS_IS],
            [SCENE_SPEECH_OPTION_SHORTEN_AI],
            [SCENE_SPEECH_OPTION_EDIT],
            [NAV_BUTTON_PREVIOUS_STEP],
            [NAV_BUTTON_START_OVER],
            *_scene_media_mode_rows_for_tail(session),
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


def _is_scheduled_unattended_ai_mode(session: dict) -> bool:
    return bool(session.get("scheduled_generation_mode") or session.get("scene_media_unattended_ai"))


def _scheduled_ai_approval_max_attempts() -> int:
    raw = str(os.getenv(SCHEDULED_AI_APPROVAL_MAX_ATTEMPTS_ENV_NAME) or "").strip()
    if not raw:
        return 25
    try:
        value = int(raw)
    except ValueError:
        return 25
    if value <= 0:
        return 1_000_000
    return value


def _ai_regenerate_attempt_limit(session: dict) -> int:
    if _is_scheduled_unattended_ai_mode(session):
        return _scheduled_ai_approval_max_attempts()
    return get_ai_approval_config().max_regenerate_attempts


def _int_env_value(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float_env_value(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _ai_approval_retry_count(session: dict) -> int:
    if _is_scheduled_unattended_ai_mode(session):
        return max(0, _int_env_value(SCHEDULED_AI_APPROVAL_RETRY_COUNT_ENV_NAME, 48))
    return max(0, _int_env_value(AI_APPROVAL_RETRY_COUNT_ENV_NAME, 3))


def _ai_approval_retry_delay_sec(session: dict, attempt: int = 1) -> float:
    if _is_scheduled_unattended_ai_mode(session):
        legacy_delay = _float_env_value(SCHEDULED_AI_APPROVAL_RETRY_DELAY_SEC_ENV_NAME, 300.0)
        max_delay = max(
            0.0,
            _float_env_value(SCHEDULED_AI_APPROVAL_RETRY_MAX_DELAY_SEC_ENV_NAME, legacy_delay),
        )
        initial_delay = max(
            0.0,
            _float_env_value(SCHEDULED_AI_APPROVAL_RETRY_INITIAL_DELAY_SEC_ENV_NAME, min(60.0, max_delay)),
        )
        if max_delay <= 0 or initial_delay <= 0:
            return 0.0
        retry_index = max(0, int(attempt) - 1)
        return min(max_delay, initial_delay * (2**retry_index))
    return max(0.0, _float_env_value(AI_APPROVAL_RETRY_DELAY_SEC_ENV_NAME, 60.0))


def _scheduled_syntax_video_retry_count(session: dict) -> int:
    if not _is_scheduled_unattended_ai_mode(session):
        return 0
    return max(1, _int_env_value(SCHEDULED_SYNTAX_VIDEO_RETRY_COUNT_ENV_NAME, 24))


def _scheduled_syntax_video_retry_delay_sec(session: dict, attempt: int = 1) -> float:
    if not _is_scheduled_unattended_ai_mode(session):
        return 0.0
    max_delay = max(0.0, _float_env_value(SCHEDULED_SYNTAX_VIDEO_RETRY_MAX_DELAY_SEC_ENV_NAME, 300.0))
    initial_delay = max(0.0, _float_env_value(SCHEDULED_SYNTAX_VIDEO_RETRY_DELAY_SEC_ENV_NAME, 60.0))
    if max_delay <= 0 or initial_delay <= 0:
        return 0.0
    retry_index = max(0, int(attempt) - 1)
    return min(max_delay, initial_delay * (2**retry_index))


def _scheduled_syntax_timeout_sec(session: dict) -> int | None:
    if not _is_scheduled_unattended_ai_mode(session):
        return None
    return max(60, _int_env_value(SCHEDULED_SYNTAX_FALLBACK_TIMEOUT_SEC_ENV_NAME, 3600))


def _scheduled_syntax_lock_timeout_sec(session: dict) -> int | None:
    if not _is_scheduled_unattended_ai_mode(session):
        return None
    timeout_sec = _scheduled_syntax_timeout_sec(session) or 3600
    return max(60, _int_env_value(SCHEDULED_SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC_ENV_NAME, timeout_sec))


async def _attempt_scheduled_syntax_batch(
    *,
    requests: list[BatchFallbackRequest],
    model: str,
    duration_sec: int,
    session: dict,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
) -> dict[str, bytes]:
    timeout_sec = _scheduled_syntax_timeout_sec(session)
    lock_timeout_sec = _scheduled_syntax_lock_timeout_sec(session)
    try:
        return await _attempt_syntax_video_fallback_batch(
            requests=requests,
            model=model,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            acquire_timeout_sec=lock_timeout_sec,
            syntax_provider=syntax_provider,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return await _attempt_syntax_video_fallback_batch(
            requests=requests,
            model=model,
            duration_sec=duration_sec,
        )


def _ai_approval_result_text(result) -> str:
    parts = [
        str(getattr(result, "recommended_action", "") or ""),
        str(getattr(result, "raw_model_response", "") or ""),
    ]
    for attr in ("critical_issues", "warnings", "positive_notes"):
        value = getattr(result, attr, None)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(part for part in parts if part).strip()


def _is_ai_approval_transient_limit_result(result) -> bool:
    text = _ai_approval_result_text(result).lower()
    if not text:
        return False
    non_retryable_markers = (
        "prompt_block_reason=prohibited_content",
        "prompt block reason=prohibited_content",
        "prohibited_content",
        "blocked due to prohibited contents",
        "prompt is blocked",
        "safety block",
        "safety blocked",
    )
    if any(marker in text for marker in non_retryable_markers):
        return False
    markers = (
        "лимит генераций",
        "лимит ai",
        "слишком много запросов",
        "подождите минуту",
        "quota",
        "resource_exhausted",
        "resource exhausted",
        "rate limit",
        "too many requests",
        "429",
        "timed out",
        "timeout",
        "ответ модели не приш",
        "empty response",
        "returned empty response",
        "пустой ответ",
    )
    return any(marker in text for marker in markers)


async def _run_ai_approval_with_limit_retry(update: Update, session: dict, *, stage: str, op):
    max_retries = _ai_approval_retry_count(session)
    attempt = 0
    while True:
        result = await asyncio.to_thread(op)
        is_transient_limit = _is_ai_approval_transient_limit_result(result)
        if not is_transient_limit:
            return result
        reason = _ai_approval_result_text(result)[:1200]
        if _is_scheduled_unattended_ai_mode(session):
            if stage == "video":
                await _safe_reply_text(
                    update.message,
                    "🤖 ИИ апрув получил временный технический сбой проверки видео. "
                    "Ночной режим не останавливаю: перегенерирую видео для этого языка и продолжу.",
                )
                return result
            session[SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY] = {
                "stage": stage,
                "attempts": attempt + 1,
                "max_retries": max_retries,
                "reason": reason,
            }
            await _safe_reply_text(
                update.message,
                "🤖 ИИ апрув получил временный технический сбой проверки "
                f"({stage}). Сохраняю шаг и отложу повтор через scheduled queue, "
                "без долгого ожидания внутри бота.",
            )
            return result
        if attempt >= max_retries:
            session[SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY] = {
                "stage": stage,
                "attempts": attempt,
                "max_retries": max_retries,
                "reason": reason,
            }
            await _safe_reply_text(
                update.message,
                "🤖 ИИ апрув не смог выполнить техническую проверку "
                f"({stage}) после {max_retries} retry. "
                "Останавливаю ночной пайплайн без перегенерации кадра.",
            )
            return result
        attempt += 1
        delay_sec = _ai_approval_retry_delay_sec(session, attempt)
        await _safe_reply_text(
            update.message,
            "🤖 ИИ апрув уперся во временный технический сбой проверки "
            f"({stage}). Жду {delay_sec:g} секунд и повторяю "
            f"(попытка {attempt}/{max_retries})."
        )
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)


def _normalize_ai_status_for_session(session: dict, status: str, *, stage: str) -> str:
    normalized = str(status or "").strip().upper()
    technical_failure = session.get(SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY)
    if (
        _is_scheduled_unattended_ai_mode(session)
        and isinstance(technical_failure, dict)
        and str(technical_failure.get("stage") or "") == stage
    ):
        return AI_APPROVAL_STATUS_HUMAN_REVIEW
    if not _is_scheduled_unattended_ai_mode(session) or normalized != AI_APPROVAL_STATUS_HUMAN_REVIEW:
        return normalized
    if stage in {"image", "end_image"}:
        return AI_APPROVAL_STATUS_REGENERATE_IMAGE
    if stage == "video":
        return AI_APPROVAL_STATUS_REGENERATE_VIDEO
    if stage == "prompt":
        return AI_APPROVAL_STATUS_FIX_PROMPT
    return normalized


def _ai_attempt_count(scene: dict, stage: str) -> int:
    history = scene.get("ai_approval_history")
    if not isinstance(history, list):
        return 0
    return sum(1 for item in history if isinstance(item, dict) and item.get("stage") == stage)


def _append_ai_approval_history(scene: dict, result, *, stage: str, language: str | None = None) -> None:
    history = scene.get("ai_approval_history")
    if not isinstance(history, list):
        history = []
    entry = ai_approval_history_entry(result, attempt=_ai_attempt_count(scene, stage) + 1)
    if language:
        entry["language"] = str(language).strip().lower()
    history.append(entry)
    scene["ai_approval_history"] = history


def _latest_ai_approval_feedback(scene: dict, *, stage: str) -> str:
    history = scene.get("ai_approval_history")
    if not isinstance(history, list):
        return ""
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("stage") != stage:
            continue
        parts: list[str] = []
        for key, label in (
            ("critical_issues", "Critical issues"),
            ("warnings", "Warnings"),
        ):
            values = item.get(key)
            if isinstance(values, list):
                text = "; ".join(str(value).strip() for value in values if str(value).strip())
                if text:
                    parts.append(f"{label}: {text}")
        recommended = str(item.get("recommended_action") or "").strip()
        if recommended:
            parts.append(f"Recommended action: {recommended}")
        raw = str(item.get("raw_model_response") or "").strip()
        if raw and not parts:
            parts.append(raw[:1200])
        return "\n".join(parts).strip()
    return ""


def _scheduled_every_start_prompt_comment(session: dict) -> str:
    explicit = str(session.get("scheduled_generation_every_start_prompt_comment") or "").strip()
    if explicit:
        return explicit
    return scheduled_generation_every_start_prompt_comment(
        str(session.get("scheduled_generation_comment") or "")
    )


def _append_scheduled_every_start_prompt_comment(session: dict, feedback: str) -> str:
    comment = _scheduled_every_start_prompt_comment(session)
    if not comment:
        return str(feedback or "").strip()
    feedback_text = str(feedback or "").strip()
    comment_block = (
        "MANDATORY SCHEDULED COMMENT FOR EVERY START PROMPT GENERATION/REGENERATION:\n"
        f"{comment}"
    )
    return f"{feedback_text}\n\n{comment_block}".strip()


async def _regenerate_start_prompt_from_ai_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None,
    *,
    chat_id: int | None,
    session: dict,
    scene: dict,
    idx: int,
) -> bool:
    feedback = _latest_ai_approval_feedback(scene, stage="image")
    if not feedback:
        feedback = (
            "AI approval rejected the start frame. Rewrite the start frame prompt so the next image fixes "
            "the visual mismatch, preserves required characters, contains no visible text, and matches the real dish."
        )
    dish_brief = _dish_visual_brief_for_scene(session, scene, idx)
    if dish_brief:
        feedback = (
            f"{feedback}\n\n"
            "Keep the real dish appearance exactly:\n"
            f"{dish_brief}"
        )
    feedback = _append_scheduled_every_start_prompt_comment(session, feedback)
    await _safe_reply_text(
        update.message,
        "🤖 ИИ апрув дал фидбэк по стартовому кадру. "
        "Сначала переписываю prompt с учетом замечаний, потом генерирую новый кадр.\n\n"
        f"{_prompt_preview(feedback)}",
    )
    try:
        new_prompt = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_image_prompt_ai_feedback",
            lambda: _regenerate_image_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await _safe_reply_text(update.message, str(e))
        return False
    except Exception as e:
        logger.exception("ai feedback image prompt regeneration failed")
        await _safe_reply_text(update.message, f"Ошибка перегенерации prompt по AI feedback: {_user_error_message(e)}")
        return False
    new_prompt = _enforce_start_prompt_shot_type(scene, str(new_prompt or ""))
    if not new_prompt.strip():
        return False
    scene["start_frame_prompt"] = new_prompt
    if "start_frame_prompt_ru" in scene:
        scene["start_frame_prompt_ru"] = new_prompt
    _invalidate_scene_video_prompts_after_start_prompt_change(scene, _scene_target_languages(session, scene))
    scene[START_PROMPT_RAW_MODE_KEY] = False
    scene["last_ai_image_feedback"] = feedback
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
    await _safe_reply_text(
        update.message,
        f"🤖 Prompt сцены {idx + 1} обновлен по AI feedback. Генерирую стартовый кадр заново.",
    )
    return True


async def _regenerate_scene_prompts_from_prompt_ai_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None,
    *,
    chat_id: int | None,
    session: dict,
    scene: dict,
    idx: int,
) -> bool:
    feedback = _latest_ai_approval_feedback(scene, stage="prompt")
    if not feedback:
        feedback = (
            "AI approval rejected the scene prompts. Rewrite the start frame prompt and video prompts so visible "
            "ingredients, garnishes, characters, kitchen physics, and dish state match the recipe and references."
        )
    dish_brief = _dish_visual_brief_for_scene(session, scene, idx)
    if dish_brief:
        feedback = (
            f"{feedback}\n\n"
            "Keep dish and ingredient fidelity exactly:\n"
            f"{dish_brief}"
        )
    feedback = _append_scheduled_every_start_prompt_comment(session, feedback)
    await _safe_reply_text(
        update.message,
        "🤖 ИИ апрув отклонил связку prompt/image/video до генерации. "
        "Переписываю start prompt и video prompts по замечаниям, затем повторю сцену с начала.\n\n"
        f"{_prompt_preview(feedback)}",
    )
    try:
        new_start_prompt = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_scene_start_prompt_ai_prompt_feedback",
            lambda: _regenerate_image_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await _safe_reply_text(update.message, str(e))
        return False
    except Exception as e:
        logger.exception("ai prompt feedback start prompt regeneration failed")
        await _safe_reply_text(update.message, f"Ошибка перегенерации start prompt по AI feedback: {_user_error_message(e)}")
        return False

    new_start_prompt = _enforce_start_prompt_shot_type(scene, str(new_start_prompt or ""))
    if not new_start_prompt.strip():
        return False

    original_video_prompt = scene.get("video_prompt")
    original_language = scene.get("video_prompt_current_language")
    prompt_map = scene.get("video_prompt_by_lang")
    if isinstance(prompt_map, dict) and prompt_map:
        language_prompts = {
            str(lang).strip().lower() or "ru": str(prompt or "")
            for lang, prompt in prompt_map.items()
            if str(lang).strip()
        }
    else:
        language = str(scene.get("video_prompt_current_language") or "ru").strip().lower() or "ru"
        language_prompts = {language: str(scene.get("video_prompt") or "")}

    rewritten_by_lang: dict[str, str] = {}
    try:
        for language, current_prompt in language_prompts.items():
            scene["video_prompt_current_language"] = language
            scene["video_prompt"] = current_prompt
            rewritten = await _call_with_503_retries(
                update,
                context,
                chat_id,
                f"regenerate_scene_video_prompt_ai_prompt_feedback_{language}",
                lambda: _regenerate_video_prompt_from_error(scene, feedback),
            )
            rewritten_by_lang[language] = _compose_locked_video_prompt(
                scene,
                language,
                str(rewritten or "").strip(),
            )
    except ServiceUnavailableRetriesExhaustedError as e:
        await _safe_reply_text(update.message, str(e))
        return False
    except Exception as e:
        logger.exception("ai prompt feedback video prompt regeneration failed")
        await _safe_reply_text(update.message, f"Ошибка перегенерации video prompt по AI feedback: {_user_error_message(e)}")
        return False
    finally:
        scene["video_prompt_current_language"] = original_language
        scene["video_prompt"] = original_video_prompt

    if not rewritten_by_lang or not all(value.strip() for value in rewritten_by_lang.values()):
        return False

    scene["start_frame_prompt"] = new_start_prompt
    if "start_frame_prompt_ru" in scene:
        scene["start_frame_prompt_ru"] = new_start_prompt
    scene[START_PROMPT_RAW_MODE_KEY] = False
    scene["video_prompt_by_lang"] = rewritten_by_lang
    scene["video_prompt"] = rewritten_by_lang.get("ru") or next(iter(rewritten_by_lang.values()))
    scene["last_ai_prompt_feedback"] = feedback
    scene.pop("start_frame_image_b64", None)
    scene.pop("end_frame_image_b64", None)
    _invalidate_scene_videos_after_frame_change(scene, list(rewritten_by_lang))
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
    await _safe_reply_text(
        update.message,
        f"🤖 Prompts сцены {idx + 1} обновлены по AI feedback. Повторяю сцену с start prompt.",
    )
    return True


def _scene_recipe_text(session: dict, scene: dict) -> str:
    return str(
        session.get("recipe_full_text")
        or scene.get("recipe_step")
        or scene.get("action")
        or scene.get("text")
        or ""
    ).strip()


def _format_generation_timing_value(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _scene_generation_phrase(scene: dict, *, language: str | None = None) -> str:
    language = normalize_language_code(str(language or ""))
    speech_by_lang = scene.get("speech_by_lang")
    candidates: list[object] = []
    if language and isinstance(speech_by_lang, dict):
        candidates.append(speech_by_lang.get(language))
    candidates.append(scene.get("narration_excerpt"))
    if not language and isinstance(speech_by_lang, dict):
        candidates.append(speech_by_lang.get("ru"))
    candidates.extend((scene.get("text"), scene.get("recipe_step"), scene.get("action")))
    for candidate in candidates:
        phrase = " ".join(str(candidate or "").split())
        if phrase:
            return phrase
    return ""


def _scene_generation_phrase_notice_text(
    scene: dict,
    *,
    idx: int,
    target: str,
    language: str | None = None,
) -> str:
    target_labels = {
        "start": "стартового кадра",
        "end": "конечного кадра",
        "video": "видео",
    }
    label = target_labels.get(str(target or "").strip().lower(), "сцены")
    phrase = _scene_generation_phrase(scene, language=language)
    lines = [f"🎙 Фраза для генерации {label}:"]
    lines.append(f"«{phrase}»" if phrase else "Без озвучки.")

    meta: list[str] = []
    start = _format_generation_timing_value(scene.get("start_sec"))
    end = _format_generation_timing_value(scene.get("end_sec"))
    if start and end:
        meta.append(f"{start}–{end} сек")
    beat = _format_generation_timing_value(scene.get("semantic_beat"))
    if beat:
        meta.append(f"смысловая сцена {beat}")
    else:
        meta.append(f"сцена {idx + 1}")
    clip_index = _format_generation_timing_value(scene.get("semantic_clip_index"))
    clip_count = _format_generation_timing_value(scene.get("semantic_clip_count"))
    if clip_index and clip_count:
        meta.append(f"клип {clip_index}/{clip_count}")
    normalized_language = normalize_language_code(str(language or ""))
    if normalized_language:
        meta.append(f"язык {normalized_language.upper()}")
    lines.append("⏱ " + " · ".join(meta))
    return "\n".join(lines)


def _dish_visual_brief_for_session(session: dict, scene: dict | None = None) -> str:
    existing = str(session.get("dish_visual_brief") or "").strip()
    if existing:
        return existing
    scene = scene or {}
    title = str(session.get("recipe_title") or session.get("selected_concept") or "").strip()
    recipe = str(session.get("recipe_full_text") or scene.get("recipe_step") or scene.get("text") or "").strip()
    brief = build_dish_visual_brief(title, recipe)
    if brief:
        session["dish_visual_brief"] = brief
    return brief


def _scene_count_for_dish_visual_gate(session: dict) -> int:
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if isinstance(scenes, list) and scenes:
        return len(scenes)
    scenes = session.get("scenes_plan") if isinstance(session, dict) else None
    if isinstance(scenes, list) and scenes:
        return len(scenes)
    return 0


def _should_enforce_dish_visual_for_scene(session: dict, idx: int) -> bool:
    if idx <= 0:
        return True
    scene_count = _scene_count_for_dish_visual_gate(session)
    return bool(scene_count and idx == scene_count - 1)


def _dish_visual_brief_for_scene(session: dict, scene: dict | None, idx: int) -> str:
    if is_story_generation(session):
        return ""
    if not _should_enforce_dish_visual_for_scene(session, idx):
        return ""
    return _dish_visual_brief_for_session(session, scene)


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
    await _safe_reply_text(update.message, format_ai_approval_summary(result))


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
    result = await _run_ai_approval_with_limit_retry(
        update,
        session,
        stage="image",
        op=lambda: check_start_frame(
            image_bytes=image_bytes,
            scene_prompt=str(scene.get("start_frame_prompt_effective") or scene.get("start_frame_prompt") or "").strip(),
            scene_description=_scene_description(scene),
            recipe_text=_scene_recipe_text(session, scene),
            dish_visual_brief=_dish_visual_brief_for_scene(session, scene, idx),
            scene_number=scene.get("scene") or idx + 1,
            previous_reference_images=_dedupe_image_reference_bytes(
                [
                    *_load_scene_reference_images(
                        scene,
                        include_location=False,
                        prompt_keys=("start_frame_prompt", "start_frame_prompt_ru"),
                    ),
                    *advertising_product_reference_bytes(session),
                ]
            ),
            expected_characters=[str(x) for x in (scene.get("characters") or [])],
            expected_objects=(
                ["the exact same single product package from the mandatory advertising reference, fully visible and not duplicated"]
                if is_advertising_generation(session)
                else []
            ),
            style_requirements="colorful cartoon style, soft stylized shading, non realistic rendering, warm cozy lighting",
            config=cfg,
        ),
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
    result = await _run_ai_approval_with_limit_retry(
        update,
        session,
        stage="prompt",
        op=lambda: check_scene_prompts(
            start_frame_prompt=start_frame_prompt,
            end_frame_prompt=_image_end_scene_prompt(scene),
            video_prompt=_scene_video_prompt_for_language(scene, language),
            scene_description=_scene_description(scene),
            recipe_text=_scene_recipe_text(session, scene),
            dish_visual_brief=_dish_visual_brief_for_scene(session, scene, idx),
            scene_number=scene.get("scene") or idx + 1,
            expected_characters=[str(x) for x in (scene.get("characters") or [])],
            config=cfg,
        ),
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
    result = await _run_ai_approval_with_limit_retry(
        update,
        session,
        stage="end_image",
        op=lambda: check_start_frame(
            image_bytes=image_bytes,
            scene_prompt=str(scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or "").strip(),
            scene_description=_scene_description(scene),
            recipe_text=_scene_recipe_text(session, scene),
            dish_visual_brief=_dish_visual_brief_for_scene(session, scene, idx),
            scene_number=scene.get("scene") or idx + 1,
            previous_reference_images=references,
            expected_characters=[str(x) for x in (scene.get("characters") or [])],
            expected_objects=[],
            style_requirements="colorful cartoon style, soft stylized shading, non realistic rendering, warm cozy lighting",
            frame_kind="end",
            config=cfg,
        ),
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
) -> AIApprovalResult:
    cfg = get_ai_approval_config()
    start_bytes = _decode_b64(str(scene.get("start_frame_image_b64", "")).strip())
    result = await _run_ai_approval_with_limit_retry(
        update,
        session,
        stage="video",
        op=lambda: check_video(
            video_bytes=video_bytes,
            scene_prompt=_scene_video_prompt_for_language(scene, language),
            expected_speech_text=_scene_speech_for_language(scene, language),
            duration=scene.get("duration_sec"),
            recipe_text=_scene_recipe_text(session, scene),
            dish_visual_brief=_dish_visual_brief_for_scene(session, scene, idx),
            scene_number=scene.get("scene") or idx + 1,
            language=language,
            previous_reference_images=_dedupe_image_reference_bytes(
                [*_load_scene_reference_images(scene, include_location=False), *advertising_product_reference_bytes(session)]
            ),
            generated_start_frame=start_bytes,
            config=cfg,
        ),
    )
    _append_ai_approval_history(scene, result, stage="video", language=language)
    await _send_ai_approval_summary(update, result)
    return result


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


def _set_scene_image_edit_pending(session: dict, *, scene_idx: int, target: str, mode: str = "edit") -> None:
    pending = {"scene_idx": scene_idx, "target": target}
    if mode != "edit":
        pending["mode"] = mode
    session[SCENE_IMAGE_EDIT_PENDING_KEY] = pending


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


def _is_discipline_social_project(project_slug: str | None) -> bool:
    return str(project_slug or "").strip().lower() == DISCIPLINE_PROJECT_SLUG


def _is_grandma_hashtag(tag: str) -> bool:
    value = str(tag or "").casefold()
    return "бабуш" in value or "grandma" in value


def _discipline_specific_hashtags(
    source: str,
    body: str,
    existing_tags: list[str],
    *,
    language: str,
) -> list[str]:
    fixed = {
        tag.casefold()
        for tag in (
            DISCIPLINE_HASHTAGS_FIXED_EN if language == "en" else DISCIPLINE_HASHTAGS_FIXED_RU
        )
    }
    if language == "en":
        existing = [
            tag.lower()
            for tag in existing_tags
            if re.fullmatch(r"#[a-zA-Z0-9_]+", tag)
            and tag.casefold() not in fixed
            and not _is_grandma_hashtag(tag)
        ]
    else:
        existing = [
            tag.lower()
            for tag in existing_tags
            if re.search(r"[а-яё]", tag.casefold())
            and tag.casefold() not in fixed
            and not _is_grandma_hashtag(tag)
        ]
    if existing:
        return _dedupe_hashtags(existing)[:2]

    text = f"{source} {body}".casefold()
    if language == "en":
        known = [
            (("прокраст", "procrast"), "#procrastination"),
            (("фокус", "focus"), "#focus"),
            (("привыч", "habit"), "#habits"),
            (("план", "plan"), "#planning"),
            (("мотивац", "motivat"), "#motivation"),
            (("проект", "project"), "#startnow"),
        ]
        tags = [tag for needles, tag in known if any(needle in text for needle in needles)]
        return _dedupe_hashtags(tags or ["#selfdiscipline"])[:2]
    known = [
        (("прокраст",), "#прокрастинация"),
        (("фокус",), "#фокус"),
        (("привыч",), "#полезныепривычки"),
        (("план",), "#планирование"),
        (("мотивац",), "#мотивация"),
        (("проект",), "#начнисейчас"),
    ]
    tags = [tag for needles, tag in known if any(needle in text for needle in needles)]
    return _dedupe_hashtags(tags or ["#самодисциплина"])[:2]


def _discipline_social_copy_is_valid(text: str) -> bool:
    value = str(text or "").casefold()
    forbidden = ("бабуш", "grandma", "ингредиент", "ingredient", "рецепт", "recipe")
    return not any(marker in value for marker in forbidden)


def _with_social_hashtags(
    text: str,
    *,
    recipe: str,
    language: str,
    advertising: bool = False,
    project_slug: str | None = None,
) -> str:
    clean = _clean_social_copy_message(text)
    existing_tags = re.findall(r"#[\wа-яА-ЯёЁ]+", clean, flags=re.UNICODE)
    clean = re.sub(r"(?:^|\s)#[\wа-яА-ЯёЁ]+", "", clean, flags=re.UNICODE)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = "\n".join(line.rstrip() for line in clean.splitlines()).strip()
    if _is_discipline_social_project(project_slug):
        thematic = _discipline_specific_hashtags(
            recipe,
            clean,
            existing_tags,
            language=language,
        )
        fixed = DISCIPLINE_HASHTAGS_FIXED_EN if language == "en" else DISCIPLINE_HASHTAGS_FIXED_RU
        tags = fixed + thematic
    elif advertising and language == "en":
        tags = ["#ad", "#product", "#howto"] + [tag for tag in existing_tags if re.fullmatch(r"#[a-zA-Z0-9_]+", tag)][:2]
    elif advertising:
        tags = ["#реклама", "#товар", "#полезныйсовет"] + [tag for tag in existing_tags if re.search(r"[а-яё]", tag.lower())][:2]
    elif language == "en":
        tags = SOCIAL_FIXED_HASHTAGS_EN + _recipe_specific_hashtags_en(recipe, clean, existing_tags)
    else:
        tags = SOCIAL_FIXED_HASHTAGS_RU + _recipe_specific_hashtags_ru(recipe, existing_tags)
    return f"{clean}\n\n{' '.join(_dedupe_hashtags(tags))}".strip()


def _fallback_social_copy(
    recipe: str,
    *,
    advertising: bool = False,
    project_slug: str | None = None,
) -> dict[str, str]:
    recipe_text = _clean_social_copy_message(recipe)
    if _is_discipline_social_project(project_slug):
        ru_description = (
            "Большое дело кажется неподъёмным, пока мы пытаемся охватить его целиком. "
            "Эта короткая история показывает проблему, один выполнимый первый шаг и путь к завершённому результату."
        )
        en_description = (
            "A big task feels overwhelming when we try to hold all of it at once. "
            "This short story shows the problem, one doable first step, and the path to a finished result."
        )
        return {
            "youtube_ru_title": "Начать важнее, чем сделать всё сразу",
            "youtube_ru_description": _with_social_hashtags(
                ru_description, recipe=recipe_text, language="ru", project_slug=project_slug
            ),
            "instagram_ru": _with_social_hashtags(
                ru_description, recipe=recipe_text, language="ru", project_slug=project_slug
            ),
            "vk_ru": _with_social_hashtags(
                ru_description, recipe=recipe_text, language="ru", project_slug=project_slug
            ),
            "tiktok_ru": _with_social_hashtags(
                ru_description, recipe=recipe_text, language="ru", project_slug=project_slug
            ),
            "instagram_en": _with_social_hashtags(
                en_description, recipe=recipe_text, language="en", project_slug=project_slug
            ),
            "youtube_en_title": "Starting matters more than doing it all at once",
            "youtube_en_description": _with_social_hashtags(
                en_description, recipe=recipe_text, language="en", project_slug=project_slug
            ),
        }
    if advertising:
        ru_description = f"Показываем товар в деле и честно разбираем последовательность действий.\n\n{recipe_text}".strip()
        en_description = f"A clear product demonstration with a practical step by step result.\n\n{recipe_text}".strip()
        return {
            "youtube_ru_title": "Товар в деле, понятный результат",
            "youtube_ru_description": _with_social_hashtags(ru_description, recipe=recipe, language="ru", advertising=True),
            "instagram_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru", advertising=True),
            "vk_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru", advertising=True),
            "tiktok_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru", advertising=True),
            "instagram_en": _with_social_hashtags(en_description, recipe=recipe, language="en", advertising=True),
            "youtube_en_title": "Product in action, clear result",
            "youtube_en_description": _with_social_hashtags(en_description, recipe=recipe, language="en", advertising=True),
        }
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
        "instagram_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "vk_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "tiktok_ru": _with_social_hashtags(ru_description, recipe=recipe, language="ru"),
        "instagram_en": _with_social_hashtags(en_description, recipe=recipe, language="en"),
        "youtube_en_title": "Homemade grandma style recipe",
        "youtube_en_description": _with_social_hashtags(en_description, recipe=recipe, language="en"),
    }


def _extract_first_http_url(text: str) -> str:
    match = re.search(r"https?://\S+", text or "")
    if not match:
        return ""
    return match.group(0).rstrip(").,;!?'\"")


async def _resolve_social_recipe_text(recipe: str) -> str:
    text = str(recipe or "").strip()
    source_url = _extract_first_http_url(text)
    if not source_url:
        return text
    try:
        return await asyncio.to_thread(fetch_recipe_text_from_url, source_url)
    except Exception:
        logger.exception("social recipe source url extraction failed")
        return text


def _social_copy_prompt(
    recipe: str,
    *,
    scheduled_comment: str = "",
    advertising: bool = False,
    product_context: str = "",
    project_slug: str | None = None,
) -> str:
    comment = str(scheduled_comment or "").strip()
    comment_block = (
        "КОММЕНТАРИЙ ПОЛЬЗОВАТЕЛЯ К ГЕНЕРАЦИИ:\n"
        f"{comment}\n\n"
        if comment
        else ""
    )
    if _is_discipline_social_project(project_slug):
        return (
            "Ты редактор коротких видео проекта «Дисциплина» для Reels, YouTube Shorts, VK и TikTok.\n\n"
            "Из утверждённой истории или метода подготовь публикационные тексты. Это НЕ кулинарный проект.\n\n"
            "ТРЕБОВАНИЯ:\n"
            "- русские тексты раскрывают проблему героя, применённый метод, первый выполнимый шаг и завершённый результат\n"
            "- английские тексты являются естественной адаптацией той же истории\n"
            "- не оформляй текст как рецепт, не добавляй ингредиенты, готовку, блюда или кулинарные шаги\n"
            "- нигде не упоминай бабушку, бабушкины рецепты, grandma или семейную кухню\n"
            "- русский обязательный хэштег: #тихаядисциплина, после него 1-2 русских хэштега строго по теме ролика\n"
            "- английский обязательный хэштег: #quietdiscipline, после него 1-2 английских хэштега строго по теме ролика\n"
            "- не добавляй кулинарные или бабушкины хэштеги\n"
            "- без markdown, кавычек и тире; не выдумывай факты, которых нет в исходнике\n\n"
            "Верни строго JSON объект без markdown с ключами:\n"
            "youtube_ru_title, youtube_ru_description, instagram_ru, vk_ru, "
            "tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
            f"{comment_block}"
            "УТВЕРЖДЁННАЯ ИСТОРИЯ ИЛИ МЕТОД:\n"
            f"{recipe.strip()}"
        )
    if advertising:
        return (
            "Ты эксперт по рекламным Reels, YouTube Shorts и продвижению в соцсетях.\n\n"
            "На основе утверждённой идеи/рецепта/истории и карточки товара подготовь публикационные тексты.\n"
            "Не превращай бытовую инструкцию или историю в рецепт. Не выдумывай свойства, эффективность, состав, скидки, "
            "медицинские обещания или факты о товаре. Ясно обозначь рекламный характер публикации.\n"
            "Сделай отдельные RU title/description для YouTube Shorts, RU-тексты для Instagram, VK, TikTok, "
            "а также EN Instagram и EN YouTube title/description. Тексты человечные, конкретные, легко копируются. "
            "Если источник recipe, сохрани ингредиенты и шаги; если how-to, сохрани проблему, действия и результат; если story, "
            "сохрани хук и вывод. В каждом тексте естественно назови товар и его роль. Без markdown, кавычек и тире.\n"
            "Русские тексты: #реклама #товар #полезныйсовет плюс 1-2 тематических хэштега. "
            "Английские: #ad #product #howto плюс 1-2 тематических.\n"
            "Верни строго JSON без markdown с ключами: youtube_ru_title, youtube_ru_description, "
            "instagram_ru, vk_ru, tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
            f"{comment_block}"
            f"КАРТОЧКА ТОВАРА:\n{str(product_context or '').strip()}\n\n"
            f"УТВЕРЖДЁННЫЙ ИСХОДНИК:\n{recipe.strip()}"
        )
    return (
        "Ты эксперт по Reels, YouTube Shorts и продвижению в соцсетях.\n\n"
        "Из утвержденного рецепта ниже сделай публикационные тексты.\n\n"
        "ТРЕБОВАНИЯ:\n"
        "- сделай 4 максимально вирусных русских описания для YouTube Shorts, Instagram, VK и TikTok\n"
        "- для YouTube Shorts отдельно верни заголовок и отдельно описание\n"
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
        "ЗАПРЕТ НА НЕПОДТВЕРЖДЕННЫЕ БАБУШКИНЫ КЛИШЕ:\n"
        "- не используй формулировки «моя бабушка», «у моей бабушки», «как у моей бабушки», «бабушка научила», "
        "«рецепт моей бабушки» и похожие заявления о личной семейной истории, если этого нет дословно или явно "
        "в утвержденном рецепте/комментарии пользователя\n"
        "- можно сохранять брендовые хэштеги и общий теплый домашний тон, но не придумывай личную бабушку автора\n\n"
        "ЕСЛИ ЕСТЬ КОММЕНТАРИЙ ПОЛЬЗОВАТЕЛЯ:\n"
        "- обязательно учитывай его как editorial direction для тона, акцентов, подачи и вирусного угла\n"
        "- комментарий не заменяет рецепт\n"
        "- не добавляй ингредиенты, шаги и факты, которых нет в утвержденном рецепте\n\n"
        "Верни строго JSON объект без markdown с ключами:\n"
        "youtube_ru_title, youtube_ru_description, instagram_ru, vk_ru, tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
        f"{comment_block}"
        "УТВЕРЖДЕННЫЙ РЕЦЕПТ:\n"
        f"{recipe.strip()}"
    )


async def _generate_social_copy_from_recipe(
    recipe: str,
    *,
    scheduled_comment: str = "",
    advertising: bool = False,
    product_context: str = "",
    project_slug: str | None = None,
) -> dict[str, str]:
    fallback = _fallback_social_copy(recipe, advertising=advertising, project_slug=project_slug)
    prompt = _social_copy_prompt(
        recipe,
        scheduled_comment=scheduled_comment,
        advertising=advertising,
        product_context=product_context,
        project_slug=project_slug,
    )
    try:
        raw = await asyncio.to_thread(
            gemini_generate_sync,
            prompt,
            model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
        )
        obj = extract_json_object(raw or "")
    except Exception:
        logger.exception("social copy generation failed")
        return fallback
    out: dict[str, str] = {}
    for key, fallback_value in fallback.items():
        value = _clean_social_copy_message(str(obj.get(key) or ""))
        value = value or fallback_value
        if _is_discipline_social_project(project_slug) and not _discipline_social_copy_is_valid(value):
            value = fallback_value
        if key in SOCIAL_COPY_RU_KEYS:
            value = _with_social_hashtags(
                value,
                recipe=recipe,
                language="ru",
                advertising=advertising,
                project_slug=project_slug,
            )
        elif key in SOCIAL_COPY_EN_KEYS:
            value = _with_social_hashtags(
                value,
                recipe=recipe,
                language="en",
                advertising=advertising,
                project_slug=project_slug,
            )
        out[key] = value
    return out


def _social_target_button_label(target_key: str, selected: set[str]) -> str:
    mark = "✅" if target_key in selected else "☐"
    return f"{mark} {publication_target_label(target_key)}"


def _social_video_variant_button_label(variant: str, selected_variant: str) -> str:
    mark = "✅" if variant == selected_variant else "☐"
    return f"{mark} video: {PUBLICATION_VIDEO_VARIANT_LABELS.get(variant, variant)}"


def _social_scheduled_at_label(pending: dict) -> str:
    raw = str(pending.get(SOCIAL_PUBLISH_SCHEDULED_AT_KEY) or "").strip()
    if not raw:
        return "после команды «Публиковать выбранное»"
    try:
        scheduled = normalize_publication_scheduled_at(raw)
    except ValueError:
        return raw
    moscow = scheduled.astimezone(timezone(timedelta(hours=3)))
    return moscow.strftime("%Y-%m-%d %H:%M MSK")


def _social_publish_target_keys_for_copy(copy: dict[str, str]) -> list[str]:
    return [target.key for target in build_publication_targets(copy)]


def _social_selected_target_keys(pending: dict) -> set[str]:
    copy = pending.get("copy") if isinstance(pending.get("copy"), dict) else {}
    available = set(_social_publish_target_keys_for_copy(copy))
    raw = pending.get(SOCIAL_PUBLISH_TARGETS_KEY)
    if isinstance(raw, list):
        selected = {str(item) for item in raw if str(item) in available}
    else:
        selected = set()
    return selected


def _social_descriptions_approval_markup(pending: dict | None = None) -> ReplyKeyboardMarkup:
    keyboard = [["ОК"], ["ФИДБЕК"], ["Исправь"]]
    if isinstance(pending, dict):
        if pending.get("origin") == "production_final":
            keyboard.insert(1, ["Публиковать выбранное"])
        copy = pending.get("copy") if isinstance(pending.get("copy"), dict) else {}
        available = _social_publish_target_keys_for_copy(copy)
        selected = _social_selected_target_keys(pending)
        for idx in range(0, len(available), 2):
            keyboard.append([
                _social_target_button_label(key, selected)
                for key in available[idx : idx + 2]
            ])
        selected_variant = str(pending.get(SOCIAL_PUBLISH_VIDEO_VARIANT_KEY) or DEFAULT_PUBLICATION_VIDEO_VARIANT)
        for variant in PUBLICATION_VIDEO_VARIANT_LABELS:
            keyboard.append([_social_video_variant_button_label(variant, selected_variant)])
        keyboard.append(["Время: сейчас"])
    keyboard.append([NAV_BUTTON_START_OVER])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _social_publication_selection_summary(pending: dict) -> str:
    copy = pending.get("copy") if isinstance(pending.get("copy"), dict) else {}
    selected = _social_selected_target_keys(pending)
    targets = build_publication_targets(copy)
    lines = ["Куда публикуем:"]
    for target in targets:
        title_flag = "title" if target.title else "no title"
        desc_len = len(str(target.description or ""))
        lines.append(
            f"{'✅' if target.key in selected else '☐'} {publication_target_label(target)} "
            f"({target.language.upper()}, {title_flag}, {desc_len} chars)"
        )
    variant = str(pending.get(SOCIAL_PUBLISH_VIDEO_VARIANT_KEY) or DEFAULT_PUBLICATION_VIDEO_VARIANT)
    lines.append(f"Версия видео: {PUBLICATION_VIDEO_VARIANT_LABELS.get(variant, variant)}")
    lines.append(f"Время публикации: {_social_scheduled_at_label(pending)}")
    return "\n".join(lines)


def _toggle_social_publication_selection(pending: dict, text: str) -> bool:
    normalized = str(text or "").replace("✅", "").replace("☐", "").strip()
    copy = pending.get("copy") if isinstance(pending.get("copy"), dict) else {}
    available = _social_publish_target_keys_for_copy(copy)
    selected = _social_selected_target_keys(pending)
    for target_key in available:
        if normalized == publication_target_label(target_key):
            if target_key in selected:
                selected.remove(target_key)
            else:
                selected.add(target_key)
            pending[SOCIAL_PUBLISH_TARGETS_KEY] = [key for key in available if key in selected]
            return True
    prefix = "video:"
    if normalized.lower().startswith(prefix):
        label = normalized[len(prefix):].strip()
        for variant, variant_label in PUBLICATION_VIDEO_VARIANT_LABELS.items():
            if label == variant_label:
                pending[SOCIAL_PUBLISH_VIDEO_VARIANT_KEY] = variant
                return True
    if normalized.lower() == "время: сейчас":
        pending.pop(SOCIAL_PUBLISH_SCHEDULED_AT_KEY, None)
        return True
    return False


def _apply_social_publication_time(pending: dict, text: str) -> bool:
    raw = str(text or "").strip()
    match = re.match(r"^(?:время|публикация|schedule|time)\s*:?\s*(.+)$", raw, flags=re.IGNORECASE)
    if not match:
        return False
    value = match.group(1).strip()
    if value.lower() in {"сейчас", "now", "нет", "no", "-"}:
        pending.pop(SOCIAL_PUBLISH_SCHEDULED_AT_KEY, None)
        return True
    scheduled = normalize_publication_scheduled_at(value, default_timezone=timezone(timedelta(hours=3)))
    pending[SOCIAL_PUBLISH_SCHEDULED_AT_KEY] = scheduled.isoformat()
    return True


def _social_copy_messages(copy: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("YouTube Shorts RU заголовок, скопировать отдельно", copy["youtube_ru_title"]),
        ("YouTube Shorts RU описание, скопировать отдельно", copy["youtube_ru_description"]),
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
    pending: dict | None = None,
) -> None:
    mapping = _social_copy_video_targets(video_targets)
    if mapping:
        await _reply_text_with_optional_markup(update, mapping)
    for title, body in _social_copy_messages(copy):
        await _send_copyable_message(update, title, body)
    if isinstance(pending, dict) and pending.get("instagram_ru_trial_description"):
        await _send_copyable_message(update, "Instagram RU пробный Reel, скопировать отдельно",
                                     pending["instagram_ru_trial_description"])
    elif isinstance(pending, dict) and pending.get("trial_generation_failed"):
        await _reply_text_with_optional_markup(update, "Пробное описание не удалось сгенерировать. Основные тексты сохранены; повторите генерацию пробного перед публикацией.")
    if isinstance(pending, dict):
        await _reply_text_with_optional_markup(update, _social_publication_selection_summary(pending))
    await _reply_text_with_optional_markup(
        update,
        "Проверьте описания.\n"
        "Кнопками ниже можно снять/поставить площадки и выбрать версию финального видео.\n"
        "Время можно задать сообщением «Время 2026-11-14 18:30».\n"
        "«ОК» — сохранить и завершить без публикации.\n"
        "Для публикации отдельно отметьте площадки и нажмите «Публиковать выбранное».\n"
        "«ФИДБЕК» или «Исправь» — пришлите, что поменять, и я перепишу описания.",
        reply_markup=_social_descriptions_approval_markup(pending),
    )


async def _refine_social_copy_from_feedback(
    *,
    recipe: str,
    previous_copy: dict[str, str],
    feedback: str,
    advertising: bool = False,
    product_context: str = "",
    project_slug: str | None = None,
) -> dict[str, str]:
    discipline_fallback = _fallback_social_copy(
        recipe,
        advertising=advertising,
        project_slug=project_slug,
    )
    fallback = previous_copy or discipline_fallback
    if _is_discipline_social_project(project_slug):
        fallback = {
            key: (
                str(fallback.get(key) or "")
                if _discipline_social_copy_is_valid(str(fallback.get(key) or ""))
                else discipline_fallback[key]
            )
            for key in discipline_fallback
        }
    prompt = (
        (
            "Ты редактируешь публикационные тексты проекта «Дисциплина». Сохрани JSON-структуру и учти фидбэк. "
            "Это описание короткой истории или метода, а не рецепт: никаких ингредиентов, блюд, готовки и рецептурной структуры. "
            "Нигде не упоминай бабушку, grandma, бабушкины рецепты или семейную кухню. "
            "Русские описания: #тихаядисциплина + 1-2 русских тематических хэштега. "
            "Английские описания: #quietdiscipline + 1-2 английских тематических хэштега. "
            "Верни строго JSON объект без markdown с ключами: youtube_ru_title, youtube_ru_description, "
            "instagram_ru, vk_ru, tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
            f"ИСТОРИЯ ИЛИ МЕТОД:\n{recipe.strip()}\n\n"
            f"ПРЕДЫДУЩИЕ ОПИСАНИЯ JSON:\n{previous_copy}\n\n"
            f"ФИДБЭК ПОЛЬЗОВАТЕЛЯ:\n{feedback.strip()}\n"
        )
        if _is_discipline_social_project(project_slug)
        else
        (
            "Ты редактируешь рекламные публикационные тексты для Reels, YouTube Shorts, VK и TikTok.\n"
            "Сохрани JSON-структуру. Учти фидбэк, не выдумывай свойства, обещания или факты о товаре. "
            "Не превращай нерецептурную инструкцию/историю в рецепт. Русские хэштеги: #реклама #товар #полезныйсовет. "
            "Английские: #ad #product #howto.\n"
            "Верни строго JSON объект без markdown с ключами: youtube_ru_title, youtube_ru_description, "
            "instagram_ru, vk_ru, tiktok_ru, instagram_en, youtube_en_title, youtube_en_description.\n\n"
            f"КАРТОЧКА ТОВАРА:\n{product_context}\n\n"
            f"ИСХОДНИК:\n{recipe.strip()}\n\n"
            f"ПРЕДЫДУЩИЕ ОПИСАНИЯ JSON:\n{previous_copy}\n\n"
            f"ФИДБЭК ПОЛЬЗОВАТЕЛЯ:\n{feedback.strip()}\n"
        )
        if advertising
        else
        "Ты редактируешь публикационные тексты для Reels, YouTube Shorts, VK и TikTok.\n\n"
        "Сохрани структуру JSON и все требования к хэштегам.\n"
        "Учти фидбэк пользователя, но не выдумывай ингредиенты и количества.\n"
        "Русские описания: #убабушкивкуснее #бабушкинырецепты #рецепты + 1-2 хэштега по рецепту.\n"
        "Английские описания: #grandmatastesbetter #grandmasrecipes #recipes + 1-2 английских хэштега по рецепту.\n"
        "Не используй «моя бабушка», «у моей бабушки», «как у моей бабушки», «бабушка научила», "
        "«рецепт моей бабушки» и похожую личную семейную легенду, если это явно не дано в рецепте или фидбэке.\n"
        "Верни строго JSON объект без markdown с ключами:\n"
        "youtube_ru_title, youtube_ru_description, instagram_ru, vk_ru, tiktok_ru, "
        "instagram_en, youtube_en_title, youtube_en_description.\n\n"
        f"РЕЦЕПТ:\n{recipe.strip()}\n\n"
        f"ПРЕДЫДУЩИЕ ОПИСАНИЯ JSON:\n{previous_copy}\n\n"
        f"ФИДБЭК ПОЛЬЗОВАТЕЛЯ:\n{feedback.strip()}\n"
    )
    try:
        raw = await asyncio.to_thread(
            gemini_generate_sync,
            prompt,
            model_profile=GEMINI_MODEL_PROFILE_EDIT,
        )
        obj = extract_json_object(raw or "")
    except Exception:
        logger.exception("social copy refinement failed")
        return fallback
    out: dict[str, str] = {}
    for key, fallback_value in fallback.items():
        value = _clean_social_copy_message(str(obj.get(key) or ""))
        value = value or fallback_value
        if _is_discipline_social_project(project_slug) and not _discipline_social_copy_is_valid(value):
            value = fallback_value
        if key in SOCIAL_COPY_RU_KEYS:
            value = _with_social_hashtags(
                value,
                recipe=recipe,
                language="ru",
                advertising=advertising,
                project_slug=project_slug,
            )
        elif key in SOCIAL_COPY_EN_KEYS:
            value = _with_social_hashtags(
                value,
                recipe=recipe,
                language="en",
                advertising=advertising,
                project_slug=project_slug,
            )
        out[key] = value
    return out


async def _prepare_trial_copy_for_pending(session: dict, pending: dict) -> None:
    from ..publication_trial_copy import generate_trial_copy
    from ..publication_copy_store import save_bot_publication_copy

    pending.pop("instagram_ru_trial_description", None)
    pending.pop("trial_generation_failed", None)
    try:
        pending["instagram_ru_trial_description"] = await generate_trial_copy(
            recipe=pending["recipe"], normal_copy=pending["copy"],
            feedback=pending.get("last_feedback", ""),
            project_slug=pending.get("project_slug") or session.get("selected_project_key"),
        )
    except Exception:
        logger.exception("Trial caption generation failed; normal copy is preserved")
        pending["trial_generation_failed"] = True
    save_bot_publication_copy(session, pending)


async def _start_social_descriptions_approval(
    update: Update,
    *,
    session: dict,
    recipe: str,
    copy: dict[str, str],
    origin: str,
    video_targets: list[dict[str, str]] | None = None,
    advertising: bool = False,
    product_context: str = "",
    project_slug: str | None = None,
) -> None:
    session["state"] = STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
    pending = {
        "recipe": recipe,
        "copy": copy,
        "origin": origin,
        "video_targets": video_targets or [],
        "advertising": bool(advertising),
        "product_context": str(product_context or ""),
        "project_slug": str(project_slug or ""),
        SOCIAL_PUBLISH_TARGETS_KEY: [],
        SOCIAL_PUBLISH_VIDEO_VARIANT_KEY: DEFAULT_PUBLICATION_VIDEO_VARIANT,
    }
    session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = pending
    from ..publication_copy_store import save_bot_publication_copy

    save_bot_publication_copy(session, pending)
    await _prepare_trial_copy_for_pending(session, pending)
    await _send_social_copy_for_approval(update, copy, video_targets=video_targets, pending=pending)


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
    recipe = str(session.get("copy_mode_description_text") or session.get("recipe_full_text") or "").strip()
    if str(session.get("generation_mode") or "") == "copy" and recipe == "Копия видео":
        return False
    if not recipe or session.get(SOCIAL_DESCRIPTIONS_SENT_KEY) or session.get(SOCIAL_DESCRIPTIONS_PENDING_KEY):
        return False
    run_id = str(session.get("generation_run_id") or "").strip()
    if not run_id:
        return False
    resolved_recipe = await _resolve_social_recipe_text(recipe)
    if resolved_recipe and resolved_recipe != recipe:
        recipe = resolved_recipe
        session["recipe_full_text"] = resolved_recipe
    scheduled_comment = str(session.get("scheduled_generation_comment") or "").strip()
    advertising = is_advertising_generation(session)
    product_context = str(session.get("advertising_product_brief") or session.get("brand_context") or "")
    project_slug = str(session.get("selected_project_key") or "").strip()
    discipline_project = _is_discipline_social_project(project_slug)
    await reply_text_without_keyboard(
        update.message,
        (
            "Готовлю рекламные описания для публикаций на основе утверждённого источника и карточки товара.\n"
            if advertising
            else (
                "Готовлю описания для публикаций на основе утверждённой истории или метода.\n"
                if discipline_project
                else "Готовлю описания для публикаций на основе утверждённого рецепта.\n"
            )
        )
        + f"Описания генерируются с учётом комментария: {'да' if scheduled_comment else 'нет'}.",
    )
    if discipline_project:
        copy = await _generate_social_copy_from_recipe(
            recipe,
            scheduled_comment=scheduled_comment,
            project_slug=project_slug,
        )
    elif advertising:
        copy = await _generate_social_copy_from_recipe(
            recipe,
            scheduled_comment=scheduled_comment,
            advertising=True,
            product_context=product_context,
        )
    else:
        copy = await _generate_social_copy_from_recipe(
            recipe,
            scheduled_comment=scheduled_comment,
        )
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
                "platforms": "YouTube Shorts RU, Instagram RU, VK RU, TikTok RU",
            },
            {
                "language": "en",
                "video_label": "EN видео / финальный master",
                "platforms": "Instagram EN, YouTube Shorts EN",
            },
        ],
        advertising=advertising,
        product_context=product_context,
        project_slug=project_slug,
    )
    return True


async def send_social_descriptions_for_recipe(update: Update, recipe: str) -> None:
    """Generate and send copyable social descriptions for an arbitrary recipe text."""
    recipe_text = str(recipe or "").strip()
    if not recipe_text or update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    session = SESSIONS.setdefault(chat_id, {})
    project_slug = str(session.get("selected_project_key") or "").strip()
    discipline_project = _is_discipline_social_project(project_slug)
    await reply_text_without_keyboard(
        update.message,
        (
            "Готовлю описания для публикаций на основе истории или метода."
            if discipline_project
            else "Готовлю описания для публикаций на основе рецепта."
        ),
    )
    recipe_text = await _resolve_social_recipe_text(recipe_text)
    if discipline_project:
        copy = await _generate_social_copy_from_recipe(recipe_text, project_slug=project_slug)
    else:
        copy = await _generate_social_copy_from_recipe(recipe_text)
    await _start_social_descriptions_approval(
        update,
        session=session,
        recipe=recipe_text,
        copy=copy,
        origin="description_mode",
        project_slug=project_slug,
    )


def _mirror_story_visual_track_for_english(session: dict, scenes: list[dict]) -> int:
    """Reuse approved RU Kling clips for EN final assembly without another generation."""
    if not is_story_generation(session) or not bool(session.get("story_english_requested")):
        return 0
    mirrored = 0
    for scene_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        source = resolve_scene_video_for_production(scene, "ru")
        video_bytes = selected_video_bytes_for_production(scene, "ru")
        if source is None or not video_bytes or not bool(source.get("approved_for_production")):
            continue
        source_candidate_id = str(source.get("candidate_id") or "")
        existing = resolve_scene_video_for_production(scene, "en")
        if (
            isinstance(existing, dict)
            and bool(existing.get("approved_for_production"))
            and str(existing.get("story_visual_alias_of") or "") == source_candidate_id
        ):
            mirrored += 1
            continue
        candidate = register_video_candidate(
            scene,
            language="en",
            video_bytes=video_bytes,
            source_image_candidate_id=str(source.get("source_image_candidate_id") or "") or None,
            speed_coef=float(source.get("speed_coef") or default_scene_video_speed_coef(scene)),
        )
        candidate["liked"] = bool(source.get("liked"))
        candidate["approved_for_production"] = True
        candidate["story_visual_alias_of"] = source_candidate_id
        _snapshot_candidate_speech_source(scene, "en", candidate)
        approvals = dict(scene.get("video_approvals_by_lang") or {}) if isinstance(scene.get("video_approvals_by_lang"), dict) else {}
        approvals["en"] = True
        scene["video_approvals_by_lang"] = approvals
        _persist_video_candidate_best_effort(session, scene_idx=scene_idx, candidate=candidate)
        mirrored += 1
    if mirrored == len(scenes):
        session["target_languages"] = ["ru", "en"]
    return mirrored


async def _finish_scene_media_production(update: Update, session: dict, *, message: str = "Все сцены обработаны. Production завершён.") -> bool:
    session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
    total_media_cost = estimated_session_total_media_cost(session)
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    mirrored_story_scenes = (
        _mirror_story_visual_track_for_english(session, scenes)
        if isinstance(scenes, list) and scenes
        else 0
    )
    await update.message.reply_text(
        f"{message}\n"
        f"Оценка суммарной media-side себестоимости: ~${total_media_cost:.4f}.\n\n"
        "Собираю финальные master-версии ролика; в Telegram отправлю мини-копии, если файл больше лимита Bot API."
        + (
            f"\nEN-версия использует те же утверждённые Kling-клипы без повторной генерации: {mirrored_story_scenes}/{len(scenes)} сцен."
            if mirrored_story_scenes and isinstance(scenes, list)
            else ""
        )
    )
    if isinstance(production, dict) and isinstance(scenes, list) and scenes:
        try:
            chat_id = update.effective_chat.id if update.effective_chat is not None else None
            if chat_id is None:
                raise RuntimeError("No chat id is available for final build queue.")
            queued = await asyncio.to_thread(
                queue_final_builds_for_session,
                int(chat_id),
                session,
                scenes,
                advance_series_after_success=True,
            )
            languages = ", ".join(queued.get("languages") or [])
            variants = ", ".join(queued.get("variants") or [])
            await update.message.reply_text(
                "Финальная сборка поставлена в очередь final_build_worker.\n"
                f"Run: {queued.get('run_id')}\n"
                f"Языки: {languages or '-'}\n"
                f"Версии: {variants or '-'}\n\n"
                "Бот больше не будет держать тяжелый ffmpeg-рендер внутри Telegram handler; "
                "final_build_worker доставит Telegram-мини-копии и затем предложит описания."
            )
        except Exception as e:
            logger.exception("post production queue failed")
            production["post_production"] = {"error": str(e)}
            await update.message.reply_text(
                f"Финальную сборку ролика пока не удалось поставить в очередь: {_user_error_message(e, domain='video')}"
            )
    session["state"] = STATE_READY_FOR_PRODUCTION
    await update.message.reply_text(
        "Production-сцены завершены. Жду финальный рендер и доставку Telegram-мини-копий; публикацию предложу после доставки файлов.",
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
    try:
        time_changed = _apply_social_publication_time(pending, text)
    except ValueError as exc:
        await update.message.reply_text(
            f"Не поняла время публикации: {exc}\nНапример: Время 2026-06-24 18:30",
            reply_markup=_social_descriptions_approval_markup(pending),
        )
        return True
    if time_changed:
        session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = pending
        await update.message.reply_text(
            _social_publication_selection_summary(pending),
            reply_markup=_social_descriptions_approval_markup(pending),
        )
        return True
    if _toggle_social_publication_selection(pending, text):
        session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = pending
        await update.message.reply_text(
            _social_publication_selection_summary(pending),
            reply_markup=_social_descriptions_approval_markup(pending),
        )
        return True
    if low in {"ок", "ok", "okay", "approve", "утвердить", "публиковать выбранное"}:
        publish_requested = low == "публиковать выбранное"
        origin = str(pending.get("origin") or "description_mode")
        copy = pending.get("copy")
        selected_target_keys = sorted(_social_selected_target_keys(pending))
        if publish_requested and (origin != "production_final" or not selected_target_keys):
            await update.message.reply_text(
                "Не выбрана ни одна площадка. Отметьте хотя бы одну кнопку или пришлите «ФИДБЕК».",
                reply_markup=_social_descriptions_approval_markup(pending),
            )
            return True
        if isinstance(copy, dict):
            from ..publication_copy_store import save_bot_publication_copy

            save_bot_publication_copy(session, pending, approved=True)
        if publish_requested and origin == "production_final" and isinstance(copy, dict):
            production = session.get("production")
            post_production = production.get("post_production") if isinstance(production, dict) else None
            recipe = str(pending.get("recipe") or "").strip()
            if not isinstance(post_production, dict) or not recipe:
                await update.message.reply_text("Финальные файлы ещё не готовы к публикации. Дождитесь доставки ролика.")
                return True
            if isinstance(post_production, dict) and recipe:
                try:
                    run_id = str(session.get("generation_run_id") or "").strip()
                    with SessionLocal() as db:
                        jobs = queue_publication_bundle(
                            db,
                            project_slug=str(session.get("selected_project_key") or "").strip() or None,
                            recipe=recipe,
                            copy={str(k): str(v) for k, v in copy.items()},
                            post_production=post_production,
                            origin=origin,
                            selected_target_keys=selected_target_keys,
                            video_variant=str(pending.get(SOCIAL_PUBLISH_VIDEO_VARIANT_KEY) or ""),
                            scheduled_at=pending.get(SOCIAL_PUBLISH_SCHEDULED_AT_KEY),
                            source_run_id=run_id or None,
                        )
                        if run_id:
                            for job in jobs:
                                job.payload_json = {
                                    **(job.payload_json or {}),
                                    "run_id": run_id,
                                }
                            from ..models import GenerationArtifact

                            db.add(
                                GenerationArtifact(
                                    run_id=uuid.UUID(run_id),
                                    scene_id=None,
                                    artifact_type="publication_copy_approved",
                                    label="Bot-approved publication copy",
                                    uri=f"db://generation_runs/{run_id}/publication_copy/approved",
                                    language=None,
                                    mime_type="application/json",
                                    payload_json={
                                        "copy": {str(k): str(v) for k, v in copy.items()},
                                        "recipe": recipe,
                                        "origin": origin,
                                    },
                                )
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
                    return True
        if isinstance(copy, dict):
            session[SOCIAL_DESCRIPTIONS_APPROVED_KEY] = {
                "copy": {str(k): str(v) for k, v in copy.items()},
                "recipe": str(pending.get("recipe") or ""),
                "origin": origin,
                "publication_options": dict(pending),
            }
        session.pop(SOCIAL_DESCRIPTIONS_PENDING_KEY, None)
        session[SOCIAL_DESCRIPTIONS_SENT_KEY] = True
        if origin == "production_final":
            session["state"] = STATE_READY_FOR_PRODUCTION
            mark_generation_run_ready(session)
            await update.message.reply_text(
                "Описания сохранены. Генерация завершена."
                + ("\nПубликация поставлена в очередь." if publish_requested else "\nНичего не опубликовано. Можно отдельно выбрать публикацию."),
                reply_markup=first_step_menu_markup() if publish_requested else ReplyKeyboardMarkup(
                    [["Выбрать публикацию"], *first_step_menu_markup().keyboard], resize_keyboard=True,
                ),
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
    refinement_kwargs = {
        "recipe": recipe,
        "previous_copy": {str(k): str(v) for k, v in previous_copy.items()},
        "feedback": feedback,
        "advertising": bool(pending.get("advertising")),
        "product_context": str(pending.get("product_context") or ""),
    }
    project_slug = str(pending.get("project_slug") or session.get("selected_project_key") or "").strip()
    if _is_discipline_social_project(project_slug):
        refinement_kwargs["project_slug"] = project_slug
    from ..publication_copy_store import save_bot_publication_copy

    # Preserve even legacy pending text before replacing it with a revision.
    if not pending.get("copy_revision_id"):
        save_bot_publication_copy(session, pending)
    copy = await _refine_social_copy_from_feedback(**refinement_kwargs)
    session["state"] = STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
    next_pending = {
        **pending,
        "copy": copy,
        "last_feedback": feedback,
    }
    next_pending.pop("instagram_ru_trial_description", None)
    session[SOCIAL_DESCRIPTIONS_PENDING_KEY] = next_pending
    from ..publication_copy_store import save_bot_publication_copy

    save_bot_publication_copy(session, next_pending)
    await _prepare_trial_copy_for_pending(session, next_pending)
    await _send_social_copy_for_approval(update, copy, video_targets=pending.get("video_targets"), pending=next_pending)
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


def _speech_from_video_prompt(video_prompt: str) -> str:
    """Extract the spoken phrase after `CHARACTERS SPEAKS:` from an existing prompt."""
    prompt = str(video_prompt or "")
    match = re.search(r"characters\s+speaks\s*:", prompt, flags=re.IGNORECASE)
    if not match:
        return ""
    tail = prompt[match.end():].strip()
    if not tail:
        return ""
    tail = re.split(
        r"(?i)\b(?:voice_lock_full(?:_[a-z_]+)?|speech_timing_plan|speaker_dialogue_order|character_dialogue_script)\s*:",
        tail,
        maxsplit=1,
    )[0]
    return normalize_speech_phrase_for_voice(tail.splitlines()[0].strip())


def _scene_speech_for_language_or_prompt(scene: dict, language: str) -> str:
    speech = _scene_speech_for_language(scene, language)
    if speech:
        return speech
    return _speech_from_video_prompt(_scene_video_prompt_for_language(scene, language))


def _speech_text_key(text: str) -> str:
    text = re.sub(r"\[\s*пауза\s*\]", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"[.,!?;:…]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _trim_speech_to_budget(text: str, max_chars: int, *, fallback: str = "Готово") -> str:
    words = str(text or "").strip().split()
    while words and speech_chars_excluding_pauses(" ".join(words)) > max_chars:
        words.pop()
    phrase = normalize_speech_phrase_for_voice(" ".join(words).strip(" ,.!?;:…") or fallback)
    while words and speech_chars_excluding_pauses(phrase) > max_chars:
        words.pop()
        phrase = normalize_speech_phrase_for_voice(" ".join(words).strip(" ,.!?;:…") or fallback)
    return phrase


def _fallback_scheduled_ru_speech_for_budget(text: str, max_chars: int) -> str:
    normalized = normalize_speech_phrase_for_voice(text)
    has_final_cta = _speech_text_key(normalized).endswith(_speech_text_key(FINAL_SCENE_SUBSCRIBE_CTA))
    if has_final_cta:
        for candidate in (
            f"Блюдо готово! [пауза] {FINAL_SCENE_SUBSCRIBE_CTA}",
            f"Готово! [пауза] {FINAL_SCENE_SUBSCRIBE_CTA}",
            FINAL_SCENE_SUBSCRIBE_CTA,
        ):
            if speech_chars_excluding_pauses(candidate) <= max_chars:
                return candidate
    parts = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", normalized) if part.strip()]
    if parts:
        candidate = parts[0]
        if speech_chars_excluding_pauses(candidate) <= max_chars:
            return candidate
    return _trim_speech_to_budget(normalized, max_chars)


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
                    max_llm_attempts=1,
                    rate_limit_max_attempts=1,
                ),
                retry_ai_rate_limit=False,
            )
            if shortened is None:
                await safe_send_message(
                    context.bot,
                    chat_id=chat_id,
                    text=(
                        "Снова не получилось автоматически сократить реплику. "
                        "Нажмите кнопку для повтора или пришлите короткую фразу."
                    ),
                    reply_markup=_scene_speech_options_markup(session=session),
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
                reply_markup=_scene_speech_approval_markup(session=session),
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
    if isinstance(scenes, list):
        idx = _align_draft_repair_scene_cursor(session, scenes, idx)
        return scenes, idx
    return None, idx


def _draft_repair_plan(session: dict) -> list[dict]:
    raw = session.get(DRAFT_REPAIR_ACTIVE_PLAN_KEY)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("scene_index"))
        except (TypeError, ValueError):
            continue
        stage = str(item.get("stage") or "").strip().lower()
        languages = [str(lang).strip().lower() for lang in (item.get("languages") or []) if str(lang).strip()]
        if stage not in {"prompt", "image", "video"} or not languages:
            continue
        out.append({"scene_index": idx, "stage": stage, "languages": languages})
    return sorted(out, key=lambda row: int(row["scene_index"]))


def _draft_repair_substep_for_stage(stage: str) -> str:
    return SCENE_MEDIA_SUBSTEP_VIDEO if str(stage).strip().lower() == "video" else SCENE_MEDIA_SUBSTEP_START


def _draft_repair_item_for_scene(session: dict, idx: int) -> dict | None:
    for item in _draft_repair_plan(session):
        if int(item["scene_index"]) == int(idx):
            return item
    return None


def _promote_draft_repair_frame_stage_to_video(session: dict, scene: dict, idx: int) -> None:
    raw = session.get(DRAFT_REPAIR_ACTIVE_PLAN_KEY)
    if not isinstance(raw, list):
        return
    changed = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            scene_index = int(item.get("scene_index"))
        except (TypeError, ValueError):
            continue
        if scene_index != int(idx):
            continue
        stage = str(item.get("stage") or "").strip().lower()
        if stage not in {"prompt", "image"}:
            continue
        item["stage"] = "video"
        for language in item.get("languages") or []:
            lang = normalize_language_code(str(language)) or ""
            if lang and not _draft_repair_has_generated_video(scene, lang):
                _mark_draft_repair_ignore_restored_video_candidate(scene, lang)
        changed = True
    if changed:
        session[DRAFT_REPAIR_ACTIVE_PLAN_KEY] = raw
        session["scene_media_language_index"] = 0


_SCENE_MEDIA_REPAIR_SUBSTEPS = frozenset(
    {
        SCENE_MEDIA_SUBSTEP_START,
        SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
        SCENE_MEDIA_SUBSTEP_END,
        SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
        SCENE_MEDIA_SUBSTEP_VIDEO,
    }
)


def _apply_draft_repair_scene_settings(session: dict, idx: int, *, force_substep: bool = False) -> None:
    item = _draft_repair_item_for_scene(session, idx)
    if not item:
        return
    stage = str(item.get("stage") or "").strip().lower()
    current_substep = str(session.get("scene_media_substep") or "").strip()
    if stage == "video":
        if force_substep or current_substep != SCENE_MEDIA_SUBSTEP_VIDEO:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
            session["scene_media_language_index"] = 0
    elif force_substep or current_substep not in _SCENE_MEDIA_REPAIR_SUBSTEPS:
        session["scene_media_substep"] = _draft_repair_substep_for_stage(stage)
        session["scene_media_language_index"] = 0
    if stage != "video":
        return
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not (0 <= int(idx) < len(scenes)):
        return
    scene = scenes[int(idx)]
    if not isinstance(scene, dict):
        return
    for language in item.get("languages") or []:
        lang = normalize_language_code(str(language)) or ""
        if lang and not _draft_repair_has_generated_video(scene, lang):
            _mark_draft_repair_ignore_restored_video_candidate(scene, lang)


def _align_draft_repair_scene_cursor(session: dict, scenes: list[dict], idx: int) -> int:
    plan = _draft_repair_plan(session)
    if not plan:
        return idx
    selected = [int(item["scene_index"]) for item in plan if 0 <= int(item["scene_index"]) < len(scenes)]
    if not selected:
        session.pop(DRAFT_REPAIR_ACTIVE_PLAN_KEY, None)
        return idx
    if idx in selected:
        _apply_draft_repair_scene_settings(session, idx)
        return idx
    next_indexes = [item for item in selected if item >= idx]
    if not next_indexes:
        session.pop(DRAFT_REPAIR_ACTIVE_PLAN_KEY, None)
        session["image_scene_index"] = len(scenes)
        return len(scenes)
    next_idx = min(next_indexes)
    session["image_scene_index"] = next_idx
    _apply_draft_repair_scene_settings(session, next_idx, force_substep=True)
    return next_idx


def _advance_scene_media_after_current(session: dict, idx: int, *, total: int | None = None) -> int:
    production = session.get("production") or {}
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    scene_count = len(scenes) if isinstance(scenes, list) else int(total or 0)
    next_idx = int(idx) + 1
    session["image_scene_index"] = next_idx
    session["scene_media_language_index"] = 0
    if isinstance(scenes, list):
        if not _draft_repair_plan(session):
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            if 0 <= next_idx < len(scenes):
                following = scenes[next_idx]
                source_id = following.get("continuation_source_video_candidate_id")
                approved_sources = {
                    next((c.get("candidate_id") for c in reversed(candidates)
                          if c.get("approved_for_production")), None)
                    for candidates in (scenes[idx].get("video_candidates_by_lang") or {}).values()
                }
                if source_id and source_id in approved_sources and any(
                    c.get("approved_for_video") for c in following.get("image_candidates") or []
                ):
                    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
            return next_idx
        return _align_draft_repair_scene_cursor(session, scenes, next_idx)
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
    if scene_count and next_idx >= scene_count:
        return scene_count
    return next_idx


def _scene_target_languages(session: dict, scene: dict) -> list[str]:
    """Resolve active languages for current scene media workflow."""
    if is_advertising_generation(session) or str(scene.get("generation_mode") or "").strip().lower() == "advertising":
        return ["ru"]
    try:
        idx = int(session.get("image_scene_index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    repair_item = _draft_repair_item_for_scene(session, idx)
    if repair_item:
        repair_languages = [
            code
            for code in (normalize_language_code(str(lang)) for lang in repair_item.get("languages", []))
            if code
        ]
        if repair_languages:
            return list(dict.fromkeys(repair_languages))
    raw = session.get("target_languages")
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            code = normalize_language_code(str(item))
            if code and code not in out:
                out.append(code)
    # Story mode intentionally generates the silent Kling visual only once.
    # Optional EN narration reuses the approved RU visual candidate at final build.
    if is_story_generation(session) and out:
        return out
    prompt_map = scene.get("video_prompt_by_lang")
    if isinstance(prompt_map, dict):
        for key in prompt_map:
            code = normalize_language_code(str(key))
            if code and code not in out:
                out.append(code)
    fallback_prompt_map = _video_prompt_by_lang_with_candidate_fallback(scene)
    for key in fallback_prompt_map:
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
    mapping = _video_prompt_by_lang_with_candidate_fallback(scene)
    value = str(mapping.get(language, "")).strip()
    if value:
        if str(scene.get("generation_mode") or "").strip().lower() == "story":
            value = ensure_story_video_camera_safety(value, allow_reveal=bool(scene.get("story_camera_reveal_requested")))
        if not isinstance(scene.get("video_prompt_by_lang"), dict) or str(scene["video_prompt_by_lang"].get(language, "")).strip() != value:
            scene["video_prompt_by_lang"] = {**(scene.get("video_prompt_by_lang") if isinstance(scene.get("video_prompt_by_lang"), dict) else {}), language: value}
            if language == "ru" or not str(scene.get("video_prompt") or "").strip():
                scene["video_prompt"] = value
        return value
    value = str(scene.get("video_prompt", "")).strip()
    if value and str(scene.get("generation_mode") or "").strip().lower() == "story":
        value = ensure_story_video_camera_safety(value, allow_reveal=bool(scene.get("story_camera_reveal_requested")))
        scene["video_prompt"] = value
    return value


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


def _scene_video_approved_for_language(scene: dict, language: str) -> bool:
    if _video_regeneration_requested(scene, language):
        return False
    approvals = scene.get("video_approvals_by_lang")
    return isinstance(approvals, dict) and bool(approvals.get(language))


def _scene_video_feedback_languages(session: dict, scene: dict, requested_language: str) -> list[str]:
    """Scope feedback so approved language videos are not invalidated accidentally."""
    languages = _scene_target_languages(session, scene)
    if requested_language not in languages:
        requested_language = languages[0] if languages else "ru"
    unapproved = [lang for lang in languages if not _scene_video_approved_for_language(scene, lang)]
    if unapproved and requested_language not in unapproved:
        return [unapproved[0]]
    if unapproved and len(unapproved) < len(languages):
        return [requested_language]
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


def _clear_scene_dialogue_for_language(scene: dict, language: str) -> None:
    """Manual phrase edits replace stale per-speaker dialogue for that language."""
    dialogue_map = scene.get("dialogue_by_lang")
    if isinstance(dialogue_map, dict):
        dialogue_map[language] = []
        scene["dialogue_by_lang"] = dialogue_map
    if language == "ru":
        scene["dialogue"] = []


def _scene_speech_for_language(scene: dict, language: str) -> str:
    """Resolve current speech text for language with RU fallback."""
    speech_map = scene.get("speech_by_lang")
    if isinstance(speech_map, dict):
        val = normalize_speech_phrase_for_voice(str(speech_map.get(language, "")).strip())
        if val:
            return val
    if language == "ru":
        return normalize_speech_phrase_for_voice(str(scene.get("text", "")).strip())
    return ""


_DIALOGUE_SPEAKER_ALIASES = {
    "grandma": "grandma",
    "бабушка": "grandma",
    "babushka": "grandma",
    "grandfather": "grandfather",
    "grandpa": "grandfather",
    "дедушка": "grandfather",
    "grandson": "grandson",
    "внук": "grandson",
    "daughter": "daughter",
    "дочь": "daughter",
    "granddaughter": "granddaughter",
    "внучка": "granddaughter",
    "banana": "banana",
    "банан": "banana",
    "strawberry": "strawberry",
    "клубника": "strawberry",
}

_DIALOGUE_SPEAKER_PROMPT_LABELS = {
    "grandma": "GRANDMA",
    "grandfather": "GRANDFATHER",
    "grandson": "GRANDSON",
    "granddaughter": "GRANDDAUGHTER",
    "daughter": "DAUGHTER",
    "banana": "BANANA",
    "strawberry": "STRAWBERRY",
}

_DIALOGUE_SPEAKER_TIMING_LABELS_RU = {
    "grandma": "Бабушка",
    "grandfather": "Дедушка",
    "grandson": "Внук",
    "granddaughter": "Внучка",
    "daughter": "Дочь",
    "banana": "Банан",
    "strawberry": "Клубника",
}


def _normalize_dialogue_speaker(value: str) -> str:
    return _DIALOGUE_SPEAKER_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def _dialogue_speaker_timing_label(speaker: str, language: str) -> str:
    normalized = _normalize_dialogue_speaker(speaker)
    if normalize_language_code(language) == "ru":
        return _DIALOGUE_SPEAKER_TIMING_LABELS_RU.get(normalized, normalized)
    return _DIALOGUE_SPEAKER_PROMPT_LABELS.get(normalized, normalized.upper()).title()


def _speech_timing_text_for_speaker(speaker: str, text: str, language: str) -> str:
    clean = normalize_speech_phrase_for_voice(text)
    if not clean:
        return ""
    label = _dialogue_speaker_timing_label(speaker, language)
    known_labels = set(_DIALOGUE_SPEAKER_PROMPT_LABELS.values())
    known_labels.update(value.upper() for value in _DIALOGUE_SPEAKER_TIMING_LABELS_RU.values())
    match = re.match(r"^([^:\n]{1,40}):\s*", clean)
    if match and match.group(1).strip().upper() in known_labels:
        return clean
    return f"{label}: {clean}"


def _dialogue_from_prompt_timing_plan(prompt: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    in_timing = False
    for line in str(prompt or "").splitlines():
        clean = line.strip()
        if not clean:
            if in_timing:
                break
            continue
        if clean.lower().startswith("speech_timing_plan:"):
            in_timing = True
            continue
        if not in_timing:
            continue
        if not clean.startswith("-"):
            break
        match = re.match(r"^-\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?s:\s*([^:\n]{1,40}):\s*(.+)$", clean)
        if not match:
            continue
        speaker = _normalize_dialogue_speaker(match.group(1))
        # A colon inside narration is punctuation, not an arbitrary speaker label.
        if speaker not in _DIALOGUE_SPEAKER_PROMPT_LABELS:
            continue
        text = normalize_speech_phrase_for_voice(match.group(2))
        if speaker and text:
            out.append({"speaker": speaker, "text": text})
    return out


def _manual_dialogue_from_video_prompt(prompt: str) -> list[dict[str, str]]:
    timing_dialogue = _dialogue_from_prompt_timing_plan(prompt)
    if timing_dialogue:
        return timing_dialogue
    return _dialogue_from_character_dialogue_script(prompt)


def _aggregate_dialogue_text(dialogue: list[dict[str, str]]) -> str:
    return " [пауза] ".join(
        normalize_speech_phrase_for_voice(str(item.get("text") or "").strip())
        for item in dialogue
        if str(item.get("text") or "").strip()
    ).strip()


def _dialogue_from_character_dialogue_script(prompt: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    in_script = False
    for line in str(prompt or "").splitlines():
        clean = line.strip()
        if not clean:
            if in_script:
                break
            continue
        if clean.lower().startswith("character_dialogue_script:"):
            in_script = True
            continue
        if not in_script:
            continue
        if clean.lower().startswith(("voice_lock_full_", "speech_timing_plan:", "speaker_dialogue_order:")):
            break
        match = re.match(r"^([^:\n]{1,40}):\s*(.+)$", clean)
        if not match:
            break
        speaker = _normalize_dialogue_speaker(match.group(1))
        text = normalize_speech_phrase_for_voice(match.group(2))
        if speaker and text:
            out.append({"speaker": speaker, "text": text})
    return out


def _scene_dialogue_for_language(scene: dict, language: str, *, prompt: str = "") -> list[dict[str, str]]:
    dialogue_map = scene.get("dialogue_by_lang")
    raw = dialogue_map.get(language) if isinstance(dialogue_map, dict) else None
    if not isinstance(raw, list) and normalize_language_code(language) == "ru":
        raw = scene.get("dialogue")
    out: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            speaker = _normalize_dialogue_speaker(item.get("speaker"))
            text = normalize_speech_phrase_for_voice(str(item.get("text") or "").strip())
            if speaker and text:
                out.append({"speaker": speaker, "text": text})
    if out:
        return out
    plan_map = scene.get("speech_plan_by_lang")
    plan = plan_map.get(language) if isinstance(plan_map, dict) else None
    if isinstance(plan, list):
        for item in plan:
            if not isinstance(item, dict):
                continue
            speaker = _normalize_dialogue_speaker(item.get("speaker"))
            text = normalize_speech_phrase_for_voice(str(item.get("text") or "").strip())
            if speaker and text:
                out.append({"speaker": speaker, "text": text})
    if out:
        return out
    out = _manual_dialogue_from_video_prompt(prompt)
    if out:
        return out
    return []


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
    if speaker == "daughter":
        return (
            "female voice English language adult around thirty years old; neutral English accent; tone warm lively; pitch medium; speech rate normal 0.95; clear articulation."
            if lang == "en"
            else "female voice Russian language adult around thirty years old; neutral Russian accent; tone warm lively; pitch medium; speech rate normal 0.95; clear articulation."
        )
    return (
        "neutral English voice, medium pitch, calm delivery."
        if lang == "en"
        else "neutral Russian voice, medium pitch, calm delivery."
    )


_MANUAL_PROMPT_VOICE_LOCK_SPEAKERS = (
    "grandma",
    "grandfather",
    "grandson",
    "granddaughter",
    "daughter",
    "banana",
    "strawberry",
)

_MANUAL_PROMPT_SPEAKER_MARKERS = {
    "grandma": ("grandma", "grandmother", "babushka", "бабуш"),
    "grandfather": ("grandfather", "grandpa", "дедуш"),
    "grandson": ("grandson", "внук", "внучек", "внучок"),
    "granddaughter": ("granddaughter", "внучк"),
    "daughter": ("daughter", "дочь", "дочка"),
    "banana": ("banana", "банан"),
    "strawberry": ("strawberry", "клубник"),
}


def _manual_prompt_voice_lock_speakers(scene: dict, prompt: str) -> list[str]:
    out: list[str] = []
    chars = scene.get("characters")
    if isinstance(chars, list):
        for item in chars:
            speaker = _normalize_dialogue_speaker(item)
            if speaker in _MANUAL_PROMPT_VOICE_LOCK_SPEAKERS and speaker not in out:
                out.append(speaker)
    low = str(prompt or "").lower()
    for speaker in _MANUAL_PROMPT_VOICE_LOCK_SPEAKERS:
        if speaker in out:
            continue
        markers = _MANUAL_PROMPT_SPEAKER_MARKERS.get(speaker, ())
        if any(marker in low for marker in markers):
            out.append(speaker)
    return out


def _append_manual_prompt_voice_locks(prompt: str, scene: dict, language: str) -> str:
    out = str(prompt or "").strip()
    if not out:
        return out
    for speaker in _manual_prompt_voice_lock_speakers(scene, out):
        if re.search(rf"\bvoice_lock_full_{re.escape(speaker)}\s*:", out, flags=re.IGNORECASE):
            continue
        out = f"{out}\nvoice_lock_full_{speaker}: {_dialogue_voice_lock_full(speaker, language)}".strip()
    return out


def _scene_voice_speakers_for_language(scene: dict, language: str) -> set[str]:
    speakers = {
        _normalize_dialogue_speaker(item.get("speaker"))
        for item in _scene_dialogue_for_language(scene, language)
    }
    plan_map = scene.get("speech_plan_by_lang")
    plan = plan_map.get(language) if isinstance(plan_map, dict) else None
    if isinstance(plan, list):
        for item in plan:
            if isinstance(item, dict):
                speaker = _normalize_dialogue_speaker(item.get("speaker"))
                if speaker:
                    speakers.add(speaker)
    return {speaker for speaker in speakers if speaker}


def _scene_voice_speaker_order_for_language(scene: dict, language: str, *, prompt: str = "") -> list[str]:
    out: list[str] = []
    for item in _scene_dialogue_for_language(scene, language, prompt=prompt):
        speaker = _normalize_dialogue_speaker(item.get("speaker"))
        if speaker and speaker not in out:
            out.append(speaker)
    plan_map = scene.get("speech_plan_by_lang")
    plan = plan_map.get(language) if isinstance(plan_map, dict) else None
    if isinstance(plan, list):
        for item in plan:
            if isinstance(item, dict):
                speaker = _normalize_dialogue_speaker(item.get("speaker"))
                if speaker and speaker not in out:
                    out.append(speaker)
    return out


def _scene_requires_grandma_named_voice_lock(scene: dict, language: str) -> bool:
    speakers = _scene_voice_speakers_for_language(scene, language)
    if speakers:
        return "grandma" in speakers
    characters = scene.get("characters")
    if isinstance(characters, list):
        normalized = {
            _normalize_dialogue_speaker(value)
            for value in characters
            if str(value or "").strip()
        }
        if "grandma" in normalized:
            return True
        if normalized:
            return False
    for key in ("text", "action", "recipe_step", "start_frame_prompt", "video_prompt"):
        low = str(scene.get(key) or "").lower()
        if "grandma" in low or "babushka" in low or "бабуш" in low:
            return True
    return True


def _ensure_named_grandma_voice_lock(prompt: str, scene: dict, language: str) -> str:
    out = str(prompt or "").strip()
    for speaker in _scene_voice_speaker_order_for_language(scene, language, prompt=out):
        if re.search(rf"\bvoice_lock_full_{re.escape(speaker)}\s*:", out, flags=re.IGNORECASE):
            continue
        out = f"{out}\nvoice_lock_full_{speaker}: {_dialogue_voice_lock_full(speaker, language)}".strip()
    for speaker in _manual_prompt_voice_lock_speakers(scene, out):
        if re.search(rf"\bvoice_lock_full_{re.escape(speaker)}\s*:", out, flags=re.IGNORECASE):
            continue
        out = f"{out}\nvoice_lock_full_{speaker}: {_dialogue_voice_lock_full(speaker, language)}".strip()
    if _scene_voice_speakers_for_language(scene, language):
        return out
    if not _scene_requires_grandma_named_voice_lock(scene, language):
        return out
    if re.search(r"\bvoice_lock_full_grandma\s*:", out, flags=re.IGNORECASE):
        return out
    return f"{out}\nvoice_lock_full_grandma: {_dialogue_voice_lock_full('grandma', language)}".strip()


def _strip_dialogue_prompt_metadata(prompt: str) -> str:
    out = str(prompt or "").strip()
    patterns = (
        r"\n?speech_timing_plan:\s*(?:\n\s*-\s*[^\n]*)+",
        r"\n?speaker_dialogue_order:\s*[^\n]*",
        r"\n?character_dialogue_script:\s*(?:\n[^\n:]+:\s*[^\n]*)+",
        r"(?m)^\s*-\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?s:\s*[^\n]*\n?",
        r"\n?voice_lock_full(?:_[a-z_]+)?:\s*[^\n]*(?:\n\s*(?:tone|emotional state|pitch|speech rate|no pauses(?: at all)?|articulation|delivery)\b[^\n]*)*",
    )
    for pattern in patterns:
        out = re.sub(pattern, "", out, flags=re.IGNORECASE)
    out = re.sub(
        r"(?im)^\s*(?:tone|emotional state|pitch|speech rate|no pauses(?: at all)?|articulation|delivery)\b[^\n]*\n?",
        "",
        out,
    )
    return out.strip()


def _append_dialogue_prompt_metadata(prompt: str, scene: dict, language: str) -> str:
    dialogue = _scene_dialogue_for_language(scene, language, prompt=prompt)
    if len(dialogue) < 2:
        return _strip_dialogue_prompt_metadata(prompt)
    order: list[str] = []
    lines: list[str] = []
    voice_locks: list[str] = []
    seen_locks: set[str] = set()
    for item in dialogue:
        speaker = item["speaker"]
        label = _DIALOGUE_SPEAKER_PROMPT_LABELS.get(speaker, speaker.upper())
        if label not in order:
            order.append(label)
        lines.append(f"{label}: {item['text']}")
        if speaker not in seen_locks:
            voice_locks.append(f"voice_lock_full_{speaker}: {_dialogue_voice_lock_full(speaker, language)}")
            seen_locks.add(speaker)
    base = _strip_dialogue_prompt_metadata(prompt)
    metadata = [
        f"speaker_dialogue_order: {', '.join(order)}",
        "character_dialogue_script:",
        *lines,
        *voice_locks,
    ]
    return f"{base}\n" + "\n".join(metadata)


def _speech_plan_for_locked_video_prompt(
    scene: dict,
    language: str,
    speech: str,
    prompt: str,
) -> list[dict[str, str | int]]:
    dialogue = _manual_dialogue_from_video_prompt(prompt)
    if dialogue:
        return [
            {
                "speaker": item["speaker"],
                "text": _speech_timing_text_for_speaker(item["speaker"], item["text"], language),
                "order": index + 1,
                "intent": "speech",
                "language": normalize_language_code(language) or "ru",
            }
            for index, item in enumerate(dialogue)
            if str(item.get("text") or "").strip()
        ]
    plan_map = scene.get("speech_plan_by_lang")
    plan = plan_map.get(language) if isinstance(plan_map, dict) else None
    if isinstance(plan, list) and plan:
        out: list[dict[str, str | int]] = []
        for index, item in enumerate(plan):
            if not isinstance(item, dict):
                continue
            text = normalize_speech_phrase_for_voice(str(item.get("text") or ""))
            if not text:
                continue
            speaker = _normalize_dialogue_speaker(item.get("speaker"))
            rendered = _speech_timing_text_for_speaker(speaker, text, language) if speaker else text
            updated = dict(item)
            updated["text"] = rendered
            updated.setdefault("order", index + 1)
            updated.setdefault("language", normalize_language_code(language) or "ru")
            out.append(updated)
        if out:
            return out
    dialogue = _scene_dialogue_for_language(scene, language, prompt=prompt)
    if dialogue:
        return [
            {
                "speaker": item["speaker"],
                "text": _speech_timing_text_for_speaker(item["speaker"], item["text"], language),
                "order": index + 1,
                "intent": "speech",
                "language": normalize_language_code(language) or "ru",
            }
            for index, item in enumerate(dialogue)
            if str(item.get("text") or "").strip()
        ]
    return build_default_speech_plan(speech, language)


def _is_speechless_video_scene(scene: dict, session: dict | None = None) -> bool:
    if session is not None and is_silence_generation(session):
        return True
    mode = str(scene.get("generation_mode") or scene.get("mode") or "").strip().lower()
    model = str(scene.get("model") or "").strip().lower()
    return mode == "silence" or model.startswith("kling")


def _scene_video_runtime_policy(scene: dict, session: dict) -> tuple[str, str, bool]:
    """Resolve the paid video launch policy, with an immutable Story-mode lock."""
    model = str(scene.get("model", "veo3_fast")).strip() or "veo3_fast"
    if is_story_generation(session) or str(scene.get("generation_mode") or "").strip().lower() == "story":
        scene["model"] = "kling_3_0"
        scene["voice_needed"] = False
        scene["has_lipsync"] = False
        return "kling_3_0", SYNTAX_PROVIDER_KLING, False
    return model, syntax_provider_for_scene_generation(scene, session), True


def _normalize_scene_aspect_ratio_value(value: object) -> str:
    raw = str(value or "").strip()
    return raw if raw in {"9:16", "16:9"} else ""


def _session_production_plan_looks_horizontal(session: dict | None) -> bool:
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return False
    scene_dicts = [scene for scene in scenes if isinstance(scene, dict)]
    if any(_normalize_scene_aspect_ratio_value(scene.get("aspect_ratio")) == "16:9" for scene in scene_dicts):
        return True
    veo_8_sec_count = 0
    for scene in scene_dicts:
        model = str(scene.get("model") or "").strip().lower()
        try:
            duration_sec = int(scene.get("duration_sec") or 0)
        except Exception:
            duration_sec = 0
        if model.startswith("veo") and duration_sec == 8:
            veo_8_sec_count += 1
    return len(scene_dicts) >= 24 and veo_8_sec_count >= 24


def _scene_aspect_ratio(scene: dict, session: dict | None = None) -> str:
    raw = _normalize_scene_aspect_ratio_value(scene.get("aspect_ratio"))
    if raw:
        return raw
    if is_long_generation(scene) or is_long_generation(session or {}):
        return "16:9"
    if _session_production_plan_looks_horizontal(session):
        return "16:9"
    return aspect_ratio_for_generation_mode(session or {})


_VIDEO_PROMPT_ASPECT_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[.!?;\n]))\s*"
    r"(?:render\s+this\s+video\s+in\s+)?"
    r"(?:(?:horizontal|vertical)\s+)?(?:landscape|portrait)?"
    r"[^.!?\n;]{0,140}?"
    r"(?:aspect\s+ratio\s*)?(?:9:16|16:9)"
    r"[^.!?\n;]*(?:[.!?;]|$)",
    flags=re.IGNORECASE,
)


def _force_video_prompt_aspect_ratio(prompt: str, aspect_ratio: str) -> str:
    target = _normalize_scene_aspect_ratio_value(aspect_ratio) or "9:16"
    text = str(prompt or "").strip()
    if not text:
        return text
    text = _VIDEO_PROMPT_ASPECT_SENTENCE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" \n\t.;")
    if target == "16:9":
        rule = "Render this video in horizontal landscape 16:9 aspect ratio. Never use vertical portrait framing."
    else:
        rule = "Render this video in vertical portrait 9:16 aspect ratio. Never use horizontal landscape framing."
    if rule.lower() not in text.lower():
        text = f"{text}. {rule}" if text else rule
    return text


def _strip_speechless_video_prompt_metadata(prompt: str) -> str:
    out = str(prompt or "").strip()
    out = re.sub(r"\blang\s*[:=]\s*[a-z]{2}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bCHARACTERS?\s+SPEAKS?\s*:[^\n.]*(?:\.|\n|$)", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bspeaker_dialogue_order\s*:[^\n]*", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bcharacter_dialogue_script\s*:(?:\n[^\n:]+:\s*[^\n]*)+", "", out, flags=re.IGNORECASE)
    out = re.sub(
        r"\bvoice_lock_full(?:_[a-z_]+)?\s*:.*?(?=\bvisual style\b|\bno on-screen text\b|\bAudio enabled\b|\bNo dialogue\b|\bPixar\b|\bExtreme close\b|\bNostalgic\b|\bOnly grandmother\b|\n|$)",
        "",
        out,
        flags=re.IGNORECASE | re.DOTALL,
    )
    out = re.sub(r"\ball spoken phrases? and voiceover are in [^.]+\.?", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(?:accurate\s+)?lip[-\s]?sync\b[^.]*\.?", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bvoiceover\s+in\s+[A-Za-zА-Яа-я -]+\.?", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip(" \n\t.;")
    return out


def _strip_story_speechless_boilerplate(prompt: str) -> str:
    """Remove Story/Kling transport rules before adding one canonical copy."""
    out = str(prompt or "").strip()
    out = re.sub(
        r"\b\d+(?:\.\d+)?-second\s+Kling\s+3\.0\s+image-to-video\s+shot\.\s*No audio\.\s*",
        " ",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b(?:No|Forbid)\s+dialogue\s*,[^.]*?watermark\s*\.?",
        " ",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\bPreserve the approved start-frame identity, age, historical era, objects and composition\s*\.?",
        " ",
        out,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", out).strip(" \n\t.;")


def _compose_speechless_video_prompt(scene: dict, language: str, prompt: str) -> str:
    cleaned_prompt = _strip_story_speechless_boilerplate(sanitize_character_names_and_keys(prompt))
    base = _strip_speechless_video_prompt_metadata(cleaned_prompt)
    if not base:
        return ""
    try:
        duration = int(scene.get("duration_sec") or 4)
    except Exception:
        duration = 4
    duration = max(3, min(4, duration))
    scene["duration_sec"] = duration
    if str(scene.get("generation_mode") or "").strip().lower() == "story":
        prefix = f"{duration}-second Kling 3.0 image-to-video shot. No audio."
        if base and base[-1] not in ".!?":
            base = f"{base}."
        suffix = (
            "No dialogue, no speech, no voiceover, no lip sync, no music, no ambient sound, "
            "no sound effects, no on-screen text, no subtitles, no logos, no watermark. "
            "Preserve the approved start-frame identity, age, historical era, objects and composition."
        )
        text = " ".join(part for part in (prefix, base, suffix) if part).strip()
        return _prefix_video_prompt_with_language(text, language)
    required = (
        f"{duration}-second Kling 3.0 image-to-video shot. "
        "No dialogue, no voiceover, no subtitles, no on-screen text. "
        "Audio enabled; natural cooking sounds only: sauce pouring, bubbling cream sauce, sizzling food, "
        "spoon against ceramic, soft cloth movement, cat purrs or tiny paw taps when visible. "
        "No background music. Pixar-like non-realistic cartoon style. "
        "Extreme close-up, food-first framing; the dish and the orange cartoon cat face are very large in frame. "
        "Nostalgic rustic grandma kitchen atmosphere. "
        "Only grandmother and exactly one orange cartoon cat are present; no other cats, animals, or people."
    )
    text = " ".join(part for part in (base, required) if part).strip()
    return _prefix_video_prompt_with_language(text, language)


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
    syntax_provider: str | None = None,
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
        "syntax_provider": syntax_provider,
        "start_image_b64": _encode_b64(start_image),
        "end_image_b64": _encode_b64(end_image) if end_image else "",
        "syntax_error_message": (syntax_error_message or "")[:8000],
    }


def _scene_video_provider_payload_for_language(scene: dict, pending: dict, language: str) -> dict:
    payload = dict(pending)
    entries = pending.get("entries_by_lang")
    lang = normalize_language_code(language) or str(language or "").strip().lower() or "ru"
    entry = entries.get(lang) if isinstance(entries, dict) else None
    if isinstance(entry, dict):
        payload.update(entry)
    if not str(payload.get("start_image_b64") or payload.get("start_frame_image_b64") or "").strip():
        start_bytes = selected_image_bytes_for_video(scene, kind="start")
        if start_bytes:
            payload["start_image_b64"] = _encode_b64(start_bytes)
    if not str(payload.get("end_image_b64") or payload.get("end_frame_image_b64") or "").strip():
        end_bytes = selected_image_bytes_for_video(scene, kind="end")
        if end_bytes:
            payload["end_image_b64"] = _encode_b64(end_bytes)
    return payload


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
    cost_summary = _store_scene_video_result(
        scene,
        language,
        video_bytes,
        session=session,
        scene_idx=idx,
        provider=provider,
        source_image_candidate_id=str((resolve_scene_image_for_video(scene, kind='start') or {}).get("candidate_id") or ""),
    )
    vb = io.BytesIO(video_bytes)
    vb.name = f"scene_{idx + 1}_{language}.mp4"
    sent_message = await _safe_reply_video(
        update.message,
        video=vb,
        caption=f"Сцена {idx + 1}/{total}, язык {language}: видео готово{caption_suffix}",
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


async def _send_scene_video_approval_prompt(update: Update, session: dict, languages: list[str]) -> None:
    await _reply_text_with_optional_markup(
        update,
        "Ожидаю апрув языков: используйте `ОК все`, `ОК <lang>`, "
        "`ПРОМПТ <lang>: ...`, `ФИДБЕК: ...`, `ФРАЗА <lang>: ...`, `СКОРОСТЬ 1.25` "
        "или `ПЕРЕГЕНЕРИРОВАТЬ <lang>`.",
        reply_markup=scene_language_approval_markup(languages, session=session),
    )


def _compose_locked_video_prompt(scene: dict, language: str, prompt: str) -> str:
    """
    Build final prompt while locking phrase in CHARACTERS SPEAKS.

    Prompt edits (`ПРОМПТ`/`ФИДБЕК`) must not alter spoken phrase.
    """
    if _is_speechless_video_scene(scene):
        return _compose_speechless_video_prompt(scene, language, prompt)
    prompt_dialogue = _manual_dialogue_from_video_prompt(prompt)
    base_prompt = append_prompt_timing_hints(sanitize_character_names_and_keys(prompt), None, duration_sec=0.0)
    out = enforce_video_prompt_voice_requirements(base_prompt, scene, target_language=language)
    speech = _aggregate_dialogue_text(prompt_dialogue) if prompt_dialogue else _scene_speech_for_language(scene, language)
    if speech:
        out = _sync_scene_text_into_video_prompt(out, speech)
    out = _append_dialogue_prompt_metadata(out, scene, language)
    language_name = "Russian" if normalize_language_code(language) == "ru" else "English"
    language_rule = f"all spoken phrases and voiceover are in {language_name} language only"
    if language_rule.lower() not in out.lower():
        out = f"{out}\n{language_rule}.".strip()
    out = _ensure_named_grandma_voice_lock(out, scene, language)
    plan = _speech_plan_for_locked_video_prompt(scene, language, speech, out)
    out = append_prompt_timing_hints(out, plan, duration_sec=float(scene.get("duration_sec") or 0.0))
    out = _ensure_advertising_product_video_lock(scene, out)
    return _prefix_video_prompt_with_language(out, language)


def _ensure_advertising_product_video_lock(scene: dict, prompt: str) -> str:
    if str(scene.get("generation_mode") or "").strip().lower() != "advertising":
        return str(prompt or "").strip()
    meta = scene.get("prompt_meta_json") if isinstance(scene.get("prompt_meta_json"), dict) else {}
    text = clean_advertising_video_prompt(prompt)
    for key in ("advertising_product_lock_en", "advertising_product_lock_ru"):
        product_lock = str(scene.get(key) or meta.get(key) or "").strip()
        if product_lock:
            text = " ".join(text.replace(product_lock, "").split())
    continuity = advertising_video_continuity_en()
    return text if continuity.lower() in text.lower() else f"{text} {continuity}".strip()


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


def _video_regeneration_requested(scene: dict, language: str) -> bool:
    mapping = scene.get(SCENE_VIDEO_REGEN_REQUESTED_KEY)
    return isinstance(mapping, dict) and bool(mapping.get(language))


def _video_regeneration_requested_languages(scene: dict, languages: list[str]) -> list[str]:
    return [lang for lang in languages if _video_regeneration_requested(scene, lang)]


def _set_video_regeneration_requested(scene: dict, language: str, requested: bool) -> None:
    mapping = scene.get(SCENE_VIDEO_REGEN_REQUESTED_KEY)
    if not isinstance(mapping, dict):
        mapping = {}
    if requested:
        mapping[language] = True
    else:
        mapping.pop(language, None)
    scene[SCENE_VIDEO_REGEN_REQUESTED_KEY] = mapping


def _invalidate_scene_language_video(scene: dict, language: str) -> None:
    """Request a fresh render after phrase/prompt edits without losing approved video."""
    _set_video_regeneration_requested(scene, language, True)


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
    _clear_scene_dialogue_for_language(scene, lang)
    phrase = _scene_speech_for_language(scene, lang)
    _set_scene_speech_budget_override(scene, lang, False)
    _refresh_scene_language_speech_metadata(scene, lang, auto_shortened=origin == "auto_shorten")
    source_prompt = _strip_dialogue_prompt_metadata(_scene_video_prompt_for_language(scene, lang))
    locked = _compose_locked_video_prompt(
        scene,
        lang,
        source_prompt,
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
    preserve_approved: bool = True,
) -> tuple[list[str], list[str]]:
    """Retranslate changed phrase into other scene languages."""
    source_lang = normalize_language_code(source_language) or "ru"
    updated: list[str] = []
    failed: list[str] = []
    for lang in _scene_target_languages(session, scene):
        if lang == source_lang:
            continue
        if preserve_approved and _scene_video_approved_for_language(scene, lang):
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
                    retry_ai_rate_limit=False,
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
        "Preserve meaningful sentence punctuation from the source where natural; if there is no final . ! ? or …, add a period.\n"
        "Do not use hyphen-minus, en dash, or em dash in the spoken phrase; use words or [пауза] instead.\n"
        "If user feedback is provided, revise the translation according to that feedback.\n"
        "Return only translated phrase.\n\n"
        f"SOURCE:\n{source_text.strip()}\n"
        f"{feedback_block}"
    )
    raw = await asyncio.to_thread(
        gemini_generate_sync,
        prompt,
        rate_limit_max_attempts=1,
        model_profile=GEMINI_MODEL_PROFILE_EDIT,
    )
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
        prev_en = _scene_speech_for_language_or_prompt(scene, "en")
        if prev_en != phrase:
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
        "source_image_candidate_id": source_image_candidate_id,
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
        speed_coef=default_scene_video_speed_coef(scene),
    )
    _mark_draft_repair_generated_video(scene, language)
    _clear_draft_repair_ignore_restored_video_candidate(scene, language)
    if session is not None and scene_idx is not None:
        _persist_video_candidate_best_effort(session, scene_idx=scene_idx, candidate=candidate)
    _set_video_regeneration_requested(scene, language, False)
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


DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY = "draft_repair_ignore_restored_video_candidates_by_lang"
DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY = "draft_repair_generated_video_by_lang"


def _draft_repair_ignores_restored_video_candidate(scene: dict, language: str) -> bool:
    lang = normalize_language_code(language) or "ru"
    ignored = scene.get(DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY)
    return isinstance(ignored, dict) and bool(ignored.get(lang))


def _mark_draft_repair_generated_video(scene: dict, language: str) -> None:
    lang = normalize_language_code(language) or "ru"
    generated = scene.get(DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY)
    if not isinstance(generated, dict):
        generated = {}
    generated[lang] = True
    scene[DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY] = generated


def _draft_repair_has_generated_video(scene: dict, language: str) -> bool:
    lang = normalize_language_code(language) or "ru"
    generated = scene.get(DRAFT_REPAIR_GENERATED_VIDEO_BY_LANG_KEY)
    return isinstance(generated, dict) and bool(generated.get(lang))


def _mark_draft_repair_ignore_restored_video_candidate(scene: dict, language: str) -> None:
    lang = normalize_language_code(language) or "ru"
    ignored = scene.get(DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY)
    if not isinstance(ignored, dict):
        ignored = {}
    ignored[lang] = True
    scene[DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY] = ignored


def _clear_draft_repair_ignore_restored_video_candidate(scene: dict, language: str) -> None:
    lang = normalize_language_code(language) or "ru"
    ignored = scene.get(DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY)
    if not isinstance(ignored, dict):
        return
    ignored.pop(lang, None)
    if ignored:
        scene[DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY] = ignored
    else:
        scene.pop(DRAFT_REPAIR_IGNORE_RESTORED_VIDEO_CANDIDATES_KEY, None)


def _current_start_image_candidate_id(scene: dict) -> str:
    return str((resolve_scene_image_for_video(scene, kind="start") or {}).get("candidate_id") or "").strip()


def _video_candidate_matches_current_start(scene: dict, candidate: dict | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    current_id = _current_start_image_candidate_id(scene)
    source_id = str(candidate.get("source_image_candidate_id") or "").strip()
    if not current_id:
        return True
    if source_id:
        return source_id == current_id
    return current_id == "legacy-start"


def _video_result_matches_current_start(scene: dict, language: str, result: dict) -> bool:
    current_id = _current_start_image_candidate_id(scene)
    if not current_id:
        return True
    source_id = str(result.get("source_image_candidate_id") or "").strip()
    if source_id:
        return source_id == current_id
    candidate = latest_scene_video_candidate(scene, language)
    if candidate is not None:
        return _video_candidate_matches_current_start(scene, candidate)
    return current_id == "legacy-start"


def _all_scene_languages_have_video(scene: dict, languages: list[str]) -> bool:
    """True when each language has generated video result."""
    results = scene.get("video_results_by_lang")
    results = results if isinstance(results, dict) else {}
    for lang in languages:
        if _video_regeneration_requested(scene, lang):
            return False
        result = results.get(lang)
        if (
            isinstance(result, dict)
            and str(result.get("video_b64") or "").strip()
            and _video_result_matches_current_start(scene, lang, result)
        ):
            continue
        if _draft_repair_ignores_restored_video_candidate(scene, lang):
            return False
        candidate = latest_scene_video_candidate(scene, lang)
        if _video_candidate_matches_current_start(scene, candidate):
            continue
        return False
    return True


def _all_scene_languages_approved(scene: dict, languages: list[str]) -> bool:
    """True when each language has explicit approval."""
    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        return False
    return all(bool(approvals.get(lang)) and not _video_regeneration_requested(scene, lang) for lang in languages)


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
    if isinstance(results, dict):
        item = results.get(language)
        if isinstance(item, dict):
            payload = _decode_b64(str(item.get("video_b64") or "").strip())
            if payload and _video_result_matches_current_start(scene, language, item):
                return payload
    if _draft_repair_ignores_restored_video_candidate(scene, language):
        return None
    selected = resolve_scene_video_for_production(scene, language)
    if not _video_candidate_matches_current_start(scene, selected):
        return None
    return selected_video_bytes_for_production(scene, language)


def _scene_video_progress_signature(scene: dict, languages: list[str] | None = None) -> tuple:
    lang_list = languages or _scene_target_languages({}, scene)
    candidates = scene.get("video_candidates_by_lang")
    approvals = scene.get("video_approvals_by_lang")
    results = scene.get("video_results_by_lang")
    return (
        tuple(
            (
                lang,
                len(candidates.get(lang) or []) if isinstance(candidates, dict) else 0,
                isinstance(results, dict) and isinstance(results.get(lang), dict),
                isinstance(approvals, dict) and bool(approvals.get(lang)),
            )
            for lang in lang_list
        ),
    )


def _remove_scene_video_for_language(scene: dict, language: str) -> None:
    selected = resolve_scene_video_for_production(scene, language)
    selected_candidate_id = str(selected.get("candidate_id") or "") if isinstance(selected, dict) else ""
    results = scene.get("video_results_by_lang")
    if isinstance(results, dict):
        results.pop(language, None)
        scene["video_results_by_lang"] = results
    candidates_by_lang = scene.get("video_candidates_by_lang")
    lang = normalize_language_code(language) or "ru"
    if isinstance(candidates_by_lang, dict):
        bucket = candidates_by_lang.get(lang)
        if isinstance(bucket, list):
            candidates_by_lang[lang] = [
                item
                for item in bucket
                if not (
                    isinstance(item, dict)
                    and selected_candidate_id
                    and str(item.get("candidate_id") or "") == selected_candidate_id
                )
            ]
            scene["video_candidates_by_lang"] = candidates_by_lang
    approvals = scene.get("video_approvals_by_lang")
    if isinstance(approvals, dict):
        approvals[language] = False
        scene["video_approvals_by_lang"] = approvals


def _invalidate_scene_videos_after_frame_change(scene: dict, languages: list[str]) -> None:
    """Drop videos that were generated from a frame that is no longer current."""
    normalized_languages = [
        normalize_language_code(lang) or str(lang or "").strip().lower()
        for lang in languages
    ]
    normalized_languages = [lang for lang in dict.fromkeys(normalized_languages) if lang]
    if not normalized_languages:
        return

    results = scene.get("video_results_by_lang")
    if isinstance(results, dict):
        for lang in normalized_languages:
            results.pop(lang, None)
        scene["video_results_by_lang"] = results
    else:
        scene["video_results_by_lang"] = {}

    candidates_by_lang = scene.get("video_candidates_by_lang")
    if isinstance(candidates_by_lang, dict):
        for lang in normalized_languages:
            bucket = candidates_by_lang.get(lang)
            if isinstance(bucket, list):
                for candidate in bucket:
                    if isinstance(candidate, dict):
                        candidate["approved_for_production"] = False
                        _persist_video_state_best_effort(candidate)
            candidates_by_lang.pop(lang, None)
        scene["video_candidates_by_lang"] = candidates_by_lang

    approvals = scene.get("video_approvals_by_lang")
    if not isinstance(approvals, dict):
        approvals = {}
    for lang in normalized_languages:
        approvals[lang] = False
        _mark_draft_repair_ignore_restored_video_candidate(scene, lang)
        _set_video_regeneration_requested(scene, lang, False)
        _clear_ai_video_approval(scene, lang)
    scene["video_approvals_by_lang"] = approvals

    for key in ("video_provider_choice_pending", "syntax_fallback_pending"):
        scene.pop(key, None)
    _reset_video_prompt_policy_failure_count(scene)


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
        result = await _run_ai_video_check(
            update,
            session=session,
            scene=scene,
            idx=idx,
            language=language,
            video_bytes=video_bytes,
        )
        _store_ai_video_speech_qa(scene, language, result)
        status = result.status
        status = _normalize_ai_status_for_session(session, status, stage="video")
        if status == AI_APPROVAL_STATUS_APPROVE:
            _mark_ai_video_approved(scene, language)
            continue
        if status == AI_APPROVAL_STATUS_REGENERATE_VIDEO:
            if _ai_attempt_count(scene, "video") <= _ai_regenerate_attempt_limit(session):
                _remove_scene_video_for_language(scene, language)
                _clear_ai_video_approval(scene, language)
                session["scene_media_language_index"] = languages.index(language)
                await _safe_reply_text(update.message, f"🤖 ИИ апрув просит перегенерировать видео для языка {language}.")
                return False
            if _is_scheduled_unattended_ai_mode(session):
                await _safe_reply_text(
                    update.message,
                    f"Ночной режим: лимит перегенерации видео для {language} исчерпан, ручную проверку не запрашиваю."
                )
                return False
        _set_ai_human_review_pending(session, stage="video", reason=f"video QA requires manual review for {language}")
        await _safe_reply_text(update.message, "ИИ апрув не смог уверенно принять видео, нужна ручная проверка.")
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
    aliases = (keyword,)
    if keyword == "промпт":
        aliases = ("промпт", "промт")
    matched = next((alias for alias in aliases if lower.startswith(alias)), "")
    if not matched:
        return None
    tail = raw[len(matched) :].strip()
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
    applied_languages = _scene_video_feedback_languages(session, scene, lang)
    if applied_languages:
        lang = applied_languages[0]
    speechless = _is_speechless_video_scene(scene, session)
    updated_duration = _apply_video_duration_feedback(scene, feedback_text, speechless=speechless)
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
    if speechless:
        rewritten = _strip_speechless_video_prompt_metadata(rewritten)
    for applied_lang in applied_languages:
        _set_scene_video_prompt_for_language(
            scene,
            applied_lang,
            _compose_locked_video_prompt(scene, applied_lang, rewritten),
        )
        _invalidate_scene_language_video(scene, applied_lang)
    session["scene_media_language_index"] = languages.index(lang) if lang in languages else 0
    has_generated_videos = _all_scene_languages_have_video(scene, languages)
    preview_lang = lang if lang in applied_languages else (applied_languages[0] if applied_languages else lang)
    preview_prompt = _scene_video_prompt_for_language(scene, preview_lang)
    duration_note = f"\nДлительность обновлена: {updated_duration} сек." if updated_duration is not None else ""
    await update.message.reply_text(
        "Подготовила обновлённый video prompt по фидбэку для языков: "
        f"{', '.join(applied_languages)}. "
        + (
            "Проверьте prompt и нажмите «ОК», чтобы перегенерировать изменённые языки параллельно."
            if has_generated_videos
            else "Нажмите «ОК», чтобы сгенерировать видео."
        )
        + duration_note
        + f"\n\nТекущий prompt ({preview_lang}):\n{_prompt_preview(preview_prompt)}"
    )
    if not has_generated_videos:
        await _send_next_scene_media_step(update, session)
    return True


async def _continue_scene_from_previous_video(
    update: Update,
    *,
    session: dict,
    scene: dict,
    idx: int,
) -> bool:
    """Seed this Kling scene with the exact final frame of the approved prior clip."""
    scenes, _ = _scene_media_context(session)
    if not scenes:
        return False
    source_info = continuation_source_for_scene(session, scenes, idx)
    if source_info is None:
        await _safe_reply_text(
            update.message,
            "Для продолжения нужен утверждённый ролик предыдущей сцены с доступным файлом.",
        )
        await _send_next_scene_media_step(update, session)
        return True
    _language, source_candidate = source_info
    source_bytes = video_candidate_bytes(source_candidate)
    if not source_bytes:
        await _safe_reply_text(
            update.message,
            "Не удалось прочитать утверждённый ролик предыдущей сцены. Можно сгенерировать стартовый кадр обычным способом.",
        )
        await _send_next_scene_media_step(update, session)
        return True
    await _safe_reply_text(update.message, "Беру последний кадр утверждённого предыдущего видео.")
    try:
        frame = await asyncio.to_thread(extract_last_video_frame, video_bytes=source_bytes)
    except Exception:
        logger.exception("scene_media.continuation_last_frame_failed scene_idx=%s", idx)
        await _safe_reply_text(
            update.message,
            "Не удалось извлечь последний кадр. Можно сгенерировать стартовый кадр обычным способом.",
        )
        await _send_next_scene_media_step(update, session)
        return True

    _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
    _set_end_frame_requested(scene, False)
    scene.pop("end_frame_image_b64", None)
    scene["start_frame_image_b64"] = _encode_b64(frame)
    image_candidate = register_image_candidate(scene, kind="start", image_bytes=frame)
    image_candidate["liked"] = True
    approved = mark_image_candidate_approved(scene, str(image_candidate.get("candidate_id") or ""))
    _persist_image_candidate_best_effort(session, scene_idx=idx, candidate=approved)
    _persist_image_state_best_effort(approved)
    scene["continuation_source_video_candidate_id"] = str(source_candidate.get("candidate_id") or "")
    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
    session["scene_media_language_index"] = 0
    await _safe_reply_text(
        update.message,
        "Стартовый кадр взят из предыдущего видео. Переходим к апруву генерации продолжения.",
    )
    await _send_next_scene_media_step(update, session)
    return True


async def _handle_scene_media_common_restarts(update: Update, session: dict, scene: dict, text: str) -> bool:
    has_prompt_override = _extract_prompt_override(text) is not None
    has_format_or_feedback = _extract_format_or_feedback(text) is not None
    if has_prompt_override or has_format_or_feedback:
        return False
    low = str(text or "").strip().lower()
    if low == SCENE_MEDIA_CONTINUE_PREVIOUS_VIDEO_BUTTON.lower():
        _scenes, idx = _scene_media_context(session)
        return await _continue_scene_from_previous_video(
            update,
            session=session,
            scene=scene,
            idx=idx,
        )
    if low == NAV_BUTTON_PREVIOUS_STEP.lower():
        current_substep = str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
        if current_substep == SCENE_MEDIA_SUBSTEP_START:
            await _reply_text_with_optional_markup(
                update,
                "Это первый медиа-шаг текущей сцены. Для выхода используйте «В начало».",
                reply_markup=scene_image_approval_markup(session=session),
            )
            return True
        if current_substep == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
            target_substep = SCENE_MEDIA_SUBSTEP_START
        elif current_substep == SCENE_MEDIA_SUBSTEP_END:
            target_substep = (
                SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
                if scene.get("start_frame_image_b64")
                else SCENE_MEDIA_SUBSTEP_START
            )
        elif current_substep == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
            target_substep = SCENE_MEDIA_SUBSTEP_END
        elif current_substep == SCENE_MEDIA_SUBSTEP_VIDEO:
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
        else:
            target_substep = SCENE_MEDIA_SUBSTEP_START
        session["scene_media_substep"] = target_substep
        session["scene_media_language_index"] = 0
        await update.message.reply_text("Назад: возвращаюсь к предыдущему медиа-шагу.")
        await _send_next_scene_media_step(update, session)
        return True
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
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await update.message.reply_text("Ок, добавляю конечный кадр для этой сцены.")
        await _send_next_scene_media_step(update, session)
        return True
    if low == SCENE_MEDIA_SKIP_END_FRAME_BUTTON.lower():
        _set_end_frame_requested(scene, False)
        scene.pop("end_frame_image_b64", None)
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
        session["scene_media_language_index"] = 0
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        await update.message.reply_text("Ок, конечный кадр для этой сцены не используем.")
        await _send_next_scene_media_step(update, session)
        return True
    if _wants_restart_start_frame(text):
        scene.pop("start_frame_image_b64", None)
        scene.pop("end_frame_image_b64", None)
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
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
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
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


def _invalidate_scene_video_prompts_after_start_prompt_change(scene: dict, languages: list[str]) -> None:
    """Drop video prompts/results that no longer match a changed start-frame prompt."""
    _invalidate_scene_videos_after_frame_change(scene, languages)
    for key in (
        "video_prompt",
        "video_prompt_by_lang",
        "video_prompt_current_language",
        "syntax_prompt_by_lang",
        VIDEO_PROMPT_RAW_BY_LANG_KEY,
        SYNTAX_PROMPT_BY_LANG_KEY,
    ):
        scene.pop(key, None)
    scene["video_prompt_stale_reason"] = "start_frame_prompt_changed"


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
            await _safe_reply_text(update.message, 'После "ПРОМПТ:" пришлите новый текст промпта.')
            return True
        _apply_shot_type_hint_from_text(scene, manual_prompt)
        scene["start_frame_prompt"] = manual_prompt
        if "start_frame_prompt_ru" in scene:
            scene["start_frame_prompt_ru"] = manual_prompt
        _invalidate_scene_video_prompts_after_start_prompt_change(scene, _scene_target_languages(session, scene))
        scene[START_PROMPT_RAW_MODE_KEY] = True
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _safe_reply_text(update.message, "Обновила стартовый промпт вручную. Сразу генерирую стартовый кадр.")
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
        await _safe_reply_text(update.message, 'После "ФИДБЕК:" пришлите комментарий, что изменить.')
        return True
    _apply_shot_type_hint_from_text(scene, feedback)
    feedback = _append_scheduled_every_start_prompt_comment(session, feedback)
    await _safe_reply_text(update.message, "Перегенерирую стартовый промпт (нейросеть).")
    try:
        new_prompt = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "regenerate_image_prompt",
            lambda: _regenerate_image_prompt_from_feedback(scene, feedback),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await _safe_reply_text(update.message, str(e))
        return True
    except Exception as e:
        logger.exception("image prompt regeneration failed")
        await _safe_reply_text(update.message, f"Ошибка перегенерации: {_user_error_message(e)}")
        return True
    new_prompt = _enforce_start_prompt_shot_type(scene, new_prompt)
    scene["start_frame_prompt"] = new_prompt
    if "start_frame_prompt_ru" in scene:
        scene["start_frame_prompt_ru"] = new_prompt
    _invalidate_scene_video_prompts_after_start_prompt_change(scene, _scene_target_languages(session, scene))
    scene[START_PROMPT_RAW_MODE_KEY] = False
    await _safe_reply_text(update.message, "Готово. Проверь обновлённый стартовый промпт.")
    await _send_next_scene_media_step(update, session)
    return True


async def _approve_planned_scene_start(
    update: Update, *, session: dict, scene: dict, idx: int,
    **context_args,
) -> bool:
    from ..discipline_modes import FLOW, MODE_KEY

    if idx > 0 and scene.get(MODE_KEY) == FLOW:
        return await _continue_scene_from_previous_video(update, session=session, scene=scene, idx=idx)
    return await _scene_media_start_ok(update, session=session, scene=scene, idx=idx, **context_args)


async def _scene_media_start_ok(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    idx: int,
) -> bool:
    # A normal generation is always prompt-driven.  The current image may be
    # sent back to the model only by the explicit «ИЗМЕНИТЬ» image-edit flow.
    scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
    auto_approve = _is_scene_media_auto_approve_enabled(session) or _is_scheduled_unattended_ai_mode(session)
    try:
        base_prompt = _image_scene_prompt(scene)
    except Exception as e:
        await _safe_reply_text(update.message, f"Ошибка подготовки промпта: {e}")
        return True
    scene_reference_images = _load_scene_reference_images(
        scene,
        include_location=False,
        prompt_keys=("start_frame_prompt", "start_frame_prompt_ru"),
    )
    user_reference_images = _scene_user_reference_images(scene, "start")
    advertising_reference_images = advertising_product_reference_bytes(session)
    scene_refs = _dedupe_image_reference_bytes(
        [
            *scene_reference_images,
            *user_reference_images,
            *advertising_reference_images,
        ]
    )
    raw_mode = bool(scene.get(START_PROMPT_RAW_MODE_KEY))
    silence_mode = str(session.get("generation_mode") or "").strip().lower() == "silence"
    if raw_mode:
        prompt = str(base_prompt or "").strip()
    else:
        story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
        # In Story mode references lock character identity only.  Its historic
        # watercolor style comes from the scene prompt, because character
        # sheets may intentionally come from different source styles.
        styled_base_prompt = (
            str(base_prompt or "").strip()
            if story_scene
            else _append_reference_style_to_image_prompt(base_prompt, bool(scene_refs))
        )
        prompt = _append_scene_identity_to_image_prompt(
            styled_base_prompt,
            scene,
            include_location=False,
            allow_grandma_face_crop_rule=not silence_mode,
        )
    prompt = append_dish_visual_brief_to_prompt(prompt, _dish_visual_brief_for_scene(session, scene, idx))
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
    await _safe_reply_text(update.message, f"{prefix}{_prompt_preview(prompt)}")
    refs_cfg = scene.get("references")
    if isinstance(refs_cfg, dict) and refs_cfg and not scene_refs:
        await _safe_reply_text(
            update.message,
            "Внимание: референсы для сцены указаны, но ни один файл не загрузился. Проверь пути в `references`.",
        )
    if _is_scene_media_ai_approve_enabled(session):
        prompt_status = await _run_ai_prompt_check(
            update,
            session=session,
            scene=scene,
            idx=idx,
            start_frame_prompt=prompt,
        )
        prompt_status = _normalize_ai_status_for_session(session, prompt_status, stage="prompt")
        if prompt_status == AI_APPROVAL_STATUS_APPROVE:
            await _safe_reply_text(update.message, "🤖 ИИ апрув принял prompt. Перехожу к генерации стартового кадра.")
        elif prompt_status in {AI_APPROVAL_STATUS_FIX_PROMPT, AI_APPROVAL_STATUS_REGENERATE_IMAGE}:
            if _is_scheduled_unattended_ai_mode(session):
                rewritten = await _regenerate_scene_prompts_from_prompt_ai_feedback(
                    update,
                    context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                )
                if not rewritten:
                    _set_ai_human_review_pending(session, stage="prompt", reason="scheduled prompt QA rewrite failed")
                    await _safe_reply_text(
                        update.message,
                        "🤖 Ночной режим: prompt не принят, а автоматическая правка не удалась. "
                        "Останавливаю сцену для повторного запуска/проверки.",
                    )
                return True
            elif _ai_attempt_count(scene, "prompt") <= _ai_regenerate_attempt_limit(session):
                scene.pop("start_frame_image_b64", None)
                scene.pop("end_frame_image_b64", None)
                _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                await _safe_reply_text(
                    update.message,
                    "🤖 ИИ апрув не принял prompt и просит перегенерацию prompt/image. "
                    "Останавливаю этот шаг для безопасной правки.",
                )
                _set_ai_human_review_pending(session, stage="prompt", reason="prompt QA requires prompt regeneration")
                return True
        else:
            if _is_scheduled_unattended_ai_mode(session):
                await _safe_reply_text(
                    update.message,
                    "🤖 Ночной режим: prompt получил неопределенный AI status, HUMAN_REVIEW отключен. Продолжаю.",
                )
            else:
                _set_ai_human_review_pending(session, stage="prompt", reason="prompt QA requires manual review")
                await _safe_reply_text(update.message, "ИИ апрув не смог уверенно принять prompt, нужна проверка.")
                return True
    await _safe_reply_text(
        update.message,
        _scene_generation_phrase_notice_text(scene, idx=idx, target="start"),
    )
    if not auto_approve:
        await reply_text_without_keyboard(
            update.message,
            "Генерируем стартовый кадр. Сильная модель может отвечать до 8 минут; повторно нажимать не нужно.",
        )
    await _send_image_reference_debug(
        update,
        f"Сцена {idx + 1} start image",
        scene_refs,
        source_counts={
            "scene": len(scene_reference_images),
            "user": len(user_reference_images),
            "advertising_product": len(advertising_reference_images),
        },
    )
    image_model = _scene_image_model_for_generation(scene, "start") or primary_image_generation_model()
    try:
        image_kwargs = {
            "reference_images": scene_refs if scene_refs else None,
            "model": image_model,
        }
        start_aspect_ratio = _scene_aspect_ratio(scene, session)
        if start_aspect_ratio != "9:16":
            image_kwargs["aspect_ratio"] = start_aspect_ratio
        image_result = await _generate_scene_image_result(prompt, **image_kwargs)
        image_bytes = finish_story_historical_frame(image_result.data, scene)
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        _store_scene_image_model(scene, "start", image_result.model)
        _clear_scene_image_model_choice_pending(scene)
        add_scene_shared_cost(scene, estimate_image_generation_cost_usd(image_result.model, getattr(image_result, "usage", None)))
    except Exception as e:
        logger.exception("vertex image generation failed")
        await _ask_scene_image_model_choice(update, session=session, scene=scene, target="start", failed_model=image_model, error=e)
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
    scene.pop("end_frame_image_b64", None)
    _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
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
        image_status = _normalize_ai_status_for_session(session, image_status, stage="image")
        if image_status == AI_APPROVAL_STATUS_APPROVE:
            session["scene_media_substep"] = (
                SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
            )
            await _review_titles_after_start_frame_approval(update, session, scene)
            return True
        if image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
            if _ai_attempt_count(scene, "image") <= _ai_regenerate_attempt_limit(session):
                scene.pop("start_frame_image_b64", None)
                await _regenerate_start_prompt_from_ai_feedback(
                    update,
                    context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                )
                retry_model_message = _prepare_ai_image_retry_model_message(scene, "start")
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                await _safe_reply_text(
                    update.message,
                    f"🤖 ИИ апрув просит перегенерацию стартового фрейма с исправленным prompt. "
                    f"Текущий кадр не передаю как reference.{retry_model_message}"
                )
                return True
            if _is_scheduled_unattended_ai_mode(session):
                await _safe_reply_text(update.message, "Ночной режим: лимит перегенерации стартового фрейма исчерпан, ручную проверку не запрашиваю.")
                return True
        _set_ai_human_review_pending(session, stage="image", reason="image QA requires manual review")
        await _safe_reply_text(update.message, "ИИ апрув не смог уверенно принять стартовый фрейм, нужна ручная проверка.")
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
        "«ФИДБЕК» — перепишу промпт и создам новый кадр без предыдущей картинки.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — создам новый кадр с тем же промптом и моделью, без предыдущей картинки.",
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
    low = str(text or "").strip().lower()
    if low in {"перегенерировать", "regen", "regenerate"}:
        return await _scene_media_end_ok(
            update,
            session=session,
            scene=scene,
            idx=int(session.get("image_scene_index", 0) or 0),
        )
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
    edit_refs = [current_bytes]
    await update.message.reply_text("Изменяю текущий кадр по вашему описанию.")
    await _send_image_reference_debug(
        update,
        f"Сцена {idx + 1} {'end' if is_end else 'start'} image edit",
        edit_refs,
        source_counts={"current_image": 1},
    )
    try:
        edited_bytes = await _generate_image_with_lenient_fallback(
            edit_prompt,
            reference_images=edit_refs,
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
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
    else:
        scene.pop("end_frame_image_b64", None)
        _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
    session["scene_media_language_index"] = 0
    await _reply_text_with_optional_markup(
        update,
        "Проверьте изменённый кадр.\n"
        "«ОК» — перейти дальше.\n"
        "«ИЗМЕНИТЬ» — снова поправить текущее изображение.\n"
        "«ФИДБЕК» — переписать prompt и создать новый кадр без предыдущей картинки.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — создать новый кадр с тем же prompt без предыдущей картинки.",
        reply_markup=scene_generated_image_approval_markup(
            include_model_upgrade=_can_escalate_scene_image_model(scene, target),
            session=session,
        ),
    )
    return True


def _feedback_requests_upright_portrait(feedback: str) -> bool:
    low = str(feedback or "").strip().lower()
    return any(
        marker in low
        for marker in (
            "вертик",
            "не горизонт",
            "горизонтальн",
            "портретн",
            "portrait",
            "upright",
            "sideways",
            "повернут",
            "поворот",
            "набок",
            "на бок",
            "ориентац",
        )
    )


async def _scene_media_end_ok(update: Update, *, session: dict, scene: dict, idx: int) -> bool:
    # Discard any stale previous-version reference.  End-frame generation may
    # still use the approved start frame for scene continuity, but never the
    # previous end-frame candidate unless the user chose «ИЗМЕНИТЬ».
    scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
    auto_approve = _is_scene_media_auto_approve_enabled(session) or _is_scheduled_unattended_ai_mode(session)
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
    production = session.get("production") if isinstance(session, dict) else None
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    first_scene = scenes[0] if isinstance(scenes, list) and scenes and isinstance(scenes[0], dict) else None
    first_scene_refs = (
        _load_scene_reference_images(
            first_scene,
            include_location=False,
            prompt_keys=("end_frame_prompt", "end_frame_prompt_ru", "video_prompt"),
        )
        if isinstance(first_scene, dict)
        else []
    )
    current_scene_refs = _load_scene_reference_images(
        scene,
        include_location=False,
        prompt_keys=("end_frame_prompt", "end_frame_prompt_ru", "video_prompt"),
    )
    user_end_refs = _scene_user_reference_images(scene, "end")
    advertising_end_refs = advertising_product_reference_bytes(session)
    end_references = _dedupe_image_reference_bytes(
        [
            start_bytes,
            *first_scene_refs,
            *current_scene_refs,
            *user_end_refs,
            *advertising_end_refs,
        ]
    )
    silence_mode = str(session.get("generation_mode") or "").strip().lower() == "silence"
    end_full = str(end_prompt or "").strip() if raw_end_mode else _append_scene_identity_to_image_prompt(
        _append_reference_style_to_image_prompt(end_prompt, True),
        scene,
        include_location=False,
        allow_grandma_face_crop_rule=not silence_mode,
    )
    await _safe_reply_text(
        update.message,
        _scene_generation_phrase_notice_text(scene, idx=idx, target="end"),
    )
    if not auto_approve:
        await reply_text_without_keyboard(
            update.message,
            "Генерируем конечный кадр (референс — стартовый). "
            "Сильная модель может отвечать до 8 минут; повторно нажимать не нужно.",
        )
    await _send_image_reference_debug(
        update,
        f"Сцена {idx + 1} end image",
        end_references,
        source_counts={
            "start_frame": 1,
            "first_scene": len(first_scene_refs),
            "scene": len(current_scene_refs),
            "user": len(user_end_refs),
            "advertising_product": len(advertising_end_refs),
        },
    )
    image_model = _scene_image_model_for_generation(scene, "end") or primary_image_generation_model()
    try:
        end_image_kwargs = {
            "reference_images": end_references,
            "model": image_model,
        }
        end_aspect_ratio = _scene_aspect_ratio(scene, session)
        if end_aspect_ratio != "9:16":
            end_image_kwargs["aspect_ratio"] = end_aspect_ratio
        end_result = await _generate_scene_image_result(
            end_full if raw_end_mode else f"{end_full}\n\n{_end_frame_delta_instruction(scene)}",
            **end_image_kwargs,
        )
        end_bytes = finish_story_historical_frame(end_result.data, scene)
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        _store_scene_image_model(scene, "end", end_result.model)
        _clear_scene_image_model_choice_pending(scene)
        add_scene_shared_cost(scene, estimate_image_generation_cost_usd(end_result.model, getattr(end_result, "usage", None)))
    except Exception as e:
        logger.exception("vertex end-frame generation failed")
        await _ask_scene_image_model_choice(update, session=session, scene=scene, target="end", failed_model=image_model, error=e)
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
    _invalidate_scene_videos_after_frame_change(scene, _scene_target_languages(session, scene))
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
        image_status = _normalize_ai_status_for_session(session, image_status, stage="end_image")
        if image_status == AI_APPROVAL_STATUS_APPROVE:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
            return True
        if image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
            if _ai_attempt_count(scene, "end_image") <= _ai_regenerate_attempt_limit(session):
                scene.pop("end_frame_image_b64", None)
                retry_model_message = _prepare_ai_image_retry_model_message(scene, "end")
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
                await _safe_reply_text(
                    update.message,
                    f"🤖 ИИ апрув просит полную перегенерацию конечного фрейма. "
                    f"Текущий конечный кадр не передаю как reference.{retry_model_message}"
                )
                return True
            if _is_scheduled_unattended_ai_mode(session):
                await _safe_reply_text(update.message, "Ночной режим: лимит перегенерации конечного фрейма исчерпан, ручную проверку не запрашиваю.")
                return True
        _set_ai_human_review_pending(session, stage="end_image", reason="end image QA requires manual review")
        await _safe_reply_text(update.message, "ИИ апрув не смог уверенно принять конечный фрейм, нужна ручная проверка.")
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
        "«ФИДБЕК» — перепишу промпт и создам новый кадр без предыдущей картинки.\n"
        "«ПЕРЕГЕНЕРИРОВАТЬ» — создам новый кадр с тем же промптом и моделью, без предыдущей картинки.",
        reply_markup=scene_generated_image_approval_markup(
            include_model_upgrade=_can_escalate_scene_image_model(scene, "end"),
            session=session,
        ),
    )
    return True


async def _review_titles_after_start_frame_approval(update: Update, session: dict, scene: dict) -> bool:
    selected = resolve_scene_image_for_video(scene, kind="start")
    if selected is not None and not selected.get("approved_for_video"):
        approved = mark_image_candidate_approved(scene, str(selected.get("candidate_id") or ""))
        _persist_image_state_best_effort(approved)
    from .handlers_state_scene import _maybe_ask_title_on_first_frame
    return await _maybe_ask_title_on_first_frame(update, session)


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
        _invalidate_scene_video_prompts_after_start_prompt_change(scene, _scene_target_languages(session, scene))
        scene[START_PROMPT_RAW_MODE_KEY] = True
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await update.message.reply_text("Обновила стартовый промпт. Сразу перегенерирую стартовый кадр.")
        return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
    if low in {"ок", "ok", "okay", "approve"}:
        selected = resolve_scene_image_for_video(scene, kind="start")
        if selected is not None:
            approved = mark_image_candidate_approved(scene, str(selected.get("candidate_id") or ""))
            _persist_image_state_best_effort(approved)
        next_substep = (
            SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
        )
        session["scene_media_substep"] = next_substep
        if next_substep == SCENE_MEDIA_SUBSTEP_VIDEO:
            _promote_draft_repair_frame_stage_to_video(session, scene, idx)
        if await _review_titles_after_start_frame_approval(update, session, scene):
            return True
        await _send_next_scene_media_step(update, session)
        return True
    if low in {"перегенерировать", "regen", "regenerate"}:
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        return await _scene_media_start_ok(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
    if low in {SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON.lower(), "upgrade", "upgrade model", "повысить качество"}:
        upgraded_model = _escalate_scene_image_model(scene, "start")
        if not upgraded_model:
            await update.message.reply_text("Модель уже максимальная для текущего профиля. Могу перегенерировать на ней же.")
            return True
        await update.message.reply_text(f"Повышаю модель до `{upgraded_model}` и сразу перегенерирую стартовый кадр.")
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
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
        _set_scene_image_edit_pending(session, scene_idx=idx, target="start", mode="prompt_feedback")
        await _ask_for_feedback_text(update, "изображении")
        return True
    feedback = _extract_format_or_feedback(text)
    if feedback is None:
        feedback = str(text or "").strip()
    if not feedback:
        await update.message.reply_text("Пришлите комментарий, что изменить в изображении.")
        return True
    feedback = _append_scheduled_every_start_prompt_comment(session, feedback)
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
    _invalidate_scene_video_prompts_after_start_prompt_change(scene, _scene_target_languages(session, scene))
    # Prompt feedback always produces a fresh image from the rewritten prompt
    # and stable project/character references.  The previous generated frame
    # belongs only to the explicit «ИЗМЕНИТЬ» image-edit flow.
    scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
    if _feedback_requests_upright_portrait(feedback):
        scene["aspect_ratio"] = "9:16"
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
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        await update.message.reply_text("Обновила промпт конечного кадра. Сразу перегенерирую конечный кадр.")
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    if low in {"ок", "ok", "okay", "approve"}:
        selected = resolve_scene_image_for_video(scene, kind="end")
        if selected is not None:
            approved = mark_image_candidate_approved(scene, str(selected.get("candidate_id") or ""))
            _persist_image_state_best_effort(approved)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        _promote_draft_repair_frame_stage_to_video(session, scene, idx)
        await _send_next_scene_media_step(update, session)
        return True
    if low in {"перегенерировать", "regen", "regenerate"}:
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END
        return await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
    if low in {SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON.lower(), "upgrade", "upgrade model", "повысить качество"}:
        upgraded_model = _escalate_scene_image_model(scene, "end")
        if not upgraded_model:
            await update.message.reply_text("Модель уже максимальная для текущего профиля. Могу перегенерировать на ней же.")
            return True
        await update.message.reply_text(f"Повышаю модель до `{upgraded_model}` и сразу перегенерирую конечный кадр.")
        scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
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
        _set_scene_image_edit_pending(session, scene_idx=idx, target="end", mode="prompt_feedback")
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
    # As with the start frame, feedback rewrites the prompt and must not reuse
    # the previous generated end frame as an image reference.
    scene.pop(SCENE_IMAGE_REGEN_REFERENCE_KEY, None)
    if _feedback_requests_upright_portrait(feedback):
        scene["aspect_ratio"] = "9:16"
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
    low_text = raw_text.lower()
    if low_text == NAV_BUTTON_PREVIOUS_STEP.lower():
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
        session["scene_media_language_index"] = 0
        await update.message.reply_text("Назад: возвращаюсь к предыдущему медиа-шагу.")
        await _send_next_scene_media_step(update, session)
        return True
    if low_text in {"нет", "no", "n"}:
        _set_scene_video_feedback_pending(
            session,
            scene_idx=int(session.get("image_scene_index", 0) or 0),
            language=language,
        )
        await _reply_text_with_optional_markup(
            update,
            "Ок, видео не запускаю. Напишите, что исправить в video prompt (можно `ФИДБЕК: ...`).",
            reply_markup=scene_video_approval_markup(
                include_restart_end=_scene_needs_end_frame_step(scene),
                languages=languages,
                session=session,
            ),
        )
        return True
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
            manual_prompt = _append_manual_prompt_voice_locks(payload, scene, lang)
            _set_scene_video_prompt_for_language(
                scene,
                lang,
                manual_prompt,
            )
            _set_scene_video_prompt_raw(scene, lang, True)
            _invalidate_scene_language_video(scene, lang)
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
        await _safe_reply_text(
            update.message,
            f"Обновила video prompt вручную для {language}. Сразу генерирую видео.",
        )
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
                session=session,
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
    if _scene_speech_budget_override_enabled(scene, "ru"):
        return False
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
    auto_approve = _is_scene_media_auto_approve_enabled(session) or _is_scheduled_unattended_ai_mode(session)
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
            reply_markup=_scene_speech_user_long_phrase_markup(session=session),
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
                max_llm_attempts=1,
                rate_limit_max_attempts=1,
            ),
            retry_ai_rate_limit=False,
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        return True
    except Exception as e:
        logger.exception("scene speech auto-shorten failed")
        await update.message.reply_text(f"Не удалось автоматически сократить реплику: {_user_error_message(e)}")
        return True
    if shortened is None:
        if auto_approve:
            fallback_text = _fallback_scheduled_ru_speech_for_budget(speech_text, max_chars)
            prompt = _sync_scene_text_into_video_prompt(
                _compose_locked_video_prompt(scene, "ru", _scene_video_prompt_for_language(scene, "ru")),
                fallback_text,
            )
            _set_scene_video_prompt_for_language(scene, "ru", prompt)
            _apply_scene_phrase_update_to_scene(
                session,
                scene,
                scene_idx=scene_idx,
                language="ru",
                phrase_text=fallback_text,
                origin="auto_shorten",
            )
            await _sync_scene_phrase_translations(
                update=update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                scene_idx=scene_idx,
                source_language="ru",
                source_text=fallback_text,
            )
            spoken_after = speech_chars_excluding_pauses(str(scene.get("text", "")).strip())
            _refresh_scene_language_speech_metadata(scene, "ru", status="auto_shortened", auto_shortened=True)
            _cancel_scene_speech_timeout(chat_id)
            session.pop(SCENE_SPEECH_PENDING_KEY, None)
            await update.message.reply_text(
                f"Ночной режим: авто-сокращение не вернуло валидный ответ, поставила короткую реплику ({spoken_len} → {spoken_after}) и продолжаю."
            )
            return False
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
            reply_markup=_scene_speech_options_markup(session=session),
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
            reply_markup=_scene_speech_options_markup(session=session),
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
        reply_markup=_scene_speech_approval_markup(session=session),
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


async def _request_english_speech_approval(update: Update, session: dict, scene: dict, duration_sec: int) -> bool:
    _set_scene_speech_pending(
        session,
        scene_idx=int(session.get("image_scene_index", 0) or 0),
        duration_sec=duration_sec,
        stage="approval",
    )
    session[SCENE_SPEECH_PENDING_KEY]["language"] = "en"
    await _reply_text_with_optional_markup(
        update,
        f"Проверьте EN-реплику:\n{_scene_speech_for_language(scene, 'en')}\n\n"
        "Подтвердите запуск видео или выберите «Изменить фразу».",
        reply_markup=_scene_speech_approval_markup(session=session),
    )
    return True


async def _maybe_adjust_english_speech_for_video_budget(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    duration_sec: int,
) -> bool:
    """Shorten English semantically; preserve the original if no valid result exists."""
    if _scene_speech_budget_override_enabled(scene, "en"):
        return False
    text_en = _scene_speech_for_language(scene, "en")
    if not text_en:
        return False
    max_chars = max_speech_chars_for_duration(duration_sec, "en")
    current_chars = speech_chars_excluding_pauses(text_en)
    _refresh_scene_language_speech_metadata(scene, "en")
    if current_chars <= max_chars:
        return False
    try:
        shortened = await _call_with_503_retries(
            update, context, chat_id,
            "shorten_english_scene_speech_for_video_budget",
            lambda: asyncio.to_thread(
                shorten_scene_speech_and_sync_video_prompt_sync,
                scene, duration_sec, language="en", current_text=text_en,
                current_video_prompt=_scene_video_prompt_for_language(scene, "en"),
                max_llm_attempts=2, rate_limit_max_attempts=1,
            ),
            retry_ai_rate_limit=False,
        )
    except Exception:
        logger.exception("english scene speech shortening failed; preserving original")
        shortened = None
    if shortened is not None:
        _apply_scene_phrase_update_to_scene(
            session, scene, scene_idx=int(session.get("image_scene_index", 0) or 0),
            language="en", phrase_text=shortened["text"], origin="auto_shorten",
        )
        _set_scene_video_prompt_for_language(
            scene, "en", _compose_locked_video_prompt(scene, "en", shortened["video_prompt"]),
        )
        _refresh_scene_language_speech_metadata(scene, "en", status="auto_shortened", auto_shortened=True)
        return await _request_english_speech_approval(update, session, scene, duration_sec)
    await _reply_text_with_optional_markup(
        update,
        f"EN-реплика длиннее лимита: {current_chars} символов при лимите {max_chars}. "
        "Фраза сохранена целиком, без сокращений.",
    )
    return await _request_english_speech_approval(update, session, scene, duration_sec)


async def _scene_media_video_ok(
    update: Update,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    session: dict,
    scene: dict,
    idx: int,
    total: int,
    regenerate_language: str | None = None,
) -> bool:
    languages = _scene_target_languages(session, scene)
    scoped_regenerate_language = normalize_language_code(str(regenerate_language or ""))
    if scoped_regenerate_language not in languages:
        scoped_regenerate_language = ""
    selected_start_image = resolve_scene_image_for_video(scene, kind="start")
    start_bytes = selected_image_bytes_for_video(scene, kind="start")
    if not start_bytes:
        await update.message.reply_text("Нет стартового кадра для видео. Сначала утверди и сгенерируй стартовый кадр.")
        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
        await _send_next_scene_media_step(update, session)
        return True
    end_bytes = selected_image_bytes_for_video(scene, kind="end")

    model, syntax_provider, video_audio_enabled = _scene_video_runtime_policy(scene, session)
    aspect_ratio = _scene_aspect_ratio(scene, session)
    scene["aspect_ratio"] = aspect_ratio
    video_quality = "720" if str(session.get("generation_mode") or "").strip().lower() == "silence" else "1080"
    try:
        duration_sec = int(scene.get("duration_sec", 6))
    except Exception:
        duration_sec = 6
    speechless_video = _is_speechless_video_scene(scene, session)
    if syntax_provider == SYNTAX_PROVIDER_VEO:
        duration_sec = 8
        scene["duration_sec"] = duration_sec
    if speechless_video and syntax_provider != SYNTAX_PROVIDER_VEO:
        duration_sec = max(3, min(4, duration_sec))
        scene["duration_sec"] = duration_sec
    if not speechless_video and "ru" in languages and not any(_scene_video_prompt_raw(scene, lang) for lang in languages) and await _maybe_shorten_scene_speech_for_video_budget(
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
    if syntax_provider == SYNTAX_PROVIDER_VEO:
        duration_sec = 8
        scene["duration_sec"] = duration_sec
    if speechless_video and syntax_provider != SYNTAX_PROVIDER_VEO:
        duration_sec = max(3, min(4, duration_sec))
        scene["duration_sec"] = duration_sec
    if not speechless_video and "en" in languages and not any(_scene_video_prompt_raw(scene, lang) for lang in languages):
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
    if scoped_regenerate_language:
        pending_languages = [scoped_regenerate_language]
    else:
        requested_languages = [lang for lang in languages if _video_regeneration_requested(scene, lang)]
        missing_languages = [lang for lang in languages if not isinstance(results_map.get(lang), dict)]
        pending_languages = list(dict.fromkeys([*requested_languages, *missing_languages]))
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
        prompt_local = _ensure_advertising_product_video_lock(scene, prompt_local)
        prompt_local = _force_video_prompt_aspect_ratio(prompt_local, aspect_ratio)
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
            f"Длительность запуска: {duration_sec} сек.\n"
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
                quality=video_quality,
                aspect_ratio=aspect_ratio,
                audio_enabled=video_audio_enabled,
                skip_initial_syntax=skip_initial_syntax,
                disable_syntax_fallback=disable_syntax_fallback,
                syntax_timeout_sec=_scheduled_syntax_timeout_sec(session),
                syntax_acquire_timeout_sec=_scheduled_syntax_lock_timeout_sec(session),
                syntax_provider=syntax_provider,
            )
            return language, video, None
        except VideoProviderChoiceRequired as e:
            return language, None, e
        except Exception as e:
            return language, None, e

    generation_message = (
        "Генерируем видео параллельно для языков: " + ", ".join(pending_languages)
        if len(pending_languages) > 1
        else "Генерирую видео для языка: " + pending_languages[0]
    )
    generation_message += (
        f"\nДлительность: {duration_sec} сек. Модель: {model}. Качество: {video_quality}."
    )
    for language in pending_languages:
        if prompts_by_language.get(language, ""):
            await _safe_reply_text(
                update.message,
                _scene_generation_phrase_notice_text(
                    scene,
                    idx=idx,
                    target="video",
                    language=language,
                ),
            )
    await reply_text_without_keyboard(update.message, generation_message)
    syntax_batch_results: dict[str, bytes] = {}
    if len(pending_languages) > 1:
        batch_requests = [
            BatchFallbackRequest(
                key=language,
                prompt=_scene_syntax_prompt_for_language(scene, language, prompts_by_language.get(language, "")),
                start_image=start_bytes,
                end_image=end_bytes,
            )
            for language in pending_languages
            if prompts_by_language.get(language, "")
        ]
        if batch_requests:
            await reply_text_without_keyboard(
                update.message,
                "Сначала отправляю все языки в Syntax одним обработчиком: "
                + ", ".join(req.key for req in batch_requests)
                + f"\nДлительность каждого видео: {duration_sec} сек.",
            )
            scheduled_syntax_mode = _is_scheduled_unattended_ai_mode(session)
            max_syntax_attempts = _scheduled_syntax_video_retry_count(session) if scheduled_syntax_mode else 1
            syntax_attempt = 0
            pending_batch_requests = list(batch_requests)
            syntax_batch_errors: list[str] = []
            while pending_batch_requests:
                syntax_attempt += 1
                try:
                    batch_result = await _attempt_scheduled_syntax_batch(
                        requests=pending_batch_requests,
                        model=model,
                        duration_sec=duration_sec,
                        session=session,
                        syntax_provider=syntax_provider,
                        aspect_ratio=aspect_ratio,
                        audio_enabled=video_audio_enabled,
                    )
                except Exception as e:
                    if not scheduled_syntax_mode:
                        raise
                    batch_result = {}
                    syntax_batch_errors.append(_user_error_message(e, domain="video"))
                syntax_batch_results.update(batch_result)
                expected_keys_now = {str(req.key) for req in batch_requests}
                missing_keys_now = sorted(expected_keys_now - set(syntax_batch_results))
                if not scheduled_syntax_mode or not missing_keys_now or syntax_attempt >= max_syntax_attempts:
                    break
                delay_sec = _scheduled_syntax_video_retry_delay_sec(session, syntax_attempt)
                await _safe_reply_text(
                    update.message,
                    "Ночной режим: Syntax вернул не все видео. "
                    f"Повторяю недостающие языки: {', '.join(missing_keys_now)} "
                    f"(попытка {syntax_attempt + 1}/{max_syntax_attempts}, пауза {delay_sec:g} сек).",
                )
                if delay_sec > 0:
                    await asyncio.sleep(delay_sec)
                pending_batch_requests = [req for req in batch_requests if str(req.key) in missing_keys_now]
            expected_syntax_keys = {str(req.key) for req in batch_requests}
            returned_syntax_keys = set(syntax_batch_results)
            missing_syntax_keys = sorted(expected_syntax_keys - returned_syntax_keys)
            if missing_syntax_keys:
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
                            quality=video_quality,
                            aspect_ratio=aspect_ratio,
                            audio_enabled=video_audio_enabled,
                            syntax_provider=syntax_provider,
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
                await _safe_reply_text(
                    update.message,
                    f"Получено из Syntax: {', '.join(sorted(returned_syntax_keys)) or 'ничего'}. "
                    f"Без готового результата: {', '.join(missing_syntax_keys)}. "
                    "Пересылаю полученные видео; повтор понадобится только для остальных языков.",
                )
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
                if _is_scheduled_unattended_ai_mode(session):
                    session[SCENE_MEDIA_AI_TECHNICAL_FAILURE_SESSION_KEY] = {
                        "stage": "syntax_video",
                        "attempts": syntax_attempt,
                        "max_retries": max_syntax_attempts,
                        "reason": (
                            "Syntax вернул не весь набор языков. "
                            f"Готово: {', '.join(sorted(returned_syntax_keys)) or 'ничего'}; "
                            f"не пришли: {', '.join(missing_syntax_keys)}."
                        ),
                        "missing_languages": missing_syntax_keys,
                        "ready_languages": sorted(returned_syntax_keys),
                        "errors": syntax_batch_errors[-3:],
                    }
                    session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
                    first_missing = missing_syntax_keys[0] if missing_syntax_keys else ""
                    if first_missing in languages:
                        session["scene_media_language_index"] = languages.index(first_missing)
                    await _safe_reply_text(
                        update.message,
                        "Ночной режим: Syntax не вернул все языки за timeout. "
                        "Сохраняю текущий шаг и повторю позже без отката сцены.",
                    )
                    return True
                ready_syntax_keys = sorted(returned_syntax_keys)
                await _reply_text_with_optional_markup(
                    update,
                    (
                        "Не удалось получить все видео из Syntax. Автоматически в Vertex не ухожу.\n\n"
                        f"Языки сцены: {', '.join(sorted(expected_syntax_keys))}\n"
                        f"Получено ботом из Syntax: {', '.join(ready_syntax_keys) or 'пока ничего'}\n"
                        f"Бот ещё не получил: {', '.join(missing_syntax_keys)}\n\n"
                        "Напишите `Syntax`, чтобы отправить недостающие языки ещё раз через Syntax, "
                        "или `Vertex`, чтобы запустить Vertex/Veo для недостающих языков."
                    ),
                    reply_markup=_syntax_recovery_markup(missing_syntax_keys, session=session),
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
                reply_markup=_syntax_recovery_markup([language], session=session),
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
                        session=session,
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
            if (
                _is_scheduled_unattended_ai_mode(session)
                and not session.get(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY)
                and not _all_scene_languages_have_video(scene, languages)
            ):
                return await _run_scene_media_ai_approve(
                    update,
                    context=context,
                    chat_id=chat_id,
                    session=session,
                )
            return True

    if not requires_multilingual_approval:
        for language in languages:
            _approve_scene_video_language(scene, language, speed_coef=default_scene_video_speed_coef(scene))
        next_idx = _advance_scene_media_after_current(session, idx, total=total)
        if next_idx >= total:
            return await _finish_scene_media_production(update, session)
        await _send_next_scene_media_step(update, session)
        return True

    if auto_approve:
        for language in languages:
            _approve_scene_video_language(scene, language, speed_coef=default_scene_video_speed_coef(scene))
        next_idx = _advance_scene_media_after_current(session, idx, total=total)
        if next_idx >= total:
            return await _finish_scene_media_production(update, session)
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
        next_idx = _advance_scene_media_after_current(session, idx, total=total)
        if next_idx >= total:
            return await _finish_scene_media_production(update, session)
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
        if session.get("state") == "state_waiting_overlay_title_style_approval":
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
            _scene_video_progress_signature(scene),
            session.get("state"),
        )

        if sub == SCENE_MEDIA_SUBSTEP_START:
            await _approve_planned_scene_start(update, session=session, scene=scene, idx=idx)
        elif sub == SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL:
            session["scene_media_substep"] = (
                SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
            )
            if await _review_titles_after_start_frame_approval(update, session, scene):
                return True
        elif sub == SCENE_MEDIA_SUBSTEP_END:
            await _scene_media_end_ok(update, session=session, scene=scene, idx=idx)
        elif sub == SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL:
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_VIDEO
        elif sub == SCENE_MEDIA_SUBSTEP_VIDEO:
            languages = _scene_target_languages(session, scene)
            if _all_scene_languages_have_video(scene, languages):
                for language in languages:
                    _approve_scene_video_language(scene, language, speed_coef=default_scene_video_speed_coef(scene))
                session["scene_media_language_index"] = 0
                next_idx = _advance_scene_media_after_current(session, idx, total=len(scenes))
                if next_idx >= len(scenes):
                    return await _finish_scene_media_production(update, session)
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
            _scene_video_progress_signature(scene_after),
            session.get("state"),
        )
        if after == before:
            return True

    _set_scene_media_manual_approval(session)
    await update.message.reply_text("Автоапрув остановлен: слишком много внутренних шагов подряд. Возвращаю ручной режим.")
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
        if session.get("state") == "state_waiting_overlay_title_style_approval":
            return True
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
            _scene_video_progress_signature(scene),
            len(scene.get("ai_approval_history") or []),
            session.get("state"),
        )

        if sub == SCENE_MEDIA_SUBSTEP_START:
            await _approve_planned_scene_start(
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
                image_status = _normalize_ai_status_for_session(session, image_status, stage="image")
                if image_status == AI_APPROVAL_STATUS_APPROVE:
                    session["scene_media_substep"] = (
                        SCENE_MEDIA_SUBSTEP_END if _scene_needs_end_frame_step(scene) else SCENE_MEDIA_SUBSTEP_VIDEO
                    )
                    if await _review_titles_after_start_frame_approval(update, session, scene):
                        return True
                elif image_status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
                    if _ai_attempt_count(scene, "image") <= _ai_regenerate_attempt_limit(session):
                        scene.pop("start_frame_image_b64", None)
                        await _regenerate_start_prompt_from_ai_feedback(
                            update,
                            context,
                            chat_id=chat_id,
                            session=session,
                            scene=scene,
                            idx=idx,
                        )
                        retry_model_message = _prepare_ai_image_retry_model_message(scene, "start")
                        session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
                        await _safe_reply_text(
                            update.message,
                            f"🤖 ИИ апрув просит перегенерацию стартового фрейма с исправленным prompt. "
                            f"Текущий кадр не передаю как reference.{retry_model_message}"
                        )
                    elif _is_scheduled_unattended_ai_mode(session):
                        await _safe_reply_text(update.message, "Ночной режим: лимит перегенерации стартового фрейма исчерпан, ручную проверку не запрашиваю.")
                        return True
                else:
                    _set_ai_human_review_pending(session, stage="image", reason="image QA requires manual review")
                    await _safe_reply_text(update.message, "ИИ апрув отправил стартовый фрейм на ручную проверку.")
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
                    if (
                        _is_scheduled_unattended_ai_mode(session)
                        and not session.get(SCENE_MEDIA_AI_HUMAN_REVIEW_SESSION_KEY)
                        and not _all_scene_languages_have_video(scene, languages)
                    ):
                        continue
                    return True
                for language in languages:
                    _approve_scene_video_language(scene, language, speed_coef=default_scene_video_speed_coef(scene))
                session["scene_media_language_index"] = 0
                next_idx = _advance_scene_media_after_current(session, idx, total=len(scenes))
                if next_idx >= len(scenes):
                    return await _finish_scene_media_production(update, session)
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
            _scene_video_progress_signature(scene_after),
            len(scene_after.get("ai_approval_history") or []),
            session.get("state"),
        )
        if after == before:
            return True

    _set_scene_media_manual_approval(session)
    if _is_scheduled_unattended_ai_mode(session):
        _set_ai_human_review_pending(
            session,
            stage="auto_approve",
            reason="AI approve reached the internal step limit",
        )
    await update.message.reply_text("ИИ апрув остановлен: слишком много внутренних шагов подряд. Возвращаю ручной режим.")
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
            reply_markup=_syntax_confirmation_markup(session=session),
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
                    session=session,
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as rewrite_err:
            await _safe_reply_text(update.message, f"AI временно не смог переписать video prompt: {rewrite_err}")
        except Exception as rewrite_err:
            logger.exception("scene policy rewrite after syntax path failed")
            await _safe_reply_text(
                update.message,
                f"Не удалось переписать video prompt: {_user_error_message(rewrite_err, domain='video')}"
            )

    if choice is False:
        scene.pop("syntax_fallback_pending", None)
        await _safe_reply_text(update.message, "Ок, Syntax не запускаю.")
        await _offer_policy_rewrite_after_syntax_skip()
        return True

    payload = dict(pending)
    scene.pop("syntax_fallback_pending", None)
    await _safe_reply_text(update.message, "Запускаю Syntax fallback по вашему подтверждению…")
    try:
        video_bytes = await run_syntax_fallback_from_session_payload(payload)
    except Exception as fb_err:
        logger.exception("scene Syntax fallback execution failed")
        await _safe_reply_text(
            update.message,
            f"Ошибка Syntax fallback: {_user_error_message(fb_err, domain='video')}"
        )
        await _offer_policy_rewrite_after_syntax_skip()
        return True
    if not video_bytes:
        await _safe_reply_text(update.message, "Syntax не вернул видео. Переходим к переписыванию prompt.")
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
    await _safe_reply_text(
        update.message,
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
            _approve_scene_video_language(scene, lang, speed_coef=default_scene_video_speed_coef(scene))
        next_idx = _advance_scene_media_after_current(session, idx, total=total)
        if next_idx >= total:
            return await _finish_scene_media_production(update, session)
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
            reply_markup=_scene_image_model_retry_markup(lower_model=lower_model, session=session),
        )
        return True
    if low in downgrade_commands:
        if not lower_model:
            await update.message.reply_text(
                f"Модель ниже не настроена. Могу повторить сильную `{retry_model}`.",
                reply_markup=_scene_image_model_retry_markup(lower_model=None, session=session),
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
            prompt = _scene_video_prompt_for_language(scene, language)
            if entry is None and prompt.strip() and _video_regeneration_requested(scene, language):
                # Batch retries share scene frames/settings, but a phrase edit can
                # also invalidate a language that succeeded in the original batch.
                template = next((value for value in entries_by_lang.values() if isinstance(value, dict)), None)
                if template is not None:
                    entry = dict(template)
                    entries_by_lang[language] = entry
                    pending["languages"] = list(dict.fromkeys([*pending.get("languages", []), language]))
                    pending["missing_languages"] = list(pending["languages"])
            if isinstance(entry, dict) and prompt.strip():
                entry["prompt"] = prompt
        return
    language = normalize_language_code(str(pending.get("language") or "")) or (languages[0] if languages else "ru")
    prompt = _scene_video_prompt_for_language(scene, language)
    if prompt.strip():
        pending["prompt"] = prompt


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

    if phrase_cmd is None:
        generic_phrase = re.match(r"^\s*(?:фраза|phrase)\s*:\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
        if generic_phrase:
            phrase_cmd = (_scene_current_language(session, scene), generic_phrase.group(1).strip())
    awaiting = pending.pop("edit_input_pending", None)
    if awaiting and not any((prompt_cmd, phrase_cmd, feedback is not None, generic_prompt is not None)):
        if text.strip().lower() in {"отмена", "cancel"}:
            await _safe_reply_text(update.message, "Ввод правки отменён. Можно выбрать Syntax или Vertex.")
            return True
        if video_provider_choice_parse_user_selection(text) is None:
            if awaiting.get("kind") == "feedback":
                feedback = text.strip()
            elif awaiting.get("kind") == "phrase":
                phrase_cmd = (awaiting["language"], text.strip())

    if prompt_cmd:
        lang, payload = prompt_cmd
        if lang not in languages:
            await _safe_reply_text(
                update.message,
                f"Язык `{lang}` не входит в текущий набор: {', '.join(languages) or 'ru'}."
            )
            return True
        if not payload:
            await _safe_reply_text(update.message, f'После "ПРОМПТ {lang}:" пришлите новый prompt.')
            return True
        manual_prompt = _append_manual_prompt_voice_locks(payload, scene, lang)
        _set_scene_video_prompt_for_language(scene, lang, manual_prompt)
        _set_scene_video_prompt_raw(scene, lang, True)
        _invalidate_scene_language_video(scene, lang)
        session["scene_media_language_index"] = languages.index(lang)
        _refresh_video_provider_choice_payloads(scene, pending, languages)
        await _reply_text_with_optional_markup(
            update,
            f"Обновила retry prompt для {lang}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(languages, session=session),
        )
        return True

    if phrase_cmd:
        lang, payload = phrase_cmd
        if lang not in languages:
            await _safe_reply_text(
                update.message,
                f"Язык `{lang}` не входит в текущий набор: {', '.join(languages) or 'ru'}."
            )
            return True
        if not payload:
            pending["edit_input_pending"] = {"kind": "phrase", "language": lang}
            await _safe_reply_text(update.message, f'После "ФРАЗА {lang}:" пришлите текст фразы.')
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
            reply_markup=_syntax_recovery_markup(languages, session=session),
        )
        return True

    if generic_prompt is not None:
        lang = _scene_current_language(session, scene)
        if lang not in languages and languages:
            lang = languages[0]
        if not generic_prompt:
            await _safe_reply_text(update.message, 'После "ПРОМПТ:" пришлите новый prompt.')
            return True
        manual_prompt = _append_manual_prompt_voice_locks(generic_prompt, scene, lang)
        _set_scene_video_prompt_for_language(scene, lang, manual_prompt)
        _set_scene_video_prompt_raw(scene, lang, True)
        _invalidate_scene_language_video(scene, lang)
        if lang in languages:
            session["scene_media_language_index"] = languages.index(lang)
        _refresh_video_provider_choice_payloads(scene, pending, languages)
        await _reply_text_with_optional_markup(
            update,
            f"Обновила retry prompt для {lang}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(languages, session=session),
        )
        return True

    if feedback is not None:
        if not feedback.strip():
            pending["edit_input_pending"] = {"kind": "feedback"}
            await _safe_reply_text(update.message, 'После "ФИДБЕК:" пришлите комментарий.')
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
            await _safe_reply_text(update.message, str(e))
            return True
        except Exception as e:
            logger.exception("pending provider video prompt feedback rewrite failed")
            await _safe_reply_text(update.message, f"Ошибка переформатирования video prompt: {_user_error_message(e)}")
            return True
        applied_languages = _set_scene_video_prompt_for_all_languages(session, scene, rewritten)
        _refresh_video_provider_choice_payloads(scene, pending, applied_languages)
        await _reply_text_with_optional_markup(
            update,
            "Обновила retry prompt по фидбэку для языков: "
            f"{', '.join(applied_languages)}. Можно снова нажать `Syntax` или `Vertex`.",
            reply_markup=_syntax_recovery_markup(applied_languages, session=session),
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
                speed_coef=default_scene_video_speed_coef(scene),
            )
        next_idx = _advance_scene_media_after_current(session, idx, total=total)
        if next_idx >= total:
            return await _finish_scene_media_production(update, session)
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
    if _get_scene_phrase_translation_pending(session, scene_idx=idx) is not None:
        # Let the regular phrase translation flow consume its confirmation.
        return False
    retry_aspect_ratio = _scene_aspect_ratio(scene, session)
    scene["aspect_ratio"] = retry_aspect_ratio
    pending_languages = (
        [
            normalize_language_code(str(lang)) or str(lang)
            for lang in pending.get("languages", [])
            if str(lang).strip()
        ]
        if pending.get("source") == "scene_batch"
        else _scene_target_languages(session, scene)
    )
    low_text = " ".join(str(text or "").strip().lower().split())
    approval_command = (
        low_text in {"ок", "ok", "okay", "approve", "да", "yes", "y", "ок все", "ok all", "approve all"}
        or _parse_ok_language_command(text) is not None
    )
    current_languages = _scene_target_languages(session, scene)
    if approval_command and _all_scene_languages_have_video(scene, current_languages):
        # A late Syntax result may arrive after the recovery choice was saved.
        # Once every requested video exists, approval must win over the stale
        # Syntax/Vertex recovery prompt and continue through the normal video
        # approval branch below.
        scene.pop("video_provider_choice_pending", None)
        return False
    if await _handle_scene_video_provider_choice_edit_command(
        update,
        context,
        chat_id=chat_id,
        session=session,
        scene=scene,
        pending=pending,
        text=text,
        languages=current_languages,
    ):
        return True
    choice = video_provider_choice_parse_user_selection(text)
    if choice is None:
        await _reply_text_with_optional_markup(
            update,
            "Нажмите `Syntax` для повтора через Syntax или `Vertex` для резервного Vertex/Veo.",
            reply_markup=_syntax_recovery_markup(pending_languages, session=session),
        )
        return True

    _refresh_video_provider_choice_payloads(scene, pending, current_languages)
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
        for entry in entries_by_lang.values():
            if not isinstance(entry, dict):
                continue
            entry["aspect_ratio"] = retry_aspect_ratio
            if str(entry.get("prompt") or "").strip():
                entry["prompt"] = _force_video_prompt_aspect_ratio(str(entry.get("prompt") or ""), retry_aspect_ratio)
        if choice == "syntax":
            await _safe_reply_text(
                update.message,
                f"Повторяю через Syntax недостающие языки: {', '.join(languages)}…"
            )
            requests = [
                BatchFallbackRequest(
                    key=language,
                    prompt=_scene_syntax_prompt_for_language(
                        scene,
                        language,
                        str(entries_by_lang.get(language, {}).get("prompt") or ""),
                    ),
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
                    syntax_provider=str(
                        next(iter(entries_by_lang.values())).get("syntax_provider")
                        or syntax_provider_for_scene_generation(scene, session)
                    ),
                    aspect_ratio=retry_aspect_ratio,
                    audio_enabled=bool(next(iter(entries_by_lang.values())).get("audio_enabled", True)),
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
                    reply_markup=_syntax_recovery_markup(missing, session=session),
                )
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

        await _safe_reply_text(update.message, "Пробую резервный канал Vertex/Veo для всех языков сцены…")
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
                await _safe_reply_text(
                    update.message,
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
        await _safe_reply_text(update.message, "Готовлю повтор этой сцены через Syntax…")
        pending["aspect_ratio"] = retry_aspect_ratio
        if str(pending.get("prompt") or "").strip():
            pending["prompt"] = _force_video_prompt_aspect_ratio(str(pending.get("prompt") or ""), retry_aspect_ratio)
        syntax_payload = _scene_video_provider_payload_for_language(scene, pending, language)
        syntax_payload["aspect_ratio"] = retry_aspect_ratio
        if str(syntax_payload.get("prompt") or "").strip():
            syntax_payload["prompt"] = _force_video_prompt_aspect_ratio(str(syntax_payload.get("prompt") or ""), retry_aspect_ratio)
        try:
            video_bytes = await run_syntax_fallback_from_session_payload(syntax_payload)
        except Exception as fb_err:
            logger.exception("scene Syntax retry execution failed")
            await _reply_text_with_optional_markup(
                update,
                f"Не удалось отправить запрос в Syntax: {_user_error_message(fb_err, domain='video')}",
                reply_markup=_syntax_recovery_markup(languages, session=session),
            )
            return True
        if not video_bytes:
            await _reply_text_with_optional_markup(
                update,
                "Syntax снова не вернул видео. Можно поправить prompt/фразу, ещё раз нажать `Syntax` или переключиться на `Vertex`.",
                reply_markup=_syntax_recovery_markup(languages, session=session),
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
        await _safe_reply_text(update.message, "Пробую резервный канал Vertex/Veo…")
        pending["aspect_ratio"] = retry_aspect_ratio
        if str(pending.get("prompt") or "").strip():
            pending["prompt"] = _force_video_prompt_aspect_ratio(str(pending.get("prompt") or ""), retry_aspect_ratio)
        vertex_payload = _scene_video_provider_payload_for_language(scene, pending, language)
        vertex_payload["aspect_ratio"] = retry_aspect_ratio
        if str(vertex_payload.get("prompt") or "").strip():
            vertex_payload["prompt"] = _force_video_prompt_aspect_ratio(str(vertex_payload.get("prompt") or ""), retry_aspect_ratio)
        try:
            video_bytes = await run_vertex_fallback_from_session_payload(vertex_payload)
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
        vertex_duration_sec = int(vertex_payload.get("vertex_duration_sec") or vertex_payload.get("duration_sec") or 6)
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
    if str(text or "").strip().lower() == NAV_BUTTON_START_OVER.lower():
        SESSIONS.pop(chat_id, None)
        session.clear()
        await update.message.reply_text(
            "Начинаем сначала. Выберите действие на первом шаге.",
            reply_markup=first_step_menu_markup(),
        )
        return True
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
    # Language approval remains a command while another language's prompt is edited.
    approval_language = _parse_ok_language_command(text)
    if approval_language and session.get("scene_media_substep") == "video_prompt":
        if approval_language not in _scene_target_languages(session, scene) or not _scene_video_bytes_for_language(scene, approval_language):
            await update.message.reply_text(f"Нет готового видео для языка {approval_language}.")
            return True
        _approve_scene_video_language(scene, approval_language, speed_coef=_scene_speed_coef(scene, approval_language))
        _set_video_regeneration_requested(scene, approval_language, False)
        await update.message.reply_text(f"Готовое видео {approval_language} утверждено.")
        return True
    if await _handle_scene_reference_images_pending_text(
        update,
        session,
        scene,
        idx=idx,
        text=text,
    ):
        return True
    if text.strip().lower() in {"добавить своё изображение", "добавить свое изображение"}:
        target = _image_reference_target_for_substep(str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START))
        if target is None:
            await update.message.reply_text("Перейдите к стартовому или конечному кадру, чтобы заменить его своим изображением.")
            return True
        _set_scene_reference_images_pending(session, scene_idx=idx, target=target)
        session[SCENE_IMAGE_REFERENCE_PENDING_KEY]["use_as_frame"] = True
        await update.message.reply_text("Пришлите изображение как фото или файлом. Оно станет готовым кадром для видео без генерации картинки.")
        return True
    if text.strip().lower() == SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON.lower():
        target = _image_reference_target_for_substep(
            str(session.get("scene_media_substep") or SCENE_MEDIA_SUBSTEP_START)
        )
        if target is None:
            await update.message.reply_text("Изображения можно добавить на шаге стартового или конечного кадра.")
            return True
        return await _start_scene_reference_images_collection(
            update,
            session,
            scene,
            idx=idx,
            target=target,
        )
    image_edit_pending = _pop_scene_image_edit_pending(session, scene_idx=idx)
    if image_edit_pending is not None:
        target = str(image_edit_pending.get("target") or "start")
        mode = str(image_edit_pending.get("mode") or "edit")
        if mode == "prompt_feedback":
            if target == "end":
                session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL
                return await _scene_media_end_image_approval_step(
                    update,
                    context,
                    chat_id=chat_id,
                    session=session,
                    scene=scene,
                    idx=idx,
                    text=f"ФИДБЕК: {text}",
                )
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL
            return await _scene_media_start_image_approval_step(
                update,
                context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
                text=f"ФИДБЕК: {text}",
            )
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
            session["scene_media_language_index"] = 0
            next_idx = _advance_scene_media_after_current(session, idx, total=len(scenes))
            if next_idx >= len(scenes):
                return await _finish_scene_media_production(update, session)
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
                        f"Фразу для {target_lang} обновила вручную. Проверьте обновлённый video prompt и нажмите «ОК»."
                    )
                    await _send_next_scene_media_step(update, session)
                    return True
            if new_source_text:
                source_text = new_source_text
                phrase_translation_pending["source_text"] = source_text
            elif not feedback:
                feedback = raw_revision
            if new_source_text:
                source_text = _apply_scene_phrase_update_to_scene(
                    session,
                    scene,
                    scene_idx=idx,
                    language=source_lang,
                    phrase_text=source_text,
                    origin="user",
                )
                phrase_translation_pending["source_text"] = source_text
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
                    retry_ai_rate_limit=False,
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
                    retry_ai_rate_limit=False,
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
            f"Адаптивный перевод применён для {target_lang}. Проверьте обновлённый video prompt и нажмите «ОК»."
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
            phrase_cmd = _parse_lang_payload_command(payload, "фраза")
            if phrase_cmd:
                cmd_language, cmd_payload = phrase_cmd
                if cmd_payload.strip():
                    language = cmd_language
                    payload = cmd_payload.strip()
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
                f"Фразу для {language} обновила. Проверьте обновлённый video prompt и нажмите «ОК»."
            )
            await _send_next_scene_media_step(update, session)
            return True
    pending = session.get(SCENE_SPEECH_PENDING_KEY)
    if isinstance(pending, dict) and int(pending.get("scene_idx", -1)) == idx:
        stage = str(pending.get("stage") or "choice")
        is_ok_pending = text.lower() in {"ок", "ok", "okay", "approve"}
        if text == SCENE_SPEECH_OPTION_APPROVE:
            is_ok_pending = True
        if pending.get("language") == "en":
            if stage == "approval" and is_ok_pending:
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                _set_scene_speech_budget_override(scene, "en", True)
                save_loaded_session_if_possible(chat_id)
                await reply_text_without_keyboard(update.message, "EN-фраза подтверждена. Генерирую видео…")
                return await _scene_media_video_ok(
                    update, context=context, chat_id=chat_id, session=session,
                    scene=scene, idx=idx, total=len(scenes),
                    regenerate_language="en" if _scene_video_bytes_for_language(scene, "ru") else None,
                )
            if text == SCENE_SPEECH_OPTION_EDIT:
                pending["stage"] = "approval_manual_phrase"
                await _reply_text_with_optional_markup(
                    update, "Пришлите новую EN-фразу для озвучки.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return True
            if not text.strip():
                await update.message.reply_text("Пришлите EN-фразу или подтвердите «ОК».")
                return True
            _apply_scene_phrase_update_to_scene(
                session, scene, scene_idx=idx, language="en",
                phrase_text=text.strip(), origin="user",
            )
            return await _request_english_speech_approval(
                update, session, scene, int(pending.get("duration_sec", 6)),
            )
        if stage == "approval":
            if is_ok_pending:
                _cancel_scene_speech_timeout(chat_id)
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                _set_scene_speech_budget_override(scene, "ru", True)
                save_loaded_session_if_possible(chat_id)
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
                preserve_approved=False,
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
                reply_markup=_scene_speech_approval_markup(session=session),
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
                preserve_approved=False,
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
                reply_markup=_scene_speech_approval_markup(session=session),
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
                reply_markup=_scene_speech_options_markup(session=session),
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
                preserve_approved=False,
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
                reply_markup=_scene_speech_approval_markup(session=session),
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
                for language in _scene_target_languages(session, scene):
                    _set_scene_speech_budget_override(scene, language, True)
                session.pop(SCENE_SPEECH_PENDING_KEY, None)
                save_loaded_session_if_possible(chat_id)
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
                reply_markup=_scene_speech_user_long_phrase_markup(session=session),
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
    low_text = text.lower().strip()
    is_ok = low_text in {"ок", "ok", "okay", "approve", "да", "yes", "y"}
    is_ok_all = low_text in {"ок все", "ok all", "approve all"}
    if sub == SCENE_MEDIA_SUBSTEP_VIDEO:
        prompt_map_for_start_guard = _video_prompt_by_lang_with_candidate_fallback(scene)
        if not prompt_map_for_start_guard:
            restore_scene_prompt_fields_from_history(chat_id, session, idx)
            prompt_map_for_start_guard = _video_prompt_by_lang_with_candidate_fallback(scene)
        languages_for_start_guard = _scene_target_languages(session, scene)
        if (
            not selected_image_bytes_for_video(scene, kind="start")
            and not prompt_map_for_start_guard
            and not _all_scene_languages_have_video(scene, languages_for_start_guard)
        ):
            session["scene_media_substep"] = SCENE_MEDIA_SUBSTEP_START
            session["scene_media_language_index"] = 0
            await _safe_reply_text(
                update.message,
                "У этой сцены ещё нет стартового кадра. Возвращаюсь к генерации стартового изображения.",
            )
            await _send_next_scene_media_step(update, session)
            return True
    if sub == SCENE_MEDIA_SUBSTEP_VIDEO:
        languages = _scene_target_languages(session, scene)
        regen_lang = _parse_regenerate_language_command(text)
        if regen_lang and regen_lang in languages:
            _set_video_regeneration_requested(scene, regen_lang, True)
            session["scene_media_language_index"] = languages.index(regen_lang)
            await _safe_reply_text(
                update.message,
                f"Перегенерирую видео для языка {regen_lang} с текущим prompt.",
            )
            return await _scene_media_video_ok(
                update,
                context=context,
                chat_id=chat_id,
                session=session,
                scene=scene,
                idx=idx,
                total=len(scenes),
                regenerate_language=regen_lang,
            )
    if await _handle_scene_media_common_restarts(update, session, scene, text):
        return True
    if sub == SCENE_MEDIA_SUBSTEP_VIDEO:
        languages = _scene_target_languages(session, scene)
        requested_regen_languages = _video_regeneration_requested_languages(scene, languages)
        if is_ok and requested_regen_languages:
            await _safe_reply_text(
                update.message,
                "ОК, перегенерирую изменённые видео параллельно для языков: "
                + ", ".join(requested_regen_languages),
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
        if _all_scene_languages_have_video(scene, languages):
            low = low_text
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
                next_idx = _advance_scene_media_after_current(session, idx, total=len(scenes))
                if next_idx >= len(scenes):
                    return await _finish_scene_media_production(update, session)
                await _send_next_scene_media_step(update, session)
                return True
            if is_ok and len(languages) == 1:
                only_lang = languages[0]
                _approve_scene_video_language(scene, only_lang, speed_coef=_scene_speed_coef(scene, only_lang))
                await update.message.reply_text(f"Апрув принят для языка {only_lang}.")
            elif is_ok_all:
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
                    prompt_cmd = _parse_lang_payload_command(text, "промпт")
                    lang_feedback_cmd = _parse_lang_payload_command(text, "фидбек")
                    feedback_cmd = lang_feedback_cmd or _parse_lang_payload_command(text, "исправь")
                    phrase_cmd = _parse_lang_payload_command(text, "фраза")
                    if prompt_cmd:
                        lang, payload = prompt_cmd
                        if lang in languages and payload:
                            manual_prompt = _append_manual_prompt_voice_locks(payload, scene, lang)
                            _set_scene_video_prompt_for_language(
                                scene,
                                lang,
                                manual_prompt,
                            )
                            _set_scene_video_prompt_raw(scene, lang, True)
                            _invalidate_scene_language_video(scene, lang)
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
                    if feedback_cmd:
                        lang, payload = feedback_cmd
                        if lang in languages and not payload:
                            session["scene_media_language_index"] = languages.index(lang)
                            _set_scene_video_feedback_pending(session, scene_idx=idx, language=lang)
                            await _ask_for_feedback_text(update, "video prompt")
                            return True
                        if lang in languages and payload:
                            updated_duration = _apply_video_duration_feedback(
                                scene,
                                payload,
                                speechless=_is_speechless_video_scene(scene, session),
                            )
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
                            _invalidate_scene_language_video(scene, lang)
                            session["scene_media_language_index"] = languages.index(lang)
                            duration_note = f" Длительность: {updated_duration} сек." if updated_duration is not None else ""
                            await update.message.reply_text(
                                f"Подготовила обновлённый prompt для {lang}.{duration_note} Нажмите «ПЕРЕГЕНЕРИРОВАТЬ {lang}»."
                            )
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
                                f"Фразу для {lang} обновила отдельно. Проверьте обновлённый video prompt и нажмите «ОК»."
                            )
                            await _send_next_scene_media_step(update, session)
                            return True
                        if lang in languages and not payload:
                            _set_scene_phrase_input_pending(session, scene_idx=idx, language=lang)
                            await update.message.reply_text(
                                f'После "ФРАЗА {lang}:" пришлите текст фразы.'
                            )
                            return True
                    await _send_scene_video_approval_prompt(update, session, languages)
                    return True
            if _all_scene_languages_approved(scene, languages):
                next_idx = _advance_scene_media_after_current(session, idx, total=len(scenes))
                if next_idx >= len(scenes):
                    return await _finish_scene_media_production(update, session)
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
            _approve_planned_scene_start(update, context=context, chat_id=chat_id, session=session, scene=scene, idx=idx)
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
    if not (is_ok or (sub == SCENE_MEDIA_SUBSTEP_VIDEO and is_ok_all)):
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
