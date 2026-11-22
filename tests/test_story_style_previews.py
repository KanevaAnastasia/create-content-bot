import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from app import story_style_previews as previews
from app.handlers import handlers_state_start as start


def test_catalog_contains_all_fourteen_valid_references():
    styles=[s for s in previews.STORY_VISUAL_STYLES.values() if s['reference_path']]
    assert len(styles)==14
    for style in styles:
        with Image.open(previews.ROOT / style['reference_path']) as image:
            image.verify()


def test_pages_have_matching_photos_descriptions_and_buttons():
    all_labels=[]
    for page, count in [(0,14),(1,14),(2,14)]:
        session={previews.PAGE_KEY:page}
        message=SimpleNamespace(reply_photo=AsyncMock())
        assert asyncio.run(previews.send_story_style_previews(message,session))
        calls=message.reply_photo.await_args_list
        assert len(calls)==count
        for call, (number, key, style) in zip(calls,previews.page_items(session)):
            label=previews.choice_label(number,style)
            caption=call.kwargs['caption']
            assert caption==label+'\n'+previews.DESCRIPTIONS[style['label']]
            assert [label] in previews.page_rows(session)
            assert label in previews.page_description(session)
            assert previews.resolve_choice(label)==style['label']
            all_labels.append(label)
    assert len(set(all_labels))==14


def test_delivery_failure_keeps_style_choice_available(monkeypatch):
    message=SimpleNamespace(reply_photo=AsyncMock(side_effect=RuntimeError('offline')),reply_text=AsyncMock())
    session={'state':start.STATE_WAITING_STORY_STYLE_CHOICE}
    result=asyncio.run(start._handle_waiting_story_style_choice_state(
        SimpleNamespace(message=message),None,chat_id=1,session=session,text=previews.PREVIEW_BUTTON))
    assert result and session['state']==start.STATE_WAITING_STORY_STYLE_CHOICE
    assert 'повторить' in message.reply_text.await_args.args[0]
    assert previews.PREVIEW_BUTTON in [b.text for row in start._story_style_choice_markup().keyboard for b in row]


def test_english_choice_shows_previews_before_style_menu(monkeypatch):
    monkeypatch.setattr(start,'SESSIONS',{})
    message=SimpleNamespace(reply_photo=AsyncMock(),reply_text=AsyncMock())
    session={}
    asyncio.run(start._handle_waiting_story_english_choice_state(
        SimpleNamespace(message=message),None,chat_id=1,session=session,text='Нет'))
    assert message.reply_photo.await_count==14
    assert session['state']==start.STATE_WAITING_STORY_STYLE_CHOICE


def test_show_all_preserves_story_and_ignores_saved_page():
    message=SimpleNamespace(reply_photo=AsyncMock(),reply_text=AsyncMock())
    update=SimpleNamespace(message=message)
    session={'state':start.STATE_WAITING_STORY_STYLE_CHOICE,'user_input':'Моя история',previews.PAGE_KEY:2}
    asyncio.run(start._handle_waiting_story_style_choice_state(
        update,None,chat_id=1,session=session,text=previews.PREVIEW_BUTTON))
    assert message.reply_photo.await_count==14
    assert session['state']==start.STATE_WAITING_STORY_STYLE_CHOICE
    assert session['user_input']=='Моя история'
    assert previews.NEXT_BUTTON not in sum(previews.page_rows(session),[])
    assert previews.PREVIOUS_BUTTON not in sum(previews.page_rows(session),[])


def test_numbered_choice_enters_existing_generation_flow(monkeypatch):
    proceed=AsyncMock(return_value=True)
    monkeypatch.setattr(start,'_start_initial_story_voiceover',proceed)
    monkeypatch.setattr(start,'SESSIONS',{})
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session={}
    asyncio.run(start._handle_waiting_story_style_choice_state(
        update,None,chat_id=1,session=session,text='12. Пастельные мелки'))
    assert session[start.STORY_VISUAL_STYLE_SESSION_KEY]=='oil_pastel'
    proceed.assert_awaited_once()
