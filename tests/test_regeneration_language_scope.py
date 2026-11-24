import asyncio
from types import SimpleNamespace
from app.handlers import handlers_state_media
from app.handlers import _encode_b64

def test_ru_regeneration_does_not_validate_long_english(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _gen_ok(*args, **kwargs):
        calls.append(kwargs["prompt"])
        return b"new-ru-video"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)
            return None

        async def reply_video(self, *args, **kwargs):
            return SimpleNamespace(message_id=123)

    scene = {
        "model": "veo3_fast",
        "video_prompt_by_lang": {
            "ru": "lang:ru updated prompt CHARACTERS SPEAKS: новая фраза",
            "en": "lang:en updated prompt CHARACTERS SPEAKS: new phrase",
        },
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "duration_sec": 6,
        "text": "",
        "video_results_by_lang": {
            "ru": {"video_b64": _encode_b64(b"old-ru-video")},
            "en": {"video_b64": _encode_b64(b"old-en-video")},
        },
        "video_approvals_by_lang": {"ru": True, "en": True},
        handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY: {"ru": True, "en": True},
    }
    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    scene["speech_by_lang"] = {"en": "Long English phrase. " * 30}
    original_en = scene["speech_by_lang"]["en"]
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=1)),
            context=None,
            chat_id=1,
            session=session,
            text="ПЕРЕГЕНЕРИРОВАТЬ ru",
        )
    )

    assert out is True
    assert len(calls) == 1
    assert calls[0].startswith("lang:ru")
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"new-ru-video")
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"old-en-video")
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert scene["video_approvals_by_lang"]["en"] is True
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY] == {"en": True}
    assert any("Генерирую видео для языка: ru" in message for message in messages)
    assert not any("Генерируем видео параллельно для языков: ru, en" in message for message in messages)
    assert any("Промпт перед генерацией видео" in message and "язык ru" in message for message in messages)


    assert scene["speech_by_lang"]["en"] == original_en
    assert handlers_state_media.SCENE_SPEECH_PENDING_KEY not in session


def test_language_rule_is_not_dialogue():
    phrase = "Let's make potato boats."
    rule = " all spoken phrases and voiceover are in English language only."
    for prompt in ["CHARACTERS SPEAKS: " + phrase + rule,
                   "character_dialogue_script: GRANDMA: " + phrase + rule]:
        assert handlers_state_media._speech_from_video_prompt(prompt) == phrase


def test_scope_survives_approval_steps():
    key = handlers_state_media.SCENE_SPEECH_PENDING_KEY
    session = {key: {"scene_idx": 0, "regenerate_language": "ru"}}
    handlers_state_media._set_scene_speech_pending(session, scene_idx=0, duration_sec=8, stage="approval")
    assert session[key]["regenerate_language"] == "ru"
    handlers_state_media._set_scene_speech_pending(session, scene_idx=1, duration_sec=8, stage="approval")
    assert "regenerate_language" not in session[key]
