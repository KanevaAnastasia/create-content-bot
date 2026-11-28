import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from app import reference_publication_copy as copy


def sample():
    return {key: f'Text for {key}' for key in copy.FIELDS}


def test_feedback_is_editorial_brief_for_all_platforms(monkeypatch):
    call = Mock(return_value=json.dumps(sample()))
    monkeypatch.setattr(copy, 'gemini_generate_sync', call)
    result = copy.generate_copy(image_prompt='Жонглёр', video_prompt='Applause',
        comment='Замени героя', feedback='Вы часто не видите годы труда что стоят за успехом',
        current=sample(), project='discipline')
    assert result == sample()
    prompt = call.call_args.args[0]
    assert 'Вы часто не видите годы труда' in prompt
    assert 'главная мысль' in prompt
    assert 'Не пересказывай' in prompt
    assert all(key in prompt for key in copy.FIELDS)
    assert 'CONTENT_FABRIC_WRITING_STYLE' in prompt


def test_incomplete_copy_is_rejected(monkeypatch):
    monkeypatch.setattr(copy, 'gemini_generate_sync', lambda *a, **k: '{"instagram_ru":"текст"}')
    with pytest.raises(ValueError):
        copy.generate_copy(image_prompt='', video_prompt='')


def test_regeneration_preserves_feedback_and_persists_exact_copy(monkeypatch):
    from app.handlers import handlers_state_media as media
    session = {'production': {'json_plan': [{'start_frame_prompt': 'Кадр'}]},
               'reference_description_feedback': 'Годы труда за успехом',
               'reference_description_approved': True, 'selected_project_key': 'discipline'}
    generate = Mock(return_value=sample())
    save = Mock()
    send = AsyncMock()
    monkeypatch.setattr(copy, 'generate_copy', generate)
    monkeypatch.setattr(copy, 'save_bot_publication_copy', save)
    monkeypatch.setattr(media, '_send_copyable_message', send)
    asyncio.run(copy.prepare_copy(SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())), session))
    assert generate.call_args.kwargs['feedback'] == 'Годы труда за успехом'
    assert session['reference_publication_copy']['copy'] == sample()
    assert session['reference_description_approved'] is False
    assert save.call_args.args[1]['copy'] == sample()
    assert send.await_count == 8


def test_copy_and_feedback_survive_session_cache():
    from app.session_store import _chat_session_cache_payload
    session = {'reference_publication_copy': {'copy': sample()},
               'reference_description_feedback': 'Годы труда'}
    cached = _chat_session_cache_payload(session)
    assert cached['reference_publication_copy'] == session['reference_publication_copy']
    assert cached['reference_description_feedback'] == 'Годы труда'


def test_failed_regeneration_cannot_approve_stale_copy(monkeypatch):
    from app.handlers import handlers_state_scene as handler
    session = {'reference_publication_copy_error': True,
               'reference_publication_copy': {'copy': sample()},
               'reference_description_approved': False}
    ready = Mock()
    save = Mock()
    monkeypatch.setattr(handler, 'mark_generation_run_ready', ready)
    monkeypatch.setattr(copy, 'save_bot_publication_copy', save)
    asyncio.run(handler._handle_reference_description_state(
        SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())), None,
        chat_id=1, session=session, text='ОК'))
    ready.assert_not_called()
    save.assert_not_called()
    assert session['reference_description_approved'] is False
