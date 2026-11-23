import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import story_generation as g
from app.handlers import handlers_state_scene as h
from app.handlers import handlers_media_scene as media


def planned(monkeypatch, tmp_path):
    actor=tmp_path/'actor.png';actor.write_bytes(b'identity')
    monkeypatch.setattr(g,'build_semantic_narration_beats',lambda *a:[{'beat':i+1,'start_sec':i*4,'end_sec':(i+1)*4,'narration_excerpt':'Пишет.'} for i in range(3)])
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'characters':[{'id':'stephen_king','display_name':'Стивен Кинг','description':'Молодой Стивен Кинг'}], 'props':[{'id':'prop_typewriter','description':'чёрная машинка','is_key_story_prop':True},{'id':'prop_letter','description':'письмо'}], 'scenes':[{'characters':['stephen_king'],'props':['prop_typewriter']},{'characters':['stephen_king'],'props':['prop_typewriter']},{'characters':[],'characters_absent_reason':'Крупно письмо без людей','props':['prop_letter']}]}))
    return g.generate_story_scene_plan(source_story='Кинг пишет.',voiceover_text='Кинг пишет.',duration_sec=12,alignment={},project_slug='discipline',discipline_mode='lives_archive',story_visual_style='history_yarn_puppet',project_characters=[{'id':'vasily','description':'Василий','asset_uri':actor.as_uri(),'is_primary':True}])


def test_real_person_requires_new_approval_and_only_recurring_props(monkeypatch,tmp_path):
    result=planned(monkeypatch,tmp_path)
    bible={v['id']:v for v in result['character_bible']}
    assert bible['stephen_king']['requires_reference'] is True
    assert bible['prop_typewriter']['requires_reference'] is True
    assert bible['prop_letter']['requires_reference'] is False
    assert result['json_plan'][2]['characters']==[]


def test_approved_prop_reaches_matching_scene_image_input_and_manifest(monkeypatch,tmp_path):
    result=planned(monkeypatch,tmp_path)
    s={'generation_mode':'story','selected_project_key':'discipline','discipline_narrative_mode':'lives_archive','story_visual_style':'history_yarn_puppet','scenes_plan':result['json_plan'],'story_character_bible':result['character_bible'],'story_character_references':{}}
    for c in result['character_bible']:
        if not c['requires_reference']:continue
        path=tmp_path/(c['id']+'.png');path.write_bytes(c['id'].encode())
        s['story_character_references'][c['id']]={'approved':True,'selected_path':str(path),'description':c['description'],'reference_prompt':c['reference_prompt'],'story_visual_style_key':s['story_visual_style']}
    h._attach_story_character_references(s)
    monkeypatch.setattr('app.story_image_postprocess.crop_story_reference_border',lambda b:b)
    assert media._load_scene_reference_images(s['scenes_plan'][0])==[b'stephen_king',b'prop_typewriter']
    assert media._load_scene_reference_images(s['scenes_plan'][2])==[]
    manifest=json.loads(Path(s['scenes_plan'][0]['continuity_manifest_path']).read_text())
    assert {a['id'] for a in manifest['assets']}=={'stephen_king','prop_typewriter'}
    s['story_visual_style']='history_comic'
    with pytest.raises(ValueError,match='утвердите'):h._attach_story_character_references(s)


def test_prop_prompt_never_requests_portrait():
    prompt=h._story_character_reference_prompt({'entity_type':'prop','display_name':'машинка','description':'чёрная'},story_visual_style='history_yarn_puppet')
    assert 'портрет' not in prompt
    assert 'Без людей' in prompt


