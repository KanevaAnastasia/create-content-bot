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


@pytest.mark.parametrize('mode,label', modes.LABELS.items())
def test_mode_selection_survives_resume_and_disables_dialogue(monkeypatch,label,mode):
    monkeypatch.setattr(start,'SESSIONS',{})
    session={'selected_project_key':'discipline','series_pending_recipe':'Не успеваю читать'}
    session[start.SERIES_PENDING_RECIPE_SESSION_KEY]='Не успеваю читать'
    message=SimpleNamespace(reply_text=AsyncMock())
    launch = AsyncMock(return_value=True)
    monkeypatch.setattr(start, '_start_initial_story_voiceover', launch)
    asyncio.run(start._handle_waiting_generation_mode_state(SimpleNamespace(message=message),None,chat_id=1,session=session,text=label))
    assert session[modes.MODE_KEY]==mode
    assert session['generation_mode']=='story'
    assert session['discipline_intro_enabled'] is False
    if mode == modes.BOSS:
        assert session[start.STORY_ENGLISH_REQUESTED_SESSION_KEY] is True
        assert session['state'] == story_generation.STATE_WAITING_INNER_BOSS_IDEA
        launch.assert_not_awaited()
        from app.handlers import inner_boss_workflow
        asyncio.run(start._handle_waiting_inner_boss_idea_state(
            SimpleNamespace(message=message), None, chat_id=1, session=session,
            text="Сцена 1. Тест\nГерой открывает тест после недели подготовки.",
        ))
        assert session['state'] == inner_boss_workflow.STATE
        assert session[inner_boss_workflow.KEY]['scenes'][0]['visual'] == "Герой открывает тест после недели подготовки."
        assert start.SERIES_PENDING_RECIPE_SESSION_KEY not in session
        launch.assert_not_awaited()
    else:
        assert session['state'] == start.STATE_WAITING_STORY_ENGLISH_CHOICE
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


@pytest.mark.parametrize('mode', [m for m in modes.SHORT_FORMATS if m != modes.ARCHIVE])
def test_short_format_selects_its_illustration_and_script(monkeypatch, mode):
    session = {'selected_project_key': 'discipline', modes.MODE_KEY: mode}
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(start, '_start_initial_story_voiceover', run)
    msg = SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(start._handle_waiting_story_english_choice_state(
        SimpleNamespace(message=msg), None, chat_id=1, session=session, text='Нет'))
    assert session[start.STORY_VISUAL_STYLE_SESSION_KEY] == modes.SHORT_FORMATS[mode].style_key
    run.assert_awaited_once()
    prompts = []
    def writer(prompt, **kwargs):
        prompts.append(prompt)
        return '{"title":"Книга","voiceover_text":"Вася открыл книгу."}'
    monkeypatch.setattr(story_generation, 'gemini_generate_sync', writer)
    story_generation.generate_story_voiceover('Книга', discipline_mode=mode)
    expected_duration = {modes.BOSS: '90 секунд', modes.LITERARY: 'около минуты'}
    assert expected_duration.get(mode, '18–28') in prompts[0]
    assert '45 до 110' not in prompts[0]
    assert modes.SHORT_FORMATS[mode].script in prompts[0]
    assert modes.LABELS[mode] not in start.project_mode_labels('grandma_kitchen')


def test_continuous_plan_preserves_timeline_camera_cast_and_mode_on_restore(monkeypatch):
    from app.generation_store import merge_scene_prompt_payload, SCENE_META_SNAPSHOT_KEYS
    cast = [
        {'id': 'vasily', 'display_name': 'Василий', 'description': 'герой'},
        {'id': 'knopka', 'display_name': 'Кнопка', 'description': 'спутница'},
    ]
    def generator(prompt, **kwargs):
        return json.dumps({'characters': [{'id':'guest', 'description':'foreign character'}],
                           'scenes': [{'characters':['guest'], 'visual':'Вася тянется к книге.'}]})
    monkeypatch.setattr(story_generation, 'gemini_generate_sync', generator)
    args = dict(source_story='Книга', voiceover_text='Вася взял книгу.', duration_sec=20.3,
                alignment={}, project_characters=cast, project_slug='discipline', discipline_mode=modes.FLOW)
    plan = story_generation.generate_story_scene_plan(**args)['json_plan']
    assert len(plan) == 5
    assert all(s['duration_sec'] == 4 and s['camera_direction'] == 'locked-off camera' for s in plan)
    assert all(s['characters'] == ['vasily','knopka'] for s in plan)
    assert sum(s['timeline_duration_sec'] for s in plan) == pytest.approx(20.3)
    produced = story_generation.generate_story_production(scenes_plan=plan)['json_plan']
    assert all(s['video_prompt'].startswith('4-second') and 'locked-off camera' in s['video_prompt'] for s in produced)
    assert modes.MODE_KEY in SCENE_META_SNAPSHOT_KEYS
    scene = {}
    merge_scene_prompt_payload(scene, {'prompt_meta_json': {modes.MODE_KEY: produced[1][modes.MODE_KEY]}})
    assert scene[modes.MODE_KEY] == modes.FLOW
    plan = story_generation.generate_story_scene_plan(**{**args, 'project_slug':'another'})['json_plan']
    assert all(modes.MODE_KEY not in s and s['duration_sec'] == 3 for s in plan)


