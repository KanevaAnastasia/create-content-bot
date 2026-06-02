"""Тесты вспомогательных функций сценарных хендлеров."""

import asyncio
import io
from types import SimpleNamespace

from app.handlers.handlers_state_scene import (
    _handle_scene_approval_refine,
    _is_explicit_veo3_speech_format_request,
    _frame_prompts_slice_from_production,
    _generate_project_image,
    on_photo,
    on_text,
)
from app.handlers.handlers_state_commands import NAV_BUTTON_PREVIOUS_STEP, NAV_BUTTON_START_OVER
from app.state import SESSIONS


def test_frame_prompts_slice_from_production_maps_start_end():
    plan = [
        {
            "scene": 1,
            "location": "kitchen",
            "shot_type": "closeup",
            "model": "veo3_fast",
            "duration_sec": 4,
            "start_frame_prompt": "старт описание",
            "end_frame_prompt": "конец описание",
        }
    ]
    out = _frame_prompts_slice_from_production(plan)
    assert len(out) == 1
    assert out[0]["start_frame_prompt_ru"] == "старт описание"
    assert out[0]["end_frame_prompt_ru"] == "конец описание"
    assert "location" not in out[0]


def test_frame_prompts_slice_empty_strings_become_pusto():
    plan = [{"scene": 1, "start_frame_prompt": "", "end_frame_prompt": "   "}]
    out = _frame_prompts_slice_from_production(plan)
    assert out[0]["start_frame_prompt_ru"] == "пусто"
    assert out[0]["end_frame_prompt_ru"] == "пусто"


def test_scene_feedback_with_pause_and_scene_numbers_is_not_stress_fix():
    text = (
        "Чуть подправим фразы, в сцене 1 скажем что мы сделаем а не делаем, "
        "и пауза не нужна между словами ингредиентов и сегодня"
    )
    assert _is_explicit_veo3_speech_format_request(text) is False


def test_short_explicit_stress_fix_still_supported():
    assert _is_explicit_veo3_speech_format_request("убери ударения") is True
    assert _is_explicit_veo3_speech_format_request("нормализуй паузы veo3") is True


def test_scene_approval_feedback_mentions_pause_but_regenerates_scenes(monkeypatch):
    captured: list[str] = []
    session = {
        "state": "waiting_scene_approval",
        "selected_concept": "банановое мороженое",
        "recipe_full_text": "Заморозить бананы. Взбить со сливками.",
        "scenes_plan": [
            {"scene": 1, "text": "сегодня делаем мороженое", "model": "veo3_fast"},
        ],
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1030))

    def _raise_if_called(_scenes):
        raise AssertionError("stress formatter should not handle content scene feedback")

    async def _fake_call(_update, _context, _chat_id, _name, _fn):
        return {
            "json_plan": [
                {"scene": 1, "text": "сегодня мы сделаем мороженое", "model": "veo3_fast"},
                {"scene": 2, "text": "потом отправляем на ночь в морозилку", "model": "veo3_fast"},
            ],
            "text_plan": "СЦЕНА 1\nСЦЕНА 2",
            "regeneration_events": [],
        }

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.apply_veo3_lowercase_format",
        _raise_if_called,
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    asyncio.run(
        _handle_scene_approval_refine(
            update,
            context=None,
            chat_id=1030,
            session=session,
            text="в сцене 1 пауза не нужна между ингредиентов и сегодня",
        )
    )

    assert session["state"] == "waiting_scene_approval"
    assert len(session["scenes_plan"]) == 2
    assert any("Обновила план по правкам" in item for item in captured)


def test_on_text_first_step_generate_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1001, None)

    class _Message:
        text = "Генерировать"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1001))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите идею или рецепт обычным текстом" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1001]["state"] == "state_waiting_first_step_generate_text"


def test_on_text_first_step_button_after_completed_production_starts_new_mode():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1024] = {
        "state": "ready_for_production",
        "production": {"json_plan": [{"scene": 1}]},
        "selected_project_key": "grandma_kitchen",
        "target_languages": ["ru", "en"],
    }

    class _Message:
        text = "Генерировать"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1024))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Production уже готов" not in captured[0][0]
    assert "Пришлите идею или рецепт обычным текстом" in captured[0][0]
    assert SESSIONS[1024]["state"] == "state_waiting_first_step_generate_text"
    assert "production" not in SESSIONS[1024]


def test_on_text_first_step_description_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1021, None)

    class _Message:
        text = "Описание"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1021))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите рецепт или ссылку" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1021]["state"] == "state_waiting_first_step_description_text"


def test_on_text_first_step_image_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1002, None)

    class _Message:
        text = "Изображение"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1002))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите prompt обычным текстом" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1002]["state"] == "state_waiting_first_step_image_text"