def test_project_identity_is_attached_to_new_style_reference(monkeypatch,tmp_path):
    actor=tmp_path/'source.png';actor.write_bytes(b'actor-identity')
    c={'id':'vasily','description':'Василий','requires_reference':True,'asset_uri':actor.as_uri()}
    s={'story_character_bible':[c],'story_visual_style':'history_yarn_puppet'}
    captured={}
    def image(prompt,**kw):captured.update(prompt=prompt,**kw);return b'new-image'
    monkeypatch.setattr(h,'generate_image_vertex',image)
    monkeypatch.setattr(h,'SESSIONS',{})
    monkeypatch.setattr(h,'safe_reply_photo',AsyncMock())
    monkeypatch.setattr(h,'_store_story_character_image',lambda *a:str(tmp_path/'saved.png'))
    message=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(h._generate_story_character_reference(SimpleNamespace(message=message),chat_id=123,session=s,index=0))
    assert captured['reference_images']==[b'actor-identity']
    assert 'сохрани узнаваемые индивидуальные черты' in captured['prompt']
    assert not s['story_character_references']['vasily']['approved']


@pytest.mark.parametrize('entry',['_continue_after_scene_approval','_generate_production_after_overlay_title'])
def test_missing_reference_blocks_production(monkeypatch,entry):
    begin=AsyncMock(return_value=True)
    monkeypatch.setattr(h,'_begin_story_character_approval',begin)
    s={'generation_mode':'story','story_character_bible':[{'id':'vasily','description':'герой','requires_reference':True}]}
    asyncio.run(getattr(h,entry)(SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())),None,chat_id=1,session=s))
    begin.assert_awaited_once()


def test_history_cannot_silently_drop_cast(monkeypatch):
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'scenes':[{'visual':'Кинг печатает, Табита читает','characters':[]}]}))
    with pytest.raises(ValueError,match="исполнители"):
        g.generate_story_scene_plan(source_story='Кинг пишет.',voiceover_text='Кинг пишет.',duration_sec=5,alignment={},project_slug='discipline',discipline_mode='lives_archive',project_characters=[{'id':'vasily','description':'Василий'}])


def test_missing_approved_prop_blocks_image_input(tmp_path):
    scene={'generation_mode':'story','story_prop_ids':['prop_machine'],'story_required_reference_ids':['prop_machine'],'references':{'prop_machine':str(tmp_path/'lost.png')}}
    with pytest.raises(ValueError,match="недоступен"):
        media._load_scene_reference_images(scene)

def test_missing_project_identity_does_not_call_image_provider(monkeypatch,tmp_path):
    generator=AsyncMock()
    monkeypatch.setattr(h,'generate_image_vertex',generator)
    c={'id':'vasily','description':'герой','requires_reference':True,'asset_uri':(tmp_path/'missing.png').as_uri()}
    message=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(h._generate_story_character_reference(SimpleNamespace(message=message),chat_id=1,session={'story_character_bible':[c]},index=0))
    generator.assert_not_called()


def test_reference_requirements_survive_scene_snapshot(tmp_path,monkeypatch):
    from app.generation_store import _scene_prompt_payload,merge_scene_prompt_payload
    actor=tmp_path/'actor.png';actor.write_bytes(b'actor')
    prop=tmp_path/'prop.png';prop.write_bytes(b'prop')
    scene={'generation_mode':'story','characters':['vasily'],'story_prop_ids':['prop_machine'],
           'story_required_reference_ids':['vasily','prop_machine'],
           'story_prop_descriptions':{'prop_machine':'чёрная машинка'},
           'references':{'vasily':str(actor),'prop_machine':str(prop)},'continuity_manifest_path':'manifest.json'}
    restored={};merge_scene_prompt_payload(restored,_scene_prompt_payload(scene))
    monkeypatch.setattr('app.story_image_postprocess.crop_story_reference_border',lambda b:b)
    assert media._load_scene_reference_images(restored)==[b'actor',b'prop']
    assert restored['continuity_manifest_path']=='manifest.json'
    prop.unlink()
    with pytest.raises(ValueError,match='недоступен'):
        media._load_scene_reference_images(restored)


def test_empty_identity_never_calls_provider(monkeypatch,tmp_path):
    path=tmp_path/'empty.png';path.write_bytes(b'')
    image=AsyncMock();monkeypatch.setattr(h,'generate_image_vertex',image)
    msg=SimpleNamespace(reply_text=AsyncMock())
    c={'id':'vasily','display_name':'Василий','requires_reference':True,'asset_uri':path.as_uri()}
    asyncio.run(h._generate_story_character_reference(SimpleNamespace(message=msg),chat_id=1,session={'story_character_bible':[c]},index=0))
    image.assert_not_called()
    assert 'Василий' in msg.reply_text.call_args.args[0]

