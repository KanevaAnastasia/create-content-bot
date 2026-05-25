"""
Обработчики команд и сообщений Telegram: связывают UI с ``pipeline`` и state machine.

Состояние диалога хранится в ``SESSIONS[chat_id]`` (см. ``state.py``). Ключ ``state`` принимает
значения из ``constants.STATE_*``.

Типичный сценарий:
1. Идея или рецепт → концепты → утверждение концепта → цельный рецепт → утверждение рецепта.
2. Сцены (фразы, что происходит) → утверждение → frame prompts → утверждение → **все промпты** (start/end/video) на ознакомление → утверждение.
3. По каждой сцене подряд: стартовый промпт → генерация старта → при необходимости конечный промпт → генерация конца → video prompt → генерация видео (start+end если есть) → следующая сцена.
4. Не устраивает картинка: «старт заново» / при необходимости «конец заново» — снова этап промпта (ПРОМПТ / ФИДБЕК / «ОК»), затем повторная генерация.
5. Любой этап без «ОК» → перегенерация через нейросеть (или ручной «ПРОМПТ:»).
"""

import logging
import os
from typing import TypeVar

from telegram import Update
from .handlers_media_scene import _send_next_scene_media_step
from .handlers_generation_utils import (
    build_concepts_session as _build_concepts_session,
    build_direct_recipe_session as _build_direct_recipe_session,
    extract_recipe_from_command_text as _extract_recipe_from_command_text,
)
from .handlers_media_core import (
    _decode_b64,
    _encode_b64,
    _end_frame_image_warranted,
    _image_end_scene_prompt,
    _image_scene_prompt,
    _is_video_high_load_error,
    _is_video_invalid_image_error,
    _is_video_model_unavailable_error,
    _is_video_policy_error,
    _is_video_prompt_filtered_error,
    _is_video_safety_error,
    _normalize_veo_duration_sec,
    _parse_video_command_args,
    _user_error_message,
)
from .handlers_media_scene import (
    _end_frame_delta_instruction,
    _extract_format_or_feedback,
    _extract_prompt_override,
    _reference_keys_for_scene,
    _resolve_scene_references,
    _should_soften_image_prompt_after_error,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
)
from ..handlers_media_scene import _generate_image_with_lenient_fallback

logger = logging.getLogger("bot")

# Подшаги пошаговой генерации медиа по сцене (``STATE_WAITING_SCENE_MEDIA``).
SCENE_MEDIA_SUBSTEP_START = "start_prompt"
SCENE_MEDIA_SUBSTEP_END = "end_prompt"
SCENE_MEDIA_SUBSTEP_VIDEO = "video_prompt"

# Debounce: при серии сообщений обрабатывается только последнее после паузы (сек).
_DEBOUNCE_SEQ: dict[int, int] = {}
DEBOUNCE_DELAY_SEC = 1.5
SERVICE_UNAVAILABLE_RETRY_COUNT = 20
SERVICE_UNAVAILABLE_RETRY_DELAY_SEC = 15
VIDEO_HIGH_LOAD_RETRY_DELAY_SEC = 15
T = TypeVar("T")
TELEGRAM_MAX_TEXT_LEN = 3500
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

async def _send_next_image_approval(update: Update, session: dict) -> None:
    """Совместимость: делегирует в общий шаг медиа-пайплайна."""
    await _send_next_scene_media_step(update, session)



from .handlers_state_flow import (
    ai_approve_command,
    cancel,
    check_env,
    fix_stress_command,
    generate,
    generate_image_command,
    generate_video_command,
    on_photo,
    on_text,
    quick_generate,
    start,
)
