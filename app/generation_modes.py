"""Generation mode helpers shared by interactive and scheduled flows."""

from __future__ import annotations

from typing import Any

GENERATION_MODE_SESSION_KEY = "generation_mode"
GENERATION_MODE_NORMAL = "normal"
GENERATION_MODE_SILENCE = "silence"

SYNTAX_PROVIDER_VEO = "veo"
SYNTAX_PROVIDER_KLING = "kling"

SYNTAX_PROVIDER_COMMANDS = {
    SYNTAX_PROVIDER_VEO: "⭕ Veo",
    SYNTAX_PROVIDER_KLING: "📼 Kling",
}


def normalize_generation_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {GENERATION_MODE_SILENCE, "silent", "тихий", "без речи", "без озвучки"}:
        return GENERATION_MODE_SILENCE
    return GENERATION_MODE_NORMAL


def is_silence_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get(GENERATION_MODE_SESSION_KEY)
    return normalize_generation_mode(value) == GENERATION_MODE_SILENCE


def target_languages_for_generation_mode(value: Any, default: list[str]) -> list[str]:
    if is_silence_generation(value):
        return ["ru"]
    return list(default or ["ru"])


def syntax_provider_for_generation_mode(value: Any) -> str:
    return SYNTAX_PROVIDER_KLING if is_silence_generation(value) else SYNTAX_PROVIDER_VEO
