"""
Сборка ``telegram.ext.Application``: токен, HTTP-таймауты, регистрация handlers.

Порядок регистрации: сначала команды, затем общий текстовый handler (не команды).
"""

import asyncio
from contextlib import suppress
import logging
from dataclasses import replace
import uuid

from telegram import Update as TelegramUpdate
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .llm_utils import GeminiQuotaError
from .handlers import (
    ai_approve_command as admin_ai_approve_command,
    cancel as admin_cancel,
    check_env as admin_check_env,
    fix_stress_command as admin_fix_stress_command,
    generate as admin_generate,
    generate_image_command as admin_generate_image_command,
    generate_video_command as admin_generate_video_command,
    on_message_reaction as admin_on_message_reaction,
    on_audio as admin_on_audio,
    on_photo as admin_on_photo,
    on_text as admin_on_text,
    on_video as admin_on_video,
    quick_generate as admin_quick_generate,
    start as admin_start,
)
from .guest_access import resolve_bot_identity
from .guest_handlers import (
    guest_cancel,
    guest_on_photo,
    guest_on_text,
    guest_on_video,
    guest_start,
    project_workspace_start,
)
from .handlers.handlers_keyboards import first_step_menu_markup
from .handlers.handlers_state_scene import keyboard_for_session_state
from .quiet_mode import (
    BUTTON_CLOSE_QUIET_MODE,
    BUTTON_OPEN_QUIET_MODE,
    automatic_quiet_mode_status,
    ensure_automatic_quiet_mode_cycle,
    is_quiet_mode_enabled,
    is_quiet_mode_owner,
    quiet_mode_menu_scope,
    reset_automatic_quiet_mode_cycle,
    seconds_until_next_moscow_midnight,
    set_quiet_mode_enabled,
)
from .state import SESSIONS
from .session_store import recover_interrupted_guest_syntax_sessions

logger = logging.getLogger("bot")
_QUIET_MODE_MIDNIGHT_TASK_KEY = "quiet_mode_midnight_task"

try:
    from telegram.ext import MessageReactionHandler
except Exception:  # pragma: no cover - compatibility fallback
    MessageReactionHandler = None


async def _deny_access(update: object) -> None:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is not None:
        await message.reply_text("Доступ к боту не предоставлен. Обратитесь к администратору.")


def _identity_for_update(update: object, *, allow_username_claim: bool = False):
    telegram_user = getattr(update, "effective_user", None)
    try:
        return resolve_bot_identity(telegram_user, allow_username_claim=allow_username_claim)
    except Exception:
        logger.exception("bot.authorization_failed")
        return None


def _quiet_mode_enabled_safe() -> bool:
    try:
        return is_quiet_mode_enabled()
    except Exception:
        logger.exception("bot.quiet_mode_read_failed")
        return False


def _quiet_mode_blocks(identity: object | None, enabled: bool) -> bool:
    # Quiet mode no longer blocks navigation or ordinary messages. Guest
    # generation entry points apply the delayed synthetic failure themselves.
    _ = (identity, enabled)
    return False


async def _run_admin(callback, update, context, identity, *, quiet_enabled: bool) -> None:
    with quiet_mode_menu_scope(identity, quiet_enabled):
        await callback(update, context)


