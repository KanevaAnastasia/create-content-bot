import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.project_modes import project_mode_rows
from app import reference_generation as reference


def test_reference_mode_appears_in_grandma_and_discipline_menus():
    assert ["По референсу"] in project_mode_rows("grandma_kitchen")
    assert ["По референсу"] in project_mode_rows("discipline")
    assert ["По референсу"] not in project_mode_rows("other")


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


def test_reference_mode_is_rejected_in_unrelated_projects():
    from app.handlers import handlers_state_start as start

    message = SimpleNamespace(reply_text=AsyncMock())
    session = {start._PROJECT_KEY_SESSION: "other", "generation_mode": "story"}

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
    assert "Дисциплина" in message.reply_text.await_args.args[0]


def test_discipline_reference_scene_uses_only_approved_vasily(tmp_path, monkeypatch):
    from app.generation_store import _scene_prompt_payload, merge_scene_prompt_payload
    from app.handlers import handlers_media_scene

    image = tmp_path / "vasily.png"
    image.write_bytes(b"approved vasily")
    monkeypatch.setattr(handlers_media_scene, "ASSETS", {"characters": {
        "grandma": {"base": str(image)}, "cat": {"base": str(image)},
    }})
    calls = Mock(return_value="Вася на ступенях")
    monkeypatch.setattr(reference, "gemini_generate_sync", calls)
    hero = {"display_name": "Василий", "description": "Кудрявый герой", "asset_uri": image.as_uri()}
    prompt = reference.generate_reference_image_prompt(b"composition", comment="Камера спускается", vasily=hero)
    assert prompt == "Камера спускается"
    calls.assert_not_called()
    scene = reference.build_reference_scene(source_image=b"composition", image_prompt=prompt, vasily=hero)
    assert scene["characters"] == ["vasily"] and scene["model"] == "veo3_fast"
    assert scene["duration_sec"] == 8 and scene["aspect_ratio"] == "9:16"
    assert handlers_media_scene._resolve_scene_references(scene) == {"vasily": str(image)}
    assert handlers_media_scene._load_scene_reference_images(scene) == [b"approved vasily"]
    saved = _scene_prompt_payload(scene)
    restored = {"prompt_meta_json": saved["prompt_meta_json"]}
    merge_scene_prompt_payload(restored, saved)
    assert restored["characters"] == ["vasily"]
    assert handlers_media_scene._load_scene_reference_images(restored) == [b"approved vasily"]

    from app.handlers.handlers_media_core import enforce_video_prompt_voice_requirements
    from app.pipeline import append_veo3_video_framing_constraints

    motion = "Wide side view. Pull back to reveal the stairs, with quiet audience ambience."
    assert enforce_video_prompt_voice_requirements(motion, restored) == motion
    assert append_veo3_video_framing_constraints(motion, restored) == motion


def test_discipline_photo_with_caption_skips_grandma_cast_selection(monkeypatch):
    from app.handlers import handlers_state_scene as handler

    chat_id = 918275
    session = {"state": reference.STATE_WAITING_REFERENCE_IMAGE, "selected_project_key": "discipline"}
    monkeypatch.setitem(handler.SESSIONS, chat_id, session)
    monkeypatch.setattr(handler, "_extract_any_image_from_update", AsyncMock(return_value=b"reference"))
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "_start_reference_prompt_approval", start)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                             message=SimpleNamespace(caption="Вася на сцене", reply_text=AsyncMock()))
    asyncio.run(handler.on_photo(update, None))
    start.assert_awaited_once_with(update, None, chat_id=chat_id, session=session, comment="Вася на сцене")
    assert session[reference.REFERENCE_SOURCE_IMAGE_SESSION_KEY] == b"reference"


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

    assert result == "Пусть дедушка держит самовар"
    assert captured == {}


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


def test_reference_finish_stops_before_final_build_and_requests_original_audio_choice(monkeypatch):
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
    assert session["state"] == reference.STATE_WAITING_REFERENCE_ORIGINAL_AUDIO_CHOICE
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
        reference.REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY: "keep",
        "production": {"json_plan": [scene]},
    }