def test_king_cannot_be_all_explained_object_inserts(monkeypatch):
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'scenes':[{'characters':[],'characters_absent_reason':'Крупно листы'}]}))
    with pytest.raises(ValueError,match='реальные люди'):
        g.generate_story_scene_plan(source_story='Кинг пишет, Табита читает.',voiceover_text='Кинг пишет, Табита читает.',duration_sec=5,alignment={},project_slug='discipline',discipline_mode='lives_archive',project_characters=[{'id':'vasily','description':'Василий'},{'id':'anastasia','display_name':'Анастасия','description':'Анастасия'}])

def test_repeated_mug_stays_description_only(monkeypatch,tmp_path):
    original=g.gemini_generate_sync
    monkeypatch.setattr(g,'build_semantic_narration_beats',lambda *a:[{'beat':i+1,'start_sec':i*4,'end_sec':(i+1)*4,'narration_excerpt':'Пишет.'} for i in range(2)])
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'characters':[{'id':'stephen_king','display_name':'Стивен Кинг','description':'Молодой Кинг'}], 'props':[{'id':'prop_mug','display_name':'Кружка','description':'белая','is_key_story_prop':True}], 'scenes':[{'characters':['stephen_king'],'props':['prop_mug']}]*2}))
    result=g.generate_story_scene_plan(source_story='Пишет.',voiceover_text='Пишет.',duration_sec=8,alignment={},project_slug='discipline',discipline_mode='lives_archive',project_characters=[{'id':'vasily','description':'Василий'}])
    mug=next(c for c in result['character_bible'] if c['id']=='prop_mug')
    assert mug['requires_reference'] is False


def test_other_projects_do_not_gain_paid_prop_approval(monkeypatch,tmp_path):
    planned(monkeypatch,tmp_path)
    result=g.generate_story_scene_plan(source_story='Пишет.',voiceover_text='Пишет.',duration_sec=12,alignment={},project_slug='other_project',project_characters=[{'id':'vasily','description':'герой'}])
    assert all(not c['requires_reference'] for c in result['character_bible'] if c.get('entity_type')=='prop')


def test_rejection_waits_for_feedback_without_generation(monkeypatch,tmp_path):
    monkeypatch.setattr(h,'SESSIONS',{})
    image=AsyncMock();monkeypatch.setattr(h,'_generate_story_character_reference',image)
    continuation=AsyncMock();monkeypatch.setattr(h,'_continue_after_scene_approval',continuation)
    c={'id':'prop_machine','entity_type':'prop','requires_reference':True}
    session={'story_character_bible':[c],'story_character_references':{'prop_machine':{'approved':False}}}
    msg=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(h._handle_waiting_story_character_approval_state(SimpleNamespace(message=msg),None,chat_id=1,session=session,text='Не подходит'))
    assert session['story_character_references']['prop_machine']['approved'] is False
    image.assert_not_awaited();continuation.assert_not_awaited()
    assert 'правку' in msg.reply_text.call_args.args[0]


@pytest.mark.parametrize('kind',['character','prop'])
@pytest.mark.parametrize('remove_file',[False,True])
def test_rejected_version_cannot_be_approved_after_session_roundtrip(monkeypatch,tmp_path,kind,remove_file):
    monkeypatch.setattr(h,'SESSIONS',{})
    image=AsyncMock();monkeypatch.setattr(h,'_generate_story_character_reference',image)
    continuation=AsyncMock();monkeypatch.setattr(h,'_continue_after_scene_approval',continuation)
    path=tmp_path/'rejected.png';path.write_bytes(b'preview')
    c={'id':'item','entity_type':kind,'requires_reference':True}
    s={'story_character_bible':[c],'story_character_references':{'item':{'approved':False,'selected_path':str(path)}}}
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(h._handle_waiting_story_character_approval_state(update,None,chat_id=1,session=s,text='Не подходит'))
    restored=json.loads(json.dumps(s))
    restored['state']=h.STATE_WAITING_STORY_CHARACTER_APPROVAL
    if remove_file:path.unlink()
    buttons=[b.text for row in h.keyboard_for_session_state(restored).keyboard for b in row]
    assert 'ОК' not in buttons
    asyncio.run(h._handle_waiting_story_character_approval_state(update,None,chat_id=1,session=restored,text='ОК'))
    assert restored['story_character_references']['item']['approved'] is False
    assert str(path) in restored['story_character_references']['item']['rejected_paths']
    image.assert_not_awaited();continuation.assert_not_awaited()