def test_on_text_first_step_project_image_button_opens_character_choice():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1012, None)

    class _Message:
        text = "Изображение проект"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1012))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Выберите персонажей" in captured[0][0]
    assert "бабушка, рыжий кот" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1012]["state"] == "state_waiting_first_step_project_image_characters"
    assert SESSIONS[1012]["first_step_project_image_characters"] == ["grandma", "cat"]


def test_on_text_first_step_sound_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1014, None)

    class _Message:
        text = "Звук"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1014))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите текст для озвучки" in captured[0][0]
    assert "grandma" in captured[0][0]
    assert SESSIONS[1014]["state"] == "state_waiting_first_step_sound_text"
    assert SESSIONS[1014]["first_step_sound_character"] == "grandma"


def test_on_text_first_step_video_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1003, None)

    class _Message:
        text = "Видео"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1003))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Шаг 1/2" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1003]["state"] == "state_waiting_first_step_video_text"


def test_on_text_first_step_generate_text_launches_pipeline(monkeypatch):
    captured_calls: list[tuple[str, bool]] = []
    SESSIONS[1004] = {"state": "state_waiting_first_step_generate_text"}

    class _Message:
        text = "Сырники с клубникой"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_generation_with_recipe(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        captured_calls.append((recipe, prefer_vertex_api))

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_generation_with_recipe",
        _fake_start_generation_with_recipe,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1004))
    asyncio.run(on_text(update, context=None))
    assert captured_calls == [("Сырники с клубникой", False)]


def test_on_text_first_step_description_text_generates_social_copy(monkeypatch):
    captured_calls: list[str] = []
    SESSIONS[1022] = {"state": "state_waiting_first_step_description_text"}

    class _Message:
        text = "Сырники с клубникой"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_description_with_recipe(update, context, *, recipe):
        _ = (update, context)
        captured_calls.append(recipe)

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_description_with_recipe",
        _fake_start_description_with_recipe,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1022))
    asyncio.run(on_text(update, context=None))
    assert captured_calls == ["Сырники с клубникой"]


def test_on_text_first_step_image_text_generates_image(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    SESSIONS[1005] = {"state": "state_waiting_first_step_image_text"}

    class _Message:
        text = "Бабушка с котом и пирогом"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"img"
            assert "Изображение готово" in caption
            sent["ok"] = True

    async def _fake_generate(prompt: str):
        assert "пирогом" in prompt
        return b"img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1005))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "Генерируем изображение" in captured[0][0]
    assert SESSIONS[1005]["state"] == ""
    assert SESSIONS[1005]["selected_project_key"] == "grandma_kitchen"


def test_on_text_first_step_image_accepts_custom_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1016] = {"state": "state_waiting_first_step_image_text"}

    class _Message:
        text = "Добавить свое"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"img"
            assert "Изображение готово" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1016]["state"] == "state_waiting_first_step_image_references"
    assert "Пришлите одно или несколько" in captured[-1][0]

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"custom-ref"

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_photo(photo_update, context=None))
    assert SESSIONS[1016]["first_step_custom_reference_images"] == [b"custom-ref"]
    assert "Добавила файл #1" in captured[-1][0]

    class _DoneMessage(_Message):
        text = "больше файлов не нужно"

    update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1016]["state"] == "state_waiting_first_step_image_text"
    assert "Теперь пришлите prompt" in captured[-1][0]

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Красивая тарелка супа в мультяшном стиле"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert seen["prompt"] == "Красивая тарелка супа в мультяшном стиле"
    assert seen["reference_images"] == [b"custom-ref"]
    assert SESSIONS[1016]["state"] == ""


def test_on_text_first_step_sound_text_generates_audio(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    SESSIONS[1015] = {
        "state": "state_waiting_first_step_sound_text",
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "Варим до мягкости."

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_audio(self, audio, caption):
            data = audio.getvalue() if hasattr(audio, "getvalue") else b""
            assert data == b"mp3"
            assert "Звук готов" in caption
            sent["ok"] = True

    class _Result:
        audio_bytes = b"mp3"

    def _fake_generate(text):
        assert text == "Варим до мягкости."
        return _Result()

    monkeypatch.setattr("app.handlers.handlers_state_scene.generate_elevenlabs_speech", _fake_generate)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1015))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "Генерируем звук" in captured[0][0]
    assert SESSIONS[1015]["state"] == ""
    assert SESSIONS[1015]["selected_project_key"] == "grandma_kitchen"