def test_reference_music_choice_parses_all_four_buttons():
    assert reference.reference_music_choice("Без музыки") == "none"
    assert reference.reference_music_choice("Дефолтная") == "default"
    assert reference.reference_music_choice("Новогодняя") == "holiday"
    assert reference.reference_music_choice("Своя") == "custom"


def test_reference_original_audio_choice_parses_both_buttons():
    assert reference.reference_original_audio_choice("Сохранить оригинальное аудио") == "keep"
    assert reference.reference_original_audio_choice("Убрать оригинальное аудио") == "remove"


def test_reference_original_audio_choice_opens_music_step():
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(
        media.handle_reference_original_audio_choice_state(
            update,
            session,
            "Убрать оригинальное аудио",
        )
    )

    assert result is True
    assert session[reference.REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY] == "remove"
    assert session["state"] == reference.STATE_WAITING_REFERENCE_MUSIC_CHOICE


def test_existing_music_step_without_audio_choice_is_migrated():
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    session.pop(reference.REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Дефолтная"))

    assert result is True
    assert session["state"] == reference.STATE_WAITING_REFERENCE_ORIGINAL_AUDIO_CHOICE
    assert reference.REFERENCE_MUSIC_CHOICE_SESSION_KEY not in session


def test_reference_no_music_is_still_upscaled_before_description(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video()
    result_path = tmp_path / "upscaled.mp4"
    result_path.write_bytes(b"upscaled-video")
    seen = {}

    def fake_render(video_bytes, **kwargs):
        seen["render"] = (video_bytes, kwargs)
        return str(result_path)

    description = AsyncMock(return_value=True)
    sent_video = AsyncMock()
    monkeypatch.setattr(media, "_start_reference_description_approval", description)
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    monkeypatch.setattr(media, "_persist_reference_final_variant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media, "_safe_reply_video", sent_video)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Без музыки"))

    assert result is True
    assert session[reference.REFERENCE_MUSIC_CHOICE_SESSION_KEY] == "none"
    assert seen["render"] == (
        b"approved-veo",
        {
            "music_path": None,
            "music_volume": 0.0,
            "keep_original_audio": True,
            "run_id": "",
            "choice": "clean",
        },
    )
    assert "апскейла до 4K" in sent_video.await_args.kwargs["caption"]
    description.assert_awaited_once_with(update, session)


def test_reference_remove_original_audio_without_music_renders_silent_video(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    session = _reference_session_with_approved_video(b"approved-video-with-audio")
    session[reference.REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY] = "remove"
    result_path = tmp_path / "silent.mp4"
    result_path.write_bytes(b"silent-video")
    seen = {}

    def fake_render(video_bytes, **kwargs):
        seen["render"] = (video_bytes, kwargs)
        return str(result_path)

    sent_video = AsyncMock()
    description = AsyncMock(return_value=True)
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    monkeypatch.setattr(media, "_persist_reference_final_variant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media, "_safe_reply_video", sent_video)
    monkeypatch.setattr(media, "_start_reference_description_approval", description)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    result = asyncio.run(media.handle_reference_music_choice_state(update, session, "Без музыки"))

    assert result is True
    assert seen["render"] == (
        b"approved-video-with-audio",
        {
            "music_path": None,
            "music_volume": 0.0,
            "keep_original_audio": False,
            "run_id": "",
            "choice": "clean",
        },
    )
    assert "без оригинального аудио и без музыки" in sent_video.await_args.kwargs["caption"]
    description.assert_awaited_once_with(update, session)


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
    clean_path = tmp_path / "clean.mp4"
    clean_path.write_bytes(b"clean-video")
    music_result_path = tmp_path / "music.mp4"
    music_result_path.write_bytes(b"mixed-video")
    render_calls = []
    artifact_calls = []

    def fake_render(video_bytes, **kwargs):
        render_calls.append((video_bytes, kwargs))
        return str(music_result_path if kwargs.get("music_path") else clean_path)

    monkeypatch.setattr(media, "_resolve_music_path", lambda _project: (str(music_path), 0.25))
    monkeypatch.setattr(media, "render_reference_music_variant", fake_render)
    monkeypatch.setattr(
        media,
        "_persist_reference_final_variant",
        lambda *_args, **kwargs: artifact_calls.append(kwargs),
    )
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
                "music_path": None,
                "music_volume": 0.0,
                "keep_original_audio": True,
                "run_id": "",
                "choice": "clean",
            },
        ),
        (
            b"approved-video-bytes",
            {
                "music_path": str(music_path),
                "music_volume": 0.25,
                "keep_original_audio": True,
                "run_id": "",
                "choice": "default",
            },
        )
    ]
    assert artifact_calls == [
        {
            "path": str(clean_path),
            "artifact_type": "final_upscaled_4k",
            "label": "4K",
            "reference_variant": "4k",
        },
        {
            "path": str(music_result_path),
            "artifact_type": "final_subtitled_music",
            "label": "4K + музыка",
            "reference_variant": "4k_music",
        },
    ]
    assert session[reference.REFERENCE_MUSIC_RESULT_PATH_SESSION_KEY] == str(music_result_path)
    assert sent_video.await_args.kwargs["video"].read() == b"mixed-video"
    description.assert_awaited_once_with(update, session)