def test_archive_ignores_project_faces_and_keeps_real_identity(monkeypatch,tmp_path):
    result=planned(monkeypatch,tmp_path)
    bible={c['id']:c for c in result['character_bible']}
    assert 'vasily' not in bible
    assert bible['stephen_king']['display_name']=='Стивен Кинг'
    assert bible['stephen_king']['asset_uri']==''
    assert all('vasily' not in c['characters'] for c in result['json_plan'])
    assert 'project characters as actors' not in result['json_plan'][0]['era_style']


def test_stale_archive_project_cast_never_generates_reference(monkeypatch):
    image=AsyncMock();monkeypatch.setattr(h,'generate_image_vertex',image)
    session={'discipline_narrative_mode':'lives_archive','story_character_bible':[
        {'id':'vasily','display_name':'Василий','requires_reference':True}]}
    with pytest.raises(ValueError,match='реальные люди'):
        asyncio.run(h._generate_story_character_reference(SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())),chat_id=1,session=session,index=0))
    image.assert_not_called()


def test_history_real_photo_is_input_but_never_auto_approved(monkeypatch,tmp_path):
    photo=tmp_path/'king.jpg';photo.write_bytes(b'real-king-photo')
    c={'id':'stephen_king','display_name':'Стивен Кинг','description':'Stephen King, age 26, 1973',
       'requires_reference':True,'asset_uri':photo.as_uri()}
    session={'discipline_narrative_mode':'lives_archive','story_visual_style':'history_yarn_puppet','story_character_bible':[c], 'story_voiceover_text':'Стивен Кинг пишет.', 'scenes_plan':[{'characters':['stephen_king']}]}
    calls=[]
    monkeypatch.setattr(h,'generate_image_vertex',lambda prompt,**kw:calls.append((prompt,kw)) or b'stylized')
    monkeypatch.setattr(h,'SESSIONS',{})
    monkeypatch.setattr(h,'safe_reply_photo',AsyncMock())
    monkeypatch.setattr(h,'_store_story_character_image',lambda *a:str(tmp_path/'preview.png'))
    asyncio.run(h._generate_story_character_reference(SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())),chat_id=1,session=session,index=0))
    assert calls[0][1]['reference_images']==[b'real-king-photo']
    assert 'Стивен Кинг' in calls[0][0] and 'age 26' in calls[0][0]
    assert session['story_character_references']['stephen_king']['approved'] is False
    assert session['state']==h.STATE_WAITING_STORY_CHARACTER_APPROVAL


@pytest.mark.parametrize('name',[name for name,_,_ in __import__('app.historical_people',fromlist=['PEOPLE']).PEOPLE])
@pytest.mark.parametrize('wrong_cast',[[],[{'id':'writer','display_name':'Другой человек'}]])
def test_each_series_person_must_act_in_story(name,wrong_cast):
    from app.historical_people import validate_story_people
    with pytest.raises(ValueError,match='реальные люди'):
        validate_story_people(name,wrong_cast,[{'characters':[c['id'] for c in wrong_cast]}])


def test_real_people_validation_allows_object_inserts():
    from app.historical_people import validate_story_people
    validate_story_people('Октавия Батлер пишет роман.',[{'id':'octavia','display_name':'Октавия Батлер'}],
                          [{'characters':[]},{'characters':['octavia']}])


