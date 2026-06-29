import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.handlers import (
    _build_concepts_session,
    _build_direct_recipe_session,
    _extract_recipe_from_command_text,
    _extract_prompt_override,
    _extract_format_or_feedback,
    _normalize_veo_duration_sec,
    _parse_video_command_args,
    _decode_b64,
    _end_frame_delta_instruction,
    _end_frame_image_warranted,
    _encode_b64,
    _image_end_scene_prompt,
    _is_video_invalid_image_error,
    _is_video_high_load_error,
    _is_video_model_unavailable_error,
    _is_video_prompt_filtered_error,
    _is_video_policy_error,
    _is_video_safety_error,
    _generate_image_with_lenient_fallback,
    _should_soften_image_prompt_after_error,
    _user_error_message,
    _reference_keys_for_scene,
    _resolve_scene_references,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
)
import app.handlers.handlers_state_media as handlers_state_media
import app.handlers.handlers_state_scene as handlers_state_scene
import app.handlers.handlers_state_start as handlers_state_start
import app.handlers.handlers_media_scene as handlers_media_scene
import app.handlers.handlers_media_core as handlers_media_core
import logging
import app.handlers.handlers_generation_utils as handlers_generation_utils

from app.handlers.handlers_media_core import (
    VideoProviderChoiceRequired,
    _generate_video_bytes_with_high_load_retry,
    _is_syntax_fallback_trigger_error,
    _video_region_candidates,
    _build_video_prompt_rewrite_llm_prompt,
    _regenerate_video_prompt_from_text,
    enforce_video_prompt_voice_requirements,
)
from app.syntax_video_fallback import FallbackResult
from app.llm_utils import GeminiQuotaError
from app.handlers.handlers_media_scene import (
    PIXAR_REFERENCE_STYLE_PHRASE,
    VIDEO_PROMPT_POLICY_FAILURE_THRESHOLD,
    _append_scene_identity_to_image_prompt,
    _extract_prompt_override,
    _append_pixar_reference_style_phrase,
    _loaded_reference_paths,
    _planned_reference_summary,
    _should_suggest_regenerate_start_frame_after_video_prompt_fails,
    _video_policy_prompt_approval_text,
)
from app.handlers.handlers_media_scene import _scene_needs_end_frame_step


def test_extract_recipe_from_command_text():
    assert _extract_recipe_from_command_text("/generate суп с лапшой") == "суп с лапшой"
    assert _extract_recipe_from_command_text("/quick") == ""
    assert _extract_recipe_from_command_text("") == ""


def test_direct_recipe_parser_accepts_cooking_transcript():
    text = (
        "Нужен этот рецепт right the guide to cooking a perfect steak hot pan the secret here "
        "is to make sure that we literally sear the steak and not boil it season it first "
        "beautifully done get some nice large grains of pepper so we got a nice bit of heat "
        "make sure we take them out the fridge 20 minutes before you actually start cooking "
        "them pans just started smoking touch of oil lay the steaks away always away from me "
        "and let the pan do the work turn it over very carefully literally 30 seconds of the "
        "pan you can see the color beautiful that layer of fat on the back of the sirloin you "
        "want to render that down as well tilt the pan put a bit of garlic in a little flavor "
        "the steaks doesn't need to be peeled just lightly crush turning every minute to get "
        "that nice even color little knobs of butter tilt the pan then just baste the steaks "
        "let them rest and off and then from there slice rare going on to medium rare"
    )

    steps = handlers_media_core._try_parse_direct_recipe_from_user_text(text)

    assert steps is not None
    assert len(steps) >= 4
    assert any("Season" in step for step in steps)
    assert any("rest" in step.lower() for step in steps)


def test_direct_recipe_parser_keeps_short_concept_as_concept():
    text = "Стейк на горячей сковороде с чесноком, маслом и красивой корочкой"

    assert handlers_media_core._try_parse_direct_recipe_from_user_text(text) is None


def test_direct_recipe_parser_accepts_long_unnumbered_recipe():
    text = (
        "Ингредиенты\n"
        "Лимон - 2 шт.\n"
        "Клубника - 200 г\n"
        "Мята - по вкусу\n"
        "Вода - 2.5 л\n"
        "Сахар - 150 г\n\n"
        "Для начала нужно сварить сироп. В небольшую кастрюлю наливаем около одного стакана воды, "
        "ждем пока вода закипит, потом растворяем весь сахар для лимонада. Сиропом заливаем "
        "нарезанные лимоны, чтобы убрать горечь цедры. Пока лимоны остывают, тщательно моем "
        "клубнику и нарезаем ягоды. Добавляем в графин мяту и клубнику, заливаем подготовленной "
        "водой, перемешиваем и пробуем лимонад."
    )

    steps = handlers_media_core._try_parse_direct_recipe_from_user_text(text)

    assert steps is not None
    assert len(steps) >= 4
    assert any("сироп" in step.lower() for step in steps)


def test_parse_regenerate_language_command():
    assert handlers_state_media._parse_regenerate_language_command("ПЕРЕГЕНЕРИРОВАТЬ en") == "en"
    assert handlers_state_media._parse_regenerate_language_command("перегенерировать ru") == "ru"
    assert handlers_state_media._parse_regenerate_language_command("ПЕРЕГЕНЕРИРОВАТЬ") is None


def test_send_scene_video_approval_prompt_sends_language_markup():
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, **kwargs):
            self.calls.append((text, kwargs))

    message = Message()
    update = SimpleNamespace(message=message)

    asyncio.run(
        handlers_state_media._send_scene_video_approval_prompt(
            update,
            {"state": "waiting_scene_media"},
            ["ru", "en"],
        )
    )

    assert len(message.calls) == 1
    text, kwargs = message.calls[0]
    assert "ОК все" in text
    buttons = [getattr(button, "text", button) for row in kwargs["reply_markup"].keyboard for button in row]
    assert "ОК ru" in buttons
    assert "ОК en" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ ru" in buttons


def test_prefix_video_prompt_with_language():
    assert (
        handlers_state_media._prefix_video_prompt_with_language("camera move", "en")
        == "lang:en camera move"
    )
    assert (
        handlers_state_media._prefix_video_prompt_with_language("lang:ru camera move", "en")
        == "lang:en camera move"
    )


def test_parse_language_feedback_command_accepts_new_and_legacy_labels():
    assert handlers_state_media._parse_lang_payload_command("ФИДБЕК en: warmer camera", "фидбек") == (
        "en",
        "warmer camera",
    )
    assert handlers_state_media._parse_lang_payload_command("ИСПРАВЬ en: warmer camera", "исправь") == (
        "en",
        "warmer camera",
    )


def test_call_with_503_retries_ignores_typing_timeout():
    class Bot:
        async def send_chat_action(self, **_kwargs):
            raise TimeoutError("Timed out")

    async def op():
        return "ok"

    update = SimpleNamespace(message=None)
    context = SimpleNamespace(bot=Bot())

    out = asyncio.run(
        handlers_media_core._call_with_503_retries(
            update,
            context,
            123,
            "generate_scenes_plan",
            op,
        )
    )

    assert out == "ok"


def test_call_with_503_retries_waits_and_retries_ai_rate_limit(monkeypatch):
    class Bot:
        async def send_chat_action(self, **_kwargs):
            return None

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **_kwargs):
            self.replies.append(text)

    calls = {"count": 0}
    sleeps = []

    async def op():
        calls["count"] += 1
        if calls["count"] == 1:
            raise handlers_media_core.GeminiQuotaError(
                "Лимит AI: слишком много запросов подряд. Подождите минуту."
            )
        return "ok"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(handlers_media_core, "AI_RATE_LIMIT_RETRY_COUNT", 1)
    monkeypatch.setattr(handlers_media_core, "AI_RATE_LIMIT_RETRY_DELAY_SEC", 60)
    monkeypatch.setattr(handlers_media_core.asyncio, "sleep", fake_sleep)

    message = Message()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(bot=Bot())

    out = asyncio.run(
        handlers_media_core._call_with_503_retries(
            update,
            context,
            123,
            "regenerate_video_prompt_from_feedback",
            op,
        )
    )

    assert out == "ok"
    assert calls["count"] == 2
    assert sleeps == [60]
    assert any("Уперлась в лимит AI" in reply and "попробую сама" in reply for reply in message.replies)


def test_safe_reply_video_recreates_stream_for_retry(monkeypatch):
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_video(self, *, video, caption, **kwargs):
            payload = video.read()
            self.calls.append((payload, caption, getattr(video, "name", ""), kwargs))
            if len(self.calls) == 1:
                raise handlers_state_media.TimedOut("Timed out")
            return SimpleNamespace(message_id=123)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_state_media.asyncio, "sleep", fake_sleep)

    message = Message()
    stream = handlers_state_media.io.BytesIO(b"video-bytes")
    stream.name = "scene_1_ru.mp4"

    out = asyncio.run(
        handlers_state_media._safe_reply_video(
            message,
            video=stream,
            caption="test caption",
        )
    )

    assert out.message_id == 123
    assert len(message.calls) == 2
    assert message.calls[0][0] == b"video-bytes"
    assert message.calls[1][0] == b"video-bytes"
    assert message.calls[1][2] == "scene_1_ru.mp4"
    assert message.calls[1][3]["write_timeout"] == handlers_state_media._SEND_VIDEO_TIMEOUT_SEC


def test_safe_reply_video_falls_back_to_document_after_timeouts(monkeypatch):
    class Message:
        def __init__(self):
            self.video_calls = []
            self.document_calls = []

        async def reply_video(self, *, video, caption, **kwargs):
            payload = video.read()
            self.video_calls.append((payload, caption, getattr(video, "name", ""), kwargs))
            raise handlers_state_media.TimedOut("Timed out")

        async def reply_document(self, *, document, caption, filename=None, **kwargs):
            payload = document.read()
            self.document_calls.append((payload, caption, filename, getattr(document, "name", ""), kwargs))
            return SimpleNamespace(message_id=456)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_state_media.asyncio, "sleep", fake_sleep)

    message = Message()
    stream = handlers_state_media.io.BytesIO(b"video-bytes")
    stream.name = "scene_5_ru.mp4"

    out = asyncio.run(
        handlers_state_media._safe_reply_video(
            message,
            video=stream,
            caption="scene caption",
        )
    )

    assert out.message_id == 456
    assert len(message.video_calls) == 2
    assert message.document_calls == [
        (
            b"video-bytes",
            "scene caption\nTelegram не принял отправку как video, отправляю mp4 как файл.",
            "scene_5_ru.mp4",
            "scene_5_ru.mp4",
            {
                "read_timeout": handlers_state_media._SEND_VIDEO_TIMEOUT_SEC,
                "write_timeout": handlers_state_media._SEND_VIDEO_TIMEOUT_SEC,
                "connect_timeout": 30.0,
                "pool_timeout": 30.0,
            },
        )
    ]


def test_safe_reply_photo_recreates_stream_for_retry(monkeypatch):
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_photo(self, *, photo, caption):
            payload = photo.read()
            self.calls.append((payload, caption, getattr(photo, "name", ""), photo))
            if len(self.calls) == 1:
                raise handlers_state_media.TimedOut("Timed out")
            return SimpleNamespace(message_id=124)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_state_media.asyncio, "sleep", fake_sleep)

    message = Message()
    stream = handlers_state_media.io.BytesIO(b"photo-bytes")
    stream.name = "scene_1.png"

    out = asyncio.run(
        handlers_state_media._safe_reply_photo(
            message,
            photo=stream,
            caption="test caption",
        )
    )

    assert out.message_id == 124
    assert len(message.calls) == 2
    assert message.calls[0][0] == b"photo-bytes"
    assert message.calls[1][0] == b"photo-bytes"
    assert message.calls[1][2] == "scene_1.png"
    assert message.calls[0][3] is not message.calls[1][3]


def test_compose_locked_video_prompt_keeps_existing_scene_phrase():
    scene = {
        "text": "Оригинальная фраза",
        "video_prompt": "lang:ru cinematic shot. CHARACTERS SPEAKS: Старая фраза",
        "duration_sec": 6,
    }
    out = handlers_state_media._compose_locked_video_prompt(
        scene,
        "ru",
        "new style prompt. CHARACTERS SPEAKS: Подмена фразы",
    )
    assert "CHARACTERS SPEAKS: Оригинальная фраза" in out
    assert "Подмена фразы" not in out
    assert "speech_timing_plan:" in out


def test_compose_locked_video_prompt_adds_role_dialogue_and_voice_locks():
    scene = {
        "text": "Ммм как вкусно [пауза] Сейчас подам",
        "dialogue_mode": "multi",
        "dialogue": [
            {"speaker": "grandfather", "text": "Ммм как вкусно"},
            {"speaker": "grandma", "text": "Сейчас подам"},
        ],
        "dialogue_by_lang": {
            "ru": [
                {"speaker": "grandfather", "text": "Ммм как вкусно"},
                {"speaker": "grandma", "text": "Сейчас подам"},
            ]
        },
        "duration_sec": 6,
    }
    out = handlers_state_media._compose_locked_video_prompt(
        scene,
        "ru",
        "cinematic dinner setup",
    )
    assert "CHARACTERS SPEAKS: Ммм как вкусно [пауза] Сейчас подам" in out
    assert "speaker_dialogue_order: GRANDFATHER, GRANDMA" in out
    assert "GRANDFATHER: Ммм как вкусно" in out
    assert "GRANDMA: Сейчас подам" in out
    assert "voice_lock_full_grandfather:" in out
    assert "voice_lock_full_grandma:" in out
    assert "speech_timing_plan:" in out


def test_apply_scene_phrase_update_refreshes_timing_block():
    scene = {
        "text": "старая реплика",
        "speech_by_lang": {"ru": "старая реплика"},
        "speech_plan_by_lang": {
            "ru": [{"text": "старая реплика", "order": 1, "intent": "speech", "language": "ru"}]
        },
        "video_prompt": (
            "wide shot\n"
            "CHARACTERS SPEAKS: старая реплика\n"
            "speech_timing_plan:\n"
            "- 0.00-6.00s: старая реплика"
        ),
        "video_prompt_by_lang": {
            "ru": (
                "wide shot\n"
                "CHARACTERS SPEAKS: старая реплика\n"
                "speech_timing_plan:\n"
                "- 0.00-6.00s: старая реплика"
            )
        },
        "duration_sec": 6,
    }
    session = {"target_languages": ["ru"]}

    handlers_state_media._apply_scene_phrase_update_to_scene(
        session,
        scene,
        scene_idx=0,
        language="ru",
        phrase_text="новая короткая фраза",
        origin="user",
    )

    prompt = scene["video_prompt_by_lang"]["ru"]
    assert "CHARACTERS SPEAKS: новая короткая фраза" in prompt
    assert "- 0.00-6.00s: новая короткая фраза" in prompt
    assert "старая реплика" not in prompt
    assert scene["speech_plan_by_lang"]["ru"][0]["text"] == "новая короткая фраза"


def test_apply_scene_phrase_update_removes_dash_from_voice_phrase():
    scene = {
        "text": "старая реплика",
        "speech_by_lang": {"ru": "старая реплика"},
        "video_prompt": "wide shot\nCHARACTERS SPEAKS: старая реплика",
        "video_prompt_by_lang": {"ru": "wide shot\nCHARACTERS SPEAKS: старая реплика"},
        "duration_sec": 6,
    }
    session = {"target_languages": ["ru"]}

    phrase = handlers_state_media._apply_scene_phrase_update_to_scene(
        session,
        scene,
        scene_idx=0,
        language="ru",
        phrase_text="вот так — и готово",
        origin="user",
    )

    assert phrase == "вот так и готово"
    assert scene["text"] == "вот так и готово"
    assert "CHARACTERS SPEAKS: вот так и готово" in scene["video_prompt"]
    assert "—" not in scene["video_prompt"]


def test_set_scene_video_prompt_for_all_languages_preserves_each_phrase_and_invalidates_videos():
    scene = {
        "text": "Русская фраза",
        "speech_by_lang": {"ru": "Русская фраза", "en": "English line"},
        "video_prompt_by_lang": {
            "ru": "lang:ru old CHARACTERS SPEAKS: Русская фраза",
            "en": "lang:en old CHARACTERS SPEAKS: English line",
        },
        "video_results_by_lang": {"ru": {"video_b64": "x"}, "en": {"video_b64": "y"}},
        "video_approvals_by_lang": {"ru": True, "en": True},
        "model": "veo3_fast",
    }
    session = {"target_languages": ["ru", "en"]}

    applied = handlers_state_media._set_scene_video_prompt_for_all_languages(
        session,
        scene,
        "new cinematic movement. CHARACTERS SPEAKS: wrong",
    )

    assert applied == ["ru", "en"]
    assert "lang:ru" in scene["video_prompt_by_lang"]["ru"]
    assert "CHARACTERS SPEAKS: Русская фраза" in scene["video_prompt_by_lang"]["ru"]
    assert "lang:en" in scene["video_prompt_by_lang"]["en"]
    assert "CHARACTERS SPEAKS: English line" in scene["video_prompt_by_lang"]["en"]
    assert scene["video_results_by_lang"] == {"ru": {"video_b64": "x"}, "en": {"video_b64": "y"}}
    assert scene["video_approvals_by_lang"] == {"ru": True, "en": True}
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY] == {"ru": True, "en": True}


def test_ok_only_markup_includes_add_and_fix_buttons():
    markup = handlers_media_scene.ok_only_markup()
    buttons = [btn.text for row in markup.keyboard for btn in row]
    assert "ОК" in buttons
    assert "Добавь" in buttons
    assert "Исправь" in buttons


def test_scene_media_markups_use_prompt_feedback_buttons_without_add_fix():
    start_buttons = [
        btn.text for row in handlers_media_scene.scene_image_approval_markup().keyboard for btn in row
    ]
    end_buttons = [
        btn.text for row in handlers_media_scene.scene_end_approval_markup().keyboard for btn in row
    ]
    video_buttons = [
        btn.text
        for row in handlers_media_scene.scene_video_approval_markup(
            include_restart_end=True,
            languages=["ru", "en"],
        ).keyboard
        for btn in row
    ]
    for buttons in (start_buttons, end_buttons, video_buttons):
        assert "ОК" in buttons
        assert "Автоапрув" in buttons
        assert "🤖 ИИ апрув" in buttons
        assert "СТОП" not in buttons
        assert "ПРОМПТ:" in buttons
        assert "ФИДБЕК:" in buttons
        assert "Добавь" not in buttons
        assert "Исправь" not in buttons
    assert "ФРАЗА:" in video_buttons
    assert "ИЗМЕНИТЬ СКОРОСТЬ" in video_buttons
    assert "ПРОМПТ ru:" in video_buttons
    assert "ИСПРАВЬ en:" not in video_buttons
    assert "ФИДБЕК en:" not in video_buttons
    assert "ФРАЗА en:" in video_buttons


def test_scene_language_approval_markup_contains_expected_language_commands():
    buttons = [
        btn.text
        for row in handlers_media_scene.scene_language_approval_markup(["ru", "en"]).keyboard
        for btn in row
    ]
    assert "ОК все" in buttons
    assert "Автоапрув" in buttons
    assert "🤖 ИИ апрув" in buttons
    assert "СТОП" not in buttons
    assert "ОК ru" in buttons
    assert "ОК en" in buttons
    assert "ИЗМЕНИТЬ СКОРОСТЬ" in buttons
    assert "ФИДБЕК:" in buttons
    assert "ПРОМПТ ru:" in buttons
    assert "ПРОМПТ en:" in buttons
    assert "ФИДБЕК ru:" not in buttons
    assert "ФИДБЕК en:" not in buttons
    assert "ФРАЗА ru:" in buttons
    assert "ФРАЗА en:" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ ru" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ en" in buttons


