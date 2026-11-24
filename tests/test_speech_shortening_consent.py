import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.handlers import handlers_state_media as media

@pytest.mark.parametrize('language', ['ru', 'en'])
def test_long_phrase_is_unchanged_until_consent(monkeypatch, language):
    shorten = Mock(side_effect=AssertionError('No consent'))
    monkeypatch.setattr(media, 'shorten_scene_speech_and_sync_video_prompt_sync', shorten)
    scene = {'text': 'Длинная реплика. ' * 20, 'speech_by_lang': {'en': 'Long phrase. ' * 30}, 'duration_sec': 8}
    session = {'production': {'json_plan': [scene]}, 'image_scene_index': 0}
    original = deepcopy(scene)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    common = dict(context=None, session=session, chat_id=1, scene=scene, duration_sec=8)
    if language == 'ru':
        result = asyncio.run(media._maybe_shorten_scene_speech_for_video_budget(update, scene_idx=0, **common))
    else:
        result = asyncio.run(media._maybe_adjust_english_speech_for_video_budget(update, **common))
    assert result is True
    assert scene['text'] == original['text']
    assert scene['speech_by_lang'] == original['speech_by_lang']
    assert session[media.SCENE_SPEECH_PENDING_KEY]['stage'] == 'user_long_phrase_choice'
    assert session[media.SCENE_SPEECH_PENDING_KEY]['language'] == language
    assert 'символов' in update.message.reply_text.call_args.args[0]
    shorten.assert_not_called()

@pytest.mark.parametrize('language', ['ru', 'en'])
def test_consent_button_invokes_only_shortening_not_video(monkeypatch, language):
    shorten = AsyncMock(return_value=True)
    monkeypatch.setattr(media, '_maybe_shorten_scene_speech_for_video_budget', shorten)
    monkeypatch.setattr(media, '_maybe_adjust_english_speech_for_video_budget', shorten)
    video = AsyncMock(side_effect=AssertionError('Must approve new phrase first'))
    monkeypatch.setattr(media, '_scene_media_video_ok', video)
    scene = {'text': 'Исходная фраза.', 'speech_by_lang': {'en': 'Original phrase.'}}
    session = {'production': {'json_plan': [scene]}, 'image_scene_index': 0,
               'scene_media_substep': 'video_prompt', media.SCENE_SPEECH_PENDING_KEY:
               {'scene_idx': 0, 'duration_sec': 8, 'stage': 'user_long_phrase_choice', 'language': language}}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(media._handle_waiting_scene_media_state(update, context=None, chat_id=1,
        session=session, text=media.SCENE_SPEECH_OPTION_SHORTEN_AI))
    assert shorten.await_args.kwargs['shortening_approved'] is True
    video.assert_not_called()

def test_no_timeout_rewrites_phrase(monkeypatch):
    create_task = Mock(side_effect=AssertionError('Must not schedule silent consent'))
    monkeypatch.setattr(media.asyncio, 'create_task', create_task)
    media._schedule_scene_speech_timeout(context=object(), session={}, chat_id=12345)
    create_task.assert_not_called()
