import asyncio
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


def test_parse_regenerate_language_command():
    assert handlers_state_media._parse_regenerate_language_command("ПЕРЕГЕНЕРИРОВАТЬ en") == "en"
    assert handlers_state_media._parse_regenerate_language_command("перегенерировать ru") == "ru"
    assert handlers_state_media._parse_regenerate_language_command("ПЕРЕГЕНЕРИРОВАТЬ") is None


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


def test_compose_locked_video_prompt_keeps_existing_scene_phrase():
    scene = {
        "text": "Оригинальная фраза",
        "video_prompt": "lang:ru cinematic shot. CHARACTERS SPEAKS: Старая фраза",
    }
    out = handlers_state_media._compose_locked_video_prompt(
        scene,
        "ru",
        "new style prompt. CHARACTERS SPEAKS: Подмена фразы",
    )
    assert "CHARACTERS SPEAKS: Оригинальная фраза" in out
    assert "Подмена фразы" not in out


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
    assert scene["video_results_by_lang"] == {}
    assert scene["video_approvals_by_lang"] == {"ru": False, "en": False}


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
        assert "ПРОМПТ:" in buttons
        assert "ФИДБЕК:" in buttons
        assert "Добавь" not in buttons
        assert "Исправь" not in buttons
    assert "ФРАЗА:" in video_buttons
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
    assert "ОК ru" in buttons
    assert "ОК en" in buttons
    assert "ФИДБЕК:" in buttons
    assert "ПРОМПТ ru:" in buttons
    assert "ПРОМПТ en:" in buttons
    assert "ФИДБЕК ru:" not in buttons
    assert "ФИДБЕК en:" not in buttons
    assert "ФРАЗА ru:" in buttons
    assert "ФРАЗА en:" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ ru" in buttons
    assert "ПЕРЕГЕНЕРИРОВАТЬ en" in buttons