def test_change_speed_button_then_value_auto_approves_current_language():
    class _Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **_kwargs):
            self.replies.append(text)

    scene = {
        "video_results_by_lang": {"ru": {"video_b64": "x"}},
        "video_approvals_by_lang": {"ru": False},
        "video_candidates_by_lang": {
            "ru": [
                {
                    "candidate_id": "cand-1",
                    "language": "ru",
                    "video_b64": "eA==",
                    "created_at": "now",
                    "liked": False,
                    "approved_for_production": False,
                    "speed_coef": 1.2,
                    "source_image_candidate_id": "img-1",
                }
            ]
        },
    }
    session = {
        "scene_media_substep": handlers_state_media.SCENE_MEDIA_SUBSTEP_VIDEO,
        "target_languages": ["ru"],
        "production": {"json_plan": [scene, {"video_results_by_lang": {}}]},
        "image_scene_index": 0,
    }
    message = _Msg()
    update = SimpleNamespace(message=message)

    out_button = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            SimpleNamespace(),
            chat_id=1,
            session=session,
            text="ИЗМЕНИТЬ СКОРОСТЬ",
        )
    )

    assert out_button is True
    assert handlers_state_media.SCENE_VIDEO_SPEED_PENDING_KEY in session

    async def _fake_send_next_scene_media_step(_update, _session):
        return None

    monkeypatch_send = handlers_state_media._send_next_scene_media_step
    handlers_state_media._send_next_scene_media_step = _fake_send_next_scene_media_step
    try:
        out_speed = asyncio.run(
            handlers_state_media._handle_waiting_scene_media_state(
                update,
                SimpleNamespace(),
                chat_id=1,
                session=session,
                text="1.3",
            )
        )
    finally:
        handlers_state_media._send_next_scene_media_step = monkeypatch_send

    assert out_speed is True
    assert scene["video_approvals_by_lang"]["ru"] is True
    assert scene["video_candidates_by_lang"]["ru"][0]["speed_coef"] == 1.3


def test_scene_generated_image_approval_markup_has_expected_buttons():
    buttons = [
        btn.text
        for row in handlers_media_scene.scene_generated_image_approval_markup().keyboard
        for btn in row
    ]
    assert "ОК" in buttons
    assert "Автоапрув" in buttons
    assert "🤖 ИИ апрув" in buttons
    assert "СТОП" not in buttons
    assert "ПРОМПТ:" in buttons
    assert "ИЗМЕНИТЬ" in buttons
    assert "ФИДБЕК" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ" in buttons
    assert "ПОВЫСИТЬ МОДЕЛЬ" not in buttons
    buttons_with_upgrade = [
        btn.text
        for row in handlers_media_scene.scene_generated_image_approval_markup(include_model_upgrade=True).keyboard
        for btn in row
    ]
    assert "ПОВЫСИТЬ МОДЕЛЬ" in buttons_with_upgrade


def test_scene_media_markups_show_stop_button_when_auto_or_ai_enabled():
    auto_session = {
        handlers_media_scene.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY: True,
        handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY: handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_AUTO,
    }
    ai_session = {
        handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY: handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_AI,
    }

    for session in (auto_session, ai_session):
        buttons = [
            btn.text
            for row in handlers_media_scene.scene_video_approval_markup(
                include_restart_end=True,
                languages=["ru"],
                session=session,
            ).keyboard
            for btn in row
        ]
        assert "СТОП" in buttons
        assert "Автоапрув" not in buttons
        assert "🤖 ИИ апрув" not in buttons


def test_video_approval_text_shows_prompts_for_all_languages():
    scene = {
        "model": "veo3_fast",
        "duration_sec": 6,
        "video_prompt_current_language": "en",
        "video_prompt": "CHARACTERS SPEAKS: Мы продолжаем на русском",
        "video_prompt_by_lang": {
            "ru": "CHARACTERS SPEAKS: Мы продолжаем на русском",
            "en": "CHARACTERS SPEAKS: We continue in English",
        },
    }
    text = handlers_media_scene._video_approval_text(scene, scene_index=1, total=5)
    assert "Язык текущей генерации: en" in text
    assert "CHARACTERS SPEAKS: We continue in English" in text
    assert "CHARACTERS SPEAKS: Мы продолжаем на русском" in text
    assert "[ru]" in text
    assert "[en]" in text


def test_video_approval_text_shows_multi_speaker_dialogue_structure():
    scene = {
        "model": "veo3_fast",
        "duration_sec": 6,
        "video_prompt_current_language": "ru",
        "video_prompt_by_lang": {"ru": "CHARACTERS SPEAKS: Ммм [пауза] готово"},
        "dialogue_mode": "multi",
        "dialogue": [
            {"speaker": "grandfather", "text": "Ммм, вот это я понимаю, настоящий ужин!"},
            {"speaker": "grandma", "text": "Одна сковорода и вкуснейшее жаркое на всю неделю готово."},
        ],
    }
    text = handlers_media_scene._video_approval_text(scene, scene_index=1, total=5)
    assert "Структура говорящих в сцене 1:" in text
    assert "дедушка: Ммм, вот это я понимаю, настоящий ужин!" in text
    assert "бабушка: Одна сковорода и вкуснейшее жаркое на всю неделю готово." in text


def test_concept_choice_markup_has_guided_feedback_buttons():
    markup = handlers_state_start._concept_choice_markup([{"name": "Суп"}, {"name": "Рагу"}])
    buttons = [btn.text for row in markup.keyboard for btn in row]
    assert "1" in buttons
    assert "2" in buttons
    assert "Добавь" in buttons
    assert "Исправь" in buttons


def test_parse_concept_choice_ignores_feedback_numbers():
    assert handlers_state_start._parse_concept_choice("2") == 2
    assert handlers_state_start._parse_concept_choice("концепт 2") == 2
    assert handlers_state_start._parse_concept_choice("Исправь: take steaks out 20 minutes before cooking") is None


def test_guided_feedback_buttons_transform_followup_text():
    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {}
    handled, out = asyncio.run(
        handlers_state_scene._handle_guided_feedback_buttons(
            update,
            session=session,
            state="waiting_recipe_approval",
            text="Добавь",
        )
    )
    assert handled is True
    assert out == "Добавь"
    assert session["guided_step_feedback"]["mode"] == "add"
    assert "Что добавить" in messages[-1]

    handled2, out2 = asyncio.run(
        handlers_state_scene._handle_guided_feedback_buttons(
            update,
            session=session,
            state="waiting_recipe_approval",
            text="добавь стартовую фразу",
        )
    )
    assert handled2 is False
    assert out2 == "Добавь в промпт: добавь стартовую фразу"
    assert "guided_step_feedback" not in session


def test_extract_first_http_url_from_text():
    text = "Сделай по этому рецепту https://example.com/recipes/pancakes?src=tg"
    assert (
        handlers_generation_utils.extract_first_http_url(text)
        == "https://example.com/recipes/pancakes?src=tg"
    )


def test_fetch_recipe_text_from_url_prefers_recipe_markers():
    html = """
    <html><body>
      <div>Реклама и меню сайта</div>
      <h1>Лепешка на сковороде</h1>
      <h2>Ингредиенты</h2>
      <ul><li>Мука</li><li>Вода</li><li>Соль</li></ul>
      <h2>Приготовление</h2>
      <p>Смешайте муку, воду и соль.</p>
      <p>Замесите тесто и обжарьте до румяности.</p>
    </body></html>
    """

    class _Resp:
        status_code = 200
        text = html

        def raise_for_status(self):
            return None

    out = handlers_generation_utils.fetch_recipe_text_from_url(
        "https://example.com/p",
        http_get=lambda *_args, **_kwargs: _Resp(),
    )
    lowered = out.lower()
    assert "ингредиенты" in lowered
    assert "приготовление" in lowered
    assert "мука" in lowered


def test_fetch_recipe_text_from_url_understands_dunduk_style_followed_by_numbered_ingredients():
    html = """
    <html><body>
      <h1>Суп-харчо</h1>
      <p>Длинное авторское вступление про историю блюда. Как приготовить этот суп, спорят давно.</p>
      <p>Еще много вступительного текста.</p>
      <p>Итак, на 5-6 порций супа-харчо следует взять:</p>
      <p>1. Примерно 1 кг бараньих ребрышек</p>
      <p>2. Три средние луковицы.</p>
      <p>3. Одну среднюю морковь</p>
      <p>С ребрышек надобно снять пленку и нарезать вдоль по кусочкам.</p>
      <p>Далее разогреваем стальную кастрюлю и поджариваем ребрышки.</p>
    </body></html>
    """

    class _Resp:
        text = html

        def raise_for_status(self):
            return None

    out = handlers_generation_utils.fetch_recipe_text_from_url(
        "https://example.com/dunduk",
        http_get=lambda *_args, **_kwargs: _Resp(),
    )
    lowered = out.lower()
    assert "следует взять" in lowered
    assert "бараньих ребрышек" in lowered
    assert "разогреваем стальную кастрюлю" in lowered


def test_fetch_recipe_text_from_url_supports_cp1251_and_products_marker():
    html = """
    <html><body>
      <h1>Рецепт супа</h1>
      <h2>Продукты</h2>
      <p>Картофель 3 шт</p>
      <p>Морковь 1 шт</p>
      <h2>Пошаговый фото рецепт</h2>
      <p>Нарезаем овощи и кладем в кастрюлю.</p>
      <p>Варим до готовности.</p>
    </body></html>
    """.encode("cp1251")

    class _Resp:
        content = html
        encoding = ""
        apparent_encoding = "windows-1251"

        def raise_for_status(self):
            return None

    out = handlers_generation_utils.fetch_recipe_text_from_url(
        "https://example.com/russianfood",
        http_get=lambda *_args, **_kwargs: _Resp(),
    )
    lowered = out.lower()
    assert "продукты" in lowered
    assert "картофель 3 шт" in lowered
    assert "пошаговый фото рецепт" in lowered


