"""Утилиты старта пайплайна: парсинг команды и сборка session-объектов."""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import unquote

import requests

from ..constants import STATE_WAITING_CONCEPT, STATE_WAITING_RECIPE_APPROVAL
from ..multilingual import target_languages_from_env

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_RECIPE_SECTION_MARKERS = (
    "ингредиенты",
    "продукты",
    "продукты для рецепта",
    "следует взять",
    "понадобится",
    "нам понадобится",
    "на 5-6 порций",
    "приготовление",
    "способ приготовления",
    "пошаговый фото рецепт",
    "как приготовить",
    "ingredients",
    "recipe ingredients",
    "instructions",
    "directions",
    "method",
)
_RECIPE_STRONG_MARKERS = (
    "ингредиенты",
    "продукты",
    "продукты для рецепта",
    "следует взять",
    "понадобится",
    "нам понадобится",
    "на 5-6 порций",
    "пошаговый фото рецепт",
    "приготовление",
    "способ приготовления",
)


def extract_recipe_from_command_text(raw_text: str) -> str:
    """Извлекает текст рецепта из команды вида ``/generate ...`` или ``/quick ...``."""
    parts = (raw_text or "").split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def extract_first_http_url(text: str) -> str | None:
    """Возвращает первую HTTP(S)-ссылку из текста или ``None``."""
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,!?;:")


def _html_to_plain_text(html_text: str) -> str:
    """Преобразует HTML в обычный текст для последующего извлечения рецепта."""
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", html_text or "")
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?is)<!--.*?-->", " ", cleaned)
    cleaned = re.sub(r"(?is)<(br|/p|/li|/h[1-6]|/div|/tr|/td|/table|/section|/article)>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    plain = html.unescape(cleaned)
    plain = plain.replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in plain.splitlines()]
    compact = [line for line in lines if line]
    return "\n".join(compact)


def _decode_response_text(response: Any) -> str:
    """Возвращает HTML с fallback для сайтов в cp1251/windows-1251."""
    content = getattr(response, "content", None)
    if isinstance(content, bytes) and content:
        enc = str(getattr(response, "encoding", "") or "").strip()
        candidates = [enc] if enc else []
        apparent = str(getattr(response, "apparent_encoding", "") or "").strip()
        candidates.extend([apparent, "utf-8", "cp1251", "windows-1251"])
        seen: set[str] = set()
        for candidate in candidates:
            c = candidate.lower()
            if not c or c in seen:
                continue
            seen.add(c)
            try:
                return content.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue
        return content.decode("utf-8", errors="replace")
    return str(getattr(response, "text", "") or "")


