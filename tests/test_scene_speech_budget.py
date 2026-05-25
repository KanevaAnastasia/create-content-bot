"""Лимит реплики сцены: символы без токенов [пауза]."""

from __future__ import annotations

from app.scene_speech_budget import (
    PAUSE_TOKEN,
    max_speech_chars_for_duration,
    speech_chars_excluding_pauses,
)


def test_speech_chars_excluding_pauses_strips_token() -> None:
    assert speech_chars_excluding_pauses("") == 0
    assert speech_chars_excluding_pauses(f"а{PAUSE_TOKEN}б") == 2
    assert speech_chars_excluding_pauses(f"{PAUSE_TOKEN}{PAUSE_TOKEN}xy") == 2


def test_max_speech_chars_for_duration() -> None:
    assert max_speech_chars_for_duration(8) == 112
    assert max_speech_chars_for_duration(6) == 84
