"""Core recipe text parsing helpers for handlers media flow."""

from ..scene_utils import extract_recipe_steps_text, normalize_recipe_steps

def _normalize_user_recipe_steps(raw_steps: list[str]) -> list[str]:
    """
    Локальная нормализация рецепта из пользовательского ввода без вызова LLM.
    Не дополняет пустой список — иначе получались 4 одинаковые заглушки «подготовить ингредиенты».
    """
    steps = normalize_recipe_steps(raw_steps)
    if not steps:
        return []
    if len(steps) < 4:
        while len(steps) < 4:
            steps.append(steps[-1])
    while len(steps) > 6:
        steps = normalize_recipe_steps(steps[:-2] + [f"{steps[-2]}; {steps[-1]}"])
    return steps[:6]


def _is_obvious_placeholder_recipe(steps: list[str]) -> bool:
    """Все шаги одинаковые и выглядят как авто-заглушка, не настоящий рецепт."""
    if not steps:
        return True
    uniq = {s.lower().strip() for s in steps if s.strip()}
    if len(uniq) != 1:
        return False
    line = next(iter(uniq))
    if line == "подготовить ингредиенты":
        return True
    if line.startswith("подготовить ингредиенты") and len(line) < 45:
        return True
    return False


def _try_parse_direct_recipe_from_user_text(text: str) -> list[str] | None:
    """
    Если в сообщении явно распознаны шаги рецепта (минимум 2), возвращает 4–6 шагов.
    Иначе None — идём в обычный flow с концептами.
    """
    raw = normalize_recipe_steps(extract_recipe_steps_text(text))
    if len(raw) < 2:
        return None
    if _is_obvious_placeholder_recipe(raw):
        return None
    normalized = _normalize_user_recipe_steps(raw)
    if not normalized or _is_obvious_placeholder_recipe(normalized):
        return None
    return normalized


