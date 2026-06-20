"""
Контракт многошагового пайплайна для нескольких проектов.

**Текстовая фаза** (шаги из ``constants.TEXT_PIPELINE_STEPS``):
один и тот же цикл для концепта, рецепта и сценария — отдельные промпты в
``pipeline_llm_steps`` / ``pipeline``, вызов LLM, показ пользователю, при отказе —
``refining_state`` с передачей previous + feedback в модель, при «ОК» — переход к
следующему шагу каталога.

**После сценария** — список сцен; далее общая цепочка: кадры → production → по сценам
медиа (как в ``handlers_state_scene`` / ``handlers_state_media``).

**Целевая форма сцены** перед/во время пошаговой медиа-генерации задаётся
``ScenePerStepMedia``; референсы для кадров сейчас берутся из ``ASSETS`` по
``location`` (см. загрузчики в handlers); в перспективе — явные списки URI в данных сцены / БД.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from .constants import (
    POST_SCENARIO_PIPELINE_WAITING_STATES,
    TEXT_PIPELINE_STEPS,
    TextPipelineStepSpec,
    next_text_pipeline_step,
    text_pipeline_step_by_id,
    text_pipeline_step_by_refining_state,
    text_pipeline_step_by_waiting_state,
)


class ScenePerStepMedia(TypedDict, total=False):
    """
    Минимальный контракт одной сцены для цепочки «стартовый кадр → конечный кадр → видео».

    Поля совпадают с тем, что накапливает сессия после ``generate_full_production``
    и шагов ``STATE_WAITING_SCENE_MEDIA``.
    """

    scene: int
    location: str
    shot_type: str
    model: str
    duration_sec: int
    text: str
    dialogue_mode: str
    dialogue: list[dict[str, Any]]
    speech_by_lang: dict[str, str]
    speech_plan_by_lang: dict[str, list[dict[str, Any]]]
    speech_cues_by_lang: dict[str, list[dict[str, Any]]]
    speech_budget_by_lang: dict[str, dict[str, Any]]
    action: str
    characters: list[str]
    start_frame_prompt: str
    end_frame_prompt: str
    video_prompt: str
    start_frame_image_b64: str
    end_frame_image_b64: str
    image_candidates: list[dict[str, Any]]
    video_candidates_by_lang: dict[str, list[dict[str, Any]]]
    video_prompt_policy_candidate: str
    video_prompt_policy_failure_count: int
    voice_provider: str
    tts: dict[str, Any]


def ordered_text_step_ids() -> tuple[str, ...]:
    """Идентификаторы текстовых шагов в порядке прохождения."""
    return tuple(s.step_id for s in TEXT_PIPELINE_STEPS)


def describe_text_phase() -> str:
    """Краткое текстовое описание фазы для логов/админки."""
    names = ", ".join(s.title_ru for s in TEXT_PIPELINE_STEPS)
    return f"Текстовые шаги: {names}"


def describe_post_scenario_phase() -> str:
    """Состояния ожидания после утверждения сценария (медиа и финал)."""
    return " → ".join(POST_SCENARIO_PIPELINE_WAITING_STATES)


__all__ = [
    "POST_SCENARIO_PIPELINE_WAITING_STATES",
    "ScenePerStepMedia",
    "TEXT_PIPELINE_STEPS",
    "TextPipelineStepSpec",
    "describe_post_scenario_phase",
    "describe_text_phase",
    "next_text_pipeline_step",
    "ordered_text_step_ids",
    "text_pipeline_step_by_id",
    "text_pipeline_step_by_refining_state",
    "text_pipeline_step_by_waiting_state",
]