@pytest.mark.parametrize("mode", modes.SHORT_FORMATS)
def test_short_format_reaches_both_media_prompts_with_minimal_model_response(monkeypatch, mode):
    from app.handlers.handlers_media_core import _image_scene_prompt

    fmt = modes.SHORT_FORMATS[mode]
    minimal = {"start_frame_prompt_ru": "Вася и Кнопка держат книгу на столе.",
               "video_prompt_en": "4-second Kling 3.0 image-to-video shot. No audio. Slowly open the book."}
    monkeypatch.setattr(story_generation, "gemini_generate_sync", lambda *a, **kw: json.dumps({"prompts": [minimal]}))
    scene = {modes.MODE_KEY: mode, "duration_sec": 4, "story_visual_style_key": fmt.style_key, "action": "Slowly open the book"}
    produced = story_generation.generate_story_production(scenes_plan=[scene])["json_plan"][0]
    assert fmt.frame in _image_scene_prompt(produced)
    assert fmt.motion in produced["video_prompt_by_lang"]["ru"]
    assert "Slowly open the book" in produced["video_prompt"]


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


def test_timed_headline_reveals_words_in_fixed_geometry(title_event_ink):
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


@pytest.mark.parametrize('mode', [m for m in modes.SHORT_FORMATS if m not in modes.STORY_CAST_MODES])
def test_short_plans_preserve_four_second_clips_and_no_forced_camera(monkeypatch,mode):
    from app.discipline_craft import MOTION_RULES
    captured=[]
    def generator(prompt,**kwargs):
        captured.append(prompt)
        return json.dumps({'scenes':[{'visual':'Вася закрывает книгу.','camera_direction':'orbit shot','location_transition':True,'transition_note':'После чтения Вася идёт в зал по сценарию.'}]})
    monkeypatch.setattr(story_generation,'gemini_generate_sync',generator)
    plan=story_generation.generate_story_scene_plan(source_story='Закрыть книгу, вспомнить и сверить.',
        voiceover_text='Вася закрывает книгу. Потом вспоминает и сверяет.',duration_sec=12.4,
        alignment={},project_slug='discipline',discipline_mode=mode,
        project_characters=[{'id':'vasily','description':'Вася'},{'id':'knopka','description':'Кнопка'}])['json_plan']
    if mode == modes.BOSS:
        assert all(0 <= row['duration_sec'] - row['timeline_duration_sec'] < 1.01 for row in plan)
    else:
        assert all(row['duration_sec'] >= 4 for row in plan)
    assert all(row['camera_direction']=='locked-off camera' for row in plan)
    assert sum(row['timeline_duration_sec'] for row in plan)==pytest.approx(12.4)
    if mode != modes.BOSS:
        assert 'Не повторяй одно и то же движение камеры' not in captured[0]
    result=story_generation.generate_story_production(scenes_plan=plan)['json_plan']
    assert all(MOTION_RULES in row['video_prompt'] for row in result)
    if mode==modes.LEARNING:
        assert 'подсказ' in modes.script_direction(mode)
        assert 'источник' in modes.script_direction(mode)
    if mode==modes.TWO_LIVES:
        assert 'Время сверху и снизу может расходиться' in captured[0]
        assert plan[0]['location_transition'] is True
        assert 'по явно заданным действиям сценария' in captured[0]


