from http.client import RemoteDisconnected
from unittest.mock import Mock

import pytest
from requests.exceptions import ConnectionError, ReadTimeout
from urllib3.exceptions import ProtocolError
from app import llm_utils


@pytest.mark.parametrize('error', [
    ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))),
    ReadTimeout('read timeout'),
])
def test_requests_transport_failure_retries_and_allows_model_fallback(monkeypatch, error):
    submit = Mock(side_effect=[error, 'ok'])
    monkeypatch.setattr(llm_utils, 'submit_llm', submit)
    monkeypatch.setattr(llm_utils.time, 'sleep', lambda _: None)
    monkeypatch.setattr(llm_utils, 'LLM_TRANSPORT_MAX_ATTEMPTS', 2)
    assert llm_utils.safe_llm_call(lambda: 'unused', max_attempts=2) == 'ok'
    assert submit.call_count == 2
    assert llm_utils._is_retryable_with_other_key_or_model(error)


def test_transport_retries_are_bounded(monkeypatch):
    error = ConnectionError('Connection aborted.')
    submit = Mock(side_effect=error)
    monkeypatch.setattr(llm_utils, 'submit_llm', submit)
    monkeypatch.setattr(llm_utils.time, 'sleep', lambda _: None)
    monkeypatch.setattr(llm_utils, 'LLM_TRANSPORT_MAX_ATTEMPTS', 2)
    with pytest.raises(ConnectionError):
        llm_utils.safe_llm_call(lambda: None, max_attempts=5)
    assert submit.call_count == 2
