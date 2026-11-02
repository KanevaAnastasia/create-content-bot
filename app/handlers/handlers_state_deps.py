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
from ..media_costs import (
    add_scene_language_cost,
    add_scene_shared_cost,
    estimate_gemini_text_cost_usd,
    estimate_image_generation_cost_usd,
    estimated_scene_video_cost,
    estimated_session_total_media_cost,
)
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
from ..generation_store import save_video_result_estimate
from .handlers_generation_utils import (
    build_concepts_session as _build_concepts_session,
    build_direct_recipe_session as _build_direct_recipe_session,
    extract_recipe_from_command_text as _extract_recipe_from_command_text,
    extract_first_http_url as _extract_first_http_url,
    fetch_recipe_text_from_url as _fetch_recipe_text_from_url,
)

from .handlers_media_core import (
    _attempt_syntax_video_fallback_batch,
    ServiceUnavailableRetriesExhaustedError,
    SyntaxFallbackPendingConfirmation,
    VideoProviderChoiceRequired,
    _bump_debounce_generation,
    _call_with_503_retries,
    _decode_b64,
    _elevenlabs_voice_warning,
    encode_video_provider_choice_for_session,
    enforce_video_prompt_voice_requirements,
    encode_syntax_fallback_pending_for_session,
    _encode_b64,
    _generate_video_bytes_with_high_load_retry,
    get_last_video_provider,
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
    run_vertex_fallback_from_session_payload,
    run_syntax_fallback_from_session_payload,
    video_provider_choice_parse_user_selection,
    syntax_fallback_parse_user_confirmation,
    _user_error_message,
    _video_prompt_by_lang_with_candidate_fallback,
)
from .handlers_media_core_recipe import _try_parse_direct_recipe_from_user_text
from .handlers_media_reporting import _format_scenes_plan_attempt_notice, _format_scenes_regeneration_report
from .handlers_media_scene import (
    SCENE_MEDIA_AUTO_APPROVE_BUTTON,
    SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY,
    SCENE_MEDIA_AI_APPROVE_BUTTON,
    SCENE_MEDIA_STOP_BUTTON,
    SCENE_MEDIA_APPROVE_MODE_AI,
    SCENE_MEDIA_APPROVE_MODE_AUTO,
    SCENE_MEDIA_APPROVE_MODE_MANUAL,
    SCENE_MEDIA_APPROVE_MODE_SESSION_KEY,
    SCENE_MEDIA_CHANGE_SPEED_BUTTON,
    SCENE_MEDIA_ADD_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_CLEAR_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_DONE_REFERENCE_IMAGES_BUTTON,
    SCENE_MEDIA_REQUEST_END_FRAME_BUTTON,
    SCENE_MEDIA_SKIP_END_FRAME_BUTTON,
    SCENE_MEDIA_SUBSTEP_END,
    SCENE_MEDIA_SUBSTEP_END_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_START,
    SCENE_MEDIA_SUBSTEP_START_IMAGE_APPROVAL,
    SCENE_MEDIA_SUBSTEP_VIDEO,
    SCENE_MEDIA_UPGRADE_IMAGE_MODEL_BUTTON,
    SCENE_USER_REFERENCE_IMAGES_KEY,
    _append_reference_style_to_image_prompt,
    _append_scene_identity_to_image_prompt,
    _end_frame_delta_instruction,
    _extract_format_or_feedback,
    _extract_prompt_override,
    _generate_image_with_lenient_fallback,
    _generate_image_with_lenient_fallback_result,
    _load_scene_reference_images,
    _next_image_quality_model,
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
    scene_reference_images_markup,
    scene_language_approval_markup,
    scene_video_approval_markup,
    video_policy_retry_markup,
    _video_policy_prompt_approval_text,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
)

logger = logging.getLogger("bot")
# Telegram updates are processed sequentially by default, so a sleep here cannot
# coalesce messages and only adds fixed latency to every button and text reply.
DEBOUNCE_DELAY_SEC = 0.0
_DEBOUNCE_SEQ: dict[int, int] = {}
TELEGRAM_MAX_TEXT_LEN = 3500
