import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import inner_boss_workflow as workflow
from app.inner_boss_scenes import (
    PILOT_SCRIPT, clock_overlays, day_overlay, dialogue_caption_cues, effect_events, frame_actions, parse_script, render_master,
    render_scene,
)
from app.session_store import _chat_session_cache_payload


def test_pilot_and_inline_format_preserve_speakers_and_silent_titles():
    scenes = parse_script(PILOT_SCRIPT)
    assert len(scenes) == 12
    assert sum(scene["duration"] for scene in scenes) == 90
    assert [line["text"] for scene in scenes[5:] for line in scene["turns"] if line["speaker"] == "hero"] == ["Слышу"]
    assert scenes[10]["turns"] == [] and len(scenes[10]["titles"]["ru"]) == 2
    assert effect_events(scenes[2], 7, 0).count(("alarm", 2.3, 1.0)) == 1
    inline, = parse_script(
        'Сцена 1. Утро\nЗвонит будильник. **Босс:** «Ещё пять минут». '
        'Герой: "Ладно". Герой засыпает.\nЗвук: гром@1.2'
    )
    assert [turn["text"] for turn in inline["turns"]] == ["Ещё пять минут", "Ладно"]
    assert effect_events(inline, 7, 1) == [("thunder", 1.2, 1.6)]
    assert "Герой засыпает" in inline["visual"] and "Ладно" not in inline["visual"]


def test_montage_only_correction_keeps_approved_paid_assets():
    session = {}
    workflow.initialize(session, PILOT_SCRIPT)
    data = session[workflow.KEY]
    old = data["scenes"][0]
    old.update(image="approved.png", video="approved.mp4", video_duration=8, approved=True,
               audio={"ru": {"duration": 4.0}})
    next_scene = data["scenes"][1]
    workflow._replace_scene(data, old["body"].replace("щелчок, шорох", "щелчок@6, шорох"))
    revised = data["scenes"][0]
    assert revised["image"] == old["image"] and revised["video"] == old["video"]
    assert revised["audio"] == old["audio"] and "approved" not in revised
    assert revised["fx_times"]["click"] == [6]
    assert data["scenes"][1] is next_scene
    assert _chat_session_cache_payload(session)[workflow.KEY]["scenes"][0]["video"] == old["video"]


def test_scene_workflow_advances_only_after_preview_approval(monkeypatch):
    script = 'Сцена 1. Утро\nГерой открывает глаза.\n\nСцена 2. День\nГерой идёт.'
    session = {"story_english_requested": False}
    workflow.initialize(session, script)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())

    async def image(data, scene):
        scene["image"] = "generated.png"
        scene["frame_policy"] = workflow.FRAME_POLICY

    async def video(update, data, scene, *, approved_prompt):
        assert approved_prompt
        scene["video"] = "generated.mp4"

    async def edit(data, scene):
        scene["previews"] = {"ru": "edited.mp4"}

    monkeypatch.setattr(workflow, "_image", AsyncMock(side_effect=image))
    monkeypatch.setattr(workflow, "_video", AsyncMock(side_effect=video))
    monkeypatch.setattr(workflow, "_edit", AsyncMock(side_effect=edit))
    for stage in ("scene", "image", "video_prompt", "video", "edit"):
        asyncio.run(workflow.handle_text(update, session, "OK"))
        assert session[workflow.KEY]["stage"] == stage
        assert session[workflow.KEY]["index"] == 0
    asyncio.run(workflow.handle_text(update, session, "OK"))
    assert session[workflow.KEY]["index"] == 1
    assert session[workflow.KEY]["scenes"][0]["approved"]
    assert workflow._image.await_count == workflow._video.await_count == workflow._edit.await_count == 1


def test_silent_scene_and_music_render_real_video(tmp_path):
    import subprocess

    from app.post_production import _has_audio_stream, _media_duration, _resolve_ffmpeg

    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg is unavailable")
    raw = tmp_path / "raw.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=blue:s=180x320:d=3:r=30",
                    "-c:v", "libx264", str(raw)], check=True)
    scene, = parse_script("Сцена 1. Утро · 3 секунды\nГерой открывает глаза.\nЗвук: гром\nТитр: Я уже встал.")
    scene["video"] = str(raw)
    edited = tmp_path / "edit.mp4"
    render_scene(scene, edited, language="ru", duration=3)
    assert _has_audio_stream(edited)
    assert _media_duration(edited) == pytest.approx(3, abs=0.08)
    assert "Я уже встал." in edited.with_suffix(".ass").read_text()
    music = tmp_path / "music.wav"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(music)], check=True)
    output = tmp_path / "master.mp4"
    render_master([edited], music, output)
    assert _has_audio_stream(output)
    assert _media_duration(output) == pytest.approx(3, abs=0.15)