def test_reference_final_variant_is_registered_for_admin_publication(monkeypatch, tmp_path):
    from app.handlers import handlers_state_media as media

    path = tmp_path / "final-4k.mp4"
    path.write_bytes(b"video")
    calls = []
    monkeypatch.setattr(
        media,
        "persist_generation_artifact",
        lambda session, **kwargs: calls.append((session, kwargs)) or "artifact-id",
    )
    session = {"generation_run_id": "26eec660-ec72-41f1-ac65-d5c162491413"}

    media._persist_reference_final_variant(
        session,
        path=path,
        artifact_type="final_upscaled_4k",
        label="4K",
        reference_variant="4k",
    )

    assert [item[1]["language"] for item in calls] == ["ru", "en"]
    for saved_session, kwargs in calls:
        assert saved_session is session
        assert kwargs["artifact_type"] == "final_upscaled_4k"
        assert kwargs["uri"] == f"file://{path.resolve()}"
        assert kwargs["payload"]["reference_mode"] is True
        assert kwargs["payload"]["reference_variant"] == "4k"
        assert kwargs["payload"]["shared_reference_video"] is True


def test_reference_description_approval_marks_run_ready(monkeypatch):
    from app.handlers import handlers_state_scene as scene_handler

    chat_id = 918275
    session = {
        "generation_mode": "reference",
        "reference_short_description": "Бабушка и дедушка танцуют, пока Тимоша смотрит на них.",
        "state": reference.STATE_WAITING_REFERENCE_DESCRIPTION_APPROVAL,
    }
    scene_handler.SESSIONS[chat_id] = session
    mark_ready = Mock()
    monkeypatch.setattr(scene_handler, "mark_generation_run_ready", mark_ready)
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=chat_id),
    )

    result = asyncio.run(
        scene_handler._handle_reference_description_state(
            update,
            None,
            chat_id=chat_id,
            session=session,
            text="ОК",
        )
    )

    assert result is True
    assert session["reference_description_approved"] is True
    assert session["state"] == scene_handler.STATE_READY_FOR_PRODUCTION
    mark_ready.assert_called_once_with(session)
    scene_handler.SESSIONS.pop(chat_id, None)


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
    monkeypatch.setattr(media, "_persist_reference_final_variant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media, "_safe_reply_video", AsyncMock())
    monkeypatch.setattr(media, "_start_reference_description_approval", AsyncMock(return_value=True))
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))

    asyncio.run(media.handle_reference_music_choice_state(update, session, "Новогодняя"))

    assert seen["style"] == {media.THEME_KEY: media.HOLIDAY_THEME}
    assert seen["render"]["music_path"] == str(music_path)
    assert seen["render"]["music_volume"] == 0.5
    assert seen["render"]["keep_original_audio"] is True
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
    monkeypatch.setattr(media, "_persist_reference_final_variant", lambda *_args, **_kwargs: None)
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
    assert seen["render"][1]["keep_original_audio"] is True
    assert seen["render"][1]["choice"] == "custom"


