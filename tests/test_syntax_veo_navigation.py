from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.syntax_telethon_fallback import _open_syntax_veo_page_sync


def test_veo_retries_disconnected_bundled_browser_before_submission():
    broken, good = Mock(), Mock()
    broken.new_page.return_value.goto.side_effect = RuntimeError('net::ERR_SOCKET_NOT_CONNECTED')
    launch = Mock(side_effect=[broken, good])
    browser, page = _open_syntax_veo_page_sync(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), 'https://example.test', 90000)
    assert browser is good and page is good.new_page.return_value
    broken.close.assert_called_once()
    good.close.assert_not_called()
    assert launch.call_args_list[1].kwargs == {'headless': True, 'channel': 'chrome'}
    page.goto.assert_called_once_with('https://example.test', wait_until='commit', timeout=60000)


def test_veo_does_not_retry_unrelated_navigation_failure():
    broken = Mock()
    broken.new_page.return_value.goto.side_effect = RuntimeError('access denied')
    launch = Mock(return_value=broken)
    with pytest.raises(RuntimeError, match='access denied'):
        _open_syntax_veo_page_sync(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), 'https://example.test', 30000)
    launch.assert_called_once()
    broken.close.assert_called_once()


def test_veo_form_never_selects_duration(monkeypatch):
    from app import syntax_telethon_fallback as s
    from playwright import sync_api
    browser, page = Mock(), Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=Mock())
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(sync_api, 'sync_playwright', lambda: context)
    monkeypatch.setattr(s, '_open_syntax_veo_page_sync', lambda *a: (browser, page))
    for name in ('_clear_syntax_veo_uploaded_references_sync', '_upload_syntax_kling_start_frame_sync',
                 '_wait_for_syntax_veo_uploaded_count_sync', '_fill_and_commit_syntax_kling_prompt_sync',
                 '_set_syntax_aspect_ratio_sync'):
        monkeypatch.setattr(s, name, Mock())
    monkeypatch.setattr(s, '_syntax_page_contains_aspect_ratio_sync', lambda *a: True)
    select = Mock(side_effect=AssertionError('Veo must not select duration'))
    monkeypatch.setattr(s, '_try_click_syntax_option_sync', select)
    submit = Mock()
    monkeypatch.setattr(s, '_submit_syntax_veo_generate_sync', submit)
    s._fill_syntax_veo_webapp_sync(url='https://example.test', prompt='Chicken', start_image=b'png',
                                 duration_sec=6, aspect_ratio='9:16', timeout_sec=60)
    select.assert_not_called()
    submit.assert_called_once()
    browser.close.assert_called_once()


def test_old_six_second_veo_request_uses_eight(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app import syntax_telethon_fallback as s
    monkeypatch.setattr(s, '_syntax_configure_button_webview_url', AsyncMock(return_value='https://example.test'))
    fill = Mock()
    monkeypatch.setattr(s, '_fill_syntax_veo_webapp_sync', fill)
    assert asyncio.run(s._submit_syntax_veo_webview(None, None, bot_entity=None, configure_message=None,
        configure_button=None, prompt='Chicken', start_image=b'png', duration_sec=6,
        aspect_ratio='9:16', timeout_sec=60)) is None
    assert fill.call_args.kwargs['duration_sec'] == 8


@pytest.mark.parametrize("label", ["Загружено (1/2)", "Загруженно (1/50)", "Загруженна (1 / 11)"])
def test_veo_accepts_upload_counter_spelling_variants(label):
    from app import syntax_telethon_fallback as s
    page = Mock()
    page.locator.return_value.inner_text.return_value = label
    assert s._syntax_veo_uploaded_reference_count_sync(page) == 1


def test_veo_retries_system_chrome_socket_failure_after_missing_bundled_browser():
    broken, good = Mock(), Mock()
    broken.new_page.return_value.goto.side_effect = RuntimeError('net::ERR_SOCKET_NOT_CONNECTED')
    launch = Mock(side_effect=[RuntimeError("Executable doesn't exist"), broken, good])
    browser, _ = _open_syntax_veo_page_sync(
        SimpleNamespace(chromium=SimpleNamespace(launch=launch)), 'https://example.test', 30000)
    assert browser is good
    broken.close.assert_called_once()
    assert launch.call_count == 3


def test_veo_socket_retries_are_bounded():
    broken = Mock()
    broken.new_page.return_value.goto.side_effect = RuntimeError('net::ERR_SOCKET_NOT_CONNECTED')
    launch = Mock(return_value=broken)
    with pytest.raises(RuntimeError, match='ERR_SOCKET_NOT_CONNECTED'):
        _open_syntax_veo_page_sync(
            SimpleNamespace(chromium=SimpleNamespace(launch=launch)), 'https://example.test', 30000)
    assert launch.call_count == 4
    assert broken.close.call_count == 4


def test_single_veo_keeps_polling_after_webview_error(monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock
    from app import syntax_telethon_fallback as s
    submit = AsyncMock(return_value="upload counter timeout")
    result = object()
    poll = AsyncMock(return_value=result)
    monkeypatch.setattr(s, '_submit_syntax_veo_webview', submit)
    monkeypatch.setattr(s, '_poll_syntax_bot_until_video', poll)
    actual = asyncio.run(s._run_syntax_veo_configure_flow(
        None, None, bot_entity=None, configure_message=SimpleNamespace(id=15),
        configure_button=None, prompt='lang:ru test', start_image=b'png',
        duration_sec=8, aspect_ratio='9:16', reply_anchor=10, sent_ids={10, 11},
        attempt_id='test', poll=1, deadline=time.monotonic() + 60, timeout=60,
        initial_last_info='', expected_lang_token='lang:ru'))
    assert actual is result
    submit.assert_awaited_once()
    assert poll.call_args.kwargs['sent_ids'] == {10, 11, 15}
    assert poll.call_args.kwargs['initial_last_info'] == 'upload counter timeout'
