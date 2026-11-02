"""
Language-aware helpers for scene speech budgets, phrase plans, and subtitle cues.

`[пауза]` does not count towards the speech budget.
"""

from __future__ import annotations

import re

PAUSE_TOKEN = "[пауза]"
PROMPT_TIMING_MARKER = "speech_timing_plan:"
SPEECH_TERMINAL_PUNCTUATION = ".!?…"
SPEECH_DASH_CHARS_RE = re.compile(r"\s*[\-\u2010-\u2015\u2212]+\s*")
DEFAULT_SPEECH_CHARS_PER_SECOND = 14
SPEECH_CHARS_PER_SECOND_BY_LANG = {
    "ru": 14,
    "en": 11,
}
SPEECH_MAX_CHARS_BY_LANG_DURATION = {
    ("ru", 8): 125,
}


def normalize_speech_phrase_for_voice(text: str) -> str:
    """Normalize spoken phrases while preserving punctuation."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = SPEECH_DASH_CHARS_RE.sub(" ", raw)
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return ""
    closing_chars = "\"'»”)]}"
    tail = normalized.rstrip(f" \t\r\n{closing_chars}")
    if tail and tail[-1] not in SPEECH_TERMINAL_PUNCTUATION:
        closing_suffix = normalized[len(tail) :]
        normalized = f"{tail}.{closing_suffix}"
    return normalized


def speech_chars_excluding_pauses(text: str) -> int:
    """
    Длина речи в символах без учёта токенов ``[пауза]`` (регистрозависимо, как в сценарии).

    Пробелы вокруг удалённых токенов схлопываются по краям фрагментов; множественные пробелы
    между словами сохраняются как один пробел только на стыках удаления токена — упрощённо:
    удаляем все вхождения ``[пауза]`` и считаем ``len`` остатка.
    """
    raw = (text or "").strip()
    if not raw:
        return 0
    compact = raw.replace(PAUSE_TOKEN, "")
    return len(compact.strip())


def speech_chars_per_second(language: str = "ru") -> int:
    """Return language-specific speech-density budget."""
    code = str(language or "ru").strip().lower() or "ru"
    return int(SPEECH_CHARS_PER_SECOND_BY_LANG.get(code, DEFAULT_SPEECH_CHARS_PER_SECOND))


def max_speech_chars_for_duration(duration_sec: int, language: str = "ru") -> int:
    """Максимум символов речи (без пауз) для ``duration_sec`` и языка."""
    try:
        sec = int(duration_sec)
    except (TypeError, ValueError):
        sec = 6
    code = str(language or "ru").strip().lower() or "ru"
    override = SPEECH_MAX_CHARS_BY_LANG_DURATION.get((code, sec))
    if override is not None:
        return int(override)
    return max(1, sec * speech_chars_per_second(code))


def speech_budget_for_duration(duration_sec: int, language: str = "ru") -> dict[str, int | str]:
    """Metadata for scene speech-budget checks."""
    return {
        "language": str(language or "ru").strip().lower() or "ru",
        "duration_sec": int(duration_sec or 6),
        "chars_per_second": speech_chars_per_second(language),
        "max_chars": max_speech_chars_for_duration(duration_sec, language),
    }


def build_default_speech_plan(text: str, language: str = "ru") -> list[dict[str, str | int]]:
    """Split a full phrase into 1-3 natural chunks for later subtitle/audio timing."""
    raw = str(text or "").strip()
    if not raw:
        return []
    normalized = SPEECH_DASH_CHARS_RE.sub(" ", raw)
    normalized = re.sub(r"\[\s*пауза\s*\]", f" {PAUSE_TOKEN} ", normalized, flags=re.IGNORECASE)
    parts = [chunk.strip(" ,;:!?-.") for chunk in normalized.split(PAUSE_TOKEN)]
    chunks = [part for part in parts if part]
    if not chunks:
        chunks = [raw]
    if len(chunks) > 3:
        merged: list[str] = []
        step = max(1, len(chunks) // 3)
        for idx in range(0, len(chunks), step):
            merged.append(" ".join(chunks[idx : idx + step]).strip())
            if len(merged) == 2:
                tail = " ".join(chunks[idx + step :]).strip()
                if tail:
                    merged.append(tail)
                break
        chunks = [item for item in merged if item]
    return [
        {
            "text": chunk,
            "order": index + 1,
            "intent": "speech",
            "language": str(language or "ru").strip().lower() or "ru",
        }
        for index, chunk in enumerate(chunks[:3])
    ]


def build_speech_cues_from_plan(
    speech_plan: list[dict[str, str | int]] | None,
    *,
    duration_sec: float,
) -> list[dict[str, float | str]]:
    """Distribute a speech plan across a real clip duration."""
    if not isinstance(speech_plan, list):
        return []
    valid = [item for item in speech_plan if isinstance(item, dict) and str(item.get("text") or "").strip()]
    if not valid:
        return []
    safe_duration = max(0.0, float(duration_sec or 0.0))
    if safe_duration <= 0:
        return [
            {
                "text": str(item.get("text") or "").strip(),
                "start_sec": 0.0,
                "end_sec": 0.0,
            }
            for item in valid
        ]
    weights = [max(1, speech_chars_excluding_pauses(str(item.get("text") or ""))) for item in valid]
    total_weight = sum(weights) or len(valid)
    cursor = 0.0
    cues: list[dict[str, float | str]] = []
    for index, item in enumerate(valid):
        fraction = weights[index] / float(total_weight)
        next_cursor = safe_duration if index == len(valid) - 1 else cursor + (safe_duration * fraction)
        cues.append(
            {
                "text": str(item.get("text") or "").strip(),
                "start_sec": round(cursor, 3),
                "end_sec": round(min(safe_duration, next_cursor), 3),
            }
        )
        cursor = next_cursor
    return cues


def build_prompt_timing_hints(
    speech_plan: list[dict[str, str | int]] | None,
    *,
    duration_sec: float,
) -> str:
    """Render a short timing block suitable for a video prompt."""
    cues = build_speech_cues_from_plan(speech_plan, duration_sec=duration_sec)
    if not cues:
        return ""
    lines = [PROMPT_TIMING_MARKER]
    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        lines.append(
            f"- {float(cue.get('start_sec') or 0.0):.2f}-{float(cue.get('end_sec') or 0.0):.2f}s: {text}"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def append_prompt_timing_hints(
    prompt: str,
    speech_plan: list[dict[str, str | int]] | None,
    *,
    duration_sec: float,
) -> str:
    """Append or replace the stable timing block in a prompt."""
    text = str(prompt or "").strip()
    if not text:
        return text
    text = re.sub(
        rf"(?:\n)?{re.escape(PROMPT_TIMING_MARKER)}\s*(?:\n\s*-\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?s:\s*.*)*\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    timing_block = build_prompt_timing_hints(speech_plan, duration_sec=duration_sec)
    if not timing_block:
        return text
    return f"{text}\n{timing_block}".strip()
