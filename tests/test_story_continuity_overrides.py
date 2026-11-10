from app.story_generation import enforce_discipline_narrative_cast, ensure_story_video_camera_safety
from app.handlers.handlers_state_media import _scene_video_prompt_for_language, _advance_scene_media_after_current


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
