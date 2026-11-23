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
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'props':[{'id':'prop_typewriter','description':'чёрная машинка','is_key_story_prop':True},{'id':'prop_letter','description':'письмо'}], 'scenes':[{'characters':['vasily'],'props':['prop_typewriter']},{'characters':['vasily'],'props':['prop_typewriter']},{'characters':[],'characters_absent_reason':'Крупно письмо без людей','props':['prop_letter']}]}))
    return g.generate_story_scene_plan(source_story='Кинг пишет.',voiceover_text='Кинг пишет.',duration_sec=12,alignment={},project_slug='discipline',discipline_mode='lives_archive',story_visual_style='history_yarn_puppet',project_characters=[{'id':'vasily','description':'Василий','asset_uri':actor.as_uri(),'is_primary':True}])


def test_used_project_actor_requires_new_approval_and_only_recurring_props(monkeypatch,tmp_path):
    result=planned(monkeypatch,tmp_path)
    bible={v['id']:v for v in result['character_bible']}
    assert bible['vasily']['requires_reference'] is True
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
    assert media._load_scene_reference_images(s['scenes_plan'][0])==[b'vasily',b'prop_typewriter']
    assert media._load_scene_reference_images(s['scenes_plan'][2])==[]
    manifest=json.loads(Path(s['scenes_plan'][0]['continuity_manifest_path']).read_text())
    assert {a['id'] for a in manifest['assets']}=={'vasily','prop_typewriter'}
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
    assert 'сохрани лицо' in captured['prompt']
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
    with pytest.raises(ValueError,match='оба'):
        g.generate_story_scene_plan(source_story='Кинг пишет, Табита читает.',voiceover_text='Кинг пишет, Табита читает.',duration_sec=5,alignment={},project_slug='discipline',discipline_mode='lives_archive',project_characters=[{'id':'vasily','description':'Василий'},{'id':'anastasia','display_name':'Анастасия','description':'Анастасия'}])

def test_repeated_mug_stays_description_only(monkeypatch,tmp_path):
    original=g.gemini_generate_sync
    monkeypatch.setattr(g,'build_semantic_narration_beats',lambda *a:[{'beat':i+1,'start_sec':i*4,'end_sec':(i+1)*4,'narration_excerpt':'Пишет.'} for i in range(2)])
    monkeypatch.setattr(g,'gemini_generate_sync',lambda *a,**kw:json.dumps({'props':[{'id':'prop_mug','display_name':'Кружка','description':'белая','is_key_story_prop':True}], 'scenes':[{'characters':['vasily'],'props':['prop_mug']}]*2}))
    result=g.generate_story_scene_plan(source_story='Пишет.',voiceover_text='Пишет.',duration_sec=8,alignment={},project_slug='discipline',discipline_mode='lives_archive',project_characters=[{'id':'vasily','description':'Василий'}])
    mug=next(c for c in result['character_bible'] if c['id']=='prop_mug')
    assert mug['requires_reference'] is False


def test_other_projects_do_not_gain_paid_prop_approval(monkeypatch,tmp_path):
    planned(monkeypatch,tmp_path)
    result=g.generate_story_scene_plan(source_story='Пишет.',voiceover_text='Пишет.',duration_sec=12,alignment={},project_slug='other_project',project_characters=[{'id':'vasily','description':'герой'}])
    assert all(not c['requires_reference'] for c in result['character_bible'] if c.get('entity_type')=='prop')
