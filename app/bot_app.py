"""
Сборка ``telegram.ext.Application``: токен, HTTP-таймауты, регистрация handlers.

Порядок регистрации: сначала команды, затем общий текстовый handler (не команды).
"""

import logging

from telegram import Update as TelegramUpdate
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .llm_utils import GeminiQuotaError
from .handlers import (
    ai_approve_command,
    cancel,
    check_env,
    fix_stress_command,
    generate,
    generate_image_command,
    generate_video_command,
    on_message_reaction,
    on_photo,
    on_text,
    quick_generate,
    start,
)

logger = logging.getLogger("bot")

try:
    from telegram.ext import MessageReactionHandler
except Exception:  # pragma: no cover - compatibility fallback
    MessageReactionHandler = None


async def _on_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Centralized error logger to avoid silent crashes in update loop."""
    error = context.error
    if error is None:
        return
    if isinstance(error, Conflict):
        logger.warning(
            "telegram.update.conflict update=%s; another getUpdates request is active, polling will retry.",
            type(update).__name__,
        )
        return
    logger.exception("telegram.update.error update=%s", type(update).__name__, exc_info=error)
    text = str(error)
    low = text.lower()
    is_ai_limit = (
        isinstance(error, GeminiQuotaError)
        or "429" in text
        or "resource_exhausted" in low
        or "resource exhausted" in low
        or "too many requests" in low
        or "слишком много запросов" in low
    )
    if not is_ai_limit:
        return
    message = None
    if isinstance(update, TelegramUpdate):
        message = update.effective_message
    else:
        message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(
            "Уперлась в лимит AI на этом шаге. Этот запрос не удалось продолжить из аварийного обработчика; "
            "отправьте последнее действие ещё раз через минуту."
        )
    except Exception:
        logger.exception("failed to notify user about application-level AI limit")


def build_application(token: str) -> Application:
    """
    Создаёт и настраивает приложение бота.

    Параметры:
        token: строка токена от BotFather.

    Увеличенные таймауты ``HTTPXRequest`` снижают число ``TimedOut`` на медленных сетях/прокси.
    """
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=120.0, pool_timeout=30.0)
    app = Application.builder().token(token).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check_env", check_env))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reset", cancel))
    app.add_handler(CommandHandler("generate", generate))
    app.add_handler(CommandHandler("quick", quick_generate))
    app.add_handler(CommandHandler("drafts", on_text))
    app.add_handler(CommandHandler("save_draft", on_text))
    app.add_handler(CommandHandler("ai_approve", ai_approve_command))
    app.add_handler(CommandHandler("image", generate_image_command))
    app.add_handler(CommandHandler("video", generate_video_command))
    app.add_handler(CommandHandler("fix_stress", fix_stress_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    if MessageReactionHandler is not None:
        app.add_handler(MessageReactionHandler(on_message_reaction))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(_on_application_error)
    return app