def test_appearance_anchors_exclude_dialogue_and_clock_digits():
    scene = parse_script(PILOT_SCRIPT)[0]
    opening, appearance = frame_actions(scene)
    assert "спит с закрытыми глазами" in opening
    assert "приоткрывает глаза" in appearance
    assert "Босс" not in opening and "появляется Босс" in appearance
    assert "минуточек" not in opening + appearance
    assert "06:00" not in opening + appearance and "09:00" not in opening + appearance
    assert clock_overlays(scene, 8) == []
    scene["clock_position"] = [0.091, 0.772]
    on_clock = clock_overlays(scene, 8)
    assert [cue["text"] for cue in on_clock] == ["06:00", "09:00"]
    assert [cue["start_sec"] for cue in on_clock] == [0, 6.85]
    assert on_clock[0]["end_sec"] == on_clock[1]["start_sec"]
    assert all(cue["clock_display"] and cue["anchor_x_ratio"] == 0.091 for cue in on_clock)


def test_day_card_uses_explicit_header_or_first_line():
    scenes = parse_script(PILOT_SCRIPT)
    assert day_overlay(scenes[0], 8, "en")[0]["text"] == "Day 1"
    assert day_overlay(scenes[1], 7, "ru")[0]["text"] == "День 2"
    assert day_overlay(scenes[2], 7, "ru")[0]["text"] == "День 3-345"
    assert day_overlay(scenes[2], 7, "en")[0]["text"] == "Day 3-345"
    assert day_overlay(scenes[3], 8, "ru") == []
    scene, = parse_script("Сцена 1. Утро\nДень 12. Герой встаёт с кровати.")
    cue, = day_overlay(scene, 7, "ru")
    assert cue["text"] == "День 12" and cue["anchor_top_left"]
    assert cue["start_sec"] == 0 and cue["end_sec"] == 7
    assert frame_actions(scene) == ("Герой встаёт с кровати.", "")


def test_appearance_frame_requires_its_own_approval(monkeypatch):
    session = {"story_english_requested": False}
    workflow.initialize(session, "Сцена 1. Утро\nГерой открывает глаза. На стене появляется Босс.")
    data = session[workflow.KEY]
    data["stage"], data["languages"] = "image", ["ru"]
    scene = data["scenes"][0]
    scene.update(image="before.png", frame_policy=workflow.FRAME_POLICY)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())

    async def make_appearance(data, scene):
        scene["boss_image"] = "after.png"

    monkeypatch.setattr(workflow, "_boss_image", AsyncMock(side_effect=make_appearance))
    monkeypatch.setattr(workflow, "_video", AsyncMock())
    asyncio.run(workflow.handle_text(update, session, "OK"))
    assert data["stage"] == "boss_image"
    workflow._video.assert_not_called()
    asyncio.run(workflow.handle_text(update, session, "OK"))
    assert data["stage"] == "video_prompt"
    workflow._video.assert_not_called()
    asyncio.run(workflow.handle_text(update, session, "OK"))
    assert data["stage"] == "video"
    workflow._video.assert_awaited_once()


@pytest.mark.parametrize("use_segments", [False, True])
def test_caption_turns_split_even_when_provider_joins_the_text(use_segments):
    lines = ["Just five more minutes.", "Okay, five minutes won't make a difference."]
    text = "".join(lines)
    scene = {"en_turns": lines}
    audio = {"alignment": {
        "characters": list(text),
        "character_start_times_seconds": [i / 10 for i in range(len(text))],
        "character_end_times_seconds": [(i + 1) / 10 for i in range(len(text))],
    }}
    if use_segments:
        audio["turn_starts"] = [0, len(lines[0])]
    cues = dialogue_caption_cues(scene, audio, language="en", lead=1, duration=9)
    boundary = 1 + len(lines[0]) / 10
    assert all(cue["end_sec"] <= boundary for cue in cues if cue["start_sec"] < boundary)
    rendered_words = {word for cue in cues for word in cue["fixed_block_words"]}
    assert {"minutes.", "Okay,"} <= rendered_words
    assert not any("minutes.Okay," in cue["fixed_block_words"] for cue in cues)


