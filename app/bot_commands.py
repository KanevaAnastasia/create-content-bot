"""Telegram command suggestions for the owner's private chat only."""

import asyncio

from sqlalchemy import select
from telegram import BotCommand, BotCommandScopeChat, MenuButtonCommands

from .db import SessionLocal
from .models import BotUser
from .quiet_mode import QUIET_MODE_OWNER_USERNAME


OWNER_COMMANDS = (
    BotCommand("start", "Открыть главное меню"),
    BotCommand("inst", "Статистика Instagram русской Бабушки"),
    BotCommand("stat", "Статистика генераций и субтитров"),
    BotCommand("stats", "Статистика — аналог /stat"),
    BotCommand("text_model", "Выбрать модель ChatGPT для текстов"),
    BotCommand("articles", "Статьи для канала У бабушки дома"),
    BotCommand("generate", "Запустить генерацию"),
    BotCommand("quick", "Быстрая генерация"),
    BotCommand("image", "Сгенерировать изображение"),
    BotCommand("video", "Сгенерировать видео"),
    BotCommand("drafts", "Открыть черновики"),
    BotCommand("save_draft", "Сохранить текущий черновик"),
    BotCommand("ai_approve", "Автоматическое согласование"),
    BotCommand("fix_stress", "Исправить ударения"),
    BotCommand("cancel", "Отменить текущий сценарий"),
    BotCommand("reset", "Сбросить сценарий — аналог /cancel"),
    BotCommand("start_quiet", "Включить тихий режим"),
    BotCommand("stop_quiet", "Выключить тихий режим"),
    BotCommand("status_quiet", "Проверить тихий режим"),
    BotCommand("check_env", "Проверить настройки бота"),
)


def owner_chat_id() -> int | None:
    with SessionLocal() as db:
        value = db.execute(select(BotUser.telegram_id).where(
            BotUser.normalized_username == QUIET_MODE_OWNER_USERNAME,
            BotUser.role == "admin",
            BotUser.is_active.is_(True),
        )).scalar_one_or_none()
    return int(value) if value and int(value) > 0 else None


async def configure_owner_commands(bot) -> None:
    chat_id = await asyncio.to_thread(owner_chat_id)
    if chat_id is None:
        return
    # A private-chat scope never publishes the owner's commands to other users.
    await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id))
    await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