def test_tabitha_requires_correct_full_name():
    from app.historical_people import validate_story_people
    with pytest.raises(ValueError,match='Табита Кинг'):
        validate_story_people('Табита Кинг читает.',[{'id':'tabitha','display_name':'Табита Другая'}],[{'characters':['tabitha']}])


@pytest.mark.parametrize("legacy_style", ["", "пиксаровский стиль референса", "ПИКСАРОВСКИЙ СТИЛЬ РЕФЕРЕНСА"])
def test_selected_style_reaches_provider_without_global_project_look(monkeypatch,legacy_style):
    captured=[]
    def generate(prompt,**kwargs):
        captured.append(prompt)
        return SimpleNamespace(data=b'image')
    monkeypatch.setattr(media,'generate_image_vertex_result',generate)
    scene={'generation_mode':'story','story_visual_style_key':'history_yarn_puppet',
           'characters':['stephen_king'],'story_character_descriptions':{'stephen_king':'Стивен Кинг'}}
    prompt=media._append_scene_identity_to_image_prompt('King writes at a desk. '+legacy_style,scene)
    asyncio.run(media._generate_image_with_lenient_fallback_result(prompt))
    assert media.PIXAR_REFERENCE_STYLE_PHRASE.casefold() not in captured[0].casefold()
    assert 'Smooth matte painted clay/resin skin' in captured[0]
    assert 'oversized head' in captured[0]
    assert 'Soviet family home' not in captured[0]
    assert 'photorealistic details' not in media._soften_generation_prompt(captured[0])
    assert 'Preserve the original artistic style' in media._soften_generation_prompt(captured[0])


def test_default_project_style_is_preserved_without_selected_style(monkeypatch):
    captured=[]
    monkeypatch.setattr(media,'generate_image_vertex_result',lambda p,**kw:captured.append(p) or SimpleNamespace(data=b'image'))
    asyncio.run(media._generate_image_with_lenient_fallback_result('Бабушка режет яблоко.'))
    assert media.PIXAR_REFERENCE_STYLE_PHRASE in captured[0]


def test_puppet_prop_uses_miniature_object_style_not_face_instructions():
    p=h._story_character_reference_prompt({'entity_type':'prop','display_name':'машинка','description':'оливковый металл'},story_visual_style='history_yarn_puppet')
    assert 'simplified chunky shapes' in p
    assert 'not a photograph of a real manufactured object' in p
    assert 'oversized head' not in p
    assert 'Do not make every object knitted' in p


def test_approval_refreshes_replaced_preview_in_same_run(monkeypatch,tmp_path):
    import copy
    monkeypatch.setattr(h,'SESSIONS',{})
    old=tmp_path/'old.png';old.write_bytes(b'old')
    new=tmp_path/'new.png';new.write_bytes(b'new')
    c={'id':'prop_typewriter','entity_type':'prop','requires_reference':True}
    stale={'generation_run_id':'same-run','state':h.STATE_WAITING_STORY_CHARACTER_APPROVAL,
           'story_character_bible':[c],'story_character_approval_index':0,
           'story_character_references':{'prop_typewriter':{'selected_path':str(old),'rejected_paths':[str(old)],'approved':False}}}
    fresh=copy.deepcopy(stale)
    fresh['story_character_references']['prop_typewriter']['selected_path']=str(new)
    monkeypatch.setattr(h,'load_session_if_possible',lambda cid:fresh)
    monkeypatch.setattr(h,'_attach_story_character_references',lambda s:None)
    continuation=AsyncMock();monkeypatch.setattr(h,'_continue_after_scene_approval',continuation)
    msg=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(h._handle_waiting_story_character_approval_state(SimpleNamespace(message=msg),None,chat_id=1,session=stale,text='ОК'))
    assert stale['story_character_references']['prop_typewriter']['selected_path']==str(new)
    assert stale['story_character_references']['prop_typewriter']['approved'] is True
    assert str(old) in stale['story_character_references']['prop_typewriter']['rejected_paths']
    continuation.assert_awaited_once()


