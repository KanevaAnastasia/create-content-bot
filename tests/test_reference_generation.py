import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.project_modes import project_mode_rows
from app import reference_generation as reference


def test_reference_mode_only_appears_in_grandma_menu():
    assert ["По референсу"] in project_mode_rows("grandma_kitchen")
    assert ["По референсу"] not in project_mode_rows("discipline")


def test_reference_mode_selection_waits_for_image_and_skips_series():
    from app.handlers import handlers_state_start as start

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)
    session = {
        start._PROJECT_KEY_SESSION: "grandma_kitchen",
        start.SERIES_PENDING_RECIPE_SESSION_KEY: "старый текст не нужен режиму",
    }

    asyncio.run(
        start._handle_waiting_generation_mode_state(
            update,
            None,
            chat_id=918273,
            session=session,
            text="По референсу",
        )
    )

    assert session["generation_mode"] == "reference"
    assert session["target_languages"] == ["ru"]
    assert session["state"] == reference.STATE_WAITING_REFERENCE_IMAGE
    assert start.SERIES_PENDING_RECIPE_SESSION_KEY not in session
    start.SESSIONS.pop(918273, None)


def test_reference_mode_is_rejected_outside_grandma_project():
    from app.handlers import handlers_state_start as start

    message = SimpleNamespace(reply_text=AsyncMock())
    session = {start._PROJECT_KEY_SESSION: "discipline", "generation_mode": "story"}

    asyncio.run(
        start._handle_waiting_generation_mode_state(
            SimpleNamespace(message=message),
            None,
            chat_id=918274,
            session=session,
            text="По референсу",
        )
    )

    assert session["generation_mode"] == "story"
    assert "только в проекте бабушки" in message.reply_text.await_args.args[0]


def test_reference_extra_characters_parse_multiple_known_names():
    selected, unknown = reference.parse_reference_extra_characters("Дедушка, Зая и внучка")

    assert selected == ["zaya", "grandfather", "granddaughter"]
    assert unknown == []


def test_reference_extra_characters_reject_unknown_text():
    selected, unknown = reference.parse_reference_extra_characters("Дедушка и космонавт")

    assert selected == ["grandfather"]
    assert unknown == ["космонавт"]


def test_build_reference_scene_is_one_vertical_veo_scene_with_user_reference():
    scene = reference.build_reference_scene(
        source_image=b"reference-image",
        image_prompt="Бабушка и Тимоша повторяют композицию.",
        comment="Дедушка входит с корзиной.",
        extra_characters=["grandfather"],
    )

    assert scene["generation_mode"] == "reference"
    assert scene["model"] == "veo3_fast"
    assert scene["duration_sec"] == 8
    assert scene["aspect_ratio"] == "9:16"
    assert scene["characters"] == ["grandma", "cat", "grandfather"]
    assert scene["start_frame_prompt_raw_mode"] is True
    assert scene["video_prompt_by_lang"] == {"ru": ""}
    assert scene["scene_user_reference_images_by_kind"]["start"] == ["cmVmZXJlbmNlLWltYWdl"]


def test_reference_image_prompt_uses_visual_input_and_comment(monkeypatch):
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "Готовый промпт кадра"

    monkeypatch.setattr(reference, "gemini_generate_sync", fake_generate)

    result = reference.generate_reference_image_prompt(
        b"image",
        comment="Пусть дедушка держит самовар",
        extra_characters=["grandfather"],
    )

    assert result == "Готовый промпт кадра"
    assert captured["images"] == [b"image"]
    assert "Пусть дедушка держит самовар" in captured["prompt"]
    assert "grandfather" in captured["prompt"]


def test_reference_video_prompt_uses_approved_frame(monkeypatch):
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "One continuous Veo shot."

    monkeypatch.setattr(reference, "gemini_generate_sync", fake_generate)

    result = reference.generate_reference_video_prompt(
        b"approved-frame",
        image_prompt="Approved image prompt",
        comment="Кот смотрит на корзину",
        characters=["grandma", "cat"],
    )

    assert result == "One continuous Veo shot."
    assert captured["images"] == [b"approved-frame"]
    assert "8-second 9:16" in captured["prompt"]
    assert "Approved image prompt" in captured["prompt"]


