"""Tests for Syntax bot hard-reject (filters / user-facing error) detection."""

from __future__ import annotations

from app.syntax_telethon_fallback import _syntax_bot_message_is_hard_reject


def test_hard_reject_error_with_cross_emoji() -> None:
    text = "\u274c Произошла ошибка. Попробуйте ещё раз. Проверьте фильтрацию."
    assert _syntax_bot_message_is_hard_reject(text, []) is True


def test_hard_reject_proizoshla_oshibka() -> None:
    assert (
        _syntax_bot_message_is_hard_reject(
            "Произошла ошибка. Пожалуйста, попробуйте еще раз.",
            [],
        )
        is True
    )


def test_not_reject_pending_queue() -> None:
    assert (
        _syntax_bot_message_is_hard_reject(
            "[Тариф: ELITE]\nЗадача ожидает выполнения.",
            [],
        )
        is False
    )