@pytest.mark.parametrize('change',['subject','run','state','missing'])
def test_stale_confirmation_never_approves_other_subject_or_run(monkeypatch,tmp_path,change):
    import copy
    monkeypatch.setattr(h,'SESSIONS',{})
    p=tmp_path/'ref.png';p.write_bytes(b'image')
    s={'generation_run_id':'run','state':h.STATE_WAITING_STORY_CHARACTER_APPROVAL,
       'story_character_bible':[{'id':'prop_a','requires_reference':True},{'id':'prop_b','requires_reference':True}],
       'story_character_approval_index':0,'story_character_references':{'prop_a':{'selected_path':str(p),'approved':False},'prop_b':{'selected_path':str(p),'approved':False}}}
    fresh=copy.deepcopy(s)
    if change=='subject':fresh['story_character_approval_index']=1
    if change=='run':fresh['generation_run_id']='other'
    if change=='state':fresh['state']='ready_for_production'
    monkeypatch.setattr(h,'load_session_if_possible',lambda cid:None if change=='missing' else fresh)
    gen=AsyncMock();monkeypatch.setattr(h,'_generate_story_character_reference',gen)
    msg=SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(h._handle_waiting_story_character_approval_state(SimpleNamespace(message=msg),None,chat_id=1,session=s,text='ОК'))
    assert not any(r.get('approved') for r in s['story_character_references'].values())
    gen.assert_not_awaited()


def test_double_ok_before_new_preview_does_not_approve_it(monkeypatch,tmp_path):
    monkeypatch.setattr(h,'SESSIONS',{})
    p=tmp_path/'ref.png';p.write_bytes(b'image')
    s={'state':h.STATE_WAITING_STORY_CHARACTER_APPROVAL,'story_character_bible':[{'id':'prop_a','requires_reference':True}],
       'story_character_references':{'prop_a':{'selected_path':str(p),'approved':False,'presented_message_id':200}}}
    gen=AsyncMock();monkeypatch.setattr(h,'_generate_story_character_reference',gen)
    msg=SimpleNamespace(reply_text=AsyncMock(),message_id=199)
    asyncio.run(h._handle_waiting_story_character_approval_state(SimpleNamespace(message=msg),None,chat_id=1,session=s,text='ОК'))
    assert s['story_character_references']['prop_a']['approved'] is False
    gen.assert_not_awaited()


@pytest.mark.parametrize('prior_message_id',[None,100])
def test_failed_preview_delivery_cannot_be_approved(monkeypatch,tmp_path,prior_message_id):
    monkeypatch.setattr(h,'SESSIONS',{})
    c={'id':'prop_a','entity_type':'prop','requires_reference':True}
    s={'story_character_bible':[c], 'story_character_references':{'prop_a':{'presented_message_id':prior_message_id}}}
    image=tmp_path/'preview.png'
    def store(*a):image.write_bytes(b'new');return str(image)
    generator=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value=b'new')
    monkeypatch.setattr(h,'generate_image_vertex',generator)
    monkeypatch.setattr(h,'_store_story_character_image',store)
    monkeypatch.setattr(h,'safe_reply_photo',AsyncMock(side_effect=RuntimeError('delivery failed')))
    msg=SimpleNamespace(reply_text=AsyncMock(),message_id=150)
    update=SimpleNamespace(message=msg)
    with pytest.raises(RuntimeError,match='delivery failed'):
        asyncio.run(h._generate_story_character_reference(update,chat_id=1,session=s,index=0))
    record=s['story_character_references']['prop_a']
    assert record['delivery_pending'] is True and record['presented_message_id'] is None
    monkeypatch.setattr(h,'safe_reply_photo',AsyncMock(return_value=SimpleNamespace(message_id=200)))
    continuation=AsyncMock();monkeypatch.setattr(h,'_continue_after_scene_approval',continuation)
    asyncio.run(h._handle_waiting_story_character_approval_state(update,None,chat_id=1,session=s,text='ОК'))
    assert record['approved'] is False
    assert record['delivery_pending'] is False and record['presented_message_id']==200
    generator.assert_called_once()
    continuation.assert_not_awaited()