def test_reference_description_is_cleaned_but_not_mechanically_truncated(monkeypatch):
    monkeypatch.setattr(
        reference,
        "gemini_generate_sync",
        lambda *args, **kwargs: 'Описание: «Бабушка и Тимоша разыгрывают сцену с корзиной.»',
    )

    result = reference.generate_reference_description(
        image_prompt="Кадр с корзиной",
        video_prompt="The cat looks into the basket.",
    )

    assert result == "Бабушка и Тимоша разыгрывают сцену с корзиной."


def test_reference_description_repairs_multiple_sentences(monkeypatch):
    answers = iter([
        "Бабушка открывает корзину. Тимоша заглядывает внутрь.",
        "Бабушка открывает корзину, пока Тимоша заглядывает внутрь.",
    ])
    monkeypatch.setattr(reference, "gemini_generate_sync", lambda *args, **kwargs: next(answers))

    result = reference.generate_reference_description(
        image_prompt="Кадр с корзиной",
        video_prompt="The cat looks into the basket.",
    )

    assert result == "Бабушка открывает корзину, пока Тимоша заглядывает внутрь."


def test_start_image_approval_generates_reference_video_prompt_after_frame_approval(monkeypatch):
    from app.handlers import handlers_state_media as media
    from app.scene_media_candidates import register_image_candidate

    scene = reference.build_reference_scene(
        source_image=b"source",
        image_prompt="Кадр по референсу",
        comment="Тимоша заглядывает в корзину",
    )
    scene["start_frame_image_b64"] = "YXBwcm92ZWQtZnJhbWU="
    register_image_candidate(scene, kind="start", image_bytes=b"approved-frame")
    session = {
        "generation_mode": "reference",
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
        "image_scene_index": 0,
        "scene_media_substep": "start_image_approval",
        "reference_comment": "Тимоша заглядывает в корзину",
    }

    async def call_retries(_update, _context, _chat_id, _label, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(media, "_call_with_503_retries", call_retries)
    monkeypatch.setattr(media, "generate_reference_video_prompt", lambda *args, **kwargs: "Veo prompt")
    monkeypatch.setattr(media, "_persist_image_state_best_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(media, "_refresh_image_candidates_from_storage", lambda *args, **kwargs: False)
    monkeypatch.setattr(media, "_review_titles_after_start_frame_approval", AsyncMock(return_value=False))
    monkeypatch.setattr(media, "_send_next_scene_media_step", AsyncMock())
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    asyncio.run(
        media._scene_media_start_image_approval_step(
            update,
            None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            text="ОК",
        )
    )

    assert scene["video_prompt"] == "Veo prompt"
    assert scene["video_prompt_by_lang"] == {"ru": "Veo prompt"}
    assert session["scene_media_substep"] == "video_prompt"


def test_reference_finish_stops_before_final_build_and_requests_description_approval(monkeypatch):
    from app.handlers import handlers_state_media as media

    session = {
        "generation_mode": "reference",
        "production": {
            "json_plan": [
                {
                    "start_frame_prompt": "Кадр по референсу",
                    "video_prompt": "Veo prompt",
                }
            ]
        },
    }
    monkeypatch.setattr(media, "generate_reference_description", lambda **kwargs: "Бабушка и Тимоша оживляют сцену с корзиной.")
    queued = AsyncMock()
    monkeypatch.setattr(media, "queue_final_builds_for_session", queued)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media._finish_scene_media_production(update, session))

    assert result is True
    assert session["state"] == reference.STATE_WAITING_REFERENCE_DESCRIPTION_APPROVAL
    assert session["reference_short_description"] == "Бабушка и Тимоша оживляют сцену с корзиной."
    queued.assert_not_called()
