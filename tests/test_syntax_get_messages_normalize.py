"""Tests for Telethon get_messages result normalization."""

from __future__ import annotations

from app.syntax_telethon_fallback import _first_message_from_get_messages


class _Msg:
    def __init__(self, mid: int) -> None:
        self.id = mid


def test_single_message_passthrough() -> None:
    m = _Msg(42)
    assert _first_message_from_get_messages(m) is m


def test_list_first() -> None:
    a, b = _Msg(1), _Msg(2)
    assert _first_message_from_get_messages([a, b]) is a


def test_empty_list() -> None:
    assert _first_message_from_get_messages([]) is None


def test_none() -> None:
    assert _first_message_from_get_messages(None) is None
