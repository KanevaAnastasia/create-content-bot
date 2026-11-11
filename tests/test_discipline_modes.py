import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import discipline_modes as modes, story_generation, discipline_intro
from app.handlers import handlers_state_start as start
from app.session_store import _chat_session_cache_payload
from app import editorial_approved as approved


@pytest.mark.parametrize('label,mode',[('О Васе',modes.VASYA),('А что если…',modes.REFRAME)])
def test_mode_selection_survives_resume_and_disables_dialogue(monkeypatch,label,mode):
    monkeypatch.setattr(start,'SESSIONS',{})
    session={'selected_project_key':'discipline','series_pending_recipe':'Не успеваю читать'}
    session[start.SERIES_PENDING_RECIPE_SESSION_KEY]='Не успеваю читать'
    message=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(start._handle_waiting_generation_mode_state(SimpleNamespace(message=message),None,chat_id=1,session=session,text=label))
    assert session[modes.MODE_KEY]==mode
    assert session['generation_mode']=='story'
    assert session['discipline_intro_enabled'] is False
    assert session['state']==start.STATE_WAITING_STORY_ENGLISH_CHOICE
    assert _chat_session_cache_payload(session)[modes.MODE_KEY]==mode
    assert modes.MODE_KEY not in start._session_for_new_generation(session)


def test_new_modes_are_only_offered_in_discipline():
    ordinary=str(start._generation_mode_markup({}).to_dict())
    discipline=str(start._generation_mode_markup({'selected_project_key':'discipline'}).to_dict())
    assert 'О Васе' not in ordinary
    assert 'О Васе' in discipline and 'А что если' in discipline


def test_vasya_visual_direction_is_schematic_2d():
    direction = modes.visual_direction(modes.VASYA)

    assert '2D-анимацию' in direction
    assert 'плоские геометрические формы' in direction
    assert 'фотореализм' in direction


def test_forced_story_workspace_still_offers_discipline_modes(monkeypatch):
    session={'selected_project_key':'discipline','force_story_mode':True}
    monkeypatch.setattr(start,'SESSIONS',{1:session})
    monkeypatch.setattr(start,'_announce_generation_mode',AsyncMock())
    ask=AsyncMock();monkeypatch.setattr(start,'_ask_generation_mode',ask)
    run=AsyncMock();monkeypatch.setattr(start,'_handle_waiting_generation_mode_state',run)
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()),effective_chat=SimpleNamespace(id=1))
    asyncio.run(start._start_generation_with_recipe(update,None,recipe='Читаю мало',prefer_vertex_api=False))
    ask.assert_awaited_once();run.assert_not_awaited()


@pytest.mark.parametrize('mode',[modes.VASYA,modes.REFRAME])
def test_scenario_generation_and_refinement_keep_format(monkeypatch,mode):
    prompts=[]
    def fake(prompt,**kwargs):
        prompts.append(prompt)
        return json.dumps({'title':'Неверный заголовок','voiceover_text':'У меня нет времени. А что если пересмотреть задачи?'},ensure_ascii=False)
    monkeypatch.setattr(story_generation,'gemini_generate_sync',fake)
    result=story_generation.generate_story_voiceover('Не успеваю читать',discipline_mode=mode,user_feedback='Короче',previous_text='Предыдущий текст')
    assert 'Не успеваю читать' in prompts[0] and 'Короче' in prompts[0]
    assert ('Это Вася' if mode==modes.VASYA else 'А что если') in prompts[0]
    if mode==modes.REFRAME:
        assert result['title']=='У меня нет времени.'


def test_vasily_voice_cannot_be_overridden_by_obsolete_db_metadata(monkeypatch):
    rows=[(SimpleNamespace(meta_json={'voice_id':'old'}),SimpleNamespace(display_name='Василий'))]
    monkeypatch.setattr(discipline_intro,'_intro_character_rows',lambda slug:rows)
    turns=discipline_intro.discipline_intro_voice_turns({'vasily':'Привет','anastasia':'Привет'})
    assert turns[0]['voice_id']==modes.MALE_VOICE_ID
    assert turns[1]['voice_id']!=modes.MALE_VOICE_ID


