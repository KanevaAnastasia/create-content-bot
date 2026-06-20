from __future__ import annotations

import asyncio
import inspect
import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import NetworkError, RetryAfter, TimedOut

logger = logging.getLogger("bot")

_SEND_RETRY_ATTEMPTS = 3
_SEND_RETRY_BASE_DELAY_SEC = 1.0
_REPLY_MARKUP_UNSET = object()

FIRST_STEP_BUTTON_GENERATE = "Генерировать"
FIRST_STEP_BUTTON_DESCRIPTION = "Описание"
FIRST_STEP_BUTTON_IMAGE = "Изображение"
FIRST_STEP_BUTTON_PROJECT_IMAGE = "Изображение проект"
FIRST_STEP_BUTTON_VIDEO = "Видео"
FIRST_STEP_BUTTON_SOUND = "Звук"
FIRST_STEP_BUTTON_PROJECT = "Проект"


def first_step_menu_markup() -> ReplyKeyboardMarkup:
    """Кнопки первого шага: выбор режима генерации."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [FIRST_STEP_BUTTON_GENERATE, FIRST_STEP_BUTTON_DESCRIPTION],
            [FIRST_STEP_BUTTON_IMAGE, FIRST_STEP_BUTTON_PROJECT_IMAGE],
            [FIRST_STEP_BUTTON_VIDEO, FIRST_STEP_BUTTON_SOUND],
            [FIRST_STEP_BUTTON_PROJECT],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def remove_keyboard_markup() -> ReplyKeyboardRemove:
    """Скрывает старые кнопки, когда от пользователя сейчас не ждём действия."""
    return ReplyKeyboardRemove()


def _reply_text_accepts_reply_markup(message) -> bool:
    """Best-effort compatibility check for lightweight test doubles."""
    try:
        signature = inspect.signature(message.reply_text)
    except (TypeError, ValueError, AttributeError):
        return True
    params = signature.parameters
    return "reply_markup" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _type_error_is_unexpected_reply_markup(exc: TypeError) -> bool:
    text = str(exc)
    return "reply_markup" in text and (
        "unexpected keyword" in text
        or "got an unexpected" in text
        or "positional" in text
    )


async def safe_send(call, *, label: str) -> None:
    """Runs a Telegram send call with short retries on transient delivery failures."""
    for attempt in range(1, _SEND_RETRY_ATTEMPTS + 1):
        try:
            return await call()
        except RetryAfter as e:
            if attempt >= _SEND_RETRY_ATTEMPTS:
                raise
            delay = float(getattr(e, "retry_after", _SEND_RETRY_BASE_DELAY_SEC) or _SEND_RETRY_BASE_DELAY_SEC)
            logger.warning(
                "telegram.send retry_after label=%s attempt=%s delay=%ss",
                label,
                attempt,
                delay,
            )
            await asyncio.sleep(delay)
        except (NetworkError, TimedOut) as e:
            if attempt >= _SEND_RETRY_ATTEMPTS:
                raise
            delay = _SEND_RETRY_BASE_DELAY_SEC * attempt
            logger.warning(
                "telegram.send transient_error label=%s attempt=%s err=%s",
                label,
                attempt,
                str(e),
            )
            await asyncio.sleep(delay)


async def safe_reply_text(message, text: str, *, reply_markup=_REPLY_MARKUP_UNSET) -> None:
    """Sends text with retry and preserves reply_markup on retried delivery attempts."""
    accepts_reply_markup = (
        reply_markup is _REPLY_MARKUP_UNSET or _reply_text_accepts_reply_markup(message)
    )

    async def _send_once():
        if reply_markup is _REPLY_MARKUP_UNSET:
            return await message.reply_text(text)
        if accepts_reply_markup:
            try:
                return await message.reply_text(text, reply_markup=reply_markup)
            except TypeError as exc:
                if not _type_error_is_unexpected_reply_markup(exc):
                    raise
                logger.warning("telegram.reply_text_without_reply_markup_fallback err=%s", exc)
        return await message.reply_text(text)

    return await safe_send(_send_once, label="reply_text")


async def safe_send_message(bot, *, chat_id: int, text: str, **kwargs) -> None:
    """Sends a bot message with retry and preserves kwargs such as reply_markup."""
    return await safe_send(
        lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
        label="send_message",
    )


async def reply_text_without_keyboard(message, text: str) -> None:
    """Отправляет статус и убирает текущую клавиатуру; безопасно для простых тестовых стабов."""
    await safe_reply_text(message, text, reply_markup=remove_keyboard_markup())