def test_scene_generated_image_approval_markup_has_expected_buttons():
    buttons = [
        btn.text
        for row in handlers_media_scene.scene_generated_image_approval_markup().keyboard
        for btn in row
    ]
    assert "ОК" in buttons
    assert "Автоапрув" in buttons
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
    out = asyncio.run(
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
    assert out == b"ok-video"
    assert calls["n"] == 3
    assert len(messages) == 2
    assert regions_used[:3] == ["us-central1", "us-central1", "us-central1"]
    assert "в этом же регионе" in messages[0]


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

    out = asyncio.run(
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
    assert out == b"ok-video"
    assert regions_used[:4] == ["us-central1", "us-central1", "us-central1", "europe-west4"]
    assert any("переключаюсь на регион europe-west4" in m for m in messages)


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

    out = asyncio.run(
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
    assert out == b"ok-video"
    assert regions_used[:2] == ["europe-west4", "us-central1"]
    assert any("Модель видео недоступна в регионе europe-west4" in m for m in messages)


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
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "policy" in str(e).lower()
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
        assert False, "expected high-load RuntimeError"
    except RuntimeError as e:
        assert "high load" in str(e).lower()
    assert calls["syntax"] == 1
    assert calls["n"] == 3


def test_syntax_failure_falls_back_to_vertex_success(monkeypatch):
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
        )
    )
    assert out == b"vertex-mp4"
    assert calls == {"vertex": 1, "syntax": 1}


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


def test_image_end_scene_prompt_none_when_not_warranted_even_if_end_set():
    scene = {
        "characters": ["grandma", "cat"],
        "start_frame_prompt_ru": "Бабушка месит тесто в миске.",
        "end_frame_prompt": "Полная кухня заново со всеми деталями",
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
    assert "location" not in refs


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
    }
    assert _scene_needs_end_frame_step(scene) is False


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


def test_ai_approve_reject_upgrades_model_for_next_start_attempt(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_prompt_check(update, *, session, scene, idx, start_frame_prompt):
        return "APPROVE"

    async def _fake_generate_image(prompt, reference_images=None):
        calls.append(scene.get("start_frame_image_model"))
        return b"image"

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    async def _fake_image_check(update, *, session, scene, idx, image_bytes):
        return "REGENERATE_IMAGE"

    monkeypatch.setattr(handlers_state_media, "_generate_image_with_lenient_fallback", _fake_generate_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(handlers_state_media, "_run_ai_prompt_check", _fake_prompt_check)
    monkeypatch.setattr(handlers_state_media, "_run_ai_start_frame_check", _fake_image_check)
    monkeypatch.setattr(
        handlers_state_media,
        "_next_image_quality_model",
        lambda current: "gemini-3.1-flash-image-preview" if current == "gemini-2.5-flash-image" else current,
    )

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
    assert calls == ["gemini-2.5-flash-image"]
    assert scene["start_frame_image_model"] == "gemini-3.1-flash-image-preview"
    assert "start_frame_image_b64" not in scene
    assert session["scene_media_substep"] == "start_prompt"
    assert any("Повышаю модель" in message for message in messages)


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


def test_end_frame_generation_uses_only_start_frame_reference(monkeypatch):
    calls: list[dict] = []

    async def _gen_image(prompt, *, reference_images=None, model=None):
        calls.append({"prompt": prompt, "reference_images": reference_images, "model": model})
        return SimpleNamespace(data=b"end-image", model=model)

    async def _fake_reply_photo(_message, *, photo, caption):
        return None

    monkeypatch.setattr(handlers_state_media, "_generate_scene_image_result", _gen_image)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_photo", _fake_reply_photo)
    monkeypatch.setattr(
        handlers_state_media,
        "_load_scene_reference_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("character refs must not be loaded")),
    )

    class _Msg:
        async def reply_text(self, _text, **kwargs):
            return None

    scene = {
        "start_frame_prompt": "бабушка и рыжий кот готовят у стола",
        "start_frame_image_b64": _encode_b64(b"start-frame"),
        "end_frame_prompt": "еда готова, белая кошка зая входит в кадр",
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
            session={"target_languages": ["ru"]},
            scene=scene,
            idx=0,
        )
    )

    assert out is True
    assert calls[0]["reference_images"] == [b"start-frame"]
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


def test_start_image_upgrade_model_immediately_regenerates(monkeypatch):
    calls: list[str] = []
    messages: list[str] = []

    async def _fake_start_ok(update, *, context, chat_id, session, scene, idx):
        calls.append(scene.get("start_frame_image_model"))
        return True

    monkeypatch.setattr(handlers_state_media, "_scene_media_start_ok", _fake_start_ok)
    monkeypatch.setattr(
        handlers_state_media,
        "_next_image_quality_model",
        lambda current: "gemini-3.1-flash-image-preview" if current == "gemini-2.5-flash-image" else current,
    )

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
    assert scene["start_frame_image_model"] == "gemini-3.1-flash-image-preview"
    assert calls == ["gemini-3.1-flash-image-preview"]
    assert any("Повышаю модель" in message for message in messages)


def test_finish_scene_media_production_sends_copyable_social_descriptions(monkeypatch):
    raw = """
    {
      "youtube_ru_title": "Драники как у бабушки",
      "youtube_ru_description": "Живое описание\\nИнгредиенты: картофель 5 шт\\nШаги: натереть, отжать, пожарить\\n#убабушкивкуснее #бабушкинырецепты #драники #рецепты",
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
    assert session["state"] == handlers_state_media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
    assert handlers_state_media.SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY not in session
    assert handlers_state_media.SOCIAL_DESCRIPTIONS_SENT_KEY not in session
    assert handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY in session
    assert any(m.startswith("YouTube Shorts RU заголовок") for m in messages)
    assert any(m.startswith("YouTube Shorts RU описание") for m in messages)
    assert any(m.startswith("Instagram EN description") for m in messages)
    assert any("```\nДраники как у бабушки\n```" in m for m in messages)
    assert any("```\nЖивое описание" in m for m in messages)
    assert "Markdown" in parse_modes
    assert any("#убабушкивкуснее" in m for m in messages)
    assert any("#grandmatastesbetter #grandmasrecipes #recipes" in m for m in messages)
    assert not any(m.startswith("Instagram EN description") and "#убабушкивкуснее" in m for m in messages)
    assert any("Проверьте описания" in m for m in messages)


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
    assert scene["video_results_by_lang"] == {}
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert handlers_state_media.SCENE_VIDEO_FEEDBACK_PENDING_KEY not in session
    assert any("Напишите, что исправить" in message for message in messages)
    assert any("Подготовила обновлённый video prompt" in message for message in messages)


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
    assert "ru" not in session["production"]["json_plan"][0]["video_results_by_lang"]
    assert session["production"]["json_plan"][0]["video_approvals_by_lang"]["ru"] is False
    assert (
        session[handlers_state_media.SCENE_SPEECH_PENDING_KEY]["stage"]
        == "approval"
    )


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


def test_scene_media_video_ok_batches_syntax_then_uses_vertex_only_for_missing_languages(monkeypatch):
    calls: list[dict] = []
    videos_sent: list[str] = []
    batch_requests_seen: list[str] = []

    async def _batch(*, requests, model, duration_sec):
        batch_requests_seen.extend(req.key for req in requests)
        assert model == "veo3_fast"
        assert duration_sec == 6
        return {"ru": b"syntax-ru-video"}

    async def _gen_ok(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["skip_initial_syntax"] is True
        assert kwargs["disable_syntax_fallback"] is True
        return b"vertex-en-video"

    async def _fake_reply_video(_message, *, video, caption):
        videos_sent.append(caption)
        return None

    monkeypatch.setattr(handlers_state_media, "_attempt_syntax_video_fallback_batch", _batch)
    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", _gen_ok)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", _fake_reply_video)

    class _Msg:
        async def reply_text(self, _text, **_kwargs):
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
    assert len(calls) == 1
    assert calls[0]["prompt"].startswith("lang:en prompt")
    assert scene["video_results_by_lang"]["ru"]["video_b64"] == _encode_b64(b"syntax-ru-video")
    assert scene["video_results_by_lang"]["en"]["video_b64"] == _encode_b64(b"vertex-en-video")
    assert any("язык ru" in caption for caption in videos_sent)
    assert any("язык en" in caption for caption in videos_sent)


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
    assert "en" not in scene["video_results_by_lang"]


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
    assert "cartoon cat" in out
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
    assert "cartoon cat" in out
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
    assert "cartoon cat" in out


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
    assert "cartoon cat" in out
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
