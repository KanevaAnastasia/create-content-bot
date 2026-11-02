"""Подстановка реплики в video prompt без вызова Gemini."""

from __future__ import annotations

from app.scene_speech_shorten import (
    _has_final_subscribe_cta,
    _replace_speech_in_video_prompt_if_substring,
    _video_prompt_has_exact_speech_after_marker,
    shorten_scene_speech_and_sync_video_prompt_sync,
)
from app.pipeline_llm_steps import FINAL_SCENE_SUBSCRIBE_CTA_VEO3, RUSSIAN_YO_RULE


def test_replace_speech_in_video_prompt_if_substring() -> None:
    vp = "shot one CHARACTERS SPEAKS: old line here"
    out = _replace_speech_in_video_prompt_if_substring(vp, "new", "old line here")
    assert out is not None
    assert "CHARACTERS SPEAKS: new" in out
    assert "old line" not in out


def test_video_prompt_has_exact_speech_after_marker() -> None:
    assert _video_prompt_has_exact_speech_after_marker("x CHARACTERS SPEAKS: hello there", "hello there")
    assert _video_prompt_has_exact_speech_after_marker("CHARACTERS SPEAKS: ab", "ab")
    assert not _video_prompt_has_exact_speech_after_marker("no marker", "ab")


def test_has_final_subscribe_cta_accepts_veo3_form() -> None:
    assert _has_final_subscribe_cta(f"всё готОво [пауза] {FINAL_SCENE_SUBSCRIBE_CTA_VEO3}")


def test_shorten_preserves_final_subscribe_cta(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_gemini(prompt: str, **_kwargs) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            text = "всё готОво"
        else:
            text = FINAL_SCENE_SUBSCRIBE_CTA_VEO3
        return '{"text": "%s", "video_prompt": "shot CHARACTERS SPEAKS: %s"}' % (text, text)

    monkeypatch.setattr("app.scene_speech_shorten.gemini_generate_sync", _fake_gemini)
    scene = {
        "text": (
            "это невероятно нежное мороженое прямо как облачко [пауза] "
            f"{FINAL_SCENE_SUBSCRIBE_CTA_VEO3}"
        ),
        "video_prompt": "shot CHARACTERS SPEAKS: old",
    }
    out = shorten_scene_speech_and_sync_video_prompt_sync(scene, duration_sec=6)

    assert out is not None
    assert out["text"] == f"{FINAL_SCENE_SUBSCRIBE_CTA_VEO3}."
    assert out["video_prompt"].endswith(f"{FINAL_SCENE_SUBSCRIBE_CTA_VEO3}.")
    assert "must_keep_final_subscribe_cta" in calls[0]
    assert RUSSIAN_YO_RULE in calls[0]
    assert "моя бабушка" in calls[0]
    assert "бабушка научила" in calls[0]
