"""Protect the current scene plan during explicitly scoped revisions."""
from copy import deepcopy
import re


def exclusive_scene_number(feedback: str) -> int | None:
    """Only a clear single-scene restriction is safe to enforce mechanically."""
    text = re.sub(r"^(?:исправь|добавь)\s*:\s*", "", feedback.strip(), flags=re.I)
    match = re.match(
        r"(?:(?:вс[её]\s+(?:ок|хорошо|подходит))\s*[,.:;]?\s*)?"
        r"только\s+в\s+сцене\s+(\d+)\b(.*)$", text, flags=re.I | re.S,
    )
    if not match:
        return None
    # Do not silently drop a second target or an explicit structural request.
    rest = match[2]
    if re.match(r"\s*(?:[,/\-–]\s*\d|и\s+\d)", rest) or re.search(
        r"\bсцен\w*\b|\b(?:перв\w*|последн\w*)\s+(?:фраз\w*|кадр\w*)", rest, re.I,
    ):
        return None
    return int(match[1])


def preserve_other_scenes(previous: list[dict], revised: list[dict], number: int) -> list[dict]:
    expected = [item.get("scene") for item in previous]
    actual = [item.get("scene") for item in revised]
    if number not in expected or len(set(expected)) != len(expected) or actual != expected:
        raise ValueError("Модель изменила структуру сцен при точечной правке. Предыдущий план сохранён.")
    return [deepcopy(new if old.get("scene") == number else old)
            for old, new in zip(previous, revised, strict=True)]
