"""Generation mode helpers shared by interactive and scheduled flows."""

from __future__ import annotations

from typing import Any

GENERATION_MODE_SESSION_KEY = "generation_mode"
GENERATION_MODE_NORMAL = "normal"
GENERATION_MODE_SILENCE = "silence"
GENERATION_MODE_LONG = "long"
GENERATION_MODE_COPY = "copy"
GENERATION_MODE_STORY = "story"
GENERATION_MODE_VISUALIZATION = "visualization"
GENERATION_MODE_ADVERTISING = "advertising"

SYNTAX_PROVIDER_VEO = "veo"
SYNTAX_PROVIDER_KLING = "kling"

SYNTAX_PROVIDER_COMMANDS = {
    SYNTAX_PROVIDER_VEO: "Veo",
    SYNTAX_PROVIDER_KLING: "Kling",
}


def normalize_generation_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {GENERATION_MODE_SILENCE, "silent", "тихий", "без речи", "без озвучки"}:
        return GENERATION_MODE_SILENCE
    if raw in {GENERATION_MODE_LONG, "длинный", "долгий", "long horizontal", "long_horizontal"}:
        return GENERATION_MODE_LONG
    if raw in {GENERATION_MODE_COPY, "копия", "copy", "copy video", "video copy"}:
        return GENERATION_MODE_COPY
    if raw in {GENERATION_MODE_STORY, "история", "истории", "story", "narrative"}:
        return GENERATION_MODE_STORY
    if raw in {
        GENERATION_MODE_VISUALIZATION,
        "визуализация",
        "визуализировать",
        "visualisation",
        "visualize",
    }:
        return GENERATION_MODE_VISUALIZATION
    if raw in {GENERATION_MODE_ADVERTISING, "реклама", "рекламный", "advert", "advertising", "ad"}:
        return GENERATION_MODE_ADVERTISING
    return GENERATION_MODE_NORMAL


def is_silence_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_SILENCE


def is_long_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_LONG


def is_copy_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_COPY


def is_story_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) in {GENERATION_MODE_STORY, GENERATION_MODE_VISUALIZATION}


def is_visualization_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_VISUALIZATION


def is_advertising_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_ADVERTISING


def target_languages_for_generation_mode(value: Any, default: list[str]) -> list[str]:
    if (
        is_silence_generation(value)
        or is_long_generation(value)
        or is_copy_generation(value)
        or is_story_generation(value)
        or is_advertising_generation(value)
    ):
        return ["ru"]
    return list(default or ["ru"])


def aspect_ratio_for_generation_mode(value: Any) -> str:
    return "16:9" if is_long_generation(value) else "9:16"


def is_horizontal_generation(value: Any) -> bool:
    return aspect_ratio_for_generation_mode(value) == "16:9"


def syntax_provider_for_generation_mode(value: Any) -> str:
    return SYNTAX_PROVIDER_KLING if is_silence_generation(value) or is_story_generation(value) else SYNTAX_PROVIDER_VEO


def syntax_provider_for_scene_generation(scene: dict[str, Any] | None, value: Any) -> str:
    model = str((scene or {}).get("model") or "").strip().lower()
    if model.startswith("kling"):
        return SYNTAX_PROVIDER_KLING
    if model.startswith("veo"):
        return SYNTAX_PROVIDER_VEO
    return syntax_provider_for_generation_mode(value)
