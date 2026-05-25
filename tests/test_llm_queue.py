"""Тесты дедлайна вызова LLM в очереди."""

import time

import pytest

from app.llm_queue import LLMCallTimeoutError, _run_llm_call_with_deadline, resolve_llm_generate_timeout_sec


def test_run_llm_call_with_deadline_returns_value():
    assert _run_llm_call_with_deadline(lambda: "ok", timeout_sec=2.0) == "ok"


def test_run_llm_call_with_deadline_propagates_exception():
    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError, match="x"):
        _run_llm_call_with_deadline(boom, timeout_sec=2.0)


def test_run_llm_call_with_deadline_times_out():
    def slow():
        time.sleep(3.0)
        return "late"

    with pytest.raises(LLMCallTimeoutError):
        _run_llm_call_with_deadline(slow, timeout_sec=0.15)


def test_resolve_llm_generate_timeout_sec_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_GENERATE_TIMEOUT_SEC", raising=False)
    assert resolve_llm_generate_timeout_sec() == 180.0


def test_resolve_llm_generate_timeout_sec_clamped(monkeypatch):
    monkeypatch.setenv("GEMINI_GENERATE_TIMEOUT_SEC", "10")
    assert resolve_llm_generate_timeout_sec() == 30.0
    monkeypatch.setenv("GEMINI_GENERATE_TIMEOUT_SEC", "900")
    assert resolve_llm_generate_timeout_sec() == 600.0


def test_llm_timeout_is_retryable_in_gemini_loop(monkeypatch):
    from app import llm_utils

    assert llm_utils._is_retryable_with_other_key_or_model(LLMCallTimeoutError("x")) is True