@pytest.mark.parametrize("stage", ["image", "boss_image"])
def test_image_feedback_edits_only_selected_frame_and_waits_for_approval(tmp_path, monkeypatch, stage):
    from app import vertex_images

    session = {}
    workflow.initialize(session, PILOT_SCRIPT)
    data = session[workflow.KEY]
    data["stage"] = stage
    scene = data["scenes"][0]
    for key in ("image", "boss_image"):
        path = tmp_path / f"{key}.png"
        path.write_bytes(key.encode())
        scene[key] = str(path)
    scene["audio"] = {"ru": {"path": "voice.mp3"}}
    scene["video"] = "old.mp4"
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())
    monkeypatch.setattr(workflow, "_target", lambda _data, suffix: tmp_path / f"new{suffix}")
    script_rewrite = AsyncMock()
    monkeypatch.setattr(workflow, "revise_scene", script_rewrite)
    image_requests = []

    def generate(prompt, *, reference_images, aspect_ratio):
        image_requests.append((prompt, reference_images[0], aspect_ratio))
        return b"updated image"

    monkeypatch.setattr(vertex_images, "generate_image_vertex", generate)
    original_opening = scene["image"]
    asyncio.run(workflow.handle_text(update, session, "ИЗМЕНИТЬ"))
    asyncio.run(workflow.handle_text(update, session, "ОК"))
    assert not image_requests
    asyncio.run(workflow.handle_text(update, session, "Герой спит с закрытыми глазами"))
    prompt, reference, ratio = image_requests[0]
    assert "Герой спит с закрытыми глазами" in prompt
    assert reference == stage.encode() and ratio == "9:16"
    assert data["stage"] == stage and not data.get("scene_image_edit_pending")
    assert scene["audio"]["ru"]["path"] == "voice.mp3" and "video" not in scene
    script_rewrite.assert_not_called()
    if stage == "image":
        assert "boss_image" not in scene
    else:
        assert scene["image"] == original_opening


def test_frame_prompt_feedback_regenerates_without_editing_the_script(monkeypatch):
    from app import llm_utils

    session = {}
    workflow.initialize(session, PILOT_SCRIPT)
    data = session[workflow.KEY]
    data["stage"] = "image"
    original = data["scenes"][0]
    original["image"] = "old.png"
    original["audio"] = {"ru": {"path": "voice.mp3"}}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())
    monkeypatch.setattr(llm_utils, "gemini_generate_sync", lambda *_, **__: "Крупнее лицо спящего героя.")
    monkeypatch.setattr(workflow, "_revise_frame", AsyncMock())

    async def create(_data, scene):
        assert "Крупнее лицо" in scene["frame_prompt_overrides"]["image"]
        scene["image"] = "new.png"

    monkeypatch.setattr(workflow, "_image", AsyncMock(side_effect=create))
    asyncio.run(workflow.handle_text(update, session, "ФИДБЕК"))
    workflow._image.assert_not_awaited()
    asyncio.run(workflow.handle_text(update, session, "Крупнее лицо"))
    assert data["stage"] == "image"
    revised = data["scenes"][0]
    assert revised["body"] == original["body"] and revised["audio"] == original["audio"]
    assert revised["image"] == "new.png"
    workflow._revise_frame.assert_not_awaited()


def test_speech_timing_can_be_changed_repeatedly_without_generating_media(monkeypatch):
    session = {}
    workflow.initialize(session, PILOT_SCRIPT)
    data = session[workflow.KEY]
    data["stage"] = "edit"
    scene = data["scenes"][0]
    scene.update(video="raw.mp4", video_duration=8, audio={"ru": {"path": "voice.mp3"}})
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())
    monkeypatch.setattr(workflow, "_edit", AsyncMock())
    monkeypatch.setattr(workflow, "_video", AsyncMock())
    monkeypatch.setattr(workflow, "_audio", AsyncMock())
    asyncio.run(workflow.handle_text(update, session, "Сдвинуть озвучку"))
    asyncio.run(workflow.handle_text(update, session, "ОК"))
    workflow._edit.assert_not_awaited()
    asyncio.run(workflow.handle_text(update, session, "00:04,4"))
    asyncio.run(workflow.handle_text(update, session, "Озвучка:3.2"))
    assert [call.args[1]["speech_start_sec"] for call in workflow._edit.await_args_list] == [4.4, 3.2]
    assert data["stage"] == "edit"
    assert data["scenes"][0]["video"] == "raw.mp4" and data["scenes"][0]["audio"] == scene["audio"]
    workflow._video.assert_not_awaited()
    workflow._audio.assert_not_awaited()


