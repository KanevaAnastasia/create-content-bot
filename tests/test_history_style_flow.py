import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import story_generation as g, story_style_previews as previews, discipline_modes as m
from app.discipline_queue import build_story_session
from app.handlers import handlers_state_start as start
from app.history_visual_styles import HISTORY_STYLES


def test_queue_history_opens_mode_choice_with_exact_script_and_no_total():
    row=SimpleNamespace(id='idea',mode=m.ARCHIVE,title='Кинг',series='Истории несовершенных ошибок',episode=1,
                        script='Стивен Кинг выбросил первые страницы.',direction='Одна история.',review_plan='')
    session=build_story_session(row)
    assert session['state']==start.STATE_WAITING_GENERATION_MODE
    assert 'story_visual_style' not in session
    assert session['story_voiceover_text']==row.script
    assert session['series_counter_by_lang']=={'ru':'История 1','en':'Story 1'}
    assert session['story_english_requested'] is True


@pytest.mark.parametrize('key', HISTORY_STYLES)
def test_history_style_choice_preserves_accepted_text_without_model(monkeypatch,key):
    monkeypatch.setattr(start,'SESSIONS',{})
    writer=AsyncMock()
    monkeypatch.setattr(start,'_start_initial_story_voiceover',writer)
    session={'discipline_narrative_mode':m.ARCHIVE,'discipline_queue_idea_id':'idea',
             'story_voiceover_text':'Точный принятый текст.', 'story_english_requested':True}
    message=SimpleNamespace(reply_text=AsyncMock())
    number=next(i for i,k,_ in previews.page_items(session) if k==key)
    asyncio.run(start._handle_waiting_story_style_choice_state(SimpleNamespace(message=message),None,
        chat_id=123,session=session,text=str(number)))
    writer.assert_not_awaited()
    assert session['story_voiceover_text']=='Точный принятый текст.'
    assert session['story_visual_style']==key
    assert session['state']==start.STATE_WAITING_STORY_SCRIPT_APPROVAL
    assert g.story_visual_style_config(key)['reference_path']==''


def test_history_cast_keeps_female_actor_without_forcing_cat():
    scenes=[{m.MODE_KEY:m.ARCHIVE,'characters':['anastasia']}, {m.MODE_KEY:m.ARCHIVE,'characters':[]}]
    g.enforce_discipline_narrative_cast(scenes,[{'id':'anastasia','description':'героиня'}])
    assert scenes[0]['characters']==['anastasia']
    assert scenes[1]['characters']==[]


def test_history_style_duration_camera_survive_scene_and_production(monkeypatch):
    prompts=[]
    def generator(prompt,**kwargs):
        prompts.append(prompt)
        return json.dumps({'scenes':[{'characters':['anastasia'],'visual':'Табита читает лист.',
                         'camera_direction':'locked-off camera'}]})
    monkeypatch.setattr(g,'gemini_generate_sync',generator)
    plan=g.generate_story_scene_plan(source_story='Табита читает лист.',voiceover_text='Табита читает лист.',
        duration_sec=6.2,alignment={},project_slug='discipline',discipline_mode=m.ARCHIVE,
        story_visual_style='history_comic',project_characters=[{'id':'anastasia','description':'актриса'}])['json_plan']
    assert len(plan)==1
    assert plan[0]['duration_sec']==7
    assert plan[0]['story_visual_style_key']=='history_comic'
    assert plan[0]['characters']==['anastasia']
    out=g.generate_story_production(scenes_plan=plan)['json_plan'][0]
    assert plan[0]['camera_direction']=='locked-off camera'
    assert out['camera_direction']=='locked-off camera'
    assert out['generation_duration_sec']==7
    assert out['video_prompt'].startswith('7-second')
    assert 'gouache paper theatre' not in out['video_prompt']


@pytest.mark.parametrize('ends',[[1.2,5.7,14.3],[4.5,9.8,17.2],[30.2]])
def test_semantic_clips_have_no_lost_time_and_at_most_one_second_trim(ends):
    beats=[];a=0
    for i,b in enumerate(ends,1):
        beats.append(dict(beat=i,start_sec=a,end_sec=b,narration_excerpt=str(i)));a=b
    clips=g._history_beat_clips(beats)
    assert clips[0]['start_sec']==0 and clips[-1]['end_sec']==ends[-1]
    for i,c in enumerate(clips):
        used=c['end_sec']-c['start_sec']
        assert -0.001<=c['generation_duration_sec']-used<=1.001
        if i:assert clips[i-1]['end_sec']==c['start_sec']


def test_history_rejects_unknown_actor_instead_of_losing_reference():
    with pytest.raises(ValueError, match="исполнителя"):
        g.enforce_discipline_narrative_cast([{m.MODE_KEY:m.ARCHIVE,'characters':['stephen_king']}],[{'id':'vasily'}])

def test_history_direction_does_not_force_cat_or_flat_animation():
    direction=m.visual_direction(m.ARCHIVE)
    assert 'Кнопка участвует' not in direction
    assert 'Плоская рисованная' not in direction


def test_history_planner_rejects_unknown_actor_before_filter(monkeypatch):
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'scenes':[{'characters':['stephen_king'],'visual':'Кинг у машинки'}]}))
    with pytest.raises(ValueError,match="исполнителя"):
        g.generate_story_scene_plan(source_story='Кинг у машинки.',voiceover_text='Кинг у машинки.',duration_sec=5,alignment={},project_slug='discipline',discipline_mode=m.ARCHIVE,project_characters=[{'id':'vasily','description':'герой'}])

def test_history_video_prompt_cannot_override_locked_camera(monkeypatch):
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'prompts':[{'video_prompt_en':'5-second Kling 3.0 image-to-video shot. No audio. Start with a slow optical zoom-in within the existing frame.'}]}))
    scene={m.MODE_KEY:m.ARCHIVE,'generation_duration_sec':5,'camera_direction':'locked-off camera','action':'Табита читает лист.','story_visual_style_key':'history_comic'}
    video=g.generate_story_production(scenes_plan=[scene])['json_plan'][0]['video_prompt']
    assert 'locked-off camera' in video
    assert 'Start with a slow optical zoom-in' not in video
    assert 'Animate only this visual beat: Табита читает лист.' in video
