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
