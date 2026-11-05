import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import Conflict

from app import bot_app
from app.handlers.handlers_keyboards import first_step_menu_markup
from app.quiet_mode import (
    BUTTON_CLOSE_QUIET_MODE,
    BUTTON_OPEN_QUIET_MODE,
    quiet_mode_menu_scope,
)


def test_application_error_handler_logs_polling_conflict_as_warning(caplog):
    class Context:
        error = Conflict("terminated by other getUpdates request")

    with caplog.at_level(logging.WARNING, logger="bot"):
        asyncio.run(bot_app._on_application_error(None, Context()))

    assert "telegram.update.conflict" in caplog.text
    assert "Traceback" not in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def _keyboard_rows(markup):
    return [[button.text for button in row] for row in markup.keyboard]


def test_quiet_mode_button_is_only_visible_to_owner_admin():
    owner = SimpleNamespace(role="admin", normalized_username="a_kaneva")
    other_admin = SimpleNamespace(role="admin", normalized_username="another_admin")

    with quiet_mode_menu_scope(owner, False):
        assert _keyboard_rows(first_step_menu_markup())[-1] == [BUTTON_OPEN_QUIET_MODE]
    with quiet_mode_menu_scope(owner, True):
        assert _keyboard_rows(first_step_menu_markup())[-1] == [BUTTON_CLOSE_QUIET_MODE]
    with quiet_mode_menu_scope(other_admin, False):
        buttons = [button for row in _keyboard_rows(first_step_menu_markup()) for button in row]
        assert BUTTON_OPEN_QUIET_MODE not in buttons
        assert BUTTON_CLOSE_QUIET_MODE not in buttons


def test_quiet_mode_keeps_guest_navigation_working(monkeypatch):
    guest = SimpleNamespace(role="guest", normalized_username="guest")
    called = []
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: guest)
    monkeypatch.setattr(bot_app, "_quiet_mode_enabled_safe", lambda: True)

    async def fake_guest_on_text(*_args):
        called.append(True)

    monkeypatch.setattr(bot_app, "guest_on_text", fake_guest_on_text)
    message = SimpleNamespace(text="Сообщение")
    update = SimpleNamespace(message=message)

    asyncio.run(bot_app.on_text(update, SimpleNamespace()))

    assert called == [True]


def test_owner_can_open_quiet_mode_and_gets_close_button(monkeypatch):
    owner = SimpleNamespace(
        user_id="owner-id",
        role="admin",
        normalized_username="a_kaneva",
    )
    changed = []
    sent = []
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: owner)
    monkeypatch.setattr(bot_app, "_quiet_mode_enabled_safe", lambda: False)
    monkeypatch.setattr(bot_app, "set_quiet_mode_enabled", lambda _identity, enabled: changed.append(enabled))

    class Message:
        text = BUTTON_OPEN_QUIET_MODE

        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    asyncio.run(bot_app.on_text(SimpleNamespace(message=Message()), SimpleNamespace()))

    assert changed == [True]
    assert "молча пропускаются" in sent[0][0]
    assert _keyboard_rows(sent[0][1])[-1] == [BUTTON_CLOSE_QUIET_MODE]


def test_quiet_mode_toggle_restores_current_generation_keyboard(monkeypatch):
    owner = SimpleNamespace(user_id="owner-id", role="admin", normalized_username="a_kaneva")
    current_markup = object()
    sent = []
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: owner)
    monkeypatch.setattr(bot_app, "_quiet_mode_enabled_safe", lambda: False)
    monkeypatch.setattr(bot_app, "set_quiet_mode_enabled", lambda _identity, _enabled: None)
    monkeypatch.setattr(bot_app, "SESSIONS", {305: {"state": "waiting_scene_media"}})
    monkeypatch.setattr(bot_app, "keyboard_for_session_state", lambda _session: current_markup)

    class Message:
        text = BUTTON_OPEN_QUIET_MODE

        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    update = SimpleNamespace(message=Message(), effective_chat=SimpleNamespace(id=305))
    asyncio.run(bot_app.on_text(update, SimpleNamespace()))

    assert sent[0][1] is current_markup