def test_on_text_project_image_adds_character_then_generates_with_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1013] = {
        "state": "state_waiting_first_step_project_image_characters",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "grandfather"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"project-img"
            assert "дедушка" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert "grandfather" in SESSIONS[1013]["first_step_project_image_characters"]
    assert "Добавила" in captured[-1][0]

    class _DoneMessage(_Message):
        text = "больше никто не нужен"

    update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1013]["state"] == "state_waiting_first_step_project_image_prompt"
    assert "Теперь пришлите prompt" in captured[-1][0]

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"ref1", b"ref2"],
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Крупный план на кухне, все смотрят на готовый суп"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "дедушка" in str(seen["prompt"])
    assert seen["reference_images"] == [b"ref1", b"ref2"]
    assert SESSIONS[1013]["state"] == ""


def test_on_text_project_image_accepts_custom_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1017] = {
        "state": "state_waiting_first_step_project_image_characters",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "Добавить свое"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"project-img"
            assert "Изображение проекта готово" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_references"
    assert "Пришлите одно или несколько" in captured[-1][0]

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"custom-project-ref"

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_photo(photo_update, context=None))
    assert SESSIONS[1017]["first_step_custom_reference_images"] == [b"custom-project-ref"]

    class _FilesDoneMessage(_Message):
        text = "больше файлов не нужно"

    update = SimpleNamespace(message=_FilesDoneMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_characters"

    class _CharactersDoneMessage(_Message):
        text = "больше никто не нужен"

    update = SimpleNamespace(message=_CharactersDoneMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_prompt"

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"project-ref"],
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Бабушка и кот рядом с новым предметом из референса"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "бабушка, рыжий кот" in str(seen["prompt"])
    assert seen["reference_images"] == [b"project-ref", b"custom-project-ref"]
    assert SESSIONS[1017]["state"] == ""


def test_generate_project_image_sends_prompt_and_character_references(monkeypatch):
    prompt = """Уютная мультяшная кухня, пиксаровский стиль референса, яркий цветной мультфильм, мягкий стилизованный свет, никакого реализма и фотореализма. Крупный план кухонного стола сбоку под углом сверху.

На переднем плане стоит глубокая красивая керамическая тарелка, уже наполненная густым сырным супом пюре нежного кремово желтого цвета. Поверхность супа гладкая, бархатная, густая, от супа поднимается мягкий пар. Суп уже полностью взбит в однородный крем.

Рядом с тарелкой стоит небольшая деревянная мисочка с золотистыми хрустящими сухариками. Рядом лежит маленькая щепотка свежей зелени для подачи. Возле тарелки лежит серебряная ложка. На заднем плане на столе стоит светлая кастрюля с остатками готового сырного супа пюре, рядом аккуратно лежит погружной блендер, чтобы было понятно, что суп уже взбит.

Мультяшная бабушка видна по пояс, стоит за столом, смотрит в камеру с теплой спокойной улыбкой. Одна рука бабушки находится рядом с мисочкой сухариков, как будто она сейчас добавит их в тарелку. Вторая рука мягко придерживает край стола рядом с тарелкой.

Рыжий мультяшный кот очень крупно сидит на столе рядом с мисочкой сухариков, на безопасном расстоянии от горячей тарелки. Кот смотрит на сухарики с любопытством и восторгом, глаза широко открыты, уши навострены, хвост поднят и слегка изогнут. Кот не облизывается.

На фоне видна чистая уютная мультяшная кухня: светлые шкафчики, теплая подсветка, аккуратная столешница, без беспорядка. Без надписей, без текста, без логотипов, без музыки, без визуальных эффектов, без реалистичной фактуры кожи и еды."""
    captured: dict[str, object] = {}
    replies: list[str] = []

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            captured["photo_bytes"] = photo.getvalue() if hasattr(photo, "getvalue") else b""
            captured["caption"] = caption

    async def _fake_generate(effective_prompt: str, reference_images=None):
        captured["prompt"] = effective_prompt
        captured["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    update = SimpleNamespace(message=_Message())
    asyncio.run(_generate_project_image(update, prompt, ["grandma", "cat"]))

    sent_prompt = str(captured["prompt"])
    refs = captured["reference_images"]
    assert prompt.splitlines()[0] in sent_prompt
    assert "Match art style" in sent_prompt
    assert "Персонажи в кадре: бабушка, рыжий кот" in sent_prompt
    assert "Локация кадра" not in sent_prompt
    assert isinstance(refs, list)
    assert len(refs) == 2
    assert all(len(r) > 0 for r in refs)
    assert captured["photo_bytes"] == b"project-img"
    assert any("Генерируем изображение проекта" in r for r in replies)


def test_on_text_first_step_video_text_switches_to_photo_step():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1006] = {"state": "state_waiting_first_step_video_text"}

    class _Message:
        text = "6 Крупный план бабушки с котом"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1006))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1006]["state"] == "state_waiting_first_step_video_image"
    assert SESSIONS[1006]["first_step_video_duration_sec"] in {6, 8}
    assert "крупный план" in SESSIONS[1006]["first_step_video_prompt"].lower()
    assert "Шаг 2/2" in captured[0][0]