def _admin_project_workspace_identity(update: object, identity: object):
    """Return an admin identity scoped to the selected managed project, if active."""
    if str(getattr(identity, "role", "")) != "admin":
        return None
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    session = SESSIONS.get(chat_id) if chat_id is not None else None
    if not isinstance(session, dict) or session.get("project_workspace_mode") is not True:
        return None
    try:
        project_id = uuid.UUID(str(session.get("project_id") or ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return replace(identity, project_id=project_id)


def _admin_keyboard_for_current_session(update: object, identity: object, quiet_enabled: bool):
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    session = SESSIONS.get(chat_id) if chat_id is not None else None
    with quiet_mode_menu_scope(identity, quiet_enabled):
        return keyboard_for_session_state(session) if isinstance(session, dict) else first_step_menu_markup()


async def quiet_mode_command(update, context) -> None:
    _ = context
    identity = _identity_for_update(update)
    if not is_quiet_mode_owner(identity):
        return
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return
    raw_command = str(getattr(message, "text", "") or "").split(maxsplit=1)[0].split("@", 1)[0].lower()
    enabled = _quiet_mode_enabled_safe()
    if raw_command == "/start_quiet":
        if not enabled:
            enabled = set_quiet_mode_enabled(identity, True)
            status = "Тихий режим включён. Сообщения других пользователей молча пропускаются."
        else:
            status = "Тихий режим уже включён."
    elif raw_command == "/stop_quiet":
        if enabled:
            enabled = set_quiet_mode_enabled(identity, False)
            status = "Тихий режим выключен. Сообщения других пользователей снова обрабатываются."
        else:
            status = "Тихий режим уже выключен."
    else:
        status = f"Тихий режим сейчас {'включён' if enabled else 'выключен'}."
    await message.reply_text(
        status,
        reply_markup=_admin_keyboard_for_current_session(update, identity, enabled),
    )


async def quiet_mode_stats_command(update, context) -> None:
    _ = context
    identity = _identity_for_update(update)
    if not is_quiet_mode_owner(identity):
        return
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return
    status = automatic_quiet_mode_status()
    if status is None or not status["automatic_enabled"]:
        text = "Автоматический гостевой режим выключен."
        enabled = _quiet_mode_enabled_safe()
    else:
        count = int(status["generation_count"])
        target = int(status["generation_target"])
        text = f"Гостевой режим: использовано {count} из {target} генераций сегодня."
        enabled = bool(status["quiet_mode_enabled"])
    await message.reply_text(
        text,
        reply_markup=_admin_keyboard_for_current_session(update, identity, enabled),
    )


async def start(update, context) -> None:
    identity = _identity_for_update(update, allow_username_claim=True)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        workspace_identity = _admin_project_workspace_identity(update, identity)
        if workspace_identity is not None:
            await project_workspace_start(update, context, workspace_identity)
        else:
            await _run_admin(admin_start, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        await guest_start(update, context, identity)


async def cancel(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        workspace_identity = _admin_project_workspace_identity(update, identity)
        if workspace_identity is not None:
            await guest_cancel(update, context, workspace_identity)
        else:
            await _run_admin(admin_cancel, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        await guest_cancel(update, context, identity)


async def on_text(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        workspace_identity = _admin_project_workspace_identity(update, identity)
        if workspace_identity is not None:
            await guest_on_text(update, context, workspace_identity)
            return
        text = str(getattr(getattr(update, "message", None), "text", "") or "").strip()
        if is_quiet_mode_owner(identity) and text in {BUTTON_OPEN_QUIET_MODE, BUTTON_CLOSE_QUIET_MODE}:
            quiet_enabled = text == BUTTON_OPEN_QUIET_MODE
            set_quiet_mode_enabled(identity, quiet_enabled)
            message = getattr(update, "message", None)
            if message is not None:
                status = (
                    "Тихий режим открыт. Сообщения других пользователей молча пропускаются."
                    if quiet_enabled
                    else "Тихий режим закрыт. Сообщения других пользователей снова обрабатываются."
                )
                await message.reply_text(
                    status,
                    reply_markup=_admin_keyboard_for_current_session(update, identity, quiet_enabled),
                )
            return
        await _run_admin(admin_on_text, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        await guest_on_text(update, context, identity)


async def on_photo(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        workspace_identity = _admin_project_workspace_identity(update, identity)
        if workspace_identity is not None:
            await guest_on_photo(update, context, workspace_identity)
        else:
            await _run_admin(admin_on_photo, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        await guest_on_photo(update, context, identity)


async def on_audio(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        await _run_admin(admin_on_audio, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is not None:
            await message.reply_text("Сейчас аудиофайл не требуется. Используйте кнопки текущего шага.")


async def on_video(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled):
        return
    if identity is None:
        await _deny_access(update)
    elif identity.role == "admin":
        workspace_identity = _admin_project_workspace_identity(update, identity)
        if workspace_identity is not None:
            await guest_on_video(update, context, workspace_identity)
        else:
            await _run_admin(admin_on_video, update, context, identity, quiet_enabled=quiet_enabled)
    else:
        await guest_on_video(update, context, identity)


async def on_message_reaction(update, context) -> None:
    identity = _identity_for_update(update)
    quiet_enabled = _quiet_mode_enabled_safe()
    if _quiet_mode_blocks(identity, quiet_enabled) or identity is None:
        return
    if identity.role == "admin":
        await _run_admin(admin_on_message_reaction, update, context, identity, quiet_enabled=quiet_enabled)


def _admin_only(callback):
    async def wrapped(update, context):
        identity = _identity_for_update(update)
        quiet_enabled = _quiet_mode_enabled_safe()
        if _quiet_mode_blocks(identity, quiet_enabled):
            return
        if identity is None:
            await _deny_access(update)
        elif identity.role == "admin":
            await _run_admin(callback, update, context, identity, quiet_enabled=quiet_enabled)
        else:
            await guest_start(update, context, identity)

    return wrapped


check_env = _admin_only(admin_check_env)
generate = _admin_only(admin_generate)
quick_generate = _admin_only(admin_quick_generate)
ai_approve_command = _admin_only(admin_ai_approve_command)
generate_image_command = _admin_only(admin_generate_image_command)
generate_video_command = _admin_only(admin_generate_video_command)
fix_stress_command = _admin_only(admin_fix_stress_command)


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


async def _quiet_mode_midnight_loop() -> None:
    while True:
        await asyncio.sleep(seconds_until_next_moscow_midnight())
        try:
            await asyncio.to_thread(reset_automatic_quiet_mode_cycle)
        except Exception:
            logger.exception("quiet_mode.midnight_reset_failed")


async def _post_init(application: Application) -> None:
    try:
        await asyncio.to_thread(ensure_automatic_quiet_mode_cycle)
    except Exception:
        logger.exception("quiet_mode.startup_cycle_check_failed")
    try:
        await asyncio.to_thread(recover_interrupted_guest_syntax_sessions)
    except Exception:
        logger.exception("guest.syntax_startup_recovery_failed")
    application.bot_data[_QUIET_MODE_MIDNIGHT_TASK_KEY] = asyncio.create_task(
        _quiet_mode_midnight_loop(),
        name="quiet-mode-midnight-moscow",
    )


async def _post_shutdown(application: Application) -> None:
    task = application.bot_data.pop(_QUIET_MODE_MIDNIGHT_TASK_KEY, None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def build_application(token: str) -> Application:
    """
    Создаёт и настраивает приложение бота.

    Параметры:
        token: строка токена от BotFather.

    Увеличенные таймауты ``HTTPXRequest`` снижают число ``TimedOut`` на медленных сетях/прокси.
    """
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0,
        media_write_timeout=600.0,
    )
    app = (
        Application.builder()
        .token(token)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_quiet", quiet_mode_command))
    app.add_handler(CommandHandler("stop_quiet", quiet_mode_command))
    app.add_handler(CommandHandler("status_quiet", quiet_mode_command))
    app.add_handler(CommandHandler("stat", quiet_mode_stats_command))
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
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    if MessageReactionHandler is not None:
        app.add_handler(MessageReactionHandler(on_message_reaction))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(_on_application_error)
    return app