def test_inner_boss_pilot_preserves_two_speakers_and_project_menu(monkeypatch):
    from app import discipline_modes as modes, project_modes, story_generation

    def unexpected_generation(*args, **kwargs):
        raise AssertionError("The default pilot should use the reviewed script.")

    monkeypatch.setattr(story_generation, "gemini_generate_sync", unexpected_generation)
    result = story_generation.generate_story_voiceover(modes.BOSS_PILOT, discipline_mode=modes.BOSS)
    turns = modes.boss_dialogue_turns(result["voiceover_text"], "vasya-voice", "boss-voice")
    assert turns[0] == {"text": "Спи. Пять минут ничего не изменят.", "voice_id": "boss-voice"}
    assert turns[-1] == {"text": "Я увижу. В первый день я вышел, потому что загорелся. Сегодня я вообще не хотел вставать. Мотивация помогает начать. Дисциплина помогает продолжать.", "voice_id": "vasya-voice"}
    assert modes.mode_from_label("Внутренний босс") == modes.BOSS
    assert "Внутренний босс" in project_modes.project_mode_labels("discipline")
    assert "Внутренний босс" not in project_modes.project_mode_labels("grandma_kitchen")


def test_inner_boss_english_turns_and_cast_preserve_identity():
    from app import discipline_modes as modes, story_generation

    turns = modes.boss_dialogue_turns("Boss: Nobody noticed. Vasya: I did.", "hero", "shadow")
    assert turns == [{"text": "Nobody noticed.", "voice_id": "shadow"},
                     {"text": "I did.", "voice_id": "hero"}]
    scenes = [{modes.MODE_KEY: modes.BOSS, "characters": ["shadow", "discipline_knopka"]}]
    hero = {"id": "existing_id", "display_name": "Василий", "description": "Approved character"}
    story_generation.enforce_discipline_narrative_cast(scenes, [hero])
    assert scenes[0]["characters"] == ["existing_id"]


def test_alarm_is_pilot_only_unless_episode_explicitly_requests_it():
    assert modes.boss_opening_has_alarm(modes.BOSS_PILOT)
    assert modes.boss_opening_has_alarm('Вася встал. [Звук в начале: будильник]')
    assert not modes.boss_opening_has_alarm('Вася боится открыть тест.')


def test_workspace_boss_menu_opens_idea_step_without_an_extra_prompt(monkeypatch):
    from app import guest_handlers

    saved = {}
    monkeypatch.setattr(start, 'SESSIONS', saved)
    monkeypatch.setattr(guest_handlers, 'SESSIONS', saved)
    writer = AsyncMock(return_value=True)
    monkeypatch.setattr(start, '_start_initial_story_voiceover', writer)
    session = {'selected_project_key': 'discipline', 'project_workspace_mode': True}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(guest_handlers._start_workspace_format(update, None, 1, session, modes.LABELS[modes.BOSS]))
    assert session['state'] == story_generation.STATE_WAITING_INNER_BOSS_IDEA
    writer.assert_not_awaited()
    from app.handlers import inner_boss_workflow
    draft = "Сцена 1. Курс\nГерой взялся за курс и бросает после ошибки."
    monkeypatch.setattr(inner_boss_workflow, 'draft_script', lambda idea: draft)
    asyncio.run(start._handle_waiting_inner_boss_idea_state(
        update, None, chat_id=1, session=session, text='Герой взялся за курс и бросает после ошибки.',
    ))
    writer.assert_not_awaited()
    assert session['state'] == inner_boss_workflow.STATE
    assert session[start.STORY_SOURCE_SESSION_KEY] == draft


def test_literary_story_keeps_cast_when_one_interval_spans_two_beats(monkeypatch):
    beats = [
        {"beat": 1, "start_sec": 0, "end_sec": 2.8, "narration_excerpt": "Алан вошёл."},
        {"beat": 2, "start_sec": 2.8, "end_sec": 9.6, "narration_excerpt": "Продавщица протянула хлеб."},
        {"beat": 3, "start_sec": 9.6, "end_sec": 13.6, "narration_excerpt": "Алан вышел."},
    ]
    monkeypatch.setattr(story_generation, "build_semantic_narration_beats", lambda *_: beats)
    cast = [
        {"id": "alan", "display_name": "Алан", "description": "Зелёное пальто"},
        {"id": "baker", "display_name": "Продавщица", "description": "Охристый фартук"},
    ]
    monkeypatch.setattr(story_generation, "gemini_generate_sync", lambda *_args, **_kwargs: json.dumps({
        "characters": cast,
        "beats": [
            {"beat": 1, "clip_visuals": [{"characters": ["alan"], "visual": "Алан входит."}]},
            {"beat": 2, "clip_visuals": [{"characters": ["alan", "baker"], "visual": "Продавщица подаёт багет."}]},
            {"beat": 3, "clip_visuals": [{"characters": ["alan"], "visual": "Алан выходит."}]},
        ],
    }))
    plan = story_generation.generate_story_scene_plan(
        source_story="Алан в булочной.", voiceover_text="Алан в булочной.", duration_sec=13.6,
        alignment={}, project_slug="discipline", discipline_mode=modes.LITERARY,
    )["json_plan"]
    assert [(s["semantic_beat"], s["semantic_clip_index"], s["semantic_clip_count"]) for s in plan] == [
        (1, 1, 1), (2, 1, 1), (3, 1, 1),
    ]
    assert plan[1]["characters"] == ["alan", "baker"]
    assert plan[1]["action"] == "Продавщица подаёт багет."


