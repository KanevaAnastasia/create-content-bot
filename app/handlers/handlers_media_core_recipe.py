"""Core recipe text parsing helpers for handlers media flow."""

import re

from ..scene_utils import extract_recipe_steps_text, normalize_recipe_steps

_CONCEPT_TEXT_MAX_CHARS = 150
_COOKING_TRANSCRIPT_MIN_CHARS = _CONCEPT_TEXT_MAX_CHARS
_COOKING_TRANSCRIPT_MARKERS = (
    "steak",
    "season",
    "pepper",
    "salt",
    "sear",
    "pan",
    "oil",
    "garlic",
    "butter",
    "baste",
    "render",
    "rest",
    "slice",
    "cook",
    "turn",
    "сковород",
    "масл",
    "чеснок",
    "перец",
    "соль",
    "обжар",
    "готов",
    "нареж",
)
_COOKING_TRANSCRIPT_STEP_PATTERNS = (
    (r"(?:take|taking).{0,60}(?:out|from).{0,40}fridge|достан.{0,40}холодильник", "Take the steaks out of the fridge about 20 minutes before cooking."),
    (r"season|pepper|salt|приправ|посол|поперч", "Season the steaks generously with salt and coarse pepper."),
    (r"hot pan|pan.{0,40}smok|сковород.{0,40}(?:дым|горяч)", "Heat the pan until it just starts smoking, then add a little oil."),
    (r"lay.{0,50}steak|sear|обжар", "Lay the steaks away from you and sear them hard instead of boiling them."),
    (r"turn.{0,40}(?:minute|30 seconds|over)|перевер", "Turn the steaks regularly so they cook evenly and build color."),
    (r"fat.{0,60}render|render.{0,60}fat|жир.{0,40}вытоп", "Render the fat edge by holding it against the pan and tilting hot oil toward it."),
    (r"garlic|чеснок", "Add lightly crushed garlic for aroma."),
    (r"butter|baste|полива|сливоч", "Add butter, tilt the pan, and baste the steaks with the foaming butter and aromatics."),
    (r"rest|дай.{0,30}отдох", "Take the steaks off the heat and let them rest."),
    (r"slice|нареж", "Slice and serve rare to medium-rare."),
)

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


def _looks_like_cooking_transcript(text: str) -> bool:
    """Распознает кулинарный транскрипт длиннее короткого концепта."""
    low = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if len(low) <= _COOKING_TRANSCRIPT_MIN_CHARS:
        return False
    marker_hits = {marker for marker in _COOKING_TRANSCRIPT_MARKERS if marker in low}
    return len(marker_hits) >= 6


def _extract_cooking_transcript_steps(text: str) -> list[str]:
    """Собирает 4-6 канонических шагов из сплошной речи о готовке."""
    low = re.sub(r"\s+", " ", (text or "").lower()).strip()
    steps: list[str] = []
    for pattern, step in _COOKING_TRANSCRIPT_STEP_PATTERNS:
        if re.search(pattern, low, flags=re.IGNORECASE):
            steps.append(step)
    return _normalize_user_recipe_steps(steps)


def _split_long_recipe_text_into_steps(text: str) -> list[str]:
    """Грубо режет длинный пользовательский рецепт на фрагменты для fastpath-сессии."""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return []
    parts = [
        part.strip(" .;:-")
        for part in re.split(r"(?:[.!?]+|\n+)\s*", cleaned)
        if part.strip(" .;:-")
    ]
    if len(parts) < 2:
        parts = [
            part.strip(" .;:-")
            for part in re.split(r"\s{2,}|;\s*", cleaned)
            if part.strip(" .;:-")
        ]
    if not parts:
        parts = [cleaned]
    return _normalize_user_recipe_steps(parts)


def _try_parse_direct_recipe_from_user_text(text: str) -> list[str] | None:
    """
    Если в сообщении явно распознаны шаги рецепта (минимум 2), возвращает 4–6 шагов.
    Текст длиннее короткого концепта считаем готовым рецептом даже без нумерации.
    Иначе None — идём в обычный flow с концептами.
    """
    raw = normalize_recipe_steps(extract_recipe_steps_text(text))
    if len(raw) < 2:
        low = re.sub(r"\s+", " ", (text or "").lower()).strip()
        if len(low) > _CONCEPT_TEXT_MAX_CHARS:
            normalized = (
                _extract_cooking_transcript_steps(text)
                if _looks_like_cooking_transcript(text)
                else _split_long_recipe_text_into_steps(text)
            )
        else:
            return None
    else:
        if _is_obvious_placeholder_recipe(raw):
            return None
        normalized = _normalize_user_recipe_steps(raw)
    if not normalized or _is_obvious_placeholder_recipe(normalized):
        return None
    return normalized
