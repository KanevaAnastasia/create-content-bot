import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import BotCommandScopeChat, MenuButtonCommands
from telegram.ext import CommandHandler

from app import bot_app, bot_commands


def test_menu_is_only_registered_for_owner_private_chat(monkeypatch):
    monkeypatch.setattr(bot_commands, "owner_chat_id", lambda: 12345)
    bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())
    asyncio.run(bot_commands.configure_owner_commands(bot))
    bot.set_my_commands.assert_awaited_once_with(
        bot_commands.OWNER_COMMANDS, scope=BotCommandScopeChat(12345),
    )
    bot.set_chat_menu_button.assert_awaited_once_with(
        chat_id=12345, menu_button=MenuButtonCommands(),
    )


def test_no_menu_without_linked_active_owner(monkeypatch):
    monkeypatch.setattr(bot_commands, "owner_chat_id", lambda: None)
    bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())
    asyncio.run(bot_commands.configure_owner_commands(bot))
    bot.set_my_commands.assert_not_awaited()
    bot.set_chat_menu_button.assert_not_awaited()


def test_menu_covers_all_registered_commands():
    application = bot_app.build_application("123456:TEST")
    commands = {
        command
        for handlers in application.handlers.values()
        for handler in handlers if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {item.command for item in bot_commands.OWNER_COMMANDS} == commands
