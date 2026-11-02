"""
Централизованные константы проекта.

Содержит:
- имена переменных окружения для токенов;
- строки состояний state machine диалога с пользователем;
- **каталог текстовых шагов** ``TEXT_PIPELINE_STEPS`` (концепт → рецепт → сценарий), пригодный к замене на строки из БД;
- список локаций, разрешённых для сценария (должны существовать в ``config.ASSETS``).
"""

from dataclasses import dataclass
from typing import Final

# Имя переменной окружения с токеном бота от @BotFather.
TOKEN_ENV_NAME: Final[str] = "TELEGRAM_BOT_TOKEN"
# Имя переменной окружения с API-ключом Google Gemini.
GEMINI_KEY_ENV_NAME: Final[str] = "GEMINI_API_KEY"
# Опционально: список API-ключей Gemini через запятую (используется для автопереключения).
GEMINI_KEYS_ENV_NAME: Final[str] = "GEMINI_API_KEYS"
# Опционально: идентификатор модели (см. ListModels в AI Studio). По умолчанию — актуальная Flash.
GEMINI_MODEL_ENV_NAME: Final[str] = "GEMINI_MODEL"
# Опционально: fallback-модели через запятую (используются при quota exhausted).
GEMINI_MODEL_FALLBACKS_ENV_NAME: Final[str] = "GEMINI_MODEL_FALLBACKS"
# Опционально: fallback-модели для креативных генераций (сценарии, viral plan, описания).
GEMINI_CREATIVE_MODEL_FALLBACKS_ENV_NAME: Final[str] = "GEMINI_CREATIVE_MODEL_FALLBACKS"
# Опционально: fallback-модели для коротких правок (фразы, переводы, сокращения).
GEMINI_EDIT_MODEL_FALLBACKS_ENV_NAME: Final[str] = "GEMINI_EDIT_MODEL_FALLBACKS"
# Макс. секунд ожидания ответа Vertex `generate_content` (см. ``app.llm_queue``).
GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME: Final[str] = "GEMINI_GENERATE_TIMEOUT_SEC"
# Image generation is materially slower than text generation and needs its own deadline.
VERTEX_IMAGE_GENERATE_TIMEOUT_SEC_ENV_NAME: Final[str] = "VERTEX_IMAGE_GENERATE_TIMEOUT_SEC"
DEFAULT_GEMINI_MODEL: Final[str] = "gemini-2.5-flash"
# Опционально: ID вашего кастомного голоса в ElevenLabs.
ELEVENLABS_API_KEY_ENV_NAME: Final[str] = "ELEVENLABS_API_KEY"
ELEVENLABS_VOICE_ID_ENV_NAME: Final[str] = "ELEVENLABS_VOICE_ID"

# --- State machine: этапы многошагового пайплайна (значения хранятся в SESSIONS[chat_id]["state"]).
STATE_WAITING_CONCEPT: Final[str] = "waiting_concept"
STATE_REFINING_CONCEPT: Final[str] = "refining_concept"
STATE_WAITING_RECIPE_APPROVAL: Final[str] = "waiting_recipe_approval"
STATE_REFINING_RECIPE: Final[str] = "refining_recipe"
STATE_WAITING_SCENE_APPROVAL: Final[str] = "waiting_scene_approval"
STATE_REFINING_SCENES: Final[str] = "refining_scenes"
STATE_WAITING_FRAME_PROMPTS_APPROVAL: Final[str] = "waiting_frame_prompts_approval"
STATE_REFINING_FRAME_PROMPTS: Final[str] = "refining_frame_prompts"
STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL: Final[str] = "waiting_production_bundle_approval"
STATE_REFINING_PRODUCTION_BUNDLE: Final[str] = "refining_production_bundle"
# По сценам подряд: стартовый промпт → картинка → (опц.) конечный промпт → картинка → video prompt → видео → следующая сцена.
# ``waiting_frame_prompts_approval`` / ``refining_frame_prompts`` — для старых сессий; новый поток идёт сразу в production bundle.
STATE_WAITING_SCENE_MEDIA: Final[str] = "waiting_scene_media"
STATE_READY_FOR_PRODUCTION: Final[str] = "ready_for_production"

# Локации, которые допустимы в промптах сцен; пересечение с реальными ключами в ASSETS["locations"].
STRICT_LOCATIONS: Final[list[str]] = ["kitchen", "cutting", "stove", "serving", "sink", "fridge"]


@dataclass(frozen=True, slots=True)
class TextPipelineStepSpec:
    """
    Один текстовый шаг до медиа: свой промпт (см. ``pipeline_llm_steps`` / ``pipeline``), общий цикл UX.

    Паттерн: промпт → LLM → показ пользователю → «ОК» (следующий шаг) или правка
    (``refining_state``, previous + feedback снова в модель).

    Список сейчас константный; позже тот же ``step_id`` / порядок можно подставлять из БД.
    """

    step_id: str
    title_ru: str
    waiting_state: str
    refining_state: str


TEXT_PIPELINE_STEPS: Final[tuple[TextPipelineStepSpec, ...]] = (
    TextPipelineStepSpec(
        step_id="concept",
        title_ru="Концепт",
        waiting_state=STATE_WAITING_CONCEPT,
        refining_state=STATE_REFINING_CONCEPT,
    ),
    TextPipelineStepSpec(
        step_id="recipe",
        title_ru="Рецепт",
        waiting_state=STATE_WAITING_RECIPE_APPROVAL,
        refining_state=STATE_REFINING_RECIPE,
    ),
    TextPipelineStepSpec(
        step_id="scenario",
        title_ru="Сценарий",
        waiting_state=STATE_WAITING_SCENE_APPROVAL,
        refining_state=STATE_REFINING_SCENES,
    ),
)


def text_pipeline_step_by_id(step_id: str) -> TextPipelineStepSpec | None:
    """Возвращает описание шага по ``step_id`` или ``None``."""
    for s in TEXT_PIPELINE_STEPS:
        if s.step_id == step_id:
            return s
    return None


def text_pipeline_step_by_waiting_state(state: str) -> TextPipelineStepSpec | None:
    """Находит текстовый шаг по ``waiting_state`` сессии."""
    for s in TEXT_PIPELINE_STEPS:
        if s.waiting_state == state:
            return s
    return None


def text_pipeline_step_by_refining_state(state: str) -> TextPipelineStepSpec | None:
    """Находит текстовый шаг по ``refining_state`` сессии."""
    for s in TEXT_PIPELINE_STEPS:
        if s.refining_state == state:
            return s
    return None


def next_text_pipeline_step(current_step_id: str) -> TextPipelineStepSpec | None:
    """Следующий текстовый шаг в порядке ``TEXT_PIPELINE_STEPS`` или ``None``."""
    for i, s in enumerate(TEXT_PIPELINE_STEPS):
        if s.step_id == current_step_id and i + 1 < len(TEXT_PIPELINE_STEPS):
            return TEXT_PIPELINE_STEPS[i + 1]
    return None


# Опорные состояния после утверждённого сценария (до/включая пошаговую медиа-генерацию по сценам).
POST_SCENARIO_PIPELINE_WAITING_STATES: Final[tuple[str, ...]] = (
    STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
    STATE_WAITING_SCENE_MEDIA,
    STATE_READY_FOR_PRODUCTION,
)