def test_quiet_mode_commands_preserve_current_generation(monkeypatch):
    owner = SimpleNamespace(user_id="owner-id", role="admin", normalized_username="a_kaneva")
    current_markup = object()
    quiet = {"enabled": False}
    session = {
        "state": "waiting_scene_media",
        "generation_run_id": "run-123",
        "image_scene_index": 2,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1}, {"scene": 2}, {"scene": 3}]},
    }
    expected_session = {
        "state": "waiting_scene_media",
        "generation_run_id": "run-123",
        "image_scene_index": 2,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1}, {"scene": 2}, {"scene": 3}]},
    }
    sent = []
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: owner)
    monkeypatch.setattr(bot_app, "_quiet_mode_enabled_safe", lambda: quiet["enabled"])
    monkeypatch.setattr(bot_app, "SESSIONS", {305: session})
    monkeypatch.setattr(bot_app, "keyboard_for_session_state", lambda active_session: current_markup)

    def fake_set(_identity, enabled):
        quiet["enabled"] = enabled
        return enabled

    monkeypatch.setattr(bot_app, "set_quiet_mode_enabled", fake_set)

    class Message:
        text = "/start_quiet"

        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    message = Message()
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=305))

    asyncio.run(bot_app.quiet_mode_command(update, SimpleNamespace()))
    message.text = "/status_quiet"
    asyncio.run(bot_app.quiet_mode_command(update, SimpleNamespace()))
    message.text = "/stop_quiet"
    asyncio.run(bot_app.quiet_mode_command(update, SimpleNamespace()))

    assert "включён" in sent[0][0]
    assert "сейчас включён" in sent[1][0]
    assert "выключен" in sent[2][0]
    assert all(markup is current_markup for _text, markup in sent)
    assert session == expected_session


def test_quiet_mode_commands_are_silent_for_non_owner(monkeypatch):
    guest = SimpleNamespace(user_id="guest-id", role="guest", normalized_username="guest")
    sent = []
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: guest)

    class Message:
        text = "/status_quiet"

        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    asyncio.run(
        bot_app.quiet_mode_command(
            SimpleNamespace(message=Message(), effective_chat=SimpleNamespace(id=999)),
            SimpleNamespace(),
        )
    )

    assert sent == []


def test_stat_command_reports_guest_generation_usage_to_owner(monkeypatch):
    owner = SimpleNamespace(user_id="owner-id", role="admin", normalized_username="a_kaneva")
    current_markup = object()
    sent = []
    instagram = AsyncMock()
    monkeypatch.setattr(bot_app, "send_instagram_stats", instagram)
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: owner)
    monkeypatch.setattr(
        bot_app,
        "automatic_quiet_mode_status",
        lambda: {
            "automatic_enabled": True,
            "quiet_mode_enabled": False,
            "generation_count": 4,
            "generation_target": 15,
        },
    )
    monkeypatch.setattr(bot_app, "_admin_keyboard_for_current_session", lambda *_args: current_markup)
    monkeypatch.setattr(
        bot_app,
        "speech_alignment_monthly_usage",
        lambda: {"used_seconds": 40.0, "remaining_seconds": 3560.0},
    )

    class Message:
        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    asyncio.run(bot_app.quiet_mode_stats_command(SimpleNamespace(message=Message()), SimpleNamespace()))

    assert sent == [
        (
            "Гостевой режим: использовано 4 из 15 генераций сегодня.\n"
            "Точная синхронизация субтитров: осталось 59 мин 20 сек из 60 мин в этом месяце "
            "(использовано 0 мин 40 сек по учёту бота).",
            current_markup,
        )
    ]
    instagram.assert_awaited_once()


def test_stat_command_is_silent_for_non_owner(monkeypatch):
    guest = SimpleNamespace(user_id="guest-id", role="guest", normalized_username="guest")
    sent = []
    instagram = AsyncMock()
    monkeypatch.setattr(bot_app, "send_instagram_stats", instagram)
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_args, **_kwargs: guest)

    class Message:
        async def reply_text(self, text, reply_markup=None):
            sent.append((text, reply_markup))

    asyncio.run(bot_app.quiet_mode_stats_command(SimpleNamespace(message=Message()), SimpleNamespace()))

    assert sent == []
    instagram.assert_not_awaited()


def test_instagram_calendar_is_owner_only(monkeypatch):
    calendar = AsyncMock()
    monkeypatch.setattr(bot_app, "handle_calendar", calendar)
    monkeypatch.setattr(bot_app, "_identity_for_update", lambda *_: SimpleNamespace(role="guest", normalized_username="guest"))
    update = SimpleNamespace(callback_query=SimpleNamespace(answer=AsyncMock()))
    asyncio.run(bot_app.instagram_stats_callback(update, SimpleNamespace()))
    calendar.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
