"""Durable timestamps tied to the exact approved narration audio."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def valid_alignment(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    chars = value.get("characters")
    starts = value.get("character_start_times_seconds")
    ends = value.get("character_end_times_seconds")
    if not all(isinstance(items, list) for items in (chars, starts, ends)):
        return False
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        return False
    previous = 0.0
    try:
        for char, start, end in zip(chars, starts, ends, strict=True):
            start, end = float(start), float(end)
            if not isinstance(char, str) or not char or not math.isfinite(start) or not math.isfinite(end):
                return False
            if start < previous or end < start:
                return False
            previous = start
    except (TypeError, ValueError, OverflowError):
        return False
    return float(ends[-1]) > 0


def save_story_audio_metadata(audio_path: str | Path, metadata: dict[str, Any]) -> None:
    path = Path(audio_path)
    payload = {**metadata, "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    target = path.with_suffix(".alignment.json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def load_story_audio_metadata(audio_path: str | Path) -> dict[str, Any]:
    path = Path(audio_path)
    try:
        payload = json.loads(path.with_suffix(".alignment.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("audio_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            return {}
        return payload
    except (OSError, ValueError):
        return {}


def history_alignment(value: Any) -> dict[str, Any]:
    """Normalize history responses whose end array contains character durations.

    Some archived ElevenLabs responses use the end-time field for durations.
    Accept that representation only when every duration agrees with the next
    character's absolute start; never guess from a single invalid timestamp.
    """
    if valid_alignment(value):
        return value
    if not isinstance(value, dict):
        return {}
    chars = value.get("characters")
    starts = value.get("character_start_times_seconds")
    durations = value.get("character_end_times_seconds")
    if not all(isinstance(items, list) for items in (chars, starts, durations)):
        return {}
    if not chars or len(chars) != len(starts) or len(chars) != len(durations):
        return {}
    try:
        ends = [round(float(start) + float(duration), 6) for start, duration in zip(starts, durations, strict=True)]
        if any(abs(end - float(next_start)) > 0.003 for end, next_start in zip(ends[:-1], starts[1:], strict=True)):
            return {}
        converted = {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}
        return converted if valid_alignment(converted) else {}
    except (TypeError, ValueError, OverflowError):
        return {}
