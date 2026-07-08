"""Helpers for scheduled generation comments."""

from __future__ import annotations

import re


_EVERY_COMMENT_RE = re.compile(r"^\s*every(?:\s+|[.:\-–—,]\s*)(?P<body>.*)\s*$", re.IGNORECASE | re.DOTALL)


def split_scheduled_generation_comment(value: str | None) -> tuple[str, str]:
    """Return (generation_comment, every_start_prompt_comment)."""
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    match = _EVERY_COMMENT_RE.match(raw)
    if not match:
        return raw, ""
    body = str(match.group("body") or "").strip()
    return body, body


def normalize_scheduled_generation_comment(value: str | None) -> str:
    return split_scheduled_generation_comment(value)[0]


def scheduled_generation_every_start_prompt_comment(value: str | None) -> str:
    return split_scheduled_generation_comment(value)[1]