def _recipe_instruction_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("text", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = item.get("itemListElement")
        if isinstance(nested, list):
            return "\n".join(_recipe_instruction_text(x) for x in nested if _recipe_instruction_text(x)).strip()
    return ""


def _walk_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_json_objects(item)
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _walk_json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_objects(item)


def _extract_recipe_from_json_ld(html_text: str) -> str:
    """Достаёт Recipe из JSON-LD, если сайт его отдал."""
    chunks: list[str] = []
    for raw in re.findall(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text or ""):
        raw = html.unescape(raw).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _walk_json_objects(data):
            typ = obj.get("@type")
            types = [str(x).lower() for x in typ] if isinstance(typ, list) else [str(typ).lower()]
            if "recipe" not in types:
                continue
            parts: list[str] = []
            name = str(obj.get("name") or "").strip()
            if name:
                parts.append(name)
            desc = str(obj.get("description") or "").strip()
            if desc:
                parts.append(desc)
            ingredients = obj.get("recipeIngredient") or obj.get("ingredients")
            if isinstance(ingredients, list) and ingredients:
                parts.append("Ингредиенты:\n" + "\n".join(str(x).strip() for x in ingredients if str(x).strip()))
            instructions = obj.get("recipeInstructions") or obj.get("instructions")
            if isinstance(instructions, list) and instructions:
                steps = [_recipe_instruction_text(x) for x in instructions]
                steps = [x for x in steps if x]
                if steps:
                    parts.append("Приготовление:\n" + "\n".join(steps))
            elif isinstance(instructions, str) and instructions.strip():
                parts.append("Приготовление:\n" + instructions.strip())
            text = "\n\n".join(parts).strip()
            if len(text) > len("\n".join(chunks)):
                chunks = parts
    return "\n\n".join(chunks).strip()


def _recipe_like_score(text: str) -> int:
    low = text.lower()
    score = 0
    for marker in _RECIPE_STRONG_MARKERS:
        if marker in low:
            score += 20
    score += min(40, len(re.findall(r"(?m)^\s*\d+[\).]\s+\S+", text)) * 6)
    score += min(24, len(re.findall(r"\b\d+\s*(?:г|гр|кг|мл|л|ст\.?\s*л|ч\.?\s*л|штук|шт)\b", low)) * 4)
    score += min(20, len(re.findall(r"\b(?:нарез|добав|перемеш|вар|жар|довод|вливаем|клад|сол|готов)\w*", low)) * 2)
    return score


def _recipe_window_start(text: str, marker_pos: int, *, marker: str, default_back: int = 350) -> int:
    """Выбирает аккуратное начало окна, не разрезая предыдущий абзац посередине."""
    marker_low = marker.lower()
    if marker_low in _RECIPE_STRONG_MARKERS:
        nearby = text[max(0, marker_pos - 550) : marker_pos]
        for cue in ("Итак", "итак", "Продукты", "продукты"):
            cue_pos = nearby.rfind(cue)
            if cue_pos >= 0:
                return max(0, marker_pos - 550) + cue_pos
        line_start = text.rfind("\n", 0, marker_pos)
        if line_start >= 0 and marker_pos - line_start <= 350:
            return line_start + 1
        return max(0, marker_pos - 160)
    return max(0, marker_pos - default_back)


def _best_recipe_window(text: str, *, max_chars: int) -> str:
    """Выбирает наиболее вероятный фрагмент рецепта по ключевым маркерам."""
    if not text:
        return ""
    lowered = text.lower()
    candidates: list[tuple[int, int, str]] = []
    for marker in _RECIPE_SECTION_MARKERS:
        start_search = 0
        while True:
            pos = lowered.find(marker, start_search)
            if pos < 0:
                break
            start = _recipe_window_start(text, pos, marker=marker, default_back=450)
            window = text[start : start + max_chars]
            candidates.append((_recipe_like_score(window), -start, window))
            start_search = pos + len(marker)
    for match in re.finditer(r"(?m)^\s*\d+[\).]\s+\S+", text):
        start = _recipe_window_start(text, match.start(), marker="следует взять", default_back=250)
        window = text[start : start + max_chars]
        candidates.append((_recipe_like_score(window), -start, window))
    if not candidates:
        return text[:max_chars]
    candidates.sort(reverse=True)
    return candidates[0][2]


def _recipe_text_has_core_sections(text: str) -> bool:
    low = (text or "").lower()
    has_ingredients = any(x in low for x in ("ингредиенты", "продукты", "следует взять", "понадобится"))
    has_cooking = any(x in low for x in ("приготов", "нарез", "добав", "вар", "жар", "перемеш", "довод"))
    return has_ingredients and has_cooking


def fetch_recipe_text_from_url(
    url: str,
    *,
    timeout_sec: int = 20,
    max_chars: int = 3500,
    http_get: Any = requests.get,
) -> str:
    """Скачивает страницу рецепта и возвращает очищенный текст для согласования с пользователем."""
    if not (url or "").strip():
        raise ValueError("Пустая ссылка.")
    try:
        response = http_get(
            url.strip(),
            timeout=timeout_sec,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError("Не удалось загрузить страницу рецепта.") from exc
    html_text = _decode_response_text(response)
    json_ld_recipe = _extract_recipe_from_json_ld(html_text)
    if len(json_ld_recipe) >= 80 and _recipe_text_has_core_sections(json_ld_recipe):
        return unquote(json_ld_recipe[:max_chars]).strip()
    plain = _html_to_plain_text(html_text)
    if not plain.strip():
        raise RuntimeError("По ссылке не найден текст рецепта.")
    recipe_text = _best_recipe_window(plain, max_chars=max_chars).strip()
    recipe_text = unquote(recipe_text)
    if len(recipe_text) < 80:
        raise RuntimeError("По ссылке найдено слишком мало текста рецепта.")
    return recipe_text


def build_direct_recipe_session(recipe: str, parsed_steps: list[str], *, prefer_vertex_api: bool) -> dict[str, Any]:
    """Собирает session для fastpath, когда пользователь сразу прислал шаги рецепта."""
    return {
        "state": STATE_WAITING_RECIPE_APPROVAL,
        "target_languages": target_languages_from_env(),
        "user_input": recipe,
        "prefer_vertex_api": prefer_vertex_api,
        "user_feedback": None,
        "concepts": None,
        "selected_concept": "Рецепт пользователя",
        "selected_concept_name": "Рецепт пользователя",
        "recipe_full_text": recipe.strip(),
        "precheck_recipe_steps": parsed_steps,
        "recipe_steps": None,
        "scenes_plan": None,
        "scenes_text": None,
        "frame_prompts": None,
        "frame_prompts_text": None,
        "production": None,
    }


def build_concepts_session(recipe: str, concepts: list[dict[str, Any]], *, prefer_vertex_api: bool) -> dict[str, Any]:
    """Собирает session после успешной генерации списка концептов."""
    return {
        "state": STATE_WAITING_CONCEPT,
        "target_languages": target_languages_from_env(),
        "user_input": recipe,
        "prefer_vertex_api": prefer_vertex_api,
        "user_feedback": None,
        "concepts": concepts,
        "selected_concept": None,
        "selected_concept_name": None,
        "recipe_full_text": None,
        "precheck_recipe_steps": None,
        "recipe_steps": None,
        "scenes_plan": None,
        "scenes_text": None,
        "frame_prompts": None,
        "frame_prompts_text": None,
        "production": None,
    }
