"""Тесты каталога текстовых шагов и контракта ``pipeline_flow``."""

from app.constants import (
    STATE_REFINING_RECIPE,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    TEXT_PIPELINE_STEPS,
    next_text_pipeline_step,
    text_pipeline_step_by_id,
    text_pipeline_step_by_refining_state,
    text_pipeline_step_by_waiting_state,
)
from app.pipeline_flow import (
    describe_text_phase,
    ordered_text_step_ids,
)


def test_text_pipeline_steps_order_and_ids():
    ids = [s.step_id for s in TEXT_PIPELINE_STEPS]
    assert ids == ["concept", "recipe", "scenario"]
    assert ordered_text_step_ids() == tuple(ids)


def test_text_pipeline_step_lookups():
    assert text_pipeline_step_by_id("recipe") is not None
    assert text_pipeline_step_by_id("recipe").title_ru == "Рецепт"
    assert text_pipeline_step_by_id("unknown") is None
    assert text_pipeline_step_by_waiting_state(STATE_WAITING_RECIPE_APPROVAL).step_id == "recipe"
    assert text_pipeline_step_by_refining_state(STATE_REFINING_RECIPE).step_id == "recipe"


def test_next_text_pipeline_step():
    assert next_text_pipeline_step("concept").step_id == "recipe"
    assert next_text_pipeline_step("recipe").step_id == "scenario"
    assert next_text_pipeline_step("scenario") is None


def test_describe_text_phase_mentions_titles():
    text = describe_text_phase()
    assert "Концепт" in text
    assert "Рецепт" in text
    assert "Сценарий" in text


def test_waiting_scene_maps_to_scenario():
    step = text_pipeline_step_by_waiting_state(STATE_WAITING_SCENE_APPROVAL)
    assert step is not None
    assert step.step_id == "scenario"
