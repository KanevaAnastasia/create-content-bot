import copy
import pytest
from app.story_prop_references import reconcile_story_prop_references
from app.handlers.handlers_media_scene import _load_scene_reference_images
from app.generation_store import _scene_prompt_payload, merge_scene_prompt_payload


def scene(tmp_path):
    book=tmp_path/'book.png';book.write_bytes(b'approved-book')
    actor=tmp_path/'actor.png';actor.write_bytes(b'approved-king')
    return {'generation_mode':'story','story_visual_style_key':'history_yarn_puppet',
            'characters':['king'],'story_prop_ids':[],
            'references':{'king':str(actor)}, 'story_required_reference_ids':['king'],
            'story_prop_reference_catalog':{'prop_published_book':{
                'display_name':'Изданный экземпляр Кэрри','path':str(book),'approved':True,
                'style':'history_yarn_puppet','description':'утверждённая обложка'}},
            'start_frame_prompt':'На столе книга как на референсе.'}


def test_edited_prompt_attaches_book_bytes_after_database_roundtrip(tmp_path,monkeypatch):
    from app.story_image_postprocess import crop_story_reference_border
    monkeypatch.setattr('app.story_image_postprocess.crop_story_reference_border',lambda b:b)
    s=scene(tmp_path)
    restored={};merge_scene_prompt_payload(restored,_scene_prompt_payload(s))
    restored['start_frame_prompt']=s['start_frame_prompt']
    assert _load_scene_reference_images(restored)==[b'approved-king',b'approved-book']
    assert restored['story_prop_ids']==['prop_published_book']
    assert 'prop_published_book' in restored['story_required_reference_ids']
    reconcile_story_prop_references(restored)
    assert restored['story_prop_ids']==['prop_published_book']


@pytest.mark.parametrize('text',['В стиле иллюстрированной книги.', 'Книги нет.', 'Убери книгу со стола.', 'Без книги.', 'storybook style, no book'])
def test_style_or_negative_mentions_do_not_attach_book(tmp_path,text):
    s=scene(tmp_path);s['start_frame_prompt']=text
    reconcile_story_prop_references(s)
    assert s['story_prop_ids']==[]


@pytest.mark.parametrize('fault',['unapproved','style','missing'])
def test_needed_reference_fails_closed(tmp_path,fault):
    s=scene(tmp_path);r=s['story_prop_reference_catalog']['prop_published_book']
    if fault=='unapproved':r['approved']=False
    elif fault=='style':r['style']='history_comic'
    else:__import__('pathlib').Path(r['path']).unlink()
    with pytest.raises(ValueError):_load_scene_reference_images(s)


def test_positive_prop_with_negative_lettering_qualifier_is_attached(tmp_path):
    s=scene(tmp_path);s['start_frame_prompt']='На столе лежит книга без надписей.'
    reconcile_story_prop_references(s)
    assert s['story_prop_ids']==['prop_published_book']


def test_offscreen_prop_is_not_added(tmp_path):
    s=scene(tmp_path);s['start_frame_prompt']='Книга за верхней границей кадра.'
    reconcile_story_prop_references(s)
    assert s['story_prop_ids']==[]


def test_add_remove_add_survives_snapshot_and_old_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr('app.story_image_postprocess.crop_story_reference_border', lambda b: b)
    s = scene(tmp_path)
    reconcile_story_prop_references(s)
    s['instruction'] = 'Убери книгу со стола.'
    reconcile_story_prop_references(s, prompt_keys=('instruction',), explicit_edit=True)
    restored = {}
    merge_scene_prompt_payload(restored, _scene_prompt_payload(s))
    restored['start_frame_prompt'] = s['start_frame_prompt']
    assert _load_scene_reference_images(restored) == [b'approved-king']
    assert 'prop_published_book' not in restored['story_prop_descriptions']
    restored['instruction'] = 'Положи книгу на стол без машинки.'
    reconcile_story_prop_references(restored, prompt_keys=('instruction',), explicit_edit=True)
    assert _load_scene_reference_images(restored) == [b'approved-king', b'approved-book']


def test_image_edit_passes_approved_prop_bytes_to_provider(tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.handlers import handlers_state_media as media
    monkeypatch.setattr('app.story_image_postprocess.crop_story_reference_border', lambda b: b)
    s = scene(tmp_path)
    s['start_frame_image_b64'] = base64.b64encode(b'current-frame').decode()
    before = copy.deepcopy(s)
    captured = []

    async def provider(prompt, *, reference_images):
        captured.extend(reference_images)
        raise RuntimeError('test stops before paid generation')

    monkeypatch.setattr(media, '_generate_image_with_lenient_fallback', provider)
    monkeypatch.setattr(media, '_send_image_reference_debug', AsyncMock())
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    import asyncio
    asyncio.run(media._scene_media_edit_generated_image(update, session={}, scene=s, idx=0,
                                                target='start', instruction='На столе книга как на референсе.'))
    assert captured == [b'current-frame', b'approved-king', b'approved-book']
    assert s == before


def test_remove_one_prop_keeps_other_clause(tmp_path):
    s = scene(tmp_path)
    s['story_prop_reference_catalog']['prop_typewriter'] = dict(
        s['story_prop_reference_catalog']['prop_published_book'], display_name='Машинка')
    s['story_prop_ids'] = ['prop_typewriter', 'prop_published_book']
    s['instruction'] = 'Убери машинку, оставь книгу на столе.'
    reconcile_story_prop_references(s, prompt_keys=('instruction',), explicit_edit=True)
    assert s['story_prop_ids'] == ['prop_published_book']
    assert s['story_excluded_prop_ids'] == ['prop_typewriter']