def test_literary_story_reuses_only_approved_recurring_book_characters(monkeypatch, tmp_path):
    from app.handlers import handlers_state_scene as scene_handler

    book_cast = [
        {"id": "alan_greenmor", "display_name": "Алан Гринмор", "description": "Тёмные волосы, синий пиджак"},
        {"id": "baker", "display_name": "Булочница", "description": "Белый фартук"},
    ]

    def fake_planner(prompt, **kwargs):
        if '"scenes_plan"' in prompt:
            return '{"prompts": []}'
        return json.dumps({
            "characters": book_cast,
            "scenes": [
                {"characters": ["alan_greenmor", "baker"], "visual": "Алан тянется за багетом.",
                 "camera_direction": "locked-off camera"},
                {"characters": ["alan_greenmor"], "visual": "Алан замечает чёрствый хлеб.",
                 "camera_direction": "slow optical zoom-in"},
            ],
        })

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_planner)
    result = story_generation.generate_story_scene_plan(
        source_story="Алан Гринмор соглашается взять чёрствый багет.",
        voiceover_text="Алан берёт багет.",
        duration_sec=11.0,
        alignment={},
        project_slug="discipline",
        discipline_mode=modes.LITERARY,
        project_characters=[{"id": "vasily", "display_name": "Василий", "description": "Персонаж проекта"}],
    )
    scenes = result["json_plan"]
    assert [scene["characters"] for scene in scenes] == [["alan_greenmor", "baker"], ["alan_greenmor"]]
    assert [scene["duration_sec"] for scene in scenes] == [6.0, 6.0]
    assert sum(scene["timeline_duration_sec"] for scene in scenes) == pytest.approx(11)
    assert scenes[0]["camera_direction"] == "locked-off camera"
    assert scenes[1]["camera_direction"] != scenes[0]["camera_direction"]
    assert all(scene["story_visual_style_key"] == "literary_illustration" for scene in scenes)
    bible = result["character_bible"]
    assert [(character["id"], character["requires_reference"]) for character in bible] == [
        ("alan_greenmor", True), ("baker", False),
    ]

    session = {
        "selected_project_key": "discipline",
        modes.MODE_KEY: modes.LITERARY,
        "scenes_plan": scenes,
        "story_visual_style": "literary_illustration",
        scene_handler.STORY_CHARACTER_BIBLE_SESSION_KEY: bible,
    }
    with pytest.raises(ValueError, match="Сначала утвердите референс: Алан"):
        scene_handler._attach_story_character_references(session)

    reference = tmp_path / "alan.png"
    reference.write_bytes(b"approved character image")
    session[scene_handler.STORY_CHARACTER_REFERENCES_SESSION_KEY] = {
        "alan_greenmor": {
            "selected_path": str(reference),
            "approved": True,
            "description": bible[0]["description"],
            "reference_prompt": bible[0]["reference_prompt"],
            "story_visual_style_key": "literary_illustration",
        },
    }
    monkeypatch.setattr("app.story_continuity.save_continuity_manifest", lambda session: None)
    scene_handler._attach_story_character_references(session)
    assert all(scene["references"] == {"alan_greenmor": str(reference)} for scene in scenes)

    produced = story_generation.generate_story_production(scenes_plan=scenes)["json_plan"]
    assert all(scene["video_prompt"].startswith("6-second Kling") for scene in produced)
    assert all("hand-painted 2D book illustration" in scene["video_prompt"] for scene in produced)