def test_on_photo_first_step_video_image_generates_video(monkeypatch):
    captured_texts: list[str] = []
    sent = {"ok": False}
    SESSIONS[1007] = {
        "state": "state_waiting_first_step_video_image",
        "first_step_video_duration_sec": 6,
        "first_step_video_prompt": "Крупный план бабушки",
    }

    class _Message:
        photo = [object()]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured_texts.append(text)

        async def reply_video(self, video, caption):
            assert isinstance(video, io.BytesIO)
            assert video.getvalue() == b"mp4"
            assert "Видео готово" in caption
            sent["ok"] = True

    class _Context:
        bot = object()

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"img"

    async def _fake_generate_video(_update, **kwargs):
        _ = _update
        assert kwargs["duration_sec"] == 6
        assert kwargs["start_image"] == b"img"
        return b"mp4"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._extract_video_source_image",
        _fake_extract,
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_video_bytes_with_high_load_retry",
        _fake_generate_video,
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene.get_last_video_result_source", lambda: "bytes")

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1007))
    asyncio.run(on_photo(update, _Context()))
    assert sent["ok"] is True
    assert any("Генерируем видео" in t for t in captured_texts)
    assert SESSIONS[1007]["state"] == ""
    assert SESSIONS[1007]["selected_project_key"] == "grandma_kitchen"


def test_on_text_first_step_project_button_opens_project_choice():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1010, None)

    class _Message:
        text = "Проект"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1010))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Выберите проект кнопкой" in captured[0][0]
    assert SESSIONS[1010]["state"] == "state_waiting_first_step_project_selection"
    assert SESSIONS[1010]["selected_project_key"] == "grandma_kitchen"


def test_on_text_project_selection_saves_project_and_returns_menu():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1011] = {
        "state": "state_waiting_first_step_project_selection",
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "Grandma Kitchen"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1011))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Проект выбран" in captured[0][0]
    assert SESSIONS[1011]["state"] == ""
    assert SESSIONS[1011]["selected_project_key"] == "grandma_kitchen"


def test_on_text_navigation_start_over_resets_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1020] = {"state": "waiting_recipe_approval", "selected_project_key": "grandma_kitchen"}

    class _Message:
        text = NAV_BUTTON_START_OVER

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1020))
    asyncio.run(on_text(update, context=None))
    assert 1020 not in SESSIONS
    assert "Начинаем сначала" in captured[0][0]
    assert captured[0][1] is not None


def test_on_text_navigation_previous_moves_from_recipe_to_concept():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1021] = {
        "state": "waiting_recipe_approval",
        "concepts": [{"name": "сырники"}],
        "recipe_full_text": "текст",
        "scenes_plan": [{"scene": 1}],
        "production": {"json_plan": [{"scene": 1}]},
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1021))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1021]["state"] == "waiting_concept"
    assert SESSIONS[1021]["recipe_full_text"] is None
    assert SESSIONS[1021]["scenes_plan"] is None
    assert SESSIONS[1021]["production"] is None
    assert "Откатила шаг рецепта" in captured[0][0]


def test_on_text_navigation_previous_moves_from_media_video_to_start_prompt(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    send_calls = {"count": 0}
    SESSIONS[1022] = {
        "state": "waiting_scene_media",
        "image_scene_index": 1,
        "scene_media_substep": "video_prompt",
        "production": {
            "json_plan": [
                {"scene": 1, "start_frame_image_b64": "x", "end_frame_image_b64": "y"},
                {"scene": 2, "start_frame_image_b64": "x2", "end_frame_prompt": "дельта конца"},
            ]
        },
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = (update, session)
        send_calls["count"] += 1

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1022))
    asyncio.run(on_text(update, context=None))
    sess = SESSIONS[1022]
    assert sess["state"] == "waiting_scene_media"
    assert sess["image_scene_index"] == 1
    assert sess["scene_media_substep"] == "start_prompt"
    assert send_calls["count"] == 1
    assert "предыдущий подшаг текущей сцены" in captured[0][0]


def test_on_text_navigation_previous_moves_from_media_video_to_end_prompt_when_available(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    send_calls = {"count": 0}
    SESSIONS[1023] = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1}]},
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = (update, session)
        send_calls["count"] += 1

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._scene_needs_end_frame_step",
        lambda _scene: True,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1023))
    asyncio.run(on_text(update, context=None))
    sess = SESSIONS[1023]
    assert sess["state"] == "waiting_scene_media"
    assert sess["scene_media_substep"] == "end_prompt"
    assert send_calls["count"] == 1