def test_fetch_recipe_text_from_url_prefers_json_ld_recipe():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Recipe",
      "name": "Сырники",
      "recipeIngredient": ["Творог 400 г", "Яйцо 1 шт"],
      "recipeInstructions": [{"@type": "HowToStep", "text": "Смешайте творог с яйцом."}, {"text": "Обжарьте сырники."}]
    }
    </script>
    </head><body>Навигация сайта</body></html>
    """

    class _Resp:
        text = html

        def raise_for_status(self):
            return None

    out = handlers_generation_utils.fetch_recipe_text_from_url(
        "https://example.com/jsonld",
        http_get=lambda *_args, **_kwargs: _Resp(),
    )
    assert "Сырники" in out
    assert "Творог 400 г" in out
    assert "Смешайте творог" in out


def test_fetch_recipe_text_from_url_rejects_javascript_placeholder():
    html = """
    <html><body>
      <noscript>Please enable JavaScript to view this site.</noscript>
      <div>Для доступа к сайту необходимо включить поддержку JavaScript.</div>
    </body></html>
    """

    class _Resp:
        text = html

        def raise_for_status(self):
            return None

    try:
        handlers_generation_utils.fetch_recipe_text_from_url(
            "https://example.com/js-only",
            http_get=lambda *_args, **_kwargs: _Resp(),
        )
    except RuntimeError as exc:
        assert "без JavaScript" in str(exc)
    else:
        raise AssertionError("Expected JavaScript placeholder page to be rejected")


def test_fetch_recipe_text_from_url_rejects_utf8_js_placeholder_mislabelled_as_latin1():
    html = (
        '<html><head><meta charset="utf-8"></head>'
        "<noscript>Вам нужно включить поддержку js в браузере, чтобы посетить данный сайт.</noscript>"
        "</html>"
    ).encode("utf-8")

    class _Resp:
        content = html
        encoding = "ISO-8859-1"
        apparent_encoding = "utf-8"

        def raise_for_status(self):
            return None

    try:
        handlers_generation_utils.fetch_recipe_text_from_url(
            "https://example.com/js-only",
            http_get=lambda *_args, **_kwargs: _Resp(),
        )
    except RuntimeError as exc:
        assert "без JavaScript" in str(exc)
    else:
        raise AssertionError("Expected JavaScript placeholder page to be rejected")


def test_fetch_recipe_text_from_url_falls_back_to_reader_for_js_placeholder():
    js_html = (
        '<html><head><meta charset="utf-8"></head>'
        "<noscript>Вам нужно включить поддержку js в браузере, чтобы посетить данный сайт.</noscript>"
        "</html>"
    ).encode("utf-8")
    reader_text = """
    Title: Шоколадный торт на раз, два, три

    * Мука — 235 г
    * Сода — 7 г
    * Соль — 4 г
    * Какао-порошок — 65 г
    * Сахар — 300 г
    * Яйца — 2 шт.
    * Молоко — 260 г

    Приготовление:
    Соедините муку, соду, соль, сахар и какао.
    Добавьте яйца, масла, молоко и уксус.
    Перемешайте миксером и выпекайте до сухой шпажки.
    """
    seen_urls: list[str] = []

    class _DirectResp:
        content = js_html
        encoding = "ISO-8859-1"
        apparent_encoding = "utf-8"

        def raise_for_status(self):
            return None

    class _ReaderResp:
        text = reader_text

        def raise_for_status(self):
            return None

    def _get(url, **_kwargs):
        seen_urls.append(url)
        if url.startswith("https://r.jina.ai/"):
            return _ReaderResp()
        return _DirectResp()

    out = handlers_generation_utils.fetch_recipe_text_from_url(
        "https://andychef.ru/recipes/choco-123/",
        http_get=_get,
    )

    assert seen_urls == [
        "https://andychef.ru/recipes/choco-123/",
        "https://r.jina.ai/https://andychef.ru/recipes/choco-123/",
    ]
    assert "Шоколадный торт" in out
    assert "Какао-порошок" in out
    assert "Перемешайте миксером" in out


def test_build_direct_recipe_session_sets_recipe_approval_state():
    session = _build_direct_recipe_session(
        "Суп: 1) нарезать 2) варить",
        ["нарезать", "варить", "варить", "варить"],
        prefer_vertex_api=True,
    )
    assert session["state"] == "waiting_recipe_approval"
    assert session["selected_concept"] == "Рецепт пользователя"
    assert session["precheck_recipe_steps"] == ["нарезать", "варить", "варить", "варить"]
    assert session["prefer_vertex_api"] is True


def test_build_concepts_session_sets_waiting_concept_state():
    concepts = [{"name": "Суп", "hook": "Сытный и быстрый"}]
    session = _build_concepts_session("суп с лапшой", concepts, prefer_vertex_api=False)
    assert session["state"] == "waiting_concept"
    assert session["concepts"] == concepts
    assert session["selected_concept"] is None
    assert session["prefer_vertex_api"] is False


def test_extract_prompt_override_supports_prompt_and_promt():
    assert _extract_prompt_override("ПРОМПТ: hello") == "hello"
    assert _extract_prompt_override("промт: hello") == "hello"
    assert _extract_prompt_override("ФИДБЕК: hello") is None


def test_extract_format_or_feedback_supports_format_and_feedback():
    assert _extract_format_or_feedback("ФОРМАТ: cinematic short") == "cinematic short"
    assert _extract_format_or_feedback("ФИДБЕК: less camera shake") == "less camera shake"
    assert _extract_format_or_feedback("ПРОМПТ: hello") is None


def test_extract_prompt_override_supports_code_block():
    text = "ПРОМПТ: ```\nКрупный план у двери\nкот как на референсе\n```"
    assert _extract_prompt_override(text) == "Крупный план у двери\nкот как на референсе"


def test_should_soften_image_prompt_only_for_policy_like_errors():
    assert _should_soften_image_prompt_after_error(RuntimeError("policy violation")) is True
    assert _should_soften_image_prompt_after_error(RuntimeError("finish_reason=SAFETY")) is True
    assert _should_soften_image_prompt_after_error(RuntimeError("HTTP 503 unavailable")) is False
    assert _should_soften_image_prompt_after_error(RuntimeError("certificate verify failed")) is False


def test_append_pixar_reference_style_phrase_is_idempotent():
    out = _append_pixar_reference_style_phrase("exact prompt")
    assert out.endswith(PIXAR_REFERENCE_STYLE_PHRASE)
    assert _append_pixar_reference_style_phrase(out).count(PIXAR_REFERENCE_STYLE_PHRASE) == 1


def test_generate_image_with_lenient_fallback_does_not_soften_non_policy_errors(monkeypatch):
    calls = []

    def _fake_generate(prompt, reference_image=None, *, reference_images=None):
        calls.append(prompt)
        raise RuntimeError("HTTP 503 unavailable")

    monkeypatch.setattr("app.handlers_media_scene.generate_image_vertex", _fake_generate)

    try:
        asyncio.run(_generate_image_with_lenient_fallback("exact prompt", reference_images=[b"img"]))
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "503" in str(e)
    assert calls == [f"exact prompt\n\n{PIXAR_REFERENCE_STYLE_PHRASE}"]


def test_generate_image_with_lenient_fallback_softens_policy_errors(monkeypatch):
    calls = []

    def _fake_generate(prompt, reference_image=None, *, reference_images=None):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("content moderation blocked by policy")
        return b"ok"

    monkeypatch.setattr("app.handlers_media_scene.generate_image_vertex", _fake_generate)
    out = asyncio.run(_generate_image_with_lenient_fallback("exact prompt", reference_images=[b"img"]))
    assert out == b"ok"
    assert len(calls) == 2
    assert calls[0] == f"exact prompt\n\n{PIXAR_REFERENCE_STYLE_PHRASE}"
    assert calls[1] != calls[0]
    assert PIXAR_REFERENCE_STYLE_PHRASE in calls[1]


def test_restart_start_command_first_line_only():
    assert _wants_restart_start_frame("старт заново") is True
    assert _wants_restart_start_frame("СТАРТ ЗАНОВО\n") is True
    assert _wants_restart_start_frame("пересогласовать старт") is True
    assert _wants_restart_start_frame("фидбек: старт заново") is False
    assert _wants_restart_start_frame("ПРОМПТ: бабушка") is False


def test_restart_end_command():
    assert _wants_restart_end_frame("конец заново") is True
    assert _wants_restart_end_frame("перегенерировать конец") is True
    assert _wants_restart_end_frame("старт заново") is False


def test_b64_roundtrip():
    raw = b"abc123\x00\xff"
    enc = _encode_b64(raw)
    dec = _decode_b64(enc)
    assert dec == raw


def test_decode_b64_invalid_returns_none():
    assert _decode_b64("%%%not-base64%%%") is None


def test_policy_error_classifier():
    e = RuntimeError("Safety policy violation: content moderation blocked")
    assert _is_video_policy_error(e) is True
    assert _is_video_invalid_image_error(e) is False


def test_safety_error_classifier():
    e = RuntimeError("Video generation blocked: finish_reason=SAFETY.")
    assert _is_video_safety_error(e) is True
    assert _is_video_policy_error(e) is True


def test_invalid_image_error_classifier():
    e = RuntimeError("Reference image rejected: invalid image format")
    assert _is_video_invalid_image_error(e) is True


def test_high_load_error_classifier():
    e = RuntimeError(
        'Vertex operation failed: {"code":8,"message":"The service is currently experiencing high load and cannot process your request. Please try again later."}'
    )
    assert _is_video_high_load_error(e) is True


def test_video_model_unavailable_error_classifier():
    e = RuntimeError(
        'HTTP 404: {"error":{"code":404,"message":"Publisher Model `projects/p/locations/europe-west4/publishers/google/models/veo-3.1-fast-generate-001` was not found","status":"NOT_FOUND"}}'
    )
    assert _is_video_model_unavailable_error(e) is True


def test_video_high_load_retry_succeeds(monkeypatch):
    calls = {"n": 0}
    real_sleep = asyncio.sleep
    regions_used: list[str] = []
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "0")

    async def fake_to_thread(fn, **kwargs):
        calls["n"] += 1
        regions_used.append(str(kwargs.get("location")))
        if calls["n"] < 3:
            raise RuntimeError('{"code":8,"message":"experiencing high load"}')
        return b"ok-video"

    messages: list[str] = []

    class _Message:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Message())
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.sleep", lambda *_: real_sleep(0))
    try:
        asyncio.run(
            _generate_video_bytes_with_high_load_retry(
                update,
                prompt="p",
                model="veo-3.1-fast-generate-001",
                duration_sec=4,
                start_image=b"x",
                end_image=None,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        )
        assert False, "expected VideoProviderChoiceRequired"
    except VideoProviderChoiceRequired:
        pass
    assert calls["n"] == 0


def test_video_generation_disable_syntax_fallback_runs_vertex(monkeypatch):
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    calls: list[dict] = []

    async def fake_to_thread(fn, **kwargs):
        assert fn is handlers_media_core.generate_video_bytes
        calls.append(kwargs)
        return b"vertex-video"

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)

    out = asyncio.run(
        _generate_video_bytes_with_high_load_retry(
            update,
            prompt="p",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"start",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
            skip_initial_syntax=True,
            disable_syntax_fallback=True,
        )
    )

    assert out == b"vertex-video"
    assert len(calls) == 1
    assert calls[0]["prompt"] == "p"


def test_video_region_candidates_primary_and_fallbacks(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION_FALLBACKS", "europe-west4,us-east5,us-central1")
    assert _video_region_candidates() == ["us-central1", "europe-west4", "us-east5"]


def test_video_region_candidates_maps_global_to_us_central1(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("VERTEX_VIDEO_LOCATION", raising=False)
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION_FALLBACKS", "europe-west4,us-central1")
    assert _video_region_candidates() == ["us-central1", "europe-west4"]


def test_video_region_candidates_prefers_video_specific_location(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION", "us-east5")
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION_FALLBACKS", "us-central1")
    assert _video_region_candidates() == ["us-east5", "us-central1"]


def test_video_high_load_switches_region_after_local_retries(monkeypatch):
    calls = {"n": 0}
    real_sleep = asyncio.sleep
    regions_used: list[str] = []
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "0")

    async def fake_to_thread(fn, **kwargs):
        calls["n"] += 1
        regions_used.append(str(kwargs.get("location")))
        if calls["n"] < 4:
            raise RuntimeError('{"code":8,"message":"experiencing high load"}')
        return b"ok-video"

    messages: list[str] = []

    class _Message:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION_FALLBACKS", "europe-west4")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.sleep", lambda *_: real_sleep(0))

    try:
        asyncio.run(
            _generate_video_bytes_with_high_load_retry(
                update,
                prompt="p",
                model="veo-3.1-fast-generate-001",
                duration_sec=4,
                start_image=b"x",
                end_image=None,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        )
        assert False, "expected VideoProviderChoiceRequired"
    except VideoProviderChoiceRequired:
        pass
    assert regions_used == []


def test_video_404_switches_region_immediately(monkeypatch):
    calls = {"n": 0}
    regions_used: list[str] = []
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "0")

    async def fake_to_thread(fn, **kwargs):
        calls["n"] += 1
        regions_used.append(str(kwargs.get("location")))
        if calls["n"] == 1:
            raise RuntimeError(
                'HTTP 404: {"error":{"code":404,"message":"Publisher Model was not found","status":"NOT_FOUND"}}'
            )
        return b"ok-video"

    messages: list[str] = []

    class _Message:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.delenv("VERTEX_VIDEO_LOCATION", raising=False)
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION_FALLBACKS", "us-central1")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)

    try:
        asyncio.run(
            _generate_video_bytes_with_high_load_retry(
                update,
                prompt="p",
                model="veo-3.1-fast-generate-001",
                duration_sec=4,
                start_image=b"x",
                end_image=None,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        )
        assert False, "expected VideoProviderChoiceRequired"
    except VideoProviderChoiceRequired:
        pass
    assert regions_used == []


def test_prompt_filtered_error_classifier():
    e = RuntimeError(
        "Video response does not contain inline MP4 bytes. "
        'Response excerpt: {"raiMediaFilteredCount":1,"raiMediaFilteredReasons":["violated Vertex AI usage guidelines"]}'
    )
    assert _is_video_prompt_filtered_error(e) is True


def test_syntax_fallback_trigger_only_policy_safety_filtered():
    assert _is_syntax_fallback_trigger_error(RuntimeError("blocked by safety policy")) is True
    assert _is_syntax_fallback_trigger_error(
        RuntimeError(
            'Response excerpt: {"raiMediaFilteredCount":1,"raiMediaFilteredReasons":["x"]}'
        )
    ) is True
    assert _is_syntax_fallback_trigger_error(
        RuntimeError('{"code":8,"message":"high load please try again"}')
    ) is False
    assert _is_syntax_fallback_trigger_error(
        RuntimeError(
            'HTTP 404: {"error":{"code":404,"message":"Publisher Model was not found","status":"NOT_FOUND"}}'
        )
    ) is False
    assert _is_syntax_fallback_trigger_error(
        RuntimeError("Reference image rejected: invalid image format")
    ) is False
    assert _is_syntax_fallback_trigger_error(GeminiQuotaError("quota")) is False
    assert _is_syntax_fallback_trigger_error(RuntimeError("HTTP 429: too many requests")) is False


def test_video_policy_exhaustion_runs_syntax_fallback_when_enabled(monkeypatch):
    calls = {"n": 0, "syntax": 0}
    real_sleep = asyncio.sleep

    async def fake_vertex_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "try_generate_via_syntax":
            calls["syntax"] += 1
            return FallbackResult(
                success=True,
                video_bytes=b"syntax-mp4",
                video_uri=None,
                provider_message="ok",
                attempt_id="t1",
            )
        if name == "video_bytes_from_fallback_result":
            res = args[0]
            return res.video_bytes or b""
        calls["n"] += 1
        raise RuntimeError("Video generation blocked: policy violation")

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("SYNTAX_FALLBACK_MODE", "auto")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_vertex_thread)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.sleep", lambda *_: real_sleep(0))
    out = asyncio.run(
        _generate_video_bytes_with_high_load_retry(
            update,
            prompt="p",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"x",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
        )
    )
    assert out == b"syntax-mp4"
    assert calls["syntax"] == 1


def test_video_policy_exhaustion_manual_mode_still_runs_syntax(monkeypatch):
    calls = {"n": 0, "syntax": 0}

    async def fake_vertex_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "try_generate_via_syntax":
            calls["syntax"] += 1
            return FallbackResult(
                success=True,
                video_bytes=b"syntax-manual-mp4",
                video_uri=None,
                provider_message="ok",
                attempt_id="m1",
            )
        if name == "video_bytes_from_fallback_result":
            res = args[0]
            return res.video_bytes or b""
        calls["n"] += 1
        raise RuntimeError("Video generation blocked: policy violation")

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("SYNTAX_FALLBACK_MODE", "manual")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_vertex_thread)
    out = asyncio.run(
        _generate_video_bytes_with_high_load_retry(
            update,
            prompt="p",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"x",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
        )
    )
    assert out == b"syntax-manual-mp4"
    assert calls["syntax"] == 1
    assert calls["n"] == 0


def test_video_policy_exhaustion_no_syntax_when_disabled(monkeypatch):
    calls = {"n": 0, "syntax": 0}
    real_sleep = asyncio.sleep

    async def fake_vertex_thread(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "try_generate_via_syntax":
            calls["syntax"] += 1
            raise AssertionError("syntax must not run when fallback disabled")
        calls["n"] += 1
        raise RuntimeError("Video generation blocked: policy violation")

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.delenv("SYNTAX_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_vertex_thread)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.sleep", lambda *_: real_sleep(0))
    try:
        asyncio.run(
            _generate_video_bytes_with_high_load_retry(
                update,
                prompt="p",
                model="veo3_fast",
                duration_sec=6,
                start_image=b"x",
                end_image=None,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        )
        assert False, "expected VideoProviderChoiceRequired"
    except VideoProviderChoiceRequired as e:
        assert "syntax" in str(e).lower()
    assert calls["syntax"] == 0


def test_video_high_load_uses_vertex_after_syntax_failure(monkeypatch):
    calls = {"n": 0, "syntax": 0}
    real_sleep = asyncio.sleep

    async def fake_vertex_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "try_generate_via_syntax":
            calls["syntax"] += 1
            return FallbackResult(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Syntax failed",
                attempt_id="syntax-failed",
            )
        if name == "video_bytes_from_fallback_result":
            res = args[0]
            return res.video_bytes or b""
        calls["n"] += 1
        raise RuntimeError('{"code":8,"message":"experiencing high load"}')

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("SYNTAX_FALLBACK_MODE", "auto")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_vertex_thread)
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.sleep", lambda *_: real_sleep(0))
    try:
        asyncio.run(
            _generate_video_bytes_with_high_load_retry(
                update,
                prompt="p",
                model="veo3_fast",
                duration_sec=6,
                start_image=b"x",
                end_image=None,
                quality="1080",
                aspect_ratio="9:16",
                audio_enabled=True,
            )
        )
        assert False, "expected VideoProviderChoiceRequired"
    except VideoProviderChoiceRequired as e:
        assert "syntax" in str(e).lower()
    assert calls["syntax"] == 1
    assert calls["n"] == 0


def test_explicit_vertex_fallback_runs_vertex_success(monkeypatch):
    calls = {"vertex": 0, "syntax": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "try_generate_via_syntax":
            calls["syntax"] += 1
            return FallbackResult(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Veo на обновлении",
                attempt_id="syntax-update",
            )
        if name == "video_bytes_from_fallback_result":
            res = args[0]
            return res.video_bytes or b""
        calls["vertex"] += 1
        return b"vertex-mp4"

    class _Message:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Message())
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)
    out = asyncio.run(
        _generate_video_bytes_with_high_load_retry(
            update,
            prompt="p",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"x",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
            force_vertex_fallback=True,
        )
    )
    assert out == b"vertex-mp4"
    assert calls == {"vertex": 1, "syntax": 0}


def test_syntax_fallback_calls_are_serialized(monkeypatch):
    state = {"active": 0, "max_active": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "try_generate_via_syntax":
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0)
            state["active"] -= 1
            return FallbackResult(
                success=True,
                video_bytes=b"syntax-mp4",
                video_uri=None,
                provider_message="ok",
                attempt_id="serialized",
            )
        if name == "video_bytes_from_fallback_result":
            res = args[0]
            return res.video_bytes or b""
        raise AssertionError(f"unexpected to_thread function: {name}")

    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)

    async def _run_two() -> tuple[bytes | None, bytes | None]:
        return await asyncio.gather(
            handlers_media_core._attempt_syntax_video_fallback_bytes(
                prompt="p1",
                model="veo3_fast",
                duration_sec=6,
                start_image=b"x",
                end_image=None,
            ),
            handlers_media_core._attempt_syntax_video_fallback_bytes(
                prompt="p2",
                model="veo3_fast",
                duration_sec=6,
                start_image=b"y",
                end_image=None,
            ),
        )

    first, second = asyncio.run(_run_two())
    assert first == b"syntax-mp4"
    assert second == b"syntax-mp4"
    assert state["max_active"] == 2


def test_syntax_fallback_lock_wait_has_timeout(monkeypatch):
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC", "7")
    observed: list[int] = []

    async def fake_to_thread(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "try_generate_via_syntax":
            observed.append(int(kwargs["acquire_timeout_sec"]))
            raise TimeoutError
        raise AssertionError(f"unexpected to_thread function: {getattr(fn, '__name__', '')}")

    monkeypatch.setattr("app.handlers.handlers_media_core.asyncio.to_thread", fake_to_thread)

    out = asyncio.run(
        handlers_media_core._attempt_syntax_video_fallback_bytes(
            prompt="p",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"x",
            end_image=None,
        )
    )

    assert out is None
    assert observed == [7]


def test_user_error_message_video_429_not_gemini():
    e = RuntimeError("HTTP 429: too many requests")
    msg = _user_error_message(e, domain="video")
    assert "Vertex Video API" in msg
    assert "Gemini" not in msg


def test_user_error_message_video_global_html_404_is_readable():
    e = RuntimeError(
        "HTTP 404: <!DOCTYPE html><html><title>Error 404 (Not Found)!!1</title>"
        "<p>The requested URL <code>/v1/projects/p/locations/global/publishers/google/"
        "models/veo-3.1-fast-generate-001:predictLongRunning</code> was not found"
    )
    msg = _user_error_message(e, domain="video")
    assert "global" in msg
    assert "VERTEX_VIDEO_LOCATION" in msg
    assert "<!DOCTYPE html>" not in msg


def test_user_error_message_truncates_very_long_text():
    e = RuntimeError("x" * 5000)
    msg = _user_error_message(e)
    assert len(msg) <= 1201
    assert msg.endswith("…")


def test_parse_video_command_args_and_duration_normalization():
    sec, prompt = _parse_video_command_args("/video 5 Бабушка перемешивает творог")
    assert sec == 6
    assert "Бабушка" in prompt
    assert _normalize_veo_duration_sec(7) == 6


def test_parse_video_command_args_requires_seconds_and_prompt():
    try:
        _parse_video_command_args("/video")
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Формат" in str(e)


def test_reference_keys_include_required_and_extra_characters():
    scene = {"shot_type": "medium", "characters": ["daughter"]}
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "daughter": "d.png",
        "location": "loc.png",
    }
    keys = _reference_keys_for_scene(scene, refs)
    assert keys[:3] == ["grandma", "cat", "daughter"]
    assert "location" not in keys


def test_reference_keys_ignore_location_even_if_explicitly_requested():
    scene = {"shot_type": "medium", "characters": ["daughter"]}
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "daughter": "d.png",
        "location": "loc.png",
    }
    keys = _reference_keys_for_scene(scene, refs, include_location=True)
    assert keys == ["grandma", "cat", "daughter"]


def test_reference_keys_can_skip_location_for_video_images():
    scene = {"shot_type": "medium", "characters": ["daughter"]}
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "daughter": "d.png",
        "location": "loc.png",
    }
    keys = _reference_keys_for_scene(scene, refs, include_location=False)
    assert keys == ["grandma", "cat", "daughter"]


def test_image_identity_can_skip_location_for_video_images():
    scene = {"location": "kitchen", "characters": ["grandma", "cat", "zaya"], "romantic_drama_beat": "Zaya смотрит на Timosha"}
    prompt = _append_scene_identity_to_image_prompt("base with Zaya and Markiz", scene, include_location=False)
    assert "Персонажи в кадре: бабушка, рыжий кот" in prompt
    assert "белая кошка" in prompt
    assert "Фоновая драма этой сцены должна быть видна в кадре: белая кошка смотрит на рыжий кот" in prompt
    assert "Zaya" not in prompt
    assert "Markiz" not in prompt
    assert "Timosha" not in prompt
    assert "Локация кадра" not in prompt
    assert "локацию не менять" not in prompt.lower()


def test_image_identity_keeps_fruit_only_closeup_visible_cast():
    scene = {
        "characters": ["grandma", "cat", "zaya", "banana", "strawberry", "grandson"],
        "romantic_drama_beat": "Белая кошка напряжённо следит за рыжим котом.",
    }
    prompt = _append_scene_identity_to_image_prompt(
        "Крупный план: только банан и клубника разговаривают как в офисе, без бабушки и кота.",
        scene,
        include_location=False,
    )
    assert "Персонажи в кадре: banana, strawberry" in prompt
    assert "бабушка, рыжий кот" not in prompt
    assert "белая кошка" not in prompt
    assert "Фоновая драма этой сцены" not in prompt


def test_reference_keys_pick_character_mentioned_in_prompt():
    scene = {
        "shot_type": "medium",
        "characters": [],
        "start_frame_prompt_ru": "В кадре дочь помогает бабушке у миски",
        "video_prompt": "daughter enters frame and smiles",
    }
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "daughter": "d.png",
        "location": "loc.png",
    }
    keys = _reference_keys_for_scene(scene, refs)
    assert "daughter" in keys


def test_reference_keys_pick_zaya_and_markiz_mentioned_in_prompt():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Белая кошка смотрит на рыжего кота, а черно-белый кот появляется за окном",
    }
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "zaya": "z.png",
        "markiz": "m.png",
    }

    keys = _reference_keys_for_scene(scene, refs)

    assert keys == ["grandma", "cat", "zaya", "markiz"]


def test_reference_keys_skip_scene_cast_for_fruit_only_closeup():
    scene = {
        "characters": ["grandma", "cat", "zaya", "banana", "strawberry", "grandson"],
        "start_frame_prompt_ru": "Крупный план: только банан и клубника разговаривают как в офисе, без бабушки и кота.",
    }
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "zaya": "z.png",
        "grandson": "gs.png",
        "banana": "b.png",
        "strawberry": "s.png",
    }

    keys = _reference_keys_for_scene(scene, refs)

    assert keys == ["banana", "strawberry"]


def test_reference_keys_use_target_prompt_for_fruit_start_and_reveal_end():
    scene = {
        "characters": ["grandma", "cat", "banana", "strawberry"],
        "start_frame_prompt_ru": "Крупный план: только банан и клубника разговаривают как в офисе, без бабушки и кота.",
        "end_frame_prompt_ru": "Камера отъехала: банан и клубника внутри прозрачной чаши блендера, бабушка рядом у кнопки.",
    }
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "banana": "b.png",
        "strawberry": "s.png",
    }

    start_keys = _reference_keys_for_scene(
        scene,
        refs,
        prompt_keys=("start_frame_prompt", "start_frame_prompt_ru"),
    )
    end_keys = _reference_keys_for_scene(
        scene,
        refs,
        prompt_keys=("end_frame_prompt", "end_frame_prompt_ru", "video_prompt"),
    )

    assert start_keys == ["banana", "strawberry"]
    assert end_keys[:2] == ["grandma", "cat"]
    assert "banana" in end_keys
    assert "strawberry" in end_keys


def test_reference_keys_normalize_russian_character_aliases():
    scene = {
        "shot_type": "medium",
        "characters": ["бабушка", "кот", "внучка"],
    }
    refs = {
        "grandma": "g.png",
        "cat": "c.png",
        "granddaughter": "gd.png",
        "location": "loc.png",
    }
    keys = _reference_keys_for_scene(scene, refs)
    assert keys[:3] == ["grandma", "cat", "granddaughter"]


def test_prompt_mentioned_russian_granddaughter_gets_identity_and_reference():
    scene = {
        "shot_type": "medium",
        "characters": ["grandma", "cat", "zaya"],
        "start_frame_prompt_ru": "Бабушка ставит стакан, а радостная внучка заглядывает из-за стола.",
        "references": {
            "grandma": "g.png",
            "cat": "c.png",
            "zaya": "z.png",
        },
    }

    refs = _resolve_scene_references(scene)
    keys = _reference_keys_for_scene(scene, refs)
    prompt = _append_scene_identity_to_image_prompt(scene["start_frame_prompt_ru"], scene)

    assert refs["granddaughter"].endswith("assets/characters/granddaughter/base.png")
    assert keys == ["grandma", "cat", "zaya", "granddaughter"]
    assert "внучка" in prompt


def test_end_frame_delta_instruction_mentions_delta_and_references():
    txt = _end_frame_delta_instruction({"characters": ["grandma", "cat", "granddaughter"]})
    assert "Start-frame" in txt
    assert "бабушка, рыжий кот, внучка" in txt


def test_end_frame_image_warranted_when_extra_cast_not_in_start_prompt():
    scene = {
        "characters": ["grandma", "cat", "granddaughter"],
        "start_frame_prompt_ru": "Бабушка режет лук на доске, кот наблюдает.",
    }
    assert _end_frame_image_warranted(scene) is True


def test_end_frame_image_not_warranted_when_extra_cast_in_start_prompt():
    scene = {
        "characters": ["grandma", "cat", "granddaughter"],
        "start_frame_prompt_ru": "Бабушка и внучка за столом, кот рядом.",
    }
    assert _end_frame_image_warranted(scene) is False


def test_end_frame_image_not_warranted_with_russian_character_aliases():
    scene = {
        "characters": ["бабушка", "кот", "внучка"],
        "start_frame_prompt_ru": "Бабушка и внучка за столом, кот рядом.",
    }
    assert _end_frame_image_warranted(scene) is False


def test_end_frame_image_not_warranted_when_start_prompt_already_mentions_zaya():
    scene = {
        "characters": ["grandma", "cat", "zaya"],
        "start_frame_prompt_ru": "Бабушка, рыжий кот и белая кошка у окна.",
    }
    assert _end_frame_image_warranted(scene) is False


def test_end_frame_image_not_warranted_when_start_prompt_already_mentions_markiz():
    scene = {
        "characters": ["grandma", "cat", "markiz"],
        "start_frame_prompt_ru": "Бабушка, рыжий кот и черно-белый кот смотрят на миску.",
    }
    assert _end_frame_image_warranted(scene) is False


def test_image_end_scene_prompt_keeps_explicit_delta_even_without_new_character():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Бабушка месит тесто в миске.",
        "end_frame_prompt": "Кот уже внутри дома и стряхивает капли с мокрой шерсти.",
        "end_frame_requested": True,
    }
    out = _image_end_scene_prompt(scene)
    assert out is not None
    assert "Кот уже внутри дома" in out
    assert "ТОЛЬКО отличия финала" in out


def test_image_end_scene_prompt_ignored_without_explicit_request():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Бабушка месит тесто в миске.",
        "end_frame_prompt": "Кот уже внутри дома и стряхивает капли с мокрой шерсти.",
    }
    assert _image_end_scene_prompt(scene) is None


def test_resolve_scene_references_without_embedded_references():
    scene = {
        "location": "kitchen",
        "characters": [],
        "start_frame_prompt_ru": "В кадре внучка помогает бабушке и коту",
    }
    refs = _resolve_scene_references(scene)
    assert "grandma" in refs
    assert "cat" in refs
    assert "granddaughter" in refs
    assert "location" not in refs


def test_resolve_scene_references_skips_default_cast_for_fruit_only_closeup():
    scene = {
        "characters": ["grandma", "cat", "zaya", "banana", "strawberry", "grandson"],
        "start_frame_prompt_ru": "Крупный план: только банан и клубника разговаривают как в офисе, без бабушки и кота.",
        "references": {
            "grandma": "assets/characters/grandma/base.png",
            "cat": "assets/characters/cat/base.png",
            "zaya": "assets/characters/zaya/base.jpeg",
            "grandson": "assets/characters/grandson/base.png",
            "banana": "assets/characters/banana/base.png",
            "strawberry": "assets/characters/strawberry/base.png",
        },
    }
    refs = _resolve_scene_references(scene)
    assert refs == {
        "banana": "assets/characters/banana/base.png",
        "strawberry": "assets/characters/strawberry/base.png",
    }


def test_resolve_scene_references_normalizes_russian_location():
    scene = {
        "location": "кухня",
        "characters": ["бабушка", "кот"],
    }
    refs = _resolve_scene_references(scene)
    assert "location" not in refs


def test_planned_reference_summary_uses_project_root_for_relative_asset_paths():
    scene = {
        "location": "kitchen",
        "shot_type": "medium_shot",
        "characters": ["grandma", "cat"],
        "references": {
            "grandma": "assets/characters/grandma/base.png",
            "cat": "assets/characters/cat/base.png",
            "location": "assets/locations/kitchen/base.png",
        },
    }
    chars, location, paths = _planned_reference_summary(scene)
    assert chars == "бабушка, рыжий кот"
    assert location == "—"
    assert "assets/characters/grandma/base.png" in paths
    assert "assets/characters/cat/base.png" in paths
    assert "assets/locations/kitchen/base.png" not in paths


def test_planned_reference_summary_keeps_declared_paths_even_if_file_missing():
    scene = {
        "location": "kitchen",
        "shot_type": "medium_shot",
        "characters": ["grandma", "cat"],
        "references": {
            "grandma": "missing/grandma.png",
            "cat": "missing/cat.png",
            "location": "missing/location.png",
        },
    }
    chars, location, paths = _planned_reference_summary(scene)
    assert chars == "бабушка, рыжий кот"
    assert location == "—"
    assert paths == ["missing/grandma.png", "missing/cat.png"]


def test_loaded_reference_paths_reports_only_existing_files():
    scene = {
        "location": "kitchen",
        "shot_type": "medium_shot",
        "characters": ["grandma", "cat"],
        "references": {
            "grandma": "assets/characters/grandma/base.png",
            "cat": "assets/characters/cat/base.png",
            "location": "assets/locations/kitchen/base.png",
        },
    }
    # В тестовой среде ассеты могут отсутствовать, но функция должна стабильно возвращать список.
    out = _loaded_reference_paths(scene)
    assert isinstance(out, list)


def test_scene_needs_end_frame_step_handles_missing_import_regression():
    scene = {
        "characters": ["grandma", "cat", "granddaughter"],
        "start_frame_prompt_ru": "Бабушка и кот на кухне.",
        "end_frame_prompt": "В кадр входит внучка и берёт тарелку.",
        "end_frame_requested": True,
    }
    assert _scene_needs_end_frame_step(scene) is True


def test_scene_needs_end_frame_step_false_for_pusto():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Бабушка и кот на кухне.",
        "end_frame_prompt": "пусто",
    }
    assert _scene_needs_end_frame_step(scene) is False


def test_scene_needs_end_frame_step_false_without_explicit_request():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Бабушка и кот на кухне.",
        "end_frame_prompt": "Кот уже внутри дома.",
    }
    assert _scene_needs_end_frame_step(scene) is False


def test_start_prompt_feedback_switches_scene_to_closeup(monkeypatch):
    async def _fake_call(*_args, **_kwargs):
        return "Крупный план. Новый стартовый промпт."

    async def _fake_send_next(_update, _session):
        return None

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _fake_call)
    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", _fake_send_next)

    scene = {"shot_type": "medium", "start_frame_prompt": "Старый промпт", "start_frame_prompt_ru": "Старый промпт"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={},
            scene=scene,
            text="ФИДБЕК: Сделай крупный план кота у двери и сохрани референс",
        )
    )

    assert out is True
    assert scene["shot_type"] == "closeup"
    assert scene["start_frame_prompt"] == "Крупный план. Новый стартовый промпт."
    assert scene["start_frame_prompt_ru"] == "Крупный план. Новый стартовый промпт."
    assert any("Перегенерирую стартовый промпт" in text for text in messages)


def test_start_prompt_feedback_does_not_enable_end_frame_without_button(monkeypatch):
    async def _fake_call(*_args, **_kwargs):
        return "Новый стартовый промпт."

    async def _fake_send_next(_update, _session):
        return None

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = (text, kwargs)

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _fake_call)
    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", _fake_send_next)

    scene = {
        "start_frame_prompt": "Старый промпт",
        "end_frame_prompt": "Кот уже внутри дома.",
    }
    out = asyncio.run(
        handlers_state_media._scene_media_start_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"image_scene_index": 0},
            scene=scene,
            text="ФИДБЕК: в этой сцене нужен конечный кадр, где кот уже внутри дома",
        )
    )

    assert out is True
    assert scene.get("end_frame_requested") is not True


def test_end_frame_request_button_enables_end_frame(monkeypatch):
    sent: list[str] = []

    async def _fake_send_next(_update, session):
        sent.append(session["scene_media_substep"])

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            sent.append(text)

    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", _fake_send_next)

    scene = {
        "start_frame_prompt": "Старт",
        "end_frame_prompt": "Кот уже внутри дома.",
    }
    session = {"image_scene_index": 0, "scene_media_substep": "start_image_approval"}

    out = asyncio.run(
        handlers_state_media._handle_scene_media_common_restarts(
            SimpleNamespace(message=_Msg()),
            session,
            scene,
            handlers_state_media.SCENE_MEDIA_REQUEST_END_FRAME_BUTTON,
        )
    )

    assert out is True
    assert scene["end_frame_requested"] is True
    assert session["scene_media_substep"] == "end_prompt"
    assert "end_prompt" in sent


def test_send_next_scene_media_step_renders_start_image_approval_not_video():
    photos: list[str] = []
    replies: list[tuple[str, object | None]] = []

    class _Msg:
        async def reply_photo(self, photo, caption):
            _ = photo
            photos.append(caption)

        async def reply_text(self, text, reply_markup=None):
            replies.append((text, reply_markup))

    scene = {
        "start_frame_image_b64": _encode_b64(b"start-image"),
        "end_frame_prompt": "Финальный кадр",
        "video_prompt": "video prompt",
    }
    session = {
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "start_image_approval",
    }

    asyncio.run(
        handlers_media_scene._send_next_scene_media_step(
            SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=1)),
            session,
        )
    )

    assert photos == ["Сцена 1: стартовый кадр."]
    text, markup = replies[-1]
    assert "Проверьте стартовый кадр" in text
    buttons = [button.text for row in markup.keyboard for button in row]
    assert "Нужен конечный кадр" in buttons
    assert "Без конечного кадра" in buttons
    assert "ИЗМЕНИТЬ СКОРОСТЬ" not in buttons


def test_start_prompt_manual_override_switches_scene_to_closeup(monkeypatch):
    messages: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        _ = (update, context, chat_id, session, idx)
        messages.append(f"GEN:{scene.get('start_frame_prompt')}")
        return True

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)

    scene = {"shot_type": "medium", "start_frame_prompt": "Старый промпт", "start_frame_prompt_ru": "Старый промпт"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={},
            scene=scene,
            text="ПРОМПТ: Крупный план у двери, кот маленький по масштабу, без baby-face",
        )
    )

    assert out is True
    assert scene["shot_type"] == "closeup"
    assert scene["start_frame_prompt"] == "Крупный план у двери, кот маленький по масштабу, без baby-face"
    assert scene["start_frame_prompt_ru"] == "Крупный план у двери, кот маленький по масштабу, без baby-face"
    assert any("Сразу генерирую стартовый кадр" in text for text in messages)
    assert any(text.startswith("GEN:") for text in messages)


def test_start_prompt_feedback_rewrites_medium_prefix_to_closeup(monkeypatch):
    async def _fake_call(*_args, **_kwargs):
        return "Средний план. Кот у двери как на референсе."

    async def _fake_send_next(_update, _session):
        return None

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            _ = kwargs

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _fake_call)
    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", _fake_send_next)

    scene = {"shot_type": "medium", "start_frame_prompt": "Старый промпт", "start_frame_prompt_ru": "Старый промпт"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={},
            scene=scene,
            text="ФИДБЕК: Сделай крупный план и сохрани кота как на референсе",
        )
    )

    assert out is True
    assert scene["shot_type"] == "closeup"
    assert scene["start_frame_prompt"] == "Крупный план. Кот у двери как на референсе."


def test_waiting_scene_media_ok_on_video_step_triggers_video_generation(monkeypatch):
    called = {"video_ok": False}

    async def _fake_video_ok(update, **kwargs):
        called["video_ok"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {"json_plan": [{"video_prompt": "test prompt"}]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК",
        )
    )
    assert out is True
    assert called["video_ok"] is True


def test_waiting_scene_media_autoapprove_runs_to_completion(monkeypatch):
    calls: list[str] = []

    async def _fake_start_ok(update, *, session, scene, idx):
        calls.append(f"start:{idx}")
        scene["start_frame_image_b64"] = _encode_b64(f"image-{idx}".encode())
        session["scene_media_substep"] = "start_image_approval"
        return True

    async def _fake_video_ok(update, *, context=None, chat_id=None, session, scene, idx, total):
        calls.append(f"video:{idx}")
        scene["video_results_by_lang"] = {"ru": {"video_b64": _encode_b64(b"video")}}
        session["image_scene_index"] = idx + 1
        if session["image_scene_index"] >= total:
            session["state"] = "ready_for_production"
        else:
            session["scene_media_substep"] = "start_prompt"
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)
    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {
            "json_plan": [
                {"start_frame_prompt": "scene 1", "video_prompt": "video 1"},
                {"start_frame_prompt": "scene 2", "video_prompt": "video 2"},
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="Автоапрув",
        )
    )
    assert out is True
    assert calls == ["start:0", "video:0", "start:1", "video:1"]
    assert session["state"] == "ready_for_production"
    assert handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY not in session
    assert any("Автоапрув включён" in message for message in messages)


def test_waiting_scene_media_ai_approve_enables_ai_mode(monkeypatch):
    called = {"ai": False}

    async def _fake_ai_runner(update, *, context=None, chat_id=None, session):
        called["ai"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_run_scene_media_ai_approve", _fake_ai_runner)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {"json_plan": [{"start_frame_prompt": "scene 1", "video_prompt": "video 1"}]},
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="🤖 ИИ апрув",
        )
    )

    assert out is True
    assert called["ai"] is True
    assert session[handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] == "ai"
    assert any("Включён 🤖 ИИ апрув" in message for message in messages)


def test_waiting_scene_media_stop_returns_to_manual_mode(monkeypatch):
    called = {"shown": False}

    async def _fake_show_manual(update, session):
        called["shown"] = True
        return None

    monkeypatch.setattr(handlers_state_media, "_show_manual_scene_media_controls", _fake_show_manual)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {"json_plan": [{"start_frame_prompt": "scene 1", "video_prompt": "video 1"}]},
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY: handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_AI,
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="СТОП",
        )
    )

    assert out is True
    assert called["shown"] is True
    assert session[handlers_media_scene.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] == "manual"
    assert any("Автоапрув остановлен" in message for message in messages)


def test_ai_approve_start_skips_prompt_check_before_generating_image(monkeypatch):
    calls: list[str] = []

    async def _fake_prompt_check(update, *, session, scene, idx, start_frame_prompt):
        calls.append("prompt_check")
        assert "grandma cooks" in start_frame_prompt
        return "APPROVE"

    async def _fake_generate_image(prompt, reference_images=None):
        calls.append("generate_image")
        return b"image"

    async def _fake_reply_photo(_message, *, photo, caption):
        calls.append("reply_photo")

    async def _fake_image_check(update, *, session, scene, idx, image_bytes):
        calls.append("image_check")
        return "APPROVE"

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _fake_generate_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(handlers_state_media, "_run_ai_prompt_check", _fake_prompt_check)
    monkeypatch.setattr(handlers_state_media, "_run_ai_start_frame_check", _fake_image_check)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {"scene_media_approve_mode": "ai", "target_languages": ["ru"]}
    scene = {"start_frame_prompt": "grandma cooks", "characters": ["grandma", "cat"]}

    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            update,
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    assert calls == ["generate_image", "reply_photo", "image_check"]
    assert "start_frame_image_b64" in scene


def test_start_frame_generation_includes_user_reference_images(monkeypatch):
    calls: list[dict] = []

    async def _gen_image(prompt, *, reference_images=None, model=None):
        calls.append({"prompt": prompt, "reference_images": reference_images, "model": model})
        return SimpleNamespace(data=b"image", model=model)

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_scene_image_result", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(handlers_state_media, "_load_scene_reference_images", lambda *_args, **_kwargs: [b"char-ref"])

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    scene = {
        "start_frame_prompt": "grandma cooks",
        "characters": ["grandma", "cat"],
        "scene_user_reference_images_by_kind": {"start": [_encode_b64(b"user-ref")]},
    }

    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"target_languages": ["ru"]},
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    assert calls[0]["reference_images"] == [b"char-ref", b"user-ref"]


def test_scene_media_reference_photo_adds_image_to_current_scene():
    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    scene = {"start_frame_prompt": "grandma cooks"}
    session = {
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_image_reference_pending": {"scene_idx": 0, "target": "start"},
    }

    handled = asyncio.run(
        handlers_state_media.handle_scene_media_reference_photo(
            SimpleNamespace(message=_Msg()),
            session=session,
            image_bytes=b"user-ref",
        )
    )

    assert handled is True
    assert _decode_b64(scene["scene_user_reference_images_by_kind"]["start"][0]) == b"user-ref"
    assert "Добавила изображение #1" in messages[-1]


def test_ai_approve_reject_uses_primary_image_model(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_prompt_check(update, *, session, scene, idx, start_frame_prompt):
        return "APPROVE"

    async def _fake_generate_image(prompt, reference_images=None, model=None):
        calls.append(model)
        return b"image"

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    async def _fake_image_check(update, *, session, scene, idx, image_bytes):
        return "REGENERATE_IMAGE"

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _fake_generate_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(handlers_state_media, "_run_ai_prompt_check", _fake_prompt_check)
    monkeypatch.setattr(handlers_state_media, "_run_ai_start_frame_check", _fake_image_check)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {"scene_media_approve_mode": "ai", "target_languages": ["ru"]}
    scene = {
        "start_frame_prompt": "grandma cooks",
        "characters": ["grandma", "cat"],
        "start_frame_image_model": "gemini-2.5-flash-image",
    }

    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            update,
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    assert calls == ["gemini-3-pro-image"]
    assert scene["start_frame_image_model"] == "gemini-3-pro-image"
    assert "start_frame_image_b64" not in scene
    assert session["scene_media_substep"] == "start_prompt"
    assert any("текущем профиле генерации" in message for message in messages)


def test_waiting_scene_media_autoapprove_approves_existing_videos_without_regeneration(monkeypatch):
    async def _must_not_generate_video(*args, **kwargs):
        raise AssertionError("already generated videos should only be approved")

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _must_not_generate_video)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"ru")},
                        "en": {"video_b64": _encode_b64(b"en")},
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {"ru": "p-ru", "en": "p-en"},
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="Автоапрув",
        )
    )
    assert out is True
    assert session["state"] == "ready_for_production"
    assert session["production"]["json_plan"][0]["video_approvals_by_lang"] == {
        "ru": True,
        "en": True,
    }


def test_start_image_approval_edit_uses_latest_image_as_reference(monkeypatch):
    calls: list[dict] = []

    async def _gen_image(prompt, reference_images=None):
        calls.append({"prompt": prompt, "reference_images": reference_images})
        return b"edited-image"

    photos: list[bytes] = []

    async def _fake_reply_photo(_message, *, photo, caption):
        photos.append(photo.getvalue())

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "start_frame_image_b64": _encode_b64(b"latest-start"),
        "end_frame_image_b64": _encode_b64(b"old-end"),
        "start_frame_prompt_effective": "grandma cooks",
        "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"video")}},
    }
    session = {"target_languages": ["ru"], "scene_media_substep": "start_image_approval"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            update,
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ИЗМЕНИТЬ: сделай тарелку ближе",
        )
    )
    assert out is True
    assert calls[0]["reference_images"] == [b"latest-start"]
    assert "сделай тарелку ближе" in calls[0]["prompt"]
    assert scene["start_frame_image_b64"] == _encode_b64(b"edited-image")
    assert "end_frame_image_b64" not in scene
    assert scene["video_results_by_lang"] == {}
    assert photos == [b"edited-image"]


def test_image_approval_edit_button_waits_for_instruction():
    messages: list[str] = []
    parse_modes: list[str | None] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)
            parse_modes.append(kwargs.get("parse_mode"))

    update = SimpleNamespace(message=_Msg())
    session = {}
    scene = {"start_frame_image_b64": _encode_b64(b"latest-start")}
    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            update,
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=2,
            text="ИЗМЕНИТЬ",
        )
    )
    assert out is True
    assert session[handlers_state_media.SCENE_IMAGE_EDIT_PENDING_KEY] == {
        "scene_idx": 2,
        "target": "start",
    }
    assert any("что именно поменять" in message for message in messages)


def test_image_approval_edit_button_then_instruction_uses_latest_image(monkeypatch):
    calls: list[dict] = []

    async def _gen_image(prompt, reference_images=None):
        calls.append({"prompt": prompt, "reference_images": reference_images})
        return b"edited-after-button"

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    scene = {
        "start_frame_image_b64": _encode_b64(b"latest-start"),
        "end_frame_image_b64": _encode_b64(b"old-end"),
        "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"old-video")}},
    }
    session = {
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "start_image_approval",
    }

    out_button = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="ИЗМЕНИТЬ",
        )
    )
    out_instruction = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="сделай тарелку ближе",
        )
    )

    assert out_button is True
    assert out_instruction is True
    assert calls[0]["reference_images"] == [b"latest-start"]
    assert "сделай тарелку ближе" in calls[0]["prompt"]
    assert scene["start_frame_image_b64"] == _encode_b64(b"edited-after-button")
    assert "end_frame_image_b64" not in scene
    assert scene["video_results_by_lang"] == {}


def test_end_frame_generation_uses_start_frame_and_first_scene_character_refs(monkeypatch):
    calls: list[dict] = []

    async def _gen_image(prompt, *, reference_images=None, model=None):
        calls.append({"prompt": prompt, "reference_images": reference_images, "model": model})
        return SimpleNamespace(data=b"end-image", model=model)

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_scene_image_result", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)

    def _fake_load_refs(target_scene, **_kwargs):
        chars = target_scene.get("characters") or []
        if chars == ["grandma", "cat"]:
            return [b"grandma-ref", b"cat-ref"]
        if chars == ["grandma", "cat", "zaya"]:
            return [b"grandma-ref", b"cat-ref", b"zaya-ref"]
        return []

    monkeypatch.setattr(handlers_state_media, "_load_scene_reference_images", _fake_load_refs)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    scene = {
        "start_frame_prompt": "бабушка и рыжий кот готовят у стола",
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "end_frame_prompt": "еда готова, белая кошка зая входит в кадр",
        "end_frame_requested": True,
        "characters": ["grandma", "cat", "zaya"],
        "references": {
            "grandma": "assets/characters/grandma/base.png",
            "cat": "assets/characters/cat/base.png",
            "zaya": "assets/characters/zaya/base.jpeg",
        },
    }

    out = asyncio.run(
        handlers_state_media._scene_media_end_ok(
            SimpleNamespace(message=_Msg()),
            session={
                "target_languages": ["ru"],
                "production": {"json_plan": [{"characters": ["grandma", "cat"]}, scene]},
            },
            scene=scene,
            idx=1,
        )
    )

    assert out is True
    assert calls[0]["reference_images"] == [b"start-frame", b"grandma-ref", b"cat-ref", b"zaya-ref"]
    assert scene["end_frame_image_b64"] == _encode_b64(b"end-image")


def test_start_image_feedback_button_waits_for_instruction(monkeypatch):
    messages: list[str] = []

    async def _fail_call(*_args, **_kwargs):
        raise AssertionError("empty feedback button must not call LLM")

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _fail_call)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={},
            scene={"start_frame_image_b64": _encode_b64(b"latest-start")},
            idx=0,
            text="ФИДБЕК:",
        )
    )

    assert out is True
    assert any("Напишите, что исправить" in message for message in messages)


def test_start_image_regenerate_keeps_same_model(monkeypatch):
    calls: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        calls.append(scene.get("start_frame_image_model"))
        return True

    def _fail_if_escalates(*_args, **_kwargs):
        raise AssertionError("plain regenerate must not escalate the image model")

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)
    monkeypatch.setattr(handlers_state_media, "_next_image_quality_model", _fail_if_escalates)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    scene = {"start_frame_image_model": "gemini-2.5-flash-image"}
    session = {}
    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ПЕРЕГЕНЕРИРОВАТЬ",
        )
    )

    assert out is True
    assert calls == ["gemini-2.5-flash-image"]
    assert scene["start_frame_image_model"] == "gemini-2.5-flash-image"


def test_start_image_approval_prompt_override_regenerates_immediately(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        _ = (update, context, chat_id, session, idx)
        calls.append(scene.get("start_frame_prompt"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    scene = {"shot_type": "medium", "start_frame_prompt": "old", "start_frame_prompt_ru": "old"}
    session = {"scene_media_substep": "start_image_approval"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ПРОМПТ: Крупный план у двери, кот как на референсе",
        )
    )

    assert out is True
    assert calls == ["Крупный план у двери, кот как на референсе"]
    assert scene["shot_type"] == "closeup"
    assert scene["start_frame_prompt_raw_mode"] is True
    assert session["scene_media_substep"] == "start_prompt"
    assert any("Сразу перегенерирую стартовый кадр" in text for text in messages)


def test_end_image_approval_prompt_override_regenerates_immediately(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_end_ok(update, *, session, scene, idx):
        _ = (update, session, idx)
        calls.append(scene.get("end_frame_prompt"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_end_ok", _fake_end_ok)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    scene = {"end_frame_prompt": "old end"}
    session = {"scene_media_substep": "end_image_approval"}
    out = asyncio.run(
        handlers_state_media._scene_media_end_image_approval_step(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ПРОМПТ: Новый конечный кадр у двери",
        )
    )

    assert out is True
    assert calls == ["Новый конечный кадр у двери"]
    assert scene["end_frame_requested"] is True
    assert session["scene_media_substep"] == "end_prompt"
    assert any("Сразу перегенерирую конечный кадр" in text for text in messages)


def test_end_prompt_manual_override_regenerates_immediately(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_end_ok(update, *, session, scene, idx):
        _ = (update, session, idx)
        calls.append(scene.get("end_frame_prompt"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_end_ok", _fake_end_ok)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    scene = {"end_frame_prompt": "old end"}
    out = asyncio.run(
        handlers_state_media._scene_media_end_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"image_scene_index": 0},
            scene=scene,
            text="ПРОМПТ: Новый финальный кадр у двери",
        )
    )

    assert out is True
    assert calls == ["Новый финальный кадр у двери"]
    assert scene["end_frame_requested"] is True
    assert scene["end_frame_prompt_raw_mode"] is True
    assert any("Сразу генерирую конечный кадр" in text for text in messages)


def test_end_prompt_plain_regenerate_generates_current_delta(monkeypatch):
    calls: list[str] = []

    async def _fake_end_ok(update, *, session, scene, idx):
        _ = (update, session, idx)
        calls.append(scene.get("end_frame_prompt"))
        return True

    async def _fail_call(*_args, **_kwargs):
        raise AssertionError("plain regenerate must not be treated as end-frame feedback")

    monkeypatch.setattr(handlers_state_media, "_scene_media_end_ok", _fake_end_ok)
    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _fail_call)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            _ = kwargs

    scene = {"end_frame_prompt": "current end delta"}
    out = asyncio.run(
        handlers_state_media._scene_media_end_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"image_scene_index": 0},
            scene=scene,
            text="ПЕРЕГЕНЕРИРОВАТЬ",
        )
    )

    assert out is True
    assert calls == ["current end delta"]
    assert scene["end_frame_prompt"] == "current end delta"


def test_video_prompt_manual_override_uses_raw_prompt_and_generates(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_video_ok(update, *, context, chat_id, session, scene, idx, total):
        _ = (update, context, chat_id, session, idx, total)
        calls.append(scene.get("video_prompt_by_lang", {}).get("ru"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    scene = {
        "video_prompt": "old",
        "video_prompt_by_lang": {"ru": "old"},
        "speech_by_lang": {"ru": "фраза"},
    }
    session = {
        "image_scene_index": 0,
        "scene_media_language_index": 0,
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            text="ПРОМПТ: RAW VIDEO PROMPT",
            language="ru",
        )
    )

    assert out is True
    assert calls == ["RAW VIDEO PROMPT"]
    assert scene["video_prompt_raw_by_lang"]["ru"] is True
    assert any("Сразу генерирую видео" in text for text in messages)


def test_start_prompt_button_then_plain_text_is_treated_as_manual_prompt(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        _ = (update, context, chat_id, session, idx)
        calls.append(scene.get("start_frame_prompt"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    session = {"image_scene_index": 0, "scene_prompt_input_pending": {"scene_idx": 0, "target": "start_pre", "language": ""}}
    scene = {"shot_type": "medium", "start_frame_prompt": "old", "start_frame_prompt_ru": "old"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            text="Крупный план рыжего кота у двери",
        )
    )

    assert out is True
    assert calls == ["Крупный план рыжего кота у двери"]
    assert "scene_prompt_input_pending" not in session
    assert any("Сразу генерирую стартовый кадр" in text for text in messages)


def test_video_prompt_button_then_plain_text_is_treated_as_manual_prompt(monkeypatch):
    calls: list[str] = []

    async def _fake_video_ok(update, *, context, chat_id, session, scene, idx, total):
        _ = (update, context, chat_id, session, idx, total)
        calls.append(scene.get("video_prompt_by_lang", {}).get("ru"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            _ = kwargs

    scene = {
        "video_prompt": "old",
        "video_prompt_by_lang": {"ru": "old"},
        "speech_by_lang": {"ru": "фраза"},
    }
    session = {
        "image_scene_index": 0,
        "scene_media_language_index": 0,
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
        "scene_prompt_input_pending": {"scene_idx": 0, "target": "video:ru", "language": "ru"},
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_not_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            text="RAW VIDEO PROMPT 2",
            language="ru",
        )
    )

    assert out is True
    assert calls == ["RAW VIDEO PROMPT 2"]
    assert "scene_prompt_input_pending" not in session


def test_start_ok_uses_raw_manual_prompt_without_identity_block(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_generate(prompt, reference_images=None, model=None):
        captured["prompt"] = prompt
        captured["refs"] = reference_images
        captured["model"] = model
        return SimpleNamespace(data=b"img", model="gemini-3-pro-image")

    async def _fake_reply_photo(_message, *, photo, caption):
        _ = (photo, caption)
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(handlers_state_media, "_generate_scene_image_result", _fake_generate)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(handlers_state_media, "register_image_candidate", lambda *args, **kwargs: {"candidate_id": "c1"})
    monkeypatch.setattr(handlers_state_media, "register_media_message_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers_state_media, "add_scene_shared_cost", lambda *args, **kwargs: None)
    async def _fake_reply_with_markup(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers_state_media, "_reply_text_with_optional_markup", _fake_reply_with_markup)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            _ = kwargs
            return None

    scene = {
        "shot_type": "closeup",
        "characters": ["grandma", "cat", "zaya"],
        "romantic_drama_beat": "Белая кошка смотрит на рыжего кота",
        "start_frame_prompt": "Крупный план рыжего кота у двери",
        "start_frame_prompt_raw_mode": True,
    }
    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"target_languages": ["ru"]},
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    assert captured["prompt"] == "Крупный план рыжего кота у двери"
    assert "Персонажи в кадре" not in str(captured["prompt"])
    assert "Фоновая драма" not in str(captured["prompt"])


def test_image_approval_text_uses_raw_prompt_verbatim():
    scene = {
        "scene": 1,
        "shot_type": "closeup",
        "characters": ["grandma", "cat", "zaya"],
        "romantic_drama_beat": "Белая кошка смотрит на рыжего кота",
        "start_frame_prompt": "Крупный план рыжего кота у двери",
        "start_frame_prompt_raw_mode": True,
    }
    text = handlers_media_scene._image_approval_text(scene, 1, 4)
    assert "Крупный план рыжего кота у двери" in text
    assert "Режим raw prompt" in text
    assert "Персонажи в кадре" not in text
    assert "Фоновая драма этой сцены" not in text


def test_start_image_upgrade_model_command_does_not_regenerate(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        calls.append(scene.get("start_frame_image_model"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)
    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)

    scene = {"start_frame_image_model": "gemini-2.5-flash-image"}
    session = {}
    out = asyncio.run(
        handlers_state_media._scene_media_start_image_approval_step(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ПОВЫСИТЬ МОДЕЛЬ",
        )
    )

    assert out is True
    assert scene["start_frame_image_model"] == "gemini-2.5-flash-image"
    assert calls == []
    assert any("Модель уже максимальная" in message for message in messages)


def test_start_image_generation_failure_asks_for_model_choice(monkeypatch):
    messages: list[tuple[str, object | None]] = []

    async def _fake_generate(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(handlers_state_media, "_image_scene_prompt", lambda _scene: "prompt")
    monkeypatch.setattr(handlers_state_media, "_load_scene_reference_images", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(handlers_state_media, "_generate_scene_image_result", _fake_generate)
    monkeypatch.setattr(handlers_state_media, "primary_image_generation_model", lambda: "strong-model")
    monkeypatch.setattr(handlers_state_media, "_next_image_quality_model", lambda model: "lower-model" if model == "strong-model" else None)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append((text, kwargs.get("reply_markup")))

    scene = {"start_frame_prompt": "prompt"}
    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session={"target_languages": ["ru"]},
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    pending = scene.get(handlers_state_media.SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY)
    assert pending["target"] == "start"
    assert pending["retry_model"] == "strong-model"
    assert pending["lower_model"] == "lower-model"
    assert any(
        "ПОНИЗИТЬ МОДЕЛЬ" in getattr(btn, "text", "")
        for _text, markup in messages
        if hasattr(markup, "keyboard")
        for row in markup.keyboard
        for btn in row
    )
    assert any("Не удалось сгенерировать стартовый кадр" in text for text, _markup in messages)


def test_image_model_choice_downgrade_regenerates_start_frame(monkeypatch):
    calls: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        _ = (update, context, chat_id, session, idx)
        calls.append(scene.get("start_frame_image_model"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            _ = kwargs
            return None

    scene = {
        handlers_state_media.SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY: {
            "target": "start",
            "retry_model": "strong-model",
            "lower_model": "lower-model",
        }
    }
    session = {"image_scene_index": 0, "production": {"json_plan": [scene]}}
    out = asyncio.run(
        handlers_state_media._handle_scene_image_model_choice_pending(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="ПОНИЗИТЬ МОДЕЛЬ",
        )
    )

    assert out is True
    assert calls == ["lower-model"]
    assert scene["start_frame_image_model_manual"] is True
    assert handlers_state_media.SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY not in scene
    assert session["scene_media_substep"] == handlers_state_media.SCENE_MEDIA_SUBSTEP_START


def test_finish_scene_media_production_waits_for_final_delivery_before_social_descriptions(monkeypatch):
    raw = """
    {
      "youtube_ru_title": "Драники как у бабушки",
      "youtube_ru_description": "Живое описание\\nИнгредиенты: картофель 5 шт\\nШаги: натереть, отжать, пожарить\\n#убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "rutube_ru_title": "Драники для RuTube",
      "rutube_ru_description": "RuTube описание с ингредиентами и шагами. #убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "instagram_ru": "Инстаграм текст с ингредиентами 5 шт и шагами. #убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "vk_ru": "ВК текст с ингредиентами 5 шт и шагами. #убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "tiktok_ru": "Тикток текст с ингредиентами 5 шт и шагами. #убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "instagram_en": "English Instagram text with ingredients and steps. #убабушкивкуснее #бабушкинырецепты #драники #рецепты",
      "youtube_en_title": "Grandma style potato pancakes",
      "youtube_en_description": "English YouTube description with ingredients and steps. #убабушкивкуснее #бабушкинырецепты #драники #рецепты"
    }
    """
    monkeypatch.setattr(handlers_state_media, "gemini_generate_sync", lambda _prompt: raw)

    messages: list[str] = []
    parse_modes: list[str | None] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append(text)
            parse_modes.append(kwargs.get("parse_mode"))

    update = SimpleNamespace(message=_Msg())
    session = {
        "recipe_full_text": "Ингредиенты: картофель 5 шт, лук 1 шт. Натереть, отжать, пожарить.",
        handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY: True,
    }
    out = asyncio.run(handlers_state_media._finish_scene_media_production(update, session))
    assert out is True
    assert session["state"] == handlers_state_media.STATE_READY_FOR_PRODUCTION
    assert handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY not in session
    assert handlers_state_media.SOCIAL_DESCRIPTIONS_SENT_KEY not in session
    assert handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY not in session
    assert parse_modes and all(mode is None for mode in parse_modes)
    assert any("Жду финальный рендер и доставку готовых mp4" in m for m in messages)


def test_social_descriptions_selection_toggles_targets_and_video_variant():
    copy = handlers_state_media._fallback_social_copy("Картофель натереть и пожарить.")
    pending = {
        "copy": copy,
        handlers_state_media.SOCIAL_PUBLISH_TARGETS_KEY: [
            target.key for target in handlers_state_media.build_publication_targets(copy)
        ],
        handlers_state_media.SOCIAL_PUBLISH_VIDEO_VARIANT_KEY: handlers_state_media.DEFAULT_PUBLICATION_VIDEO_VARIANT,
    }

    assert handlers_state_media._toggle_social_publication_selection(pending, "✅ TikTok RU") is True
    assert "tiktok:ru" not in pending[handlers_state_media.SOCIAL_PUBLISH_TARGETS_KEY]
    assert handlers_state_media._toggle_social_publication_selection(pending, "☐ video: clean master") is True
    assert pending[handlers_state_media.SOCIAL_PUBLISH_VIDEO_VARIANT_KEY] == "clean"
    assert handlers_state_media._apply_social_publication_time(pending, "Время 2026-06-24 18:30") is True

    summary = handlers_state_media._social_publication_selection_summary(pending)

    assert "☐ TikTok RU" in summary
    assert "Версия видео: clean master" in summary
    assert "Время публикации: 2026-06-24 18:30 MSK" in summary


def test_finish_scene_media_production_queues_final_build_worker(monkeypatch):
    raw = """
    {
      "youtube_ru_title": "Драники как у бабушки",
      "youtube_ru_description": "Описание\\n#убабушкивкуснее #бабушкинырецепты #рецепты"
    }
    """
    monkeypatch.setattr(handlers_state_media, "gemini_generate_sync", lambda _prompt: raw)

    queued: list[dict[str, object]] = []
    messages: list[str] = []

    def _fake_queue(chat_id, session_arg, scenes, **kwargs):
        queued.append({"chat_id": chat_id, "session": session_arg, "scenes": scenes, **kwargs})
        return {
            "run_id": "run-1",
            "languages": ["ru", "en"],
            "variants": ["clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"],
        }

    monkeypatch.setattr(handlers_state_media, "queue_final_builds_for_session", _fake_queue)

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    update = SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=123))
    session = {
        "recipe_full_text": "Ингредиенты: картофель.",
        "production": {"json_plan": [{"video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"clip")}}}]},
    }

    out = asyncio.run(handlers_state_media._finish_scene_media_production(update, session))

    assert out is True
    assert queued == [
        {
            "chat_id": 123,
            "session": session,
            "scenes": session["production"]["json_plan"],
            "advance_series_after_success": True,
        }
    ]
    assert any("Финальная сборка поставлена в очередь final_build_worker" in m for m in messages)
    assert any("Run: run-1" in m for m in messages)


def test_build_post_production_with_retries_passes_overlay_title(monkeypatch):
    captured: dict[str, object] = {}
    advanced: list[dict] = []

    def _fake_build(
        _scenes,
        *,
        project_slug=None,
        overlay_title_by_lang=None,
        overlay_title_style=None,
        overlay_series_title_by_lang=None,
        overlay_series_counter_by_lang=None,
    ):
        captured["project_slug"] = project_slug
        captured["overlay_title_by_lang"] = overlay_title_by_lang
        captured["overlay_title_style"] = overlay_title_style
        captured["overlay_series_title_by_lang"] = overlay_series_title_by_lang
        captured["overlay_series_counter_by_lang"] = overlay_series_counter_by_lang
        return {"ok": True}

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = (text, kwargs)

    update = SimpleNamespace(message=_Msg())
    session = {"selected_project_key": "grandma_kitchen"}
    production = {
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15},
        "series_title_by_lang": {"ru": "Супы у бабушки", "en": "Grandma's Soups"},
        "series_counter_by_lang": {"ru": "день 2 из 7", "en": "day 2 of 7"},
    }

    monkeypatch.setattr(handlers_state_media, "build_post_production", _fake_build)
    monkeypatch.setattr(handlers_state_media, "persist_post_production_artifacts", lambda _session, _post: None)
    monkeypatch.setattr(
        handlers_state_media,
        "advance_series_after_successful_render",
        lambda session: advanced.append(session),
    )

    out = asyncio.run(
        handlers_state_media._build_post_production_with_retries(
            update,
            session=session,
            production=production,
            scenes=[{"scene": 1}],
        )
    )

    assert out == {"ok": True}
    assert captured["project_slug"] == "grandma_kitchen"
    assert captured["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}
    assert captured["overlay_title_style"] == {"title_color": "#c72638", "top_margin_ratio": 0.15}
    assert captured["overlay_series_title_by_lang"] == {"ru": "Супы у бабушки", "en": "Grandma's Soups"}
    assert captured["overlay_series_counter_by_lang"] == {"ru": "день 2 из 7", "en": "day 2 of 7"}
    assert advanced == [session]


def test_finish_scene_media_production_reports_queue_failure(monkeypatch):
    raw = """
    {
      "youtube_ru_title": "Драники как у бабушки",
      "youtube_ru_description": "Описание\\n#убабушкивкуснее #бабушкинырецепты #рецепты"
    }
    """
    monkeypatch.setattr(handlers_state_media, "gemini_generate_sync", lambda _prompt: raw)
    monkeypatch.setattr(
        handlers_state_media,
        "queue_final_builds_for_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

    update = SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=123))
    session = {
        "recipe_full_text": "Ингредиенты: картофель.",
        "production": {"json_plan": [{"video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"clip")}}}]},
    }

    out = asyncio.run(handlers_state_media._finish_scene_media_production(update, session))

    assert out is True
    assert any("Финальную сборку ролика пока не удалось поставить в очередь" in m for m in messages)
    assert any("queue unavailable" in m for m in messages)


def test_finish_scene_media_production_does_not_send_final_videos_inline(monkeypatch):
    raw = """
    {
      "youtube_ru_title": "Драники как у бабушки",
      "youtube_ru_description": "Описание\\n#убабушкивкуснее #бабушкинырецепты #рецепты"
    }
    """
    monkeypatch.setattr(handlers_state_media, "gemini_generate_sync", lambda _prompt: raw)
    monkeypatch.setattr(
        handlers_state_media,
        "queue_final_builds_for_session",
        lambda *_args, **_kwargs: {
            "run_id": "run-1",
            "languages": ["ru"],
            "variants": ["clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"],
        },
    )

    messages: list[str] = []
    documents: list[tuple[bytes, str, str]] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            _ = kwargs
            messages.append(text)

        async def reply_document(self, *, document, caption, filename=None):
            documents.append((document.read(), filename or getattr(document, "name", ""), caption))
            return SimpleNamespace(message_id=len(documents))

    update = SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=123))
    session = {
        "recipe_full_text": "Ингредиенты: картофель.",
        "production": {"json_plan": [{"video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"clip")}}}]},
    }

    out = asyncio.run(handlers_state_media._finish_scene_media_production(update, session))

    assert out is True
    assert session["state"] == handlers_state_media.STATE_READY_FOR_PRODUCTION
    assert documents == []
    assert any("Бот больше не будет держать тяжелый ffmpeg-рендер" in m for m in messages)
    assert not any("Финальную сборку ролика пока не удалось завершить" in m for m in messages)


def test_social_copy_cleaner_removes_character_names_and_keys():
    out = handlers_state_media._clean_social_copy_message("Zaya, zaya, Markiz и Timosha готовят пирог.")

    assert "Zaya" not in out
    assert "zaya" not in out
    assert "Markiz" not in out
    assert "Timosha" not in out
    assert "белая кошка" in out
    assert "черно-белый кот" in out
    assert "рыжий кот" in out


def test_social_descriptions_approval_ok_finishes_production():
    messages: list[tuple[str, object | None]] = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            messages.append((text, kwargs.get("reply_markup")))

    update = SimpleNamespace(message=_Msg())
    session = {
        "state": handlers_state_media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL,
        handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY: {
            "recipe": "Картофель натереть и пожарить.",
            "copy": handlers_state_media._fallback_social_copy("Картофель натереть и пожарить."),
            "origin": "production_final",
        },
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_social_descriptions_approval_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК",
        )
    )
    assert out is True
    assert session["state"] == "ready_for_production"
    assert session[handlers_state_media.SOCIAL_DESCRIPTIONS_SENT_KEY] is True
    assert handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY not in session
    assert any("Production завершён" in text for text, _ in messages)


def test_waiting_scene_media_ok_all_languages_moves_to_next_scene():
    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"ru-bytes")},
                        "en": {"video_b64": _encode_b64(b"en-bytes")},
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {"ru": "p-ru", "en": "p-en"},
                },
                {"video_prompt": "next-scene"},
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК все",
        )
    )
    assert out is True
    assert session["image_scene_index"] == 1
    assert session["scene_media_substep"] == "start_prompt"


def test_waiting_scene_media_ok_all_accepts_restored_video_candidates_without_results():
    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {},
                    "video_candidates_by_lang": {
                        "ru": [
                            {
                                "candidate_id": "ru-candidate",
                                "language": "ru",
                                "video_b64": _encode_b64(b"ru-bytes"),
                                "speed_coef": 1.2,
                                "approved_for_production": False,
                            }
                        ],
                        "en": [
                            {
                                "candidate_id": "en-candidate",
                                "language": "en",
                                "video_b64": _encode_b64(b"en-bytes"),
                                "speed_coef": 1.2,
                                "approved_for_production": False,
                            }
                        ],
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {"ru": "p-ru", "en": "p-en"},
                    "video_prompt_policy_candidate_by_lang": {"ru": "stale policy rewrite"},
                },
                {"video_prompt": "next-scene"},
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК все",
        )
    )

    assert out is True
    assert session["image_scene_index"] == 1
    assert session["scene_media_substep"] == "start_prompt"
    first_scene = session["production"]["json_plan"][0]
    assert first_scene["video_approvals_by_lang"] == {"ru": True, "en": True}
    assert first_scene["video_candidates_by_lang"]["ru"][0]["approved_for_production"] is True
    assert first_scene["video_candidates_by_lang"]["en"][0]["approved_for_production"] is True


def test_scene_media_keyboard_prefers_restored_video_candidates_over_stale_policy_retry():
    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru", "en"],
        "scene_media_substep": "video_prompt",
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {},
                    "video_candidates_by_lang": {
                        "ru": [{"candidate_id": "ru-candidate", "language": "ru", "video_b64": "cnU="}],
                        "en": [{"candidate_id": "en-candidate", "language": "en", "video_b64": "ZW4="}],
                    },
                    "video_prompt_by_lang": {"ru": "p-ru", "en": "p-en"},
                    "video_prompt_current_language": "ru",
                    "video_prompt_policy_candidate_by_lang": {"ru": "stale policy rewrite"},
                },
            ]
        },
    }

    snapshot = handlers_state_media.scene_media_keyboard_snapshot(session)

    assert snapshot.state == "scene_media.video.language_approval"
    buttons = [button for row in snapshot.rows for button in row]
    assert "ОК все" in buttons


def test_waiting_scene_media_requires_all_language_approvals_before_next_scene():
    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"ru-bytes")},
                        "en": {"video_b64": _encode_b64(b"en-bytes")},
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {"ru": "p-ru", "en": "p-en"},
                },
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }
    out_ru = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК ru",
        )
    )
    assert out_ru is True
    assert session["image_scene_index"] == 0
    assert session["video_approvals_by_lang"] if "video_approvals_by_lang" in session else True

    out_en = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ОК en",
        )
    )
    assert out_en is True
    assert session["state"] == "ready_for_production"


def test_video_feedback_button_after_generated_video_applies_next_text(monkeypatch):
    messages: list[str] = []
    calls: list[str] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    async def _call_direct(_update, _context, _chat_id, _action_name, op):
        result = op()
        if hasattr(result, "__await__"):
            return await result
        return result

    async def _rewrite(current_prompt: str, feedback: str) -> str:
        calls.append(feedback)
        return f"{current_prompt} // rewritten: {feedback}"

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _call_direct)
    monkeypatch.setattr(handlers_state_media, "_rewrite_video_prompt_by_format_instruction", _rewrite)

    scene = {
        "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"ru-video")}},
        "video_approvals_by_lang": {"ru": False},
        "video_prompt_by_lang": {"ru": "lang:ru old prompt"},
        "video_prompt": "lang:ru old prompt",
    }
    session = {
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    out_button = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="ФИДБЕК:",
        )
    )
    out_feedback = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="камера слишком дёргается",
        )
    )

    assert out_button is True
    assert out_feedback is True
    assert calls == ["камера слишком дёргается"]
    assert "rewritten: камера слишком дёргается" in scene["video_prompt_by_lang"]["ru"]
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"ru-video")
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY]["ru"] is True
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert handlers_state_media.SCENE_VIDEO_FEEDBACK_PENDING_KEY not in session
    assert any("Напишите, что исправить" in message for message in messages)
    assert any("Подготовила обновлённый video prompt" in message for message in messages)
    assert any("Текущий prompt (ru):" in message for message in messages)


def test_waiting_scene_media_retry_button_restarts_auto_shorten(monkeypatch):
    called = {"video_ok": False}

    async def _fake_video_ok(update, **kwargs):
        called["video_ok"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {"json_plan": [{"video_prompt": "test prompt", "text": "x" * 100}]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_SPEECH_PENDING_KEY: {
            "scene_idx": 0,
            "duration_sec": 6,
            "stage": "choice",
        },
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text=handlers_state_media.SCENE_SPEECH_OPTION_RETRY,
        )
    )
    assert out is True
    assert called["video_ok"] is True
    assert handlers_state_media.SCENE_SPEECH_PENDING_KEY not in session
    assert any("повторное автосокращение" in m for m in messages)


def test_waiting_scene_media_manual_phrase_updates_scene_and_waits_for_approval(monkeypatch):
    called = {"video_ok": False}

    async def _fake_video_ok(update, **kwargs):
        called["video_ok"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {
            "json_plan": [
                {
                    "video_prompt": "wide shot\ncharacters speaks: старая реплика",
                    "video_prompt_by_lang": {
                        "ru": "wide shot\ncharacters speaks: старая реплика",
                    },
                    "speech_by_lang": {"ru": "старая реплика"},
                    "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"old-video")}},
                    "video_approvals_by_lang": {"ru": True},
                    "text": "старая реплика",
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_SPEECH_PENDING_KEY: {
            "scene_idx": 0,
            "duration_sec": 6,
            "stage": "manual_phrase",
        },
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="новая короткая фраза",
        )
    )
    assert out is True
    assert called["video_ok"] is False
    assert (
        session["production"]["json_plan"][0]["text"]
        == "новая короткая фраза"
    )
    assert "CHARACTERS SPEAKS: новая короткая фраза" in session["production"]["json_plan"][0]["video_prompt"]
    assert (
        session["production"]["json_plan"][0]["speech_by_lang"]["ru"]
        == "новая короткая фраза"
    )
    assert "CHARACTERS SPEAKS: новая короткая фраза" in session["production"]["json_plan"][0]["video_prompt_by_lang"]["ru"]
    assert "старая реплика" not in session["production"]["json_plan"][0]["video_prompt_by_lang"]["ru"]
    assert "ru" in session["production"]["json_plan"][0]["video_results_by_lang"]
    assert session["production"]["json_plan"][0]["video_approvals_by_lang"]["ru"] is True
    assert session["production"]["json_plan"][0][handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY]["ru"] is True
    assert (
        session[handlers_state_media.SCENE_SPEECH_PENDING_KEY]["stage"]
        == "approval"
    )


def test_waiting_scene_media_manual_phrase_syncs_other_languages(monkeypatch):
    async def _translate(_source_text: str, target_language: str, *, feedback: str = "", previous_translation: str = ""):
        _ = (feedback, previous_translation)
        return f"{target_language}-fresh"

    monkeypatch.setattr(handlers_state_media, "_translate_scene_phrase_adaptive", _translate)

    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_prompt": "wide shot\ncharacters speaks: старая реплика",
                    "video_prompt_by_lang": {
                        "ru": "wide shot\ncharacters speaks: старая реплика",
                        "en": "wide shot\ncharacters speaks: old english",
                    },
                    "speech_by_lang": {"ru": "старая реплика", "en": "old english"},
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"old-video-ru")},
                        "en": {"video_b64": _encode_b64(b"old-video-en")},
                    },
                    "video_approvals_by_lang": {"ru": True, "en": True},
                    "text": "старая реплика",
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_SPEECH_PENDING_KEY: {
            "scene_idx": 0,
            "duration_sec": 6,
            "stage": "manual_phrase",
        },
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="новая короткая фраза",
        )
    )
    assert out is True
    scene = session["production"]["json_plan"][0]
    assert scene["speech_by_lang"]["ru"] == "новая короткая фраза"
    assert scene["speech_by_lang"]["en"] == "en fresh"
    assert "CHARACTERS SPEAKS: en fresh" in scene["video_prompt_by_lang"]["en"]
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"old-video-en")
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY]["en"] is True
    assert scene["video_approvals_by_lang"]["en"] is True


def test_waiting_scene_media_approval_ok_continues_video_generation(monkeypatch):
    called = {"video_ok": False}

    async def _fake_video_ok(update, **kwargs):
        called["video_ok"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {
            "json_plan": [
                {"video_prompt": "wide shot\nCHARACTERS SPEAKS: новая", "text": "новая"}
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_SPEECH_PENDING_KEY: {
            "scene_idx": 0,
            "duration_sec": 6,
            "stage": "approval",
        },
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text=handlers_state_media.SCENE_SPEECH_OPTION_APPROVE,
        )
    )
    assert out is True
    assert called["video_ok"] is True
    assert handlers_state_media.SCENE_SPEECH_PENDING_KEY not in session


def test_waiting_scene_media_approval_edit_button_switches_to_manual_input(monkeypatch):
    called = {"video_ok": False}

    async def _fake_video_ok(update, **kwargs):
        called["video_ok"] = True
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_video_ok", _fake_video_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

    update = SimpleNamespace(message=_Msg())
    session = {
        "production": {
            "json_plan": [
                {"video_prompt": "wide shot\nCHARACTERS SPEAKS: новая", "text": "новая"}
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_SPEECH_PENDING_KEY: {
            "scene_idx": 0,
            "duration_sec": 6,
            "stage": "approval",
        },
    }
    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text=handlers_state_media.SCENE_SPEECH_OPTION_EDIT,
        )
    )
    assert out is True
    assert called["video_ok"] is False
    assert (
        session[handlers_state_media.SCENE_SPEECH_PENDING_KEY]["stage"]
        == "approval_manual_phrase"
    )


def test_scene_media_video_ok_blocks_too_long_speech(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("video generation should not be called for too long speech")

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _must_not_call)

    def _shorten_fail(_scene: dict, _dur: int):
        assert _dur == 8
        assert _scene["duration_sec"] == 8
        return None

    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _shorten_fail)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            raise AssertionError("video should not be sent")

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera",
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 4,
        "text": "x" * 130,  # лимит речи без пауз для 8 сек = 112, всё равно нужен shorten
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media"},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    joined = " ".join(messages)
    assert "сначала увеличила видео до 8 сек" in joined
    assert "нейросети" in joined
    assert "Не удалось автоматически" in joined
    assert scene["duration_sec"] == 8


def test_scene_media_video_ok_sends_only_start_and_optional_end_frames(monkeypatch):
    calls: list[dict] = []

    async def _gen_ok(*args, **kwargs):
        calls.append(kwargs)
        return b"fake-mp4"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "end_frame_image_b64": _encode_b64(b"end-frame"),
        "duration_sec": 6,
        "text": "",
        "references": {
            "grandma": "assets/characters/grandma/base.png",
            "cat": "assets/characters/cat/base.png",
            "location": "assets/locations/kitchen/base.png",
        },
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )

    assert out is True
    assert len(calls) == 1
    assert calls[0]["start_image"] == b"start-frame"
    assert calls[0]["end_image"] == b"end-frame"
    assert "reference_images" not in calls[0]
    assert "references" not in calls[0]


def test_scene_media_video_ok_sends_no_end_frame_when_missing(monkeypatch):
    calls: list[dict] = []

    async def _gen_ok(*args, **kwargs):
        calls.append(kwargs)
        return b"fake-mp4"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    class _Msg:
        async def reply_text(self, _text):
            return None

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "duration_sec": 6,
        "text": "",
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )

    assert out is True
    assert calls[0]["start_image"] == b"start-frame"
    assert calls[0]["end_image"] is None


def test_video_prompt_invalidation_preserves_approved_result_and_requests_regen():
    scene = {
        "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"old-video")}},
        "video_approvals_by_lang": {"ru": True},
    }

    handlers_state_media._invalidate_scene_language_video(scene, "ru")

    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"old-video")
    assert scene["video_approvals_by_lang"]["ru"] is True
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY]["ru"] is True


def test_scene_media_video_ok_regenerates_requested_language_with_existing_result(monkeypatch):
    calls: list[str] = []

    async def _gen_ok(*args, **kwargs):
        calls.append(kwargs["prompt"])
        return b"new-ru-video"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            return None

        async def reply_video(self, *args, **kwargs):
            return SimpleNamespace(message_id=123)

    update = SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=1))
    scene = {
        "model": "veo3_fast",
        "video_prompt_by_lang": {
            "ru": "lang:ru fixed camera voice_lock_full: x. veo3_framing: y.",
            "en": "lang:en fixed camera voice_lock_full: x. veo3_framing: y.",
        },
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "duration_sec": 6,
        "text": "",
        "video_results_by_lang": {
            "ru": {"video_b64": _encode_b64(b"old-ru-video")},
            "en": {"video_b64": _encode_b64(b"old-en-video")},
        },
        "video_approvals_by_lang": {"ru": True, "en": True},
        handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY: {"ru": True},
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru", "en"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )

    assert out is True
    assert len(calls) == 1
    assert calls[0].startswith("lang:ru")
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"new-ru-video")
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"old-en-video")
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert scene["video_approvals_by_lang"]["en"] is True
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY] == {}


def test_regenerate_language_command_scopes_generation_when_all_languages_invalidated(monkeypatch):
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


def test_ok_regenerates_all_changed_languages_in_parallel(monkeypatch):
    batch_requests_seen: list[str] = []
    messages: list[str] = []

    async def _batch(*, requests, model, duration_sec):
        _ = (model, duration_sec)
        batch_requests_seen.extend(req.key for req in requests)
        return {"ru": b"new-ru-video", "en": b"new-en-video"}

    async def _gen_ok(*args, **kwargs):
        raise AssertionError(f"Vertex should not be called after full Syntax batch: {kwargs['prompt']}")

    monkeypatch.setattr(handlers_state_media, "_attempt_syntax_video_fallback_batch", _batch)
    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)
            return None

        async def reply_video(self, *args, **kwargs):
            return SimpleNamespace(message_id=len(messages))

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

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=1)),
            context=None,
            chat_id=1,
            session=session,
            text="ОК",
        )
    )

    assert out is True
    assert batch_requests_seen == ["ru", "en"]
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"new-ru-video")
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"new-en-video")
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert scene["video_approvals_by_lang"]["en"] is False
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY] == {}
    assert any("перегенерирую изменённые видео параллельно" in message.lower() for message in messages)
    assert any("Генерируем видео параллельно для языков: ru, en" in message for message in messages)


def test_safe_reply_text_retries_timed_out(monkeypatch):
    monkeypatch.setattr(handlers_state_media, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    calls = {"count": 0}

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise handlers_state_media.TimedOut("temporary")
            return None

    asyncio.run(handlers_state_media._safe_reply_text(_Msg(), "hello"))

    assert calls["count"] == 2


def test_scene_media_video_ok_preserves_partial_syntax_batch_and_retries_missing_languages(monkeypatch):
    batch_requests_seen: list[str] = []
    messages: list[str] = []
    captions: list[str] = []

    async def _batch(*, requests, model, duration_sec):
        batch_requests_seen.extend(req.key for req in requests)
        assert model == "veo3_fast"
        assert duration_sec == 6
        return {"ru": b"syntax-ru-video"}

    async def _gen_ok(*args, **kwargs):
        raise AssertionError("auto Vertex path must not start after partial Syntax batch")

    async def _fake_reply_video(_message, *, video, caption):
        captions.append(caption)
        return SimpleNamespace(message_id=len(captions))

    monkeypatch.setattr(handlers_state_media, "_attempt_syntax_video_fallback_batch", _batch)
    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", _fake_reply_video)

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            messages.append(_text)
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt_by_lang": {"ru": "lang:ru prompt", "en": "lang:en prompt"},
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "duration_sec": 6,
        "text": "",
        "video_results_by_lang": {},
        "video_approvals_by_lang": {"ru": False, "en": False},
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru", "en"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )

    assert out is True
    assert batch_requests_seen == ["ru", "en"]
    pending = scene["video_provider_choice_pending"]
    assert pending["source"] == "scene_batch"
    assert pending["languages"] == ["en"]
    assert pending["missing_languages"] == ["en"]
    assert set(pending["entries_by_lang"]) == {"en"}
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"syntax-ru-video")
    assert "en" not in scene["video_results_by_lang"]
    assert any("язык ru" in caption for caption in captions)
    assert any("Автоматически в Vertex не ухожу" in text for text in messages)


def test_attempt_syntax_video_fallback_batch_discards_duplicate_language_payloads(monkeypatch):
    def _fake_batch(**_kwargs):
        return {
            "ru": FallbackResult(
                success=True,
                video_bytes=b"same-video",
                video_uri=None,
                provider_message="ok",
                attempt_id="ru-attempt",
            ),
            "en": FallbackResult(
                success=True,
                video_bytes=b"same-video",
                video_uri=None,
                provider_message="ok",
                attempt_id="en-attempt",
            ),
        }

    monkeypatch.setattr(handlers_media_core, "_syntax_fallback_env_enabled", lambda: True)
    monkeypatch.setattr(handlers_media_core, "try_generate_via_syntax_batch", _fake_batch)

    out = asyncio.run(
        handlers_media_core._attempt_syntax_video_fallback_batch(
            requests=[
                handlers_media_core.BatchFallbackRequest(
                    key="ru",
                    prompt="lang:ru prompt",
                    start_image=b"start",
                ),
                handlers_media_core.BatchFallbackRequest(
                    key="en",
                    prompt="lang:en prompt",
                    start_image=b"start",
                ),
            ],
            model="veo3_fast",
            duration_sec=6,
        )
    )

    assert out == {"ru": b"same-video"}


def test_handle_scene_video_provider_choice_pending_batch_syntax_retries_all_languages(monkeypatch):
    batch_requests_seen: list[str] = []
    captions: list[str] = []
    replies: list[tuple[str, object | None]] = []

    async def _batch(*, requests, model, duration_sec):
        batch_requests_seen.extend(req.key for req in requests)
        assert model == "veo3_fast"
        assert duration_sec == 6
        return {"ru": b"video-ru", "en": b"video-en"}

    async def _fake_reply_video(_message, *, video, caption):
        captions.append(caption)
        return SimpleNamespace(message_id=len(captions))

    monkeypatch.setattr(handlers_state_media, "_attempt_syntax_video_fallback_batch", _batch)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", _fake_reply_video)

    class _Msg:
        async def reply_text(self, _text, reply_markup=None, **_kwargs):
            replies.append((_text, reply_markup))
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "duration_sec": 6,
        "video_provider_choice_pending": {
            "source": "scene_batch",
            "languages": ["ru", "en"],
            "entries_by_lang": {
                "ru": handlers_state_media._scene_video_provider_choice_entry(
                    prompt="lang:ru prompt",
                    model="veo3_fast",
                    duration_sec=6,
                    start_image=b"start",
                    end_image=None,
                    quality="1080",
                    aspect_ratio="9:16",
                    audio_enabled=True,
                    syntax_error_message="partial",
                ),
                "en": handlers_state_media._scene_video_provider_choice_entry(
                    prompt="lang:en prompt",
                    model="veo3_fast",
                    duration_sec=6,
                    start_image=b"start",
                    end_image=None,
                    quality="1080",
                    aspect_ratio="9:16",
                    audio_enabled=True,
                    syntax_error_message="partial",
                ),
            },
        },
        "video_results_by_lang": {},
    }
    session = {
        "state": "waiting_scene_media",
        "scene_media_substep": handlers_state_media.SCENE_MEDIA_SUBSTEP_VIDEO,
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
    }

    out = asyncio.run(
        handlers_state_media._handle_scene_video_provider_choice_pending(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="Syntax",
        )
    )

    assert out is True
    assert batch_requests_seen == ["ru", "en"]
    assert "video_provider_choice_pending" not in scene
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"video-ru")
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"video-en")
    assert any("язык ru" in caption for caption in captions)
    assert any("язык en" in caption for caption in captions)
    final_markup = next(markup for _text, markup in reversed(replies) if markup is not None)
    final_buttons = [button.text for row in final_markup.keyboard for button in row]
    assert "ОК все" in final_buttons
    assert "Syntax" not in final_buttons
    assert "Vertex" not in final_buttons


def test_scene_media_keyboard_snapshot_covers_each_visible_state(monkeypatch):
    monkeypatch.setattr(handlers_state_media, "_can_escalate_scene_image_model", lambda *_args: False)
    schema_path = Path(__file__).parents[1] / "docs" / "scene_media_keyboard_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    expected_states = schema["states"]
    state_aliases = schema["state_aliases"]

    def _expected_rows(state_name: str):
        canonical_name = state_aliases.get(state_name, state_name)
        return expected_states[canonical_name]

    def _session(scene, *, substep="video_prompt", **extra):
        session = {
            "state": "waiting_scene_media",
            "scene_media_substep": substep,
            "target_languages": ["ru", "en"],
            "production": {"json_plan": [scene]},
        }
        session.update(extra)
        return session

    cases = [
        (
            "scene_media.start.prompt",
            _session({"end_frame_prompt": "Финал"}, substep="start_prompt"),
        ),
        (
            "scene_media.start.image_approval",
            _session({"end_frame_prompt": "Финал"}, substep="start_image_approval"),
        ),
        (
            "scene_media.end.prompt",
            _session({}, substep="end_prompt"),
        ),
        (
            "scene_media.end.image_approval",
            _session({}, substep="end_image_approval"),
        ),
        (
            "scene_media.video.prompt",
            _session({"video_prompt_by_lang": {"ru": "ru", "en": "en"}}),
        ),
        (
            "scene_media.video.provider_choice",
            _session(
                {
                    "video_provider_choice_pending": {
                        "source": "scene_batch",
                        "languages": ["en"],
                    }
                }
            ),
        ),
        (
            "scene_media.video.syntax_confirmation",
            _session({"syntax_fallback_pending": {"source": "scene"}}),
        ),
        (
            "scene_media.image.model_choice",
            _session(
                {
                    handlers_state_media.SCENE_IMAGE_MODEL_CHOICE_PENDING_KEY: {
                        "lower_model": "imagen-fast",
                    }
                },
                substep="start_prompt",
            ),
        ),
        (
            "scene_media.video.speech.choice",
            _session(
                {},
                scene_speech_shorten_pending={"scene_idx": 0, "stage": "choice"},
            ),
        ),
        (
            "scene_media.video.speech.approval",
            _session(
                {},
                scene_speech_shorten_pending={"scene_idx": 0, "stage": "approval"},
            ),
        ),
        (
            "scene_media.video.speech.manual_phrase",
            _session(
                {},
                scene_speech_shorten_pending={"scene_idx": 0, "stage": "manual_phrase"},
            ),
        ),
        (
            "scene_media.video.speech.approval_manual_phrase",
            _session(
                {},
                scene_speech_shorten_pending={"scene_idx": 0, "stage": "approval_manual_phrase"},
            ),
        ),
        (
            "scene_media.video.speech.user_long_phrase_choice",
            _session(
                {},
                scene_speech_shorten_pending={"scene_idx": 0, "stage": "user_long_phrase_choice"},
            ),
        ),
        (
            "scene_media.video.translation.approval",
            _session(
                {},
                scene_phrase_translation_pending={"scene_idx": 0, "stage": "approval"},
            ),
        ),
        (
            "scene_media.video.translation.confirm",
            _session(
                {},
                scene_phrase_translation_pending={"scene_idx": 0, "stage": "confirm"},
            ),
        ),
        (
            "scene_media.video.translation.revision",
            _session(
                {},
                scene_phrase_translation_pending={"scene_idx": 0, "stage": "revision"},
            ),
        ),
        (
            "scene_media.input.speed_input",
            _session(
                {},
                scene_video_speed_pending={"scene_idx": 0, "language": "ru"},
            ),
        ),
        (
            "scene_media.input.phrase_input",
            _session(
                {},
                scene_phrase_input_pending={"scene_idx": 0, "language": "ru"},
            ),
        ),
        (
            "scene_media.input.feedback_input",
            _session(
                {},
                scene_video_feedback_pending={"scene_idx": 0, "language": "ru"},
            ),
        ),
        (
            "scene_media.input.prompt_input",
            _session(
                {},
                scene_prompt_input_pending={"scene_idx": 0, "target": "video"},
            ),
        ),
        (
            "scene_media.input.image_edit_input",
            _session(
                {},
                scene_image_edit_pending={"scene_idx": 0, "target": "start"},
            ),
        ),
        (
            "scene_media.input.reference_images",
            _session(
                {},
                scene_image_reference_pending={"scene_idx": 0, "target": "start"},
            ),
        ),
        (
            "scene_media.video.policy_retry",
            _session(
                {
                    "video_prompt_policy_candidate_by_lang": {"ru": "rewritten"},
                    "video_prompt_current_language": "ru",
                }
            ),
        ),
        (
            "scene_media.video.language_approval",
            _session(
                {
                    "video_prompt_by_lang": {"ru": "ru", "en": "en"},
                    "video_results_by_lang": {
                        "ru": {"video_b64": "cnU="},
                        "en": {"video_b64": "ZW4="},
                    },
                }
            ),
        ),
        (
            "scene_media.complete",
            {"state": "waiting_scene_media", "production": {"json_plan": []}},
        ),
    ]

    actual_state_names = {expected_state for expected_state, _session_value in cases}
    schema_state_names = set(expected_states) | set(state_aliases)
    assert schema_state_names == set(handlers_state_media.SCENE_MEDIA_KEYBOARD_STATE_NAMES)
    assert actual_state_names == schema_state_names

    for expected_state, session in cases:
        snapshot = handlers_state_media.scene_media_keyboard_snapshot(session)
        assert snapshot.state == expected_state
        assert [list(row) for row in snapshot.rows] == _expected_rows(expected_state)


def test_scene_media_keyboard_schema_covers_dynamic_variants(monkeypatch):
    schema_path = Path(__file__).parents[1] / "docs" / "scene_media_keyboard_schema.json"
    variants = json.loads(schema_path.read_text(encoding="utf-8"))["variants"]

    def _session(scene, *, substep="video_prompt", languages=None, **extra):
        session = {
            "state": "waiting_scene_media",
            "scene_media_substep": substep,
            "target_languages": languages or ["ru", "en"],
            "production": {"json_plan": [scene]},
        }
        session.update(extra)
        return session

    auto_mode = {handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY: True}
    scenarios = {
        "start_prompt.auto_mode": (
            _session({"end_frame_prompt": "Финал"}, substep="start_prompt", **auto_mode),
            False,
        ),
        "video_prompt.single_language": (
            _session({"video_prompt_by_lang": {"ru": "ru"}}, languages=["ru"]),
            False,
        ),
        "provider_choice.single_language": (
            _session(
                {
                    "video_provider_choice_pending": {
                        "source": "scene",
                        "language": "ru",
                    }
                },
                languages=["ru"],
            ),
            False,
        ),
        "start_image.model_upgrade": (
            _session({"end_frame_prompt": "Финал"}, substep="start_image_approval"),
            True,
        ),
        "end_image.model_upgrade": (
            _session({}, substep="end_image_approval"),
            True,
        ),
        "policy_retry.with_start_restart": (
            _session(
                {
                    "video_prompt_policy_candidate_by_lang": {"ru": "rewritten"},
                    "video_prompt_current_language": "ru",
                    "video_prompt_policy_failure_count": 4,
                }
            ),
            False,
        ),
        "language_approval.auto_mode": (
            _session(
                {
                    "video_prompt_by_lang": {"ru": "ru", "en": "en"},
                    "video_results_by_lang": {
                        "ru": {"video_b64": "cnU="},
                        "en": {"video_b64": "ZW4="},
                    },
                },
                **auto_mode,
            ),
            False,
        ),
    }

    assert set(scenarios) == set(variants)
    for scenario_name, (session, can_escalate) in scenarios.items():
        monkeypatch.setattr(
            handlers_state_media,
            "_can_escalate_scene_image_model",
            lambda *_args, enabled=can_escalate: enabled,
        )
        snapshot = handlers_state_media.scene_media_keyboard_snapshot(session)
        expected = variants[scenario_name]
        assert snapshot.state == expected["state"]
        assert [list(row) for row in snapshot.rows] == expected["rows"]


def test_handle_scene_video_provider_choice_pending_vertex_partial_keeps_recovery_keyboard(monkeypatch):
    replies: list[tuple[str, object | None]] = []

    async def _vertex(payload):
        if payload["prompt"].startswith("lang:ru"):
            return b"vertex-ru"
        raise RuntimeError("vertex unavailable")

    async def _fake_reply_video(_message, *, video, caption):
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(handlers_state_media, "run_vertex_fallback_from_session_payload", _vertex)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", _fake_reply_video)

    class _Msg:
        async def reply_text(self, text, reply_markup=None, **_kwargs):
            replies.append((text, reply_markup))

    entries = {
        language: handlers_state_media._scene_video_provider_choice_entry(
            prompt=f"lang:{language} prompt",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"start",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
            syntax_error_message="failed",
        )
        for language in ("ru", "en")
    }
    scene = {
        "video_prompt_by_lang": {"ru": "lang:ru prompt", "en": "lang:en prompt"},
        "video_provider_choice_pending": {
            "source": "scene_batch",
            "languages": ["ru", "en"],
            "entries_by_lang": entries,
        },
        "video_results_by_lang": {},
    }
    session = {
        "state": "waiting_scene_media",
        "scene_media_substep": "video_prompt",
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
    }

    out = asyncio.run(
        handlers_state_media._handle_scene_video_provider_choice_pending(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="Vertex",
        )
    )

    assert out is True
    assert scene["video_provider_choice_pending"]["languages"] == ["en"]
    assert "ru" in scene["video_results_by_lang"]
    final_markup = next(markup for _text, markup in reversed(replies) if markup is not None)
    buttons = [button.text for row in final_markup.keyboard for button in row]
    assert "Syntax" in buttons
    assert "Vertex" in buttons
    assert "ПРОМПТ en:" in buttons
    assert "ПРОМПТ ru:" not in buttons


def test_handle_scene_video_provider_choice_pending_single_failure_redraws_recovery_keyboard(monkeypatch):
    replies: list[tuple[str, object | None]] = []

    async def _syntax(_payload):
        raise RuntimeError("syntax unavailable")

    monkeypatch.setattr(handlers_state_media, "run_syntax_fallback_from_session_payload", _syntax)

    class _Msg:
        async def reply_text(self, text, reply_markup=None, **_kwargs):
            replies.append((text, reply_markup))

    pending = handlers_state_media._scene_video_provider_choice_entry(
        prompt="lang:ru prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=b"start",
        end_image=None,
        quality="1080",
        aspect_ratio="9:16",
        audio_enabled=True,
        syntax_error_message="failed",
    )
    pending.update({"source": "scene", "language": "ru"})
    scene = {
        "video_prompt_by_lang": {"ru": "lang:ru prompt"},
        "video_provider_choice_pending": pending,
        "video_results_by_lang": {},
    }
    session = {
        "state": "waiting_scene_media",
        "scene_media_substep": "video_prompt",
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
    }

    out = asyncio.run(
        handlers_state_media._handle_scene_video_provider_choice_pending(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="Syntax",
        )
    )

    assert out is True
    assert "video_provider_choice_pending" in scene
    final_markup = next(markup for _text, markup in reversed(replies) if markup is not None)
    buttons = [button.text for row in final_markup.keyboard for button in row]
    assert {"Syntax", "Vertex", "ПРОМПТ ru:", "ФРАЗА ru:"} <= set(buttons)


def test_handle_scene_video_provider_choice_pending_updates_retry_prompt():
    replies: list[tuple[str, object | None]] = []

    class _Msg:
        async def reply_text(self, text, reply_markup=None):
            replies.append((text, reply_markup))
            return None

    scene = {
        "model": "veo3_fast",
        "duration_sec": 6,
        "video_prompt_by_lang": {"ru": "lang:ru prompt", "en": "lang:en prompt"},
        "video_provider_choice_pending": {
            "source": "scene_batch",
            "languages": ["en"],
            "entries_by_lang": {
                "en": handlers_state_media._scene_video_provider_choice_entry(
                    prompt="lang:en old",
                    model="veo3_fast",
                    duration_sec=6,
                    start_image=b"start",
                    end_image=None,
                    quality="1080",
                    aspect_ratio="9:16",
                    audio_enabled=True,
                    syntax_error_message="partial",
                ),
            },
        },
        "video_results_by_lang": {},
    }
    session = {
        "state": "waiting_scene_media",
        "scene_media_substep": handlers_state_media.SCENE_MEDIA_SUBSTEP_VIDEO,
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
    }

    out = asyncio.run(
        handlers_state_media._handle_scene_video_provider_choice_pending(
            SimpleNamespace(message=_Msg()),
            context=None,
            chat_id=1,
            session=session,
            text="ПРОМПТ en: warmer camera move",
        )
    )

    assert out is True
    pending = scene["video_provider_choice_pending"]
    assert pending["entries_by_lang"]["en"]["prompt"].startswith("lang:en warmer camera move")
    assert scene["video_prompt_by_lang"]["en"].startswith("lang:en warmer camera move")
    assert replies[-1][1] is not None


def test_run_syntax_fallback_payload_reports_pre_dispatch_failure(monkeypatch):
    async def _attempt(**kwargs):
        assert kwargs["raise_on_dispatch_failure"] is True
        raise handlers_media_core.SyntaxFallbackDispatchError(
            "Telethon-сессия Syntax занята другой попыткой."
        )

    monkeypatch.setattr(handlers_media_core, "_attempt_syntax_video_fallback_bytes", _attempt)
    payload = handlers_media_core.encode_video_provider_choice_for_session(
        handlers_media_core.VideoProviderChoiceRequired(
            prompt="prompt",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"start",
            end_image=None,
            quality="1080",
            aspect_ratio="9:16",
            audio_enabled=True,
            syntax_error_message="failed",
        )
    )

    try:
        asyncio.run(handlers_media_core.run_syntax_fallback_from_session_payload(payload))
    except handlers_media_core.SyntaxFallbackDispatchError as exc:
        assert "Telethon-сессия Syntax занята" in str(exc)
    else:
        raise AssertionError("SyntaxFallbackDispatchError was not raised")


def test_syntax_dispatch_error_user_message():
    cases = [
        (
            "Syntax prompt is longer than 2000 chars, and Gemini could not shorten it",
            "prompt превышает лимит Syntax",
        ),
        (
            "No free Telethon session became available before timeout.",
            "Telethon-сессия Syntax занята",
        ),
    ]
    for provider_message, expected in cases:
        assert handlers_media_core._syntax_failure_happened_before_dispatch(provider_message) is True
        assert expected in handlers_media_core._syntax_dispatch_error_user_message(provider_message)


def test_scene_media_video_ok_auto_shortens_then_waits_for_phrase_approval(monkeypatch):
    called = {"video": False}

    async def _gen_ok(*args, **kwargs):
        called["video"] = True
        return b"fake-mp4"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    def _shorten_ok(_scene: dict, _dur: int):
        assert _dur == 8
        assert _scene["duration_sec"] == 8
        return {
            "text": "yy",
            "video_prompt": (
                "close shot veo3_framing: test. voice_lock_full: lock. "
                "CHARACTERS SPEAKS: yy"
            ),
        }

    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _shorten_ok)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 4,
        "text": "x" * 130,
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "production": {"json_plan": [scene]}},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert called["video"] is False
    assert scene["text"] == "yy"
    assert scene["duration_sec"] == 8
    joined = "\n".join(messages)
    assert "сначала увеличила видео до 8 сек" in joined
    assert "Нейросеть сократила реплику" in joined
    assert "Новая фраза:\nyy" in joined


def test_scene_media_video_ok_auto_shorten_updates_other_languages(monkeypatch):
    async def _gen_ok(*args, **kwargs):
        raise AssertionError("video generation should not start before phrase approval")

    async def _translate(_source_text: str, target_language: str, *, feedback: str = "", previous_translation: str = ""):
        _ = (feedback, previous_translation)
        return f"{target_language}-translated"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)
    monkeypatch.setattr(handlers_state_media, "_translate_scene_phrase_adaptive", _translate)
    monkeypatch.setattr(
        handlers_state_media,
        "shorten_scene_speech_and_sync_video_prompt_sync",
        lambda _scene, _dur: {
            "text": "короткая фраза",
            "video_prompt": "close shot voice_lock_full: lock. CHARACTERS SPEAKS: короткая фраза",
        },
    )

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "video_prompt_by_lang": {
            "ru": "lang:ru fixed camera CHARACTERS SPEAKS: старая русская",
            "en": "lang:en fixed camera CHARACTERS SPEAKS: old english",
        },
        "speech_by_lang": {"ru": "старая русская", "en": "old english"},
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 4,
        "text": "x" * 130,
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru", "en"], "production": {"json_plan": [scene]}},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert scene["speech_by_lang"]["ru"] == "короткая фраза"
    assert scene["speech_by_lang"]["en"] == "en translated"
    assert "CHARACTERS SPEAKS: en translated" in scene["video_prompt_by_lang"]["en"]
    assert any("Переводы обновила для языков: en." in text for text in messages)


def test_scene_media_video_ok_boosts_english_prompt_before_shortening(monkeypatch):
    calls = {"shorten": 0, "video": 0}

    async def _gen_ok(*args, **kwargs):
        calls["video"] += 1
        if kwargs["prompt"].startswith("lang:en"):
            assert "natural-fast spoken delivery" in kwargs["prompt"]
        return b"fake-mp4"

    def _must_not_shorten(*args, **kwargs):
        calls["shorten"] += 1
        raise AssertionError("english shorten should not run when prompt speedup is enough")

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)
    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _must_not_shorten)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", lambda *_args, **_kwargs: asyncio.sleep(0))

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt_by_lang": {
            "ru": "lang:ru prompt CHARACTERS SPEAKS: Коротко",
            "en": "lang:en prompt CHARACTERS SPEAKS: " + ("a" * 70),
        },
        "speech_by_lang": {"ru": "Коротко", "en": "a" * 70},
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 6,
        "video_results_by_lang": {},
        "video_approvals_by_lang": {"ru": False, "en": False},
        "text": "Коротко",
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru", "en"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert calls["shorten"] == 0
    assert "natural-fast spoken delivery" in scene["video_prompt_by_lang"]["en"]


def test_scene_media_video_ok_shortens_only_english_phrase(monkeypatch):
    async def _gen_ok(*args, **kwargs):
        assert kwargs["prompt"].startswith("lang:ru") or kwargs["prompt"].startswith("lang:en")
        return b"fake-mp4"

    def _shorten(scene: dict, duration_sec: int, *, language: str = "ru", current_text: str | None = None, current_video_prompt: str | None = None):
        assert language == "en"
        assert duration_sec == 6
        assert current_text == "old english line" * 8
        return {
            "text": "short english",
            "video_prompt": (current_video_prompt or "") + " CHARACTERS SPEAKS: short english",
        }

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)
    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _shorten)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", lambda *_args, **_kwargs: asyncio.sleep(0))

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt_by_lang": {
            "ru": "lang:ru prompt CHARACTERS SPEAKS: русская фраза",
            "en": "lang:en prompt CHARACTERS SPEAKS: " + ("old english line" * 8),
        },
        "speech_by_lang": {"ru": "русская фраза", "en": "old english line" * 8},
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 6,
        "video_results_by_lang": {"ru": {"video_b64": _encode_b64(b"ru-old")}, "en": {"video_b64": _encode_b64(b"en-old")}},
        "video_approvals_by_lang": {"ru": True, "en": True},
        "text": "русская фраза",
    }

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "target_languages": ["ru", "en"]},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert scene["speech_by_lang"]["ru"] == "русская фраза"
    assert scene["speech_by_lang"]["en"] == "short english"
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"ru-old")
    assert scene["video_approvals_by_lang"]["en"] is False


def test_scene_media_video_ok_extends_duration_to_8_before_shorten(monkeypatch):
    called = {"video": False}

    async def _gen_ok(*args, **kwargs):
        called["video"] = True
        assert kwargs["duration_sec"] == 8
        return b"fake-mp4"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    def _must_not_shorten(*_a, **_k):
        raise AssertionError("shorten should not run when duration can be extended to 8 sec")

    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _must_not_shorten)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 6,
        "text": "x" * 100,  # > 84 (6 sec), <= 112 (8 sec)
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media", "production": {"json_plan": [scene]}},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert called["video"] is True
    assert scene["duration_sec"] == 8
    assert any("увеличила видео до 8 сек" in text for text in messages)


def test_scene_media_video_ok_user_long_phrase_offers_keep_or_shorten(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("video generation should not start before explicit user choice")

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _must_not_call)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            raise AssertionError("video should not be sent")

    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y.",
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 8,
        "text": "x" * 130,
        handlers_state_media.SCENE_PHRASE_ORIGIN_KEY: {"ru": "user"},
    }
    update = SimpleNamespace(message=_Msg())
    session = {"state": "waiting_scene_media", "production": {"json_plan": [scene]}}

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session=session,
            scene=scene,
            idx=0,
            total=1,
        )
    )

    assert out is True
    assert session[handlers_state_media.SCENE_SPEECH_PENDING_KEY]["stage"] == "user_long_phrase_choice"
    assert any("оставить фразу как есть" in text.lower() for text in messages)


def test_scene_media_start_ok_shows_prompt_before_image_generation(monkeypatch):
    async def _gen_image(_prompt, reference_images=None):
        return b"fake-image"

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    async def _fake_reply_text_with_optional_markup(update, text, reply_markup=None):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(
        handlers_state_media,
        "_reply_text_with_optional_markup",
        _fake_reply_text_with_optional_markup,
    )

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    scene = {
        "start_frame_prompt": "детальный стартовый кадр с видом сверху",
        "video_prompt": "fallback",
    }
    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            update,
            session={"state": "waiting_scene_media"},
            scene=scene,
            idx=0,
        )
    )
    assert out is True
    preview_msgs = [m for m in messages if m.startswith("Промпт перед генерацией стартового кадра:\n")]
    assert preview_msgs
    assert "детальный стартовый кадр с видом сверху" in preview_msgs[0]
    assert any("Генерируем стартовый кадр" in m for m in messages)


def test_scene_media_start_ok_shows_prompt_during_autoapprove(monkeypatch):
    async def _gen_image(_prompt, reference_images=None):
        return b"fake-image"

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    scene = {
        "start_frame_prompt": "авто стартовый промпт",
        "video_prompt": "fallback",
    }
    out = asyncio.run(
        handlers_state_media._scene_media_start_ok(
            update,
            session={handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY: True},
            scene=scene,
            idx=0,
        )
    )
    assert out is True
    assert any(m.startswith("Автоапрув: промпт перед генерацией стартового кадра:\n") for m in messages)
    assert any("авто стартовый промпт" in m for m in messages)


def test_scene_media_video_ok_passes_when_only_pauses_make_string_long(monkeypatch):
    async def _gen_ok(*args, **kwargs):
        return b"fake-mp4"

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)

    def _must_not_shorten(*_a, **_k):
        raise AssertionError("shorten should not run when under speakable budget")

    monkeypatch.setattr(handlers_state_media, "shorten_scene_speech_and_sync_video_prompt_sync", _must_not_shorten)

    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text):
            messages.append(text)

        async def reply_video(self, *args, **kwargs):
            return None

    update = SimpleNamespace(message=_Msg())
    pauses = "[пауза]" * 80
    body = "z" * 40
    scene = {
        "model": "veo3_fast",
        "video_prompt": "fixed camera voice_lock_full: x. veo3_framing: y. CHARACTERS SPEAKS: " + body,
        "start_frame_image_b64": _encode_b64(b"img"),
        "duration_sec": 4,
        "text": pauses + body,
    }
    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            session={"state": "waiting_scene_media"},
            scene=scene,
            idx=0,
            total=1,
        )
    )
    assert out is True
    assert any(m.startswith("Промпт перед генерацией видео") for m in messages)
    assert any("CHARACTERS SPEAKS" in m for m in messages)


def test_phrase_command_waits_for_next_message_and_updates_phrase():
    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"ru-bytes")},
                        "en": {"video_b64": _encode_b64(b"en-bytes")},
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {
                        "ru": "lang:ru shot CHARACTERS SPEAKS: Привет",
                        "en": "lang:en shot CHARACTERS SPEAKS: Hello old",
                    },
                    "speech_by_lang": {"ru": "Привет", "en": "Hello old"},
                    "model": "veo3_fast",
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    out_cmd = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ФРАЗА en:",
        )
    )
    assert out_cmd is True
    assert handlers_state_media.SCENE_PHRASE_INPUT_PENDING_KEY in session

    out_phrase = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="Look how gentle this is",
        )
    )
    assert out_phrase is True
    scene = session["production"]["json_plan"][0]
    assert handlers_state_media.SCENE_PHRASE_INPUT_PENDING_KEY not in session
    assert scene["speech_by_lang"]["en"] == "Look how gentle this is"
    assert "CHARACTERS SPEAKS: Look how gentle this is" in scene["video_prompt_by_lang"]["en"]
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"en-bytes")
    assert scene[handlers_state_media.SCENE_VIDEO_REGEN_REQUESTED_KEY]["en"] is True


def test_pre_generation_phrase_command_updates_non_current_language():
    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_prompt_current_language": "ru",
                    "video_prompt_by_lang": {
                        "ru": "lang:ru shot CHARACTERS SPEAKS: Привет",
                        "en": "lang:en shot CHARACTERS SPEAKS: Hello old",
                    },
                    "speech_by_lang": {"ru": "Привет", "en": "Hello old"},
                    "model": "veo3_fast",
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ФРАЗА en: New english phrase",
        )
    )
    assert out is True
    scene = session["production"]["json_plan"][0]
    assert scene["speech_by_lang"]["en"] == "New english phrase"
    assert "CHARACTERS SPEAKS: New english phrase" in scene["video_prompt_by_lang"]["en"]


def test_phrase_ru_update_offers_adaptive_en_translation_confirmation():
    messages: list[str] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    update = SimpleNamespace(message=_Msg())
    session = {
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "video_results_by_lang": {
                        "ru": {"video_b64": _encode_b64(b"ru-bytes")},
                        "en": {"video_b64": _encode_b64(b"en-bytes")},
                    },
                    "video_approvals_by_lang": {"ru": False, "en": False},
                    "video_prompt_by_lang": {
                        "ru": "lang:ru shot CHARACTERS SPEAKS: Старый ru",
                        "en": "lang:en shot CHARACTERS SPEAKS: Old en",
                    },
                    "speech_by_lang": {"ru": "Старый ru", "en": "Old en"},
                    "model": "veo3_fast",
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ФРАЗА ru: Новая русская фраза",
        )
    )
    assert out is True
    assert handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY in session
    assert any("Перевести адаптивно на английский" in m for m in messages)


def test_rejected_adaptive_translation_requests_feedback_and_retries(monkeypatch):
    messages: list[str] = []
    calls: list[dict] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    async def _call_direct(_update, _context, _chat_id, _action_name, op):
        return await op()

    async def _translate(source_text: str, target_language: str, *, feedback: str = "", previous_translation: str = ""):
        calls.append(
            {
                "source_text": source_text,
                "target_language": target_language,
                "feedback": feedback,
                "previous_translation": previous_translation,
            }
        )
        return "Better warm English"

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _call_direct)
    monkeypatch.setattr(handlers_state_media, "_translate_scene_phrase_adaptive", _translate)

    update = SimpleNamespace(message=_Msg())
    scene = {
        "video_prompt_by_lang": {
            "ru": "lang:ru shot CHARACTERS SPEAKS: Новая русская фраза",
            "en": "lang:en shot CHARACTERS SPEAKS: Old en",
        },
        "speech_by_lang": {"ru": "Новая русская фраза", "en": "Old en"},
        "model": "veo3_fast",
    }
    session = {
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY: {
            "scene_idx": 0,
            "source_language": "ru",
            "source_text": "Новая русская фраза",
            "target_language": "en",
            "previous_target_text": "Old en",
            "candidate_text": "Bad literal English",
            "stage": "approval",
        },
    }

    out_no = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="нет",
        )
    )
    assert out_no is True
    pending = session[handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY]
    assert pending["stage"] == "revision"
    assert scene["speech_by_lang"]["en"] == "Old en"
    assert any("Пришлите фидбэк" in m for m in messages)

    out_feedback = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ФИДБЕК: короче и теплее",
        )
    )
    assert out_feedback is True
    pending = session[handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY]
    assert pending["stage"] == "approval"
    assert pending["candidate_text"] == "Better warm English"
    assert calls[-1] == {
        "source_text": "Новая русская фраза",
        "target_language": "en",
        "feedback": "короче и теплее",
        "previous_translation": "Bad literal English",
    }
    assert scene["speech_by_lang"]["en"] == "Old en"
    assert any("Обновлённый адаптивный перевод" in m for m in messages)

    out_yes = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="да",
        )
    )
    assert out_yes is True
    assert handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY not in session
    assert scene["speech_by_lang"]["en"] == "Better warm English"
    assert "CHARACTERS SPEAKS: Better warm English" in scene["video_prompt_by_lang"]["en"]
    assert any("Проверьте обновлённый video prompt" in m for m in messages)
    assert any("апрув перед генерацией видео" in m for m in messages)
    assert not any("ПЕРЕГЕНЕРИРОВАТЬ en" in m for m in messages)


def test_rejected_adaptive_translation_accepts_new_source_phrase_and_retries(monkeypatch):
    messages: list[str] = []
    calls: list[dict] = []

    class _Msg:
        async def reply_text(self, text, **_kwargs):
            messages.append(text)

    async def _call_direct(_update, _context, _chat_id, _action_name, op):
        return await op()

    async def _translate(source_text: str, target_language: str, *, feedback: str = "", previous_translation: str = ""):
        calls.append({"source_text": source_text, "feedback": feedback, "previous_translation": previous_translation})
        return "Fresh English line"

    monkeypatch.setattr(handlers_state_media, "_call_with_503_retries", _call_direct)
    monkeypatch.setattr(handlers_state_media, "_translate_scene_phrase_adaptive", _translate)

    update = SimpleNamespace(message=_Msg())
    scene = {
        "video_prompt_by_lang": {
            "ru": "lang:ru shot CHARACTERS SPEAKS: Старая русская",
            "en": "lang:en shot CHARACTERS SPEAKS: Old en",
        },
        "speech_by_lang": {"ru": "Старая русская", "en": "Old en"},
        "model": "veo3_fast",
    }
    session = {
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY: {
            "scene_idx": 0,
            "source_language": "ru",
            "source_text": "Старая русская",
            "target_language": "en",
            "previous_target_text": "Old en",
            "candidate_text": "Bad English",
            "stage": "revision",
        },
    }

    out = asyncio.run(
        handlers_state_media._handle_waiting_scene_media_state(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="ФРАЗА: Новая исходная русская",
        )
    )
    assert out is True
    assert scene["speech_by_lang"]["ru"] == "Новая исходная русская"
    assert "CHARACTERS SPEAKS: Новая исходная русская" in scene["video_prompt_by_lang"]["ru"]
    assert calls[-1]["source_text"] == "Новая исходная русская"
    pending = session[handlers_state_media.SCENE_PHRASE_TRANSLATION_PENDING_KEY]
    assert pending["stage"] == "approval"
    assert pending["candidate_text"] == "Fresh English line"


def test_enforce_video_prompt_voice_requirements_adds_lock_and_russian_rule():
    out = enforce_video_prompt_voice_requirements("fixed camera, close-up")
    assert "voice_lock_full:" in out
    assert "all spoken phrases and voiceover are in Russian language only" in out
    assert "non-photorealistic" in out.lower()
    assert "cartoon cat" not in out
    assert "no on-screen text or subtitles" in out.lower()
    assert "no background music" in out.lower()
    assert "veo3_framing:" in out.lower()


def test_enforce_video_prompt_voice_requirements_for_en_uses_english_constraints():
    out = enforce_video_prompt_voice_requirements(
        "fixed camera, close-up",
        target_language="en",
    )
    assert "voice_lock_full:" in out
    assert "all spoken phrases and voiceover are in English language only" in out
    assert "Russian language" not in out
    assert "non-photorealistic" in out.lower()
    assert "cartoon cat" not in out
    assert "no on-screen text or subtitles" in out.lower()
    assert "no background music" in out.lower()
    assert "Russian line" not in out
    assert "spoken Russian speech" not in out
    assert "neutral Russian accent" not in out


def test_enforce_video_prompt_voice_requirements_keeps_existing_constraints():
    src = (
        "fixed camera voice_lock_full: test lock. "
        "all spoken phrases and voiceover are in Russian language only. "
        "veo3_framing: already present."
    )
    out = enforce_video_prompt_voice_requirements(src)
    assert out.count("voice_lock_full:") == 1
    assert out.lower().count("veo3_framing:") == 1
    assert "cartoon cat" not in out


def test_enforce_video_prompt_voice_requirements_switches_voice_lock_language_token():
    src = "voice_lock_full: female voice Russian language elderly grandmother; neutral Russian accent; close shot."
    out = enforce_video_prompt_voice_requirements(src, target_language="en")
    assert "voice_lock_full:" in out
    assert "English language" in out
    assert "Russian language" not in out
    assert "neutral English accent" in out
    assert "neutral Russian accent" not in out


def test_enforce_video_prompt_voice_requirements_removes_russian_lipsync_in_en_prompt():
    src = (
        "lang:en close shot. Accurate lip sync is required with the Russian line. "
        "veo3_framing: close-up; accurate lip sync with the spoken Russian speech; camera evolves. "
        "voice_lock_full: female voice English language elderly grandmother; neutral Russian accent. "
        "CHARACTERS SPEAKS: So let's start with the fish."
    )
    out = enforce_video_prompt_voice_requirements(src, {"model": "veo3_fast"}, target_language="en")
    assert "Russian line" not in out
    assert "spoken Russian speech" not in out
    assert "neutral Russian accent" not in out
    assert "English line" in out
    assert "spoken English speech" in out
    assert "neutral English accent" in out
    assert "CHARACTERS SPEAKS: So let's start with the fish." in out


def test_enforce_video_prompt_voice_requirements_skips_framing_for_kling_scene():
    scene = {"model": "kling", "voice_needed": True, "text": "x"}
    out = enforce_video_prompt_voice_requirements("wide establishing shot", scene)
    assert "veo3_framing" not in out.lower()


def test_should_suggest_regenerate_start_frame_after_threshold():
    assert VIDEO_PROMPT_POLICY_FAILURE_THRESHOLD == 4
    assert _should_suggest_regenerate_start_frame_after_video_prompt_fails(
        {"video_prompt_policy_failure_count": 3}
    ) is False
    assert _should_suggest_regenerate_start_frame_after_video_prompt_fails(
        {"video_prompt_policy_failure_count": 4}
    ) is True


def test_video_policy_prompt_approval_text_adds_start_frame_hint_when_flagged():
    short = _video_policy_prompt_approval_text("p", suggest_regenerate_start_frame=False)
    long = _video_policy_prompt_approval_text("p", suggest_regenerate_start_frame=True)
    assert "старт заново" not in short
    assert "старт заново" in long


def test_build_video_prompt_rewrite_llm_prompt_includes_rejection_and_phrases():
    out = _build_video_prompt_rewrite_llm_prompt(
        "  kitchen scene  ",
        "filtered: policy X",
        phrases="бабушка: здравствуйте",
    )
    assert "ИСХОДНЫЙ VIDEO PROMPT:" in out
    assert "kitchen scene" in out
    assert "ПОЧЕМУ ОТКЛОНЕНО" in out
    assert "filtered: policy X" in out
    assert "бабушка: здравствуйте" in out
    assert "ФРАЗЫ И РЕПЛИКИ" in out
    assert "мультяшные" in out
    assert "субтитры" in out.lower()
    assert "музыку" in out.lower()


def test_regenerate_video_prompt_from_text_logs_stripped_prompt_and_sends_context(
    monkeypatch, caplog
):
    captured: list[str] = []

    def fake_gemini(prompt: str) -> str:
        captured.append(prompt)
        return "rewritten body"

    monkeypatch.setattr(
        "app.handlers.handlers_media_core.gemini_generate_sync", fake_gemini
    )
    caplog.set_level(logging.INFO, logger="bot")
    out = asyncio.run(
        _regenerate_video_prompt_from_text(
            "  prompt body  ",
            "RAI blocked",
            phrases="keep this line",
        )
    )
    assert captured and "ПОЧЕМУ ОТКЛОНЕНО" in captured[0]
    assert "RAI blocked" in captured[0]
    assert "keep this line" in captured[0]
    assert "prompt body" in captured[0]
    assert "voice_lock_full:" in out.lower()
    assert any(
        "[video prompt rewrite]" in r.getMessage() and "prompt body" in r.getMessage()
        for r in caplog.records
    )
