"""Helpers for multilingual video pipeline state and prompts."""

from __future__ import annotations

import os
import re

_DEFAULT_TARGET_LANGUAGES = ("ru", "en")
_LANG_CODE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_LANGUAGE_NAME_MAP = {
    "ru": "Russian",
    "en": "English",
}


def normalize_language_code(value: str) -> str:
    """Return normalized language code or empty string for invalid input."""
    code = (value or "").strip().lower().replace("_", "-")
    if not code or not _LANG_CODE_RE.match(code):
        return ""
    return code


def parse_target_languages(raw_value: str | None) -> list[str]:
    """Parse comma-separated language codes with defaults and deduplication."""
    raw = (raw_value or "").strip()
    items = raw.split(",") if raw else list(_DEFAULT_TARGET_LANGUAGES)
    out: list[str] = []
    for item in items:
        code = normalize_language_code(item)
        if code and code not in out:
            out.append(code)
    return out or list(_DEFAULT_TARGET_LANGUAGES)


def target_languages_from_env() -> list[str]:
    """Load target languages for video generation from environment."""
    return parse_target_languages(os.getenv("CONTENT_FABRIC_TARGET_LANGUAGES"))


def language_name(code: str) -> str:
    """Human-readable language name for prompts."""
    normalized = normalize_language_code(code)
    return _LANGUAGE_NAME_MAP.get(normalized, normalized or "unknown")
