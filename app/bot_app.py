"""
Сборка ``telegram.ext.Application``: токен, HTTP-таймауты, регистрация handlers.

Порядок регистрации: сначала команды, затем общий текстовый handler (не команды).
"""

import logging

from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .handlers import (
    ai_approve_command,
    cancel,
    check_env,
    fix_stress_command,
    generate,
    generate_image_command,
    generate_video_command,
    on_photo,
    on_text,
    quick_generate,
    start,
)

logger = logging.getLogger("bot")


async def _on_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Centralized error logger to avoid silent crashes in update loop."""
    logger.exception("telegram.update.error update=%s", type(update).__name__, exc_info=context.error)


def build_application(token: str) -> Application:
    """
    Создаёт и настраивает приложение бота.

    Параметры:
        token: строка токена от BotFather.

    Увеличенные таймауты ``HTTPXRequest`` снижают число ``TimedOut`` на медленных сетях/прокси.
    """
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=10.0)
    app = Application.builder().token(token).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check_env", check_env))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reset", cancel))
    app.add_handler(CommandHandler("generate", generate))
    app.add_handler(CommandHandler("quick", quick_generate))
    app.add_handler(CommandHandler("ai_approve", ai_approve_command))
    app.add_handler(CommandHandler("image", generate_image_command))
    app.add_handler(CommandHandler("video", generate_video_command))
    app.add_handler(CommandHandler("fix_stress", fix_stress_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(_on_application_error)
    return app
