import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app import syntax_telethon_fallback as syntax


@pytest.mark.parametrize('error', [None, syntax.SyntaxSubmissionUnconfirmedError('no acknowledgement')])
def test_kling_waits_for_existing_result_after_submit(monkeypatch, error):
    monkeypatch.setattr(syntax, '_wait_for_syntax_configure_button', AsyncMock(return_value=(SimpleNamespace(id=102), object())))
    monkeypatch.setattr(syntax, '_syntax_configure_button_webview_url', AsyncMock(return_value='https://example.test/form'))
    fill = Mock(side_effect=error)
    monkeypatch.setattr(syntax, '_fill_syntax_kling_webapp_sync', fill)
    poll = AsyncMock(return_value='existing-video')
    monkeypatch.setattr(syntax, '_poll_syntax_bot_until_video', poll)
    before = time.monotonic()
    result = asyncio.run(syntax._run_syntax_kling_configure_flow(
        object(), object(), bot_entity=object(), prompt='lang:ru scene', start_image=b'image',
        duration_sec=3, aspect_ratio='9:16', reply_anchor=100, sent_ids={100,101},
        attempt_id='test', poll=10, deadline=before-1, timeout=900, initial_last_info=''))
    assert result == 'existing-video'
    fill.assert_called_once()
    assert poll.call_args.kwargs['deadline'] >= before + 900
    assert poll.call_args.kwargs['expected_lang_token'] == 'lang:ru'
    assert poll.call_args.kwargs['sent_ids'] == {100,101,102}


def test_kling_preparation_failure_does_not_wait(monkeypatch):
    monkeypatch.setattr(syntax, '_wait_for_syntax_configure_button', AsyncMock(return_value=(SimpleNamespace(id=102), object())))
    monkeypatch.setattr(syntax, '_syntax_configure_button_webview_url', AsyncMock(return_value='https://example.test/form'))
    monkeypatch.setattr(syntax, '_fill_syntax_kling_webapp_sync', Mock(side_effect=ValueError('image upload failed')))
    poll = AsyncMock()
    monkeypatch.setattr(syntax, '_poll_syntax_bot_until_video', poll)
    result = asyncio.run(syntax._run_syntax_kling_configure_flow(
        object(), object(), bot_entity=object(), prompt='lang:ru scene', start_image=b'image',
        duration_sec=3, aspect_ratio='9:16', reply_anchor=100, sent_ids={100},
        attempt_id='test', poll=10, deadline=0, timeout=900, initial_last_info=''))
    assert not result.success
    poll.assert_not_called()