def test_reference_music_result_survives_minimal_session_cache():
    from app.session_store import _chat_session_cache_payload

    session = {
        reference.REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY: "remove",
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
    upscale_calls = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mixed")

    def fake_upscale(**kwargs):
        upscale_calls.append(kwargs)
        kwargs["dest_path"].write_bytes(b"upscaled")

    monkeypatch.setattr(ai_approval_service, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 8.0)
    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(post_production, "_apply_db_attenuation", lambda volume, _db: volume)
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (720, 1280))
    monkeypatch.setattr(post_production, "_upscaled_dimensions_for_render", lambda _w, _h: (2160, 3840))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)
    monkeypatch.setattr(series_theme, "holiday_music_filter", lambda _path: "")

    result = reference.render_reference_music_variant(
        b"approved-video",
        music_path=str(music_path),
        music_volume=0.2,
        choice="default",
        output_root=tmp_path / "renders",
    )

    assert Path(result).read_bytes() == b"upscaled"
    assert Path(result).name == "approved_veo_default_upscaled_4k.mp4"
    assert "amix=inputs=2:duration=first" in commands[0][commands[0].index("-filter_complex") + 1]
    assert commands[0][commands[0].index("-map") + 1] == "0:v:0"
    assert upscale_calls[0]["target_width"] == 2160
    assert upscale_calls[0]["target_height"] == 3840


def test_render_reference_music_variant_can_remove_all_original_audio(monkeypatch, tmp_path):
    from app import ai_approval_service, post_production

    commands = []
    upscale_calls = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"silent")

    def fake_upscale(**kwargs):
        upscale_calls.append(kwargs)
        kwargs["dest_path"].write_bytes(b"silent-upscaled")

    monkeypatch.setattr(ai_approval_service, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (720, 1280))
    monkeypatch.setattr(post_production, "_upscaled_dimensions_for_render", lambda _w, _h: (2160, 3840))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)

    result = reference.render_reference_music_variant(
        b"approved-video",
        music_path=None,
        keep_original_audio=False,
        choice="none",
        output_root=tmp_path / "renders",
    )

    assert Path(result).read_bytes() == b"silent-upscaled"
    assert "-an" in commands[0]
    assert "-c:v" in commands[0]
    assert "copy" in commands[0]
    assert upscale_calls


def test_reference_no_music_with_original_audio_still_runs_upscale(monkeypatch, tmp_path):
    from app import ai_approval_service, post_production

    upscale_calls = []

    def fake_upscale(**kwargs):
        upscale_calls.append(kwargs)
        kwargs["dest_path"].write_bytes(b"upscaled-with-original-audio")

    monkeypatch.setattr(ai_approval_service, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (720, 1280))
    monkeypatch.setattr(post_production, "_upscaled_dimensions_for_render", lambda _w, _h: (2160, 3840))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)

    result = reference.render_reference_music_variant(
        b"approved-video-with-audio",
        music_path=None,
        keep_original_audio=True,
        choice="none",
        output_root=tmp_path / "renders",
    )

    assert Path(result).read_bytes() == b"upscaled-with-original-audio"
    assert upscale_calls[0]["src_path"].name == "approved_veo.mp4"
    assert upscale_calls[0]["target_width"] == 2160
    assert upscale_calls[0]["target_height"] == 3840


def test_reference_music_is_louder_when_original_audio_is_absent(monkeypatch, tmp_path):
    from app import ai_approval_service, post_production, series_theme

    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"music")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"music-only")

    def fake_upscale(**kwargs):
        kwargs["dest_path"].write_bytes(b"upscaled")

    monkeypatch.setattr(ai_approval_service, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: False)
    monkeypatch.setattr(
        post_production,
        "_apply_db_attenuation",
        lambda *_args: (_ for _ in ()).throw(AssertionError("music-only volume must not be attenuated")),
    )
    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (720, 1280))
    monkeypatch.setattr(post_production, "_upscaled_dimensions_for_render", lambda _w, _h: (2160, 3840))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)
    monkeypatch.setattr(series_theme, "holiday_music_filter", lambda _path: "")

    reference.render_reference_music_variant(
        b"approved-video-without-audio",
        music_path=str(music_path),
        music_volume=0.2,
        keep_original_audio=True,
        choice="default",
        output_root=tmp_path / "renders",
    )

    audio_filter = commands[0][commands[0].index("-filter_complex") + 1]
    assert "volume=0.2000" in audio_filter
    assert "amix=" not in audio_filter


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
