import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import handlers_state_scene as scene


@pytest.mark.parametrize('result', [ConnectionError('Remote disconnected'), {}])
def test_failed_production_preserves_plan_and_offers_retry(monkeypatch, result):
    session = {'scenes_plan': [{'scene': 1}]}
    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    sessions = {}
    monkeypatch.setattr(scene, 'SESSIONS', sessions)
    monkeypatch.setattr(scene, 'reply_text_without_keyboard', AsyncMock())
    monkeypatch.setattr(scene, 'is_story_generation', lambda s: False)
    monkeypatch.setattr(scene, 'is_advertising_generation', lambda s: False)
    call = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(scene, '_call_with_503_retries', call)
    asyncio.run(scene._generate_production_after_overlay_title(update, None, chat_id=123, session=session))
    assert sessions[123]['scenes_plan'] == [{'scene': 1}]
    assert session['state'] == scene._STATE_WAITING_PRODUCTION_RETRY
    assert reply.call_args.kwargs['reply_markup'].keyboard[0][0].text == scene._PRODUCTION_RETRY_BUTTON


def test_retry_button_routes_to_saved_production(monkeypatch):
    session = {'state': scene._STATE_WAITING_PRODUCTION_RETRY, 'scenes_plan': [{'scene': 1}]}
    update = SimpleNamespace(message=object())
    monkeypatch.setattr(scene, '_debounced_session', AsyncMock(return_value=(123, session, scene._PRODUCTION_RETRY_BUTTON)))
    monkeypatch.setattr(scene, '_handle_navigation_buttons', AsyncMock(return_value=False))
    monkeypatch.setattr(scene, '_handle_draft_buttons', AsyncMock(return_value=False))
    monkeypatch.setattr(scene, 'load_session_if_possible', lambda _: None)
    generate = AsyncMock()
    monkeypatch.setattr(scene, '_generate_production_after_overlay_title', generate)
    asyncio.run(scene._on_text_impl(update, None))
    generate.assert_awaited_once_with(update, None, chat_id=123, session=session)
