import asyncio
from types import SimpleNamespace

import pytest

from app import discipline_modes as modes
from app.story_generation import enforce_discipline_narrative_cast, ensure_story_video_camera_safety
from app.handlers import handlers_state_media
from app.handlers.handlers_state_media import _scene_video_prompt_for_language, _advance_scene_media_after_current
from app.scene_media_candidates import register_video_candidate, selected_image_bytes_for_video


def test_environment_exception_removes_cast_and_stale_refs():
    scene = {'discipline_cast_override': 'environment_only', 'characters': ['v'], 'references': {'v': 'old'}}
    normal = {'characters': []}
    enforce_discipline_narrative_cast([scene, normal], [
        {'id': 'v', 'display_name': 'Василий'}, {'id': 'discipline_knopka', 'display_name': 'Кнопка'}])
    assert scene['characters'] == [] and scene['references'] == {}
    assert 'v' in normal['characters']


def test_explicit_reveal_removes_old_boundary_guard_at_generation():
    guarded = ensure_story_video_camera_safety('Move forward into the courtyard.')
    assert 'source-image boundaries' in guarded
    scene = {'generation_mode': 'story', 'story_camera_reveal_requested': True, 'video_prompt_by_lang': {'ru': guarded}}
    assert _scene_video_prompt_for_language(scene, 'ru') == 'Move forward into the courtyard.'


def test_continuation_only_skips_image_step_for_approved_source():
    source = {'candidate_id': 'source', 'approved_for_production': False}
    session = {'production': {'json_plan': [
        {'video_candidates_by_lang': {'ru': [source]}},
        {'continuation_source_video_candidate_id': 'source', 'image_candidates': [{'approved_for_video': True}]}]}}
    _advance_scene_media_after_current(session, 0)
    assert session['scene_media_substep'] == 'start_prompt'
    source['approved_for_production'] = True
    _advance_scene_media_after_current(session, 0)
    assert session['scene_media_substep'] == 'video_prompt'
    session['production']['json_plan'][0]['video_candidates_by_lang']['ru'].append(
        {'candidate_id': 'replacement', 'approved_for_production': True})
    _advance_scene_media_after_current(session, 0)
    assert session['scene_media_substep'] == 'start_prompt'


def test_continuity_flags_survive_database_prompt_restore():
    from app.generation_store import merge_scene_prompt_payload
    flags = {'discipline_cast_override': 'environment_only', 'story_camera_reveal_requested': True,
             'continuation_source_video_candidate_id': 'source'}
    scene = {}
    merge_scene_prompt_payload(scene, {'prompt_meta_json': flags})
    assert all(scene[k] == v for k, v in flags.items())


@pytest.mark.parametrize("path", ["manual_button", "planned", "ai_approve"])
def test_continue_previous_video_uses_exact_last_frame_and_skips_image_generation(monkeypatch, path):
    previous = {}
    source = register_video_candidate(previous, language="ru", video_bytes=b"approved-video")
    source["approved_for_production"] = True
    current = {
        "model": "kling_3_0",
        "generation_mode": "story",
        "speech_by_lang": {"ru": "Продолжаем."},
        "video_approvals_by_lang": {"ru": False},
    }
    if path != "manual_button":
        current[modes.MODE_KEY] = modes.FLOW
    session = {
        "target_languages": ["ru"],
        "image_scene_index": 1,
        "scene_media_substep": "start_prompt",
        "production": {"json_plan": [previous, current]},
    }
    extracted_from = []
    monkeypatch.setattr(
        handlers_state_media,
        "extract_last_video_frame",
        lambda *, video_bytes=None, video_path=None: extracted_from.append(video_bytes) or b"final-frame",
    )
    sent = []

    async def fake_send_next(_update, _session):
        sent.append(True)
        if path == "ai_approve":
            _session["state"] = handlers_state_media.STATE_READY_FOR_PRODUCTION

    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", fake_send_next)

    class Message:
        async def reply_text(self, text, **_kwargs):
            return text

    update = SimpleNamespace(message=Message())
    if path == "ai_approve":
        session[handlers_state_media.SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] = handlers_state_media.SCENE_MEDIA_APPROVE_MODE_AI
        result = asyncio.run(handlers_state_media._run_scene_media_ai_approve(
            update, context=None, chat_id=1, session=session,
        ))
    else:
        approve = (handlers_state_media._approve_planned_scene_start if path == "planned"
                   else handlers_state_media._continue_scene_from_previous_video)
        result = asyncio.run(approve(update, session=session, scene=current, idx=1))

    assert result is True
    assert extracted_from == [b"approved-video"]
    assert sent == [True]
    assert session["scene_media_substep"] == "video_prompt"
    assert current["continuation_source_video_candidate_id"] == source["candidate_id"]
    assert selected_image_bytes_for_video(current, kind="start") == b"final-frame"
    assert current["image_candidates"][-1]["approved_for_video"] is True