def alignment(text):
    return {'characters':list(text),'character_start_times_seconds':[i*.1 for i in range(len(text))],
            'character_end_times_seconds':[(i+1)*.1 for i in range(len(text))]}


def test_title_uses_spoken_words_and_real_independent_timing():
    text='Почему не ты? Начни сейчас.'
    rows=modes.title_word_timing('Почему не ты?',text,3,alignment(text))
    assert [r['start'] for r in rows]==pytest.approx([0,.7,1.0])
    assert modes.title_word_timing('Другая фраза',text,3,alignment(text))==[]
    assert modes.title_word_timing('Почему не ты?',text,3,{})==[]


def test_approved_assets_are_frozen():
    manifest=approved.data('approval.json')
    assert manifest['approved_styles']==['1','2','3','5','9']
    for name,digest in manifest['files_sha256'].items():
        assert hashlib.sha256((approved.ASSETS/name).read_bytes()).hexdigest()==digest


def test_timed_headline_reveals_words_in_fixed_geometry():
    style={'editorial_style_id':'9','word_timing_by_lang':{'ru':[
        {'text':'Почему','start':.1,'end':.4},{'text':'не','start':.5,'end':.6},
        {'text':'ты?','start':.7,'end':1.0}]}}
    cue={'text':'Почему не ты?','language':'ru','start_sec':0,'end_sec':2}
    events=approved.ass_events(cue,style,720,1280,'Default')
    assert len(events)==4
    assert ',0:00:00.10,' in events[1]
    assert ',0:00:00.50,' in events[2]
    assert ',0:00:00.70,' in events[3]
    assert all('\\p1' in line for line in events)


def test_partial_semantic_unit_does_not_change_full_word_positions():
    full=approved.part_layer('2','en',0,'I have',None)
    partial=approved.part_layer('2','en',0,'I have',1)
    assert full.size==partial.size==(720,1280)
    assert partial.getbbox()[0]==full.getbbox()[0]
    assert partial.getbbox()[2]<full.getbbox()[2]


@pytest.mark.parametrize('mode',[modes.VASYA,modes.REFRAME])
def test_new_formats_keep_project_cast_in_graphic_scenes(monkeypatch,mode):
    captured=[]
    def fake(prompt,**kwargs):
        captured.append(prompt)
        return json.dumps({'scenes':[{'visual':'Два ряда задач: полный и сокращённый.',
                                      'time_period':'present','characters':[]}]},ensure_ascii=False)
    monkeypatch.setattr(story_generation,'gemini_generate_sync',fake)
    result=story_generation.generate_story_scene_plan(source_story='Я устал',voiceover_text='Я устал.',
        duration_sec=3,alignment={},project_slug='discipline',discipline_mode=mode,
        project_characters=[{'id':'anastasia','display_name':'Анастасия','description':'героиня','is_primary':True},
                            {'id':'vasily','display_name':'Василий','description':'герой','is_primary':True},
                            {'id':'knopka','display_name':'Кнопка','description':'персонаж'}])
    assert result['json_plan'][0]['characters']==['vasily','knopka']
    assert 'productivity books' not in result['json_plan'][0]['action']
    assert 'доводят начатые дела до конца' not in result['json_plan'][0]['action']
    assert 'предметные кадры без героев запрещены' not in captured[0]
    assert 'Предметные и графические кадры допустимы' in captured[0]


@pytest.mark.parametrize('mode',[modes.VASYA,modes.REFRAME])
@pytest.mark.parametrize('language',['ru','en'])
def test_new_modes_use_selected_male_voice_for_each_language(monkeypatch,mode,language):
    from app.handlers import handlers_state_scene as scene
    from app import project_profiles
    captured=[]
    def generate(text,**kwargs):
        captured.append(kwargs)
        return story_generation.StoryAudioResult(audio_bytes=b'audio',content_type='audio/mpeg',duration_sec=2,alignment={})
    monkeypatch.setattr(scene,'generate_story_audio',generate)
    monkeypatch.setattr(scene,'store_story_audio',lambda *args,**kwargs:'/tmp/test-audio.mp3')
    monkeypatch.setattr(scene,'SESSIONS',{})
    monkeypatch.setattr(project_profiles,'resolve_project_voice_id',lambda *args:'old-narrator')
    session={'selected_project_key':'discipline',modes.MODE_KEY:mode,
        scene.STORY_VOICEOVER_SESSION_KEY:'Я устал.',scene.STORY_VOICEOVER_EN_SESSION_KEY:'I am tired.'}
    message=SimpleNamespace(reply_text=AsyncMock(),reply_audio=AsyncMock())
    fn=scene._generate_and_send_story_audio if language=='ru' else scene._generate_and_send_story_english_audio
    asyncio.run(fn(SimpleNamespace(message=message),None,chat_id=1,session=session))
    assert captured[0]['voice_id']==modes.MALE_VOICE_ID