def test_boss_passes_both_images_and_preserves_alarm_cues(tmp_path, monkeypatch):
    from app.handlers import handlers_media_core

    run = AsyncMock(return_value=b"video")
    monkeypatch.setattr(handlers_media_core, "_generate_video_bytes_with_high_load_retry", run)
    monkeypatch.setattr(workflow, "_target", lambda _, suffix: tmp_path / f"render{suffix}")
    paths = [tmp_path / name for name in ("before.png", "after.png")]
    for index, path in enumerate(paths):
        path.write_bytes(bytes([index + 1]))
    pair, _, montage = parse_script(PILOT_SCRIPT)[:3]
    pair.update(image=str(paths[0]), boss_image=str(paths[1]))
    montage.update(image=str(paths[0]), end_image=str(paths[1]))
    for scene in (pair, montage):
        asyncio.run(workflow._video(None, {}, scene))

    first, second = [call.kwargs for call in run.await_args_list]
    assert first["start_image"] == b"\x01" and first["end_image"] == b"\x02"
    assert first["kling_keyframes"] is True and first["audio_enabled"] is False
    assert first["duration_sec"] == 8 and "Keyframes" not in first["prompt"]
    assert "When Boss speaks, animate the shadow's mouth while Hero listens with his lips closed." in first["prompt"]
    assert "When Hero speaks, animate Hero's mouth while Boss stays quiet" in first["prompt"]
    assert first["prompt"].index("1. Boss, the red-edged shadow speaks:") < first["prompt"].index("2. Hero, the human character speaks:")
    assert second["end_image"] is None and "kling_keyframes" not in second
    assert "image-to-video" not in second["prompt"] and "No audio" not in second["prompt"]
    assert "At 0s: the alarm clock visibly vibrates" in second["prompt"]
    assert "At 2.3s: the alarm clock visibly vibrates" in second["prompt"]
    assert "At 0.75s: the Hero presses the clock button" in second["prompt"]


def test_scene_video_prompt_override_is_sent_without_boilerplate():
    scene = parse_script(PILOT_SCRIPT)[2]
    prompt = "At 0s: the alarm clock visibly vibrates with 06:00 on its display.\nAt 0.75s: the Hero presses the clock button."
    scene["video_prompt_override"] = prompt
    assert workflow._video_prompt(scene, 7, *frame_actions(scene)) == prompt


def test_boss_only_retake_requires_approval_of_the_new_prompt(monkeypatch):
    session = {}
    workflow.initialize(session, "Сцена 1. Зеркало\nГерой смотрит в зеркало. Там появляется Босс.\nБосс: «Ну и куда ты в них сходил?»")
    data = session[workflow.KEY]
    data["stage"] = "video"
    scene = data["scenes"][0]
    scene.update(video="old.mp4", image="before.png", boss_image="after.png", frame_feedback={"image": "Большое зеркало"})
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(workflow, "_save", lambda *_: None)
    monkeypatch.setattr(workflow, "_show", AsyncMock())
    monkeypatch.setattr(workflow, "_video", AsyncMock())

    asyncio.run(workflow.handle_text(update, session, "Перегенерировать"))
    assert data["stage"] == "video_prompt"
    text = data["video_prompt_review"]["text"]
    assert "Only Boss speaks." in text and "The Hero keeps his lips closed" in text
    assert "When Hero speaks" not in text
    assert "Большое зеркало" not in text and "Opening frame correction" not in text
    asyncio.run(workflow.handle_text(update, session, "ПРОМПТ:"))
    asyncio.run(workflow.handle_text(update, session, "ОК"))
    workflow._video.assert_not_awaited()
    asyncio.run(workflow.handle_text(update, session, "Камера неподвижна. Герой слушает."))
    updated = data["video_prompt_review"]["text"]
    assert updated.startswith("Камера неподвижна.") and "Only Boss speaks." in updated
    assert scene["video"] == "old.mp4"
    asyncio.run(workflow.handle_text(update, session, "ОК"))
    workflow._video.assert_awaited_once_with(update, data, scene, approved_prompt=updated)
    assert data["stage"] == "video"
