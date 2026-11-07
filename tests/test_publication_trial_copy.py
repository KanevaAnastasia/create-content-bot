import asyncio
import pytest

from app.publication_trial_copy import generate_trial_copy


def test_trial_is_distinct_and_does_not_modify_normal():
    normal = {"instagram_ru": "Основной #рецепт"}
    calls = []
    async def refine(**kwargs):
        calls.append(kwargs)
        return {"instagram_ru": "Другой хук #рецепт"}
    result = asyncio.run(generate_trial_copy(recipe="recipe", normal_copy=normal, refine=refine))
    assert result == "Другой хук #рецепт"
    assert normal == {"instagram_ru": "Основной #рецепт"}
    assert "Syntx" in calls[0]["feedback"]


def test_identical_trial_is_rejected():
    async def refine(**kwargs):
        return kwargs["previous_copy"]
    with pytest.raises(ValueError):
        asyncio.run(generate_trial_copy(recipe="recipe", normal_copy={"instagram_ru": "Same"}, refine=refine))
