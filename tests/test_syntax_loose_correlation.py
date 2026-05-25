"""Loose reply correlation: accept Syntax replies after our batch without reply_to."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from app import syntax_telethon_fallback as stf


def test_loose_mode_accepts_hard_reject_without_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTAX_STRICT_REPLY_MATCH", raising=False)

    class _M:
        id = 500
        out = False
        reply_to = None
        raw_text = "Произошла ошибка. Пожалуйста, попробуйте еще раз."
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(MagicMock(), message=_M(), sent_ids={100, 200})

    out = asyncio.run(_run())
    assert out is not None
    assert out[4] is True


def test_loose_mode_pending_status_without_reply_to_sets_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Статус «в очереди» без reply_to должен давать retry_later (раньше отсекался без final_delivery)."""
    monkeypatch.delenv("SYNTAX_STRICT_REPLY_MATCH", raising=False)

    class _M:
        id = 500
        out = False
        reply_to = None
        raw_text = "Запрос ожидает выполнения в очереди."
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(MagicMock(), message=_M(), sent_ids={100, 200})

    out = asyncio.run(_run())
    assert out is not None
    _vbytes, _vuri, _pmsg, retry_later, hard_reject = out
    assert retry_later is True
    assert hard_reject is False


def test_strict_mode_ignores_without_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTAX_STRICT_REPLY_MATCH", "1")

    class _M:
        id = 500
        out = False
        reply_to = None
        raw_text = "Произошла ошибка. Пожалуйста, попробуйте еще раз."
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(MagicMock(), message=_M(), sent_ids={100, 200})

    assert asyncio.run(_run()) is None


def test_loose_mode_with_lang_token_ignores_other_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTAX_STRICT_REPLY_MATCH", raising=False)

    class _M:
        id = 700
        out = False
        reply_to = None
        raw_text = "lang:ru processing request"
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(
            MagicMock(),
            message=_M(),
            sent_ids={100, 200},
            expected_lang_token="lang:en",
        )

    assert asyncio.run(_run()) is None


def test_loose_mode_with_lang_token_accepts_matching_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTAX_STRICT_REPLY_MATCH", raising=False)

    class _M:
        id = 701
        out = False
        reply_to = None
        raw_text = "lang:en request queued"
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(
            MagicMock(),
            message=_M(),
            sent_ids={100, 200},
            expected_lang_token="lang:en",
        )

    out = asyncio.run(_run())
    assert out is not None


def test_loose_mode_with_lang_token_accepts_when_not_first_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNTAX_STRICT_REPLY_MATCH", raising=False)

    class _M:
        id = 702
        out = False
        reply_to = None
        raw_text = "✅ done for lang:en request queued"
        message = raw_text
        entities: list = []
        video = None
        document = None

    async def _run() -> object:
        return await stf._process_bot_message(
            MagicMock(),
            message=_M(),
            sent_ids={100, 200},
            expected_lang_token="lang:en",
        )

    out = asyncio.run(_run())
    assert out is not None
