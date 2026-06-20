"""Лимит реплики сцены: символы без токенов [пауза]."""

from __future__ import annotations

from app.scene_speech_budget import (
    PAUSE_TOKEN,
    append_prompt_timing_hints,
    build_default_speech_plan,
    build_speech_cues_from_plan,
    max_speech_chars_for_duration,
    normalize_speech_phrase_for_voice,
    speech_budget_for_duration,
    speech_chars_excluding_pauses,
)


def test_speech_chars_excluding_pauses_strips_token() -> None:
    assert speech_chars_excluding_pauses("") == 0
    assert speech_chars_excluding_pauses(f"а{PAUSE_TOKEN}б") == 2
    assert speech_chars_excluding_pauses(f"{PAUSE_TOKEN}{PAUSE_TOKEN}xy") == 2


def test_normalize_speech_phrase_for_voice_removes_dashes() -> None:
    assert normalize_speech_phrase_for_voice("вот так — и готово") == "вот так и готово"
    assert normalize_speech_phrase_for_voice("по-домашнему – мягко") == "по домашнему мягко"


def test_max_speech_chars_for_duration() -> None:
    assert max_speech_chars_for_duration(8) == 112
    assert max_speech_chars_for_duration(6) == 84
    assert max_speech_chars_for_duration(6, "en") == 66


def test_speech_budget_and_default_plan_are_language_aware() -> None:
    budget = speech_budget_for_duration(6, "en")
    assert budget["language"] == "en"
    assert budget["chars_per_second"] == 11
    plan = build_default_speech_plan(f"Look here {PAUSE_TOKEN} this stays warm", "en")
    assert [item["text"] for item in plan] == ["Look here", "this stays warm"]


def test_build_speech_cues_from_plan_distributes_real_duration() -> None:
    plan = build_default_speech_plan("First chunk [пауза] second chunk", "en")
    cues = build_speech_cues_from_plan(plan, duration_sec=5.0)
    assert len(cues) == 2
    assert cues[0]["start_sec"] == 0.0
    assert 0.0 < cues[0]["end_sec"] < 5.0
    assert cues[-1]["end_sec"] == 5.0


def test_append_prompt_timing_hints_replaces_stale_block() -> None:
    prompt = (
        "shot\n"
        "CHARACTERS SPEAKS: новая фраза\n"
        "speech_timing_plan:\n"
        "- 0.00-6.00s: старая фраза"
    )
    plan = build_default_speech_plan("новая фраза", "ru")

    out = append_prompt_timing_hints(prompt, plan, duration_sec=6.0)

    assert "CHARACTERS SPEAKS: новая фраза" in out
    assert "- 0.00-6.00s: новая фраза" in out
    assert "старая фраза" not in out
