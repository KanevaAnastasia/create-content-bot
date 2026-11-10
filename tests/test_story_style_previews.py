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


def test_delivers_two_albums_with_exact_style_names():
    message=SimpleNamespace(reply_media_group=AsyncMock())
    assert asyncio.run(previews.send_story_style_previews(message))
    calls=message.reply_media_group.await_args_list
    assert [len(c.kwargs['media']) for c in calls]==[7,7]
    assert [item.caption for c in calls for item in c.kwargs['media']]==[
        s['label'] for s in previews.STORY_VISUAL_STYLES.values() if s['reference_path']]


def test_delivery_failure_keeps_style_choice_available(monkeypatch):
    message=SimpleNamespace(reply_media_group=AsyncMock(side_effect=RuntimeError('offline')),reply_text=AsyncMock())
    session={'state':start.STATE_WAITING_STORY_STYLE_CHOICE}
    result=asyncio.run(start._handle_waiting_story_style_choice_state(
        SimpleNamespace(message=message),None,chat_id=1,session=session,text=previews.PREVIEW_BUTTON))
    assert result and session['state']==start.STATE_WAITING_STORY_STYLE_CHOICE
    assert 'повторить' in message.reply_text.await_args.args[0]
    assert previews.PREVIEW_BUTTON in [b.text for row in start._story_style_choice_markup().keyboard for b in row]


def test_english_choice_shows_previews_before_style_menu(monkeypatch):
    monkeypatch.setattr(start,'SESSIONS',{})
    message=SimpleNamespace(reply_media_group=AsyncMock(),reply_text=AsyncMock())
    session={}
    asyncio.run(start._handle_waiting_story_english_choice_state(
        SimpleNamespace(message=message),None,chat_id=1,session=session,text='Нет'))
    assert message.reply_media_group.await_count==2
    assert session['state']==start.STATE_WAITING_STORY_STYLE_CHOICE
