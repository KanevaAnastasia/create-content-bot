import asyncio
from pathlib import Path
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


def test_reference_finish_stops_before_final_build_and_requests_music_choice(monkeypatch):
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
    queued = AsyncMock()
    monkeypatch.setattr(media, "queue_final_builds_for_session", queued)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media._finish_scene_media_production(update, session))

    assert result is True
    assert session["state"] == reference.STATE_WAITING_REFERENCE_MUSIC_CHOICE
    assert "reference_short_description" not in session
    queued.assert_not_called()


def _reference_session_with_approved_video(video_bytes: bytes = b"approved-veo"):
    from app.scene_media_candidates import mark_video_candidate_approved, register_video_candidate

    scene = {
        "start_frame_prompt": "Кадр по референсу",
        "video_prompt": "Veo prompt",
    }
    candidate = register_video_candidate(
        scene,
        language="ru",
        video_bytes=video_bytes,
        source_image_candidate_id=None,
    )
    mark_video_candidate_approved(scene, "ru", candidate["candidate_id"])
    return {
        "generation_mode": "reference",
        "selected_project_key": "grandma_kitchen",
        "production": {"json_plan": [scene]},
    }


def test_reference_music_choice_parses_all_four_buttons():
    assert reference.reference_music_choice("Без музыки") == "none"
    assert reference.reference_music_choice("Дефолтная") == "default"
    assert reference.reference_music_choice("Новогодняя") == "holiday"
    assert reference.reference_music_choice("Своя") == "custom"


def test_reference_no_music_proceeds_to_description_without_render(monkeypatch):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    description = AsyncMock(return_value=True)
    render = AsyncMock()
    monkeypatch.setattr(media, "_start_reference_description_approval", description)
    monkeypatch.setattr(media, "render_reference_music_variant", render)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Без музыки"))

    assert result is True
    assert session[reference.REFERENCE_MUSIC_CHOICE_SESSION_KEY] == "none"
    description.assert_awaited_once_with(update, session)
    render.assert_not_called()


def test_reference_custom_music_choice_waits_for_audio_upload(monkeypatch):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    description = AsyncMock(return_value=True)
    monkeypatch.setattr(media, "_start_reference_description_approval", description)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Своя"))

    assert result is True
    assert session["state"] == reference.STATE_WAITING_REFERENCE_CUSTOM_MUSIC
    assert session[reference.REFERENCE_MUSIC_CHOICE_SESSION_KEY] == "custom"
    description.assert_not_awaited()


