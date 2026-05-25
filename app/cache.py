"""
In-memory кэш ответов LLM для снижения числа вызовов и rate limit.

Ключи — SHA256 от канонического JSON сериализуемых аргументов.
При смене фидбека/шагов ключ меняется — кэш промахивается автоматически.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_concepts: dict[str, Any] = {}
_recipe_steps: dict[str, Any] = {}
_recipe_full: dict[str, Any] = {}
_derived_recipe_steps: dict[str, Any] = {}
_scenes_plan: dict[str, Any] = {}


def _key(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_concepts(user_input: str, feedback: str | None, previous: list | None) -> Any | None:
    return _concepts.get(_key(user_input.strip(), feedback or "", previous or []))


def set_concepts(user_input: str, feedback: str | None, previous: list | None, value: Any) -> None:
    _concepts[_key(user_input.strip(), feedback or "", previous or [])] = value


def get_recipe_steps(concept: str, feedback: str | None, previous_steps: list[str] | None) -> Any | None:
    return _recipe_steps.get(_key(concept.strip(), feedback or "", previous_steps or []))


def set_recipe_steps(concept: str, feedback: str | None, previous_steps: list[str] | None, value: Any) -> None:
    _recipe_steps[_key(concept.strip(), feedback or "", previous_steps or [])] = value


def get_recipe_full(concept: str, feedback: str | None, previous_text: str | None) -> Any | None:
    return _recipe_full.get(_key(concept.strip(), feedback or "", (previous_text or "").strip()))


def set_recipe_full(concept: str, feedback: str | None, previous_text: str | None, value: Any) -> None:
    _recipe_full[_key(concept.strip(), feedback or "", (previous_text or "").strip())] = value


def get_derived_recipe_steps(full_recipe_text: str, concept: str) -> Any | None:
    return _derived_recipe_steps.get(_key(full_recipe_text.strip(), concept.strip()))


def set_derived_recipe_steps(full_recipe_text: str, concept: str, value: Any) -> None:
    _derived_recipe_steps[_key(full_recipe_text.strip(), concept.strip())] = value


def get_scenes_plan(
    concept: str,
    full_recipe_text: str,
    feedback: str | None,
) -> Any | None:
    return _scenes_plan.get(
        _key(concept.strip(), (full_recipe_text or "").strip(), (feedback or "").strip())
    )


def set_scenes_plan(
    concept: str,
    full_recipe_text: str,
    feedback: str | None,
    value: Any,
) -> None:
    _scenes_plan[
        _key(concept.strip(), (full_recipe_text or "").strip(), (feedback or "").strip())
    ] = value
