import json
from app import story_generation as g
from app.generation_store import _scene_prompt_payload, merge_scene_prompt_payload
from app.discipline_craft import shot_direction_text


def test_shot_direction_survives_plan_restore_and_historical_prompt_compilation(monkeypatch):
    acting = 'Сначала взгляд на лист, затем сжатая рука, разгибание пальцев и пауза реакции.'
    cut = 'Рука остановилась справа внизу; в следующем кадре продолжает движение в той же точке.'
    requests = []
    def model(prompt, **kwargs):
        requests.append(prompt)
        if len(requests) == 1:
            assert "Для каждого clip_visual заполни performance_direction и cut_to_next" in prompt
            return json.dumps({'characters': [{'id': 'writer', 'display_name': 'Писатель', 'description': 'В очках'}],
                               'scenes': [{'characters': ['writer'], 'visual': 'Писатель отпускает лист.',
                                           'camera_direction': 'locked-off camera',
                                           'performance_direction': acting, 'cut_to_next': cut}]})
        return '{"prompts": []}'
    monkeypatch.setattr(g, 'gemini_generate_sync', model)
    planned = g.generate_story_scene_plan(source_story='История.', voiceover_text='Он отпустил лист.',
                                          duration_sec=5, alignment={}, project_slug='discipline',
                                          discipline_mode='lives_archive')
    scene = planned['json_plan'][0]
    assert acting in planned['text_plan'] and cut in planned['text_plan']
    restored = dict(scene)
    restored.pop('performance_direction'); restored.pop('cut_to_next')
    merge_scene_prompt_payload(restored, _scene_prompt_payload(scene))
    production = g.generate_story_production([restored])['json_plan'][0]
    assert acting in production['start_frame_prompt']
    assert acting in production['video_prompt'] and cut in production['video_prompt']
    assert production['generation_duration_sec'] == scene['generation_duration_sec']
    assert production['narration_excerpt'] == scene['narration_excerpt']
    assert len(requests) == 2  # Existing plan and prompt calls, no extra critique/generation pass.


def test_old_scenes_need_no_new_fields_and_notes_are_bounded():
    assert shot_direction_text({}) == ''
    assert len(shot_direction_text({'performance_direction': 'x' * 5000})) < 850
