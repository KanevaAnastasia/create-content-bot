import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import inner_boss_workflow as workflow
from app.inner_boss_scenes import PILOT_SCRIPT, clock_overlays, effect_events, frame_actions, parse_script, render_master, render_scene
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

    async def video(update, data, scene):
        scene["video"] = "generated.mp4"

    async def edit(data, scene):
        scene["previews"] = {"ru": "edited.mp4"}

    monkeypatch.setattr(workflow, "_image", AsyncMock(side_effect=image))
    monkeypatch.setattr(workflow, "_video", AsyncMock(side_effect=video))
    monkeypatch.setattr(workflow, "_edit", AsyncMock(side_effect=edit))
    for stage in ("scene", "image", "video", "edit"):
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
    cues = clock_overlays(scene, 8)
    assert [cue["text"] for cue in cues] == ["06:00", "09:00"]
    assert [cue["start_sec"] for cue in cues] == [0, 6.85]
    scene["clock_position"] = [0.091, 0.772]
    on_clock = clock_overlays(scene, 8)
    assert on_clock[0]["end_sec"] == on_clock[1]["start_sec"]
    assert all(cue["clock_display"] and cue["anchor_x_ratio"] == 0.091 for cue in on_clock)


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
    assert data["stage"] == "video"
    workflow._video.assert_awaited_once()


def test_boss_passes_both_images_only_for_the_appearance_scene(tmp_path, monkeypatch):
    from app.handlers import handlers_media_core

    run = AsyncMock(return_value=b"video")
    monkeypatch.setattr(handlers_media_core, "_generate_video_bytes_with_high_load_retry", run)
    monkeypatch.setattr(workflow, "_target", lambda _, suffix: tmp_path / f"render{suffix}")
    paths = [tmp_path / name for name in ("before.png", "after.png")]
    for index, path in enumerate(paths):
        path.write_bytes(bytes([index + 1]))
    pair, single = parse_script(PILOT_SCRIPT)[:2]
    single["visual"] = "Герой выключает будильник."
    for scene in (pair, single):
        scene.update(image=str(paths[0]), boss_image=str(paths[1]))
        asyncio.run(workflow._video(None, {}, scene))

    first, second = [call.kwargs for call in run.await_args_list]
    assert first["start_image"] == b"\x01" and first["end_image"] == b"\x02"
    assert first["kling_keyframes"] is True and first["audio_enabled"] is False
    assert first["duration_sec"] == 8 and "Keyframes" in first["prompt"]
    assert second["end_image"] is None and "kling_keyframes" not in second
