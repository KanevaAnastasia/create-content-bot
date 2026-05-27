"""State/text flow handlers extracted from handlers."""

import asyncio
import io
import logging
import os
import re
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..constants import (
    ELEVENLABS_VOICE_ID_ENV_NAME,
    GEMINI_KEY_ENV_NAME,
    GEMINI_KEYS_ENV_NAME,
    GEMINI_MODEL_ENV_NAME,
    GEMINI_MODEL_FALLBACKS_ENV_NAME,
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
    TOKEN_ENV_NAME,
)
from ..formatting import (
    build_human_readable_plan,
    format_frame_prompts_for_approval,
    format_full_recipe_for_approval,
    format_production_bundle_for_approval,
    format_viral_application_summary,
    format_viral_concept_brief,
)
from ..llm_utils import gemini_generate_sync
from ..pipeline import (
    generate_frame_prompts_for_approval,
    generate_full_production,
    generate_recipe_concepts,
    generate_recipe_from_source_text,
    generate_recipe_full_text,
    generate_scenes_plan,
    generate_viral_concept_brief,
)
from ..scene_utils import apply_veo3_lowercase_format
from ..state import SESSIONS
from ..video_generation import get_last_video_result_source, get_last_video_result_uri
from .handlers_generation_utils import (
    build_concepts_session as _build_concepts_session,
    build_direct_recipe_session as _build_direct_recipe_session,
    extract_recipe_from_command_text as _extract_recipe_from_command_text,
    extract_first_http_url as _extract_first_http_url,
    fetch_recipe_text_from_url as _fetch_recipe_text_from_url,
)

from .handlers_media_core import (
    ServiceUnavailableRetriesExhaustedError,
    SyntaxFallbackPendingConfirmation,
    _bump_debounce_generation,
    _call_with_503_retries,
    _decode_b64,
    _elevenlabs_voice_warning,
    enforce_video_prompt_voice_requirements,
    encode_syntax_fallback_pending_for_session,
    _encode_b64,
    _generate_video_bytes_with_high_load_retry,
    _image_end_scene_prompt,
    _image_scene_prompt,
    _is_video_invalid_image_error,
    _is_video_policy_error,
    _is_video_prompt_filtered_error,
    _is_video_safety_error,
    _mask_secret_tail,
    _normalize_character_key,
    _parse_video_command_args,
    _regenerate_image_prompt_from_video_error,
    _regenerate_video_prompt_from_error,
    _regenerate_video_prompt_from_text,
    run_syntax_fallback_from_session_payload,
    syntax_fallback_parse_user_confirmation,
    _user_error_message,
)
from .handlers_media_core_recipe import _try_parse_direct_recipe_from_user_text
from .handlers_media_reporting import _format_scenes_plan_attempt_notice, _format_scenes_regeneration_report
from .handlers_media_scene import (
    SCENE_MEDIA_AUTO_APPROVE_BUTTON,
    SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY,
    SCENE_MEDIA_AI_APPROVE_BUTTON,
    SCENE_MEDIA_APPROVE_MODE_AI,
    SCENE_MEDIA_APPROVE_MODE_AUTO,
    SCENE_MEDIA_APPROVE_MODE_MANUAL,
    SCENE_MEDIA_APPROVE_MODE_SESSION_KEY,
    SCENE_MEDIA_SUBSTEP_END,
    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_START,
    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_VIDEO,
    _append_reference_style_to_image_prompt,
    _append_scene_identity_to_image_prompt,
    _end_frame_delta_instruction,
    _extract_format_or_feedback,
    _extract_prompt_override,
    _generate_image_with_lenient_fallback,
    _load_scene_reference_images,
    _regenerate_end_frame_prompt_from_feedback,
    _rewrite_video_prompt_by_format_instruction,
    _scene_needs_end_frame_step,
    _send_next_scene_media_step,
    _set_end_frame_text,
    _should_suggest_regenerate_start_frame_after_video_prompt_fails,
    ok_only_markup,
    scene_end_approval_markup,
    scene_generated_image_approval_markup,
    scene_image_approval_markup,
    scene_language_approval_markup,
    scene_video_approval_markup,
    video_policy_retry_markup,
    _video_policy_prompt_approval_text,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
)

logger = logging.getLogger("bot")
DEBOUNCE_DELAY_SEC = 1.5
_DEBOUNCE_SEQ: dict[int, int] = {}
TELEGRAM_MAX_TEXT_LEN = 3500