def test_reference_default_music_renders_only_the_approved_video(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video(b"approved-video-bytes")
    music_path = tmp_path / "default.mp3"
    music_path.write_bytes(b"music")
    result_path = tmp_path / "final.mp4"
    result_path.write_bytes(b"mixed-video")
    render_calls = []

    def fake_render(video_bytes, **kwargs):
        render_calls.append((video_bytes, kwargs))
        return str(result_path)

    monkeypatch.setattr(media, "_resolve_music_path", lambda _project: (str(music_path), 0.25))
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    sent_video = AsyncMock()
    description = AsyncMock(return_value=True)
    monkeypatch.setattr(media, "_safe_reply_video", sent_video)
    monkeypatch.setattr(media, "_start_reference_description_approval", description)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Дефолтная"))

    assert result is True
    assert render_calls == [
        (
            b"approved-video-bytes",
            {
                "music_path": str(music_path),
                "music_volume": 0.25,
                "run_id": "",
                "choice": "default",
            },
        )
    ]
    assert session[reference.REFERENCE_MUSIC_RESULT_PATH_SESSION_KEY] == str(result_path)
    assert sent_video.await_args.kwargs["video"].read() == b"mixed-video"
    description.assert_awaited_once_with(update, session)


def test_reference_holiday_music_uses_christmas_series_track(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    music_path = tmp_path / "holiday.mp3"
    music_path.write_bytes(b"holiday")
    result_path = tmp_path / "holiday-final.mp4"
    result_path.write_bytes(b"holiday-video")
    seen = {}

    def fake_holiday_music(style):
        seen["style"] = style
        return str(music_path), 0.5

    def fake_render(_video_bytes, **kwargs):
        seen["render"] = kwargs
        return str(result_path)

    monkeypatch.setattr(media, "holiday_music", fake_holiday_music)
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    monkeypatch.setattr(media, "_safe_reply_video", AsyncMock())
    monkeypatch.setattr(media, "_start_reference_description_approval", AsyncMock(return_value=True))
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    asyncio.run(media.handle_reference_music_choice_state(update, session, "Новогодняя"))

    assert seen["style"] == {media.THEME_KEY: media.HOLIDAY_THEME}
    assert seen["render"]["music_path"] == str(music_path)
    assert seen["render"]["music_volume"] == 0.5
    assert seen["render"]["choice"] == "holiday"


def test_reference_custom_music_upload_is_stored_and_mixed(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    session["generation_run_id"] = "26eec660-ec72-41f1-ac65-d5c162491413"
    music_path = tmp_path / "custom.wav"
    result_path = tmp_path / "custom-final.mp4"
    result_path.write_bytes(b"custom-video")
    seen = {}

    def fake_store(music_bytes, **kwargs):
        seen["store"] = (music_bytes, kwargs)
        return str(music_path)

    def fake_render(video_bytes, **kwargs):
        seen["render"] = (video_bytes, kwargs)
        return str(result_path)

    monkeypatch.setattr(media, "store_reference_custom_music", fake_store)
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    monkeypatch.setattr(media, "_safe_reply_video", AsyncMock())
    monkeypatch.setattr(media, "_start_reference_description_approval", AsyncMock(return_value=True))
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    asyncio.run(
        media.handle_reference_custom_music_upload(
            update,
            session,
            music_bytes=b"uploaded-track",
            file_name="mine.wav",
            mime_type="audio/wav",
        )
    )

    assert seen["store"] == (
        b"uploaded-track",
        {
            "run_id": session["generation_run_id"],
            "file_name": "mine.wav",
            "mime_type": "audio/wav",
        },
    )
    assert seen["render"][0] == b"approved-veo"
    assert seen["render"][1]["music_path"] == str(music_path)
    assert seen["render"][1]["choice"] == "custom"


def test_reference_music_result_survives_minimal_session_cache():
    from app.session_store import _chat_session_cache_payload

    session = {
        reference.REFERENCE_MUSIC_CHOICE_SESSION_KEY: "holiday",
        reference.REFERENCE_MUSIC_SOURCE_PATH_SESSION_KEY: "/tmp/holiday.mp3",
        reference.REFERENCE_MUSIC_RESULT_PATH_SESSION_KEY: "/tmp/final.mp4",
    }

    assert _chat_session_cache_payload(session) == session


def test_render_reference_music_variant_mixes_source_audio_after_video(monkeypatch, tmp_path):
    from app import ai_approval_service, post_production, series_theme

    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"music")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mixed")

    monkeypatch.setattr(ai_approval_service, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 8.0)
    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(post_production, "_apply_db_attenuation", lambda volume, _db: volume)
    monkeypatch.setattr(series_theme, "holiday_music_filter", lambda _path: "")

    result = reference.render_reference_music_variant(
        b"approved-video",
        music_path=str(music_path),
        music_volume=0.2,
        choice="default",
        output_root=tmp_path / "renders",
    )

    assert Path(result).read_bytes() == b"mixed"
    assert Path(result).name == "approved_veo_default.mp4"
    assert "amix=inputs=2:duration=first" in commands[0][commands[0].index("-filter_complex") + 1]
    assert commands[0][commands[0].index("-map") + 1] == "0:v:0"


def test_reference_custom_music_audio_is_routed_from_audio_handler(monkeypatch):
    from app.handlers import handlers_state_scene as scene_handler

    chat_id = 817263
    session = {"state": reference.STATE_WAITING_REFERENCE_CUSTOM_MUSIC}
    scene_handler.SESSIONS[chat_id] = session
    monkeypatch.setattr(
        scene_handler,
        "_extract_visualization_audio_bytes",
        AsyncMock(return_value=(b"custom-music", {"file_name": "track.wav", "mime_type": "audio/wav"})),
    )
    routed = AsyncMock(return_value=True)
    monkeypatch.setattr(scene_handler, "handle_reference_custom_music_upload", routed)
    monkeypatch.setattr(scene_handler, "save_session_if_possible", lambda *_args, **_kwargs: None)
    update = SimpleNamespace(
        message=SimpleNamespace(),
        effective_chat=SimpleNamespace(id=chat_id),
    )

    asyncio.run(scene_handler.on_audio(update, SimpleNamespace()))

    routed.assert_awaited_once_with(
        update,
        session,
        music_bytes=b"custom-music",
        file_name="track.wav",
        mime_type="audio/wav",
    )
    scene_handler.SESSIONS.pop(chat_id, None)