@pytest.mark.parametrize('rebuild',['draft','production'])
def test_restoring_before_production_keeps_approved_fonts_and_timing(monkeypatch,rebuild):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db, drafts, scene_media_store
    from app.models import GenerationRun,Scene
    engine=create_engine('sqlite+pysqlite:///:memory:',future=True)
    db.create_tables(engine=engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'get_engine',lambda:engine)
    monkeypatch.setattr(db,'get_session_factory',lambda:factory)
    with db.SessionLocal() as session:
        run=GenerationRun(status='ready',language='ru',script='У меня нет энергии.',title_color='#fff7e8')
        session.add(run);session.flush()
        session.add(Scene(run_id=run.id,scene_order=1,text='Сцена',duration_sec=3));session.commit()
        run_id=str(run.id)
    original={'editorial_style_id':'9','word_timing_by_lang':{'ru':[{'text':'У','start':0,'end':.1}]}}
    snapshot={'overlay_title_style':original.copy(),'production':{'json_plan':[{'scene':1,'text':'Сцена'}]}}
    if rebuild=='draft':
        drafts._session_from_generation_run(snapshot,run_id)
    else:
        scene_media_store.rebuild_production_session(snapshot,run_id)
    assert snapshot['overlay_title_style']['editorial_style_id']=='9'
    assert snapshot['overlay_title_style']['word_timing_by_lang']==original['word_timing_by_lang']


def test_narrative_cast_replaces_aliases_and_attaches_real_reference_files(tmp_path):
    from app.handlers import handlers_state_scene as handler
    from app.handlers.handlers_media_scene import _loaded_reference_paths
    hero=tmp_path/'hero.jpg'; hero.write_bytes(b'hero')
    pet=tmp_path/'pet.jpg'; pet.write_bytes(b'pet')
    bible=[{'id':'character_real_id','display_name':'Василий','description':'approved hero'},
           {'id':'discipline_knopka','display_name':'Кнопка','description':'approved pet'}]
    scenes=[{'generation_mode':'story','characters':['vasiliy'] if i%2 else []} for i in range(20)]
    session={'selected_project_key':'discipline',modes.MODE_KEY:modes.REFRAME,
             handler.STORY_CHARACTER_BIBLE_SESSION_KEY:bible,'scenes_plan':scenes,
             handler.STORY_CHARACTER_REFERENCES_SESSION_KEY:{
                 'character_real_id':{'approved':True,'selected_path':str(hero)},
                 'discipline_knopka':{'approved':True,'selected_path':str(pet)}}}
    handler._attach_story_character_references(session)
    assert all('character_real_id' in s['characters'] and 'vasiliy' not in s['characters'] for s in scenes)
    assert sum('discipline_knopka' in s['characters'] for s in scenes)>=12
    assert all(str(hero) in _loaded_reference_paths(s) for s in scenes)
    assert sum(str(pet) in _loaded_reference_paths(s) for s in scenes)>=12
    before=[list(s['characters']) for s in scenes]
    handler._attach_story_character_references(session)
    assert before==[s['characters'] for s in scenes]


def test_narrative_cast_requires_approved_reference(tmp_path):
    from app.handlers import handlers_state_scene as handler
    session={'selected_project_key':'discipline',modes.MODE_KEY:modes.REFRAME,
             handler.STORY_CHARACTER_BIBLE_SESSION_KEY:[
                 {'id':'v','display_name':'Василий'}, {'id':'k','display_name':'Кнопка'}],
             'scenes_plan':[{'characters':[]}],handler.STORY_CHARACTER_REFERENCES_SESSION_KEY:{}}
    with pytest.raises(ValueError,match='референс'):
        handler._attach_story_character_references(session)
