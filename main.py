"""
Точка входа Telegram-бота (polling).

Вся бизнес-логика вынесена в пакет ``app/``; здесь только:
- загрузка переменных из ``.env``;
- сборка ``Application`` (handlers, таймауты HTTP);
- запуск long-polling к API Telegram.

Запуск:
    1) poetry install
    2) в ``.env`` задать TELEGRAM_BOT_TOKEN, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION и VERTEX_API_KEY
       (опционально ``GEMINI_MODEL``, иначе ``gemini-2.5-flash``)
    3) poetry run python main.py
"""

import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler

from app.bot_app import build_application
from app.constants import TOKEN_ENV_NAME
from app.db import DATABASE_URL_ENV_NAME, health_check
from app.env_utils import load_env_file
from app.llm_logging import get_llm_io_logger
from telegram.error import Conflict, NetworkError, TimedOut

import fcntl
import sys
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

_LOCK_FILE = Path("/tmp/content_fabric_bot.lock")
_LOCK_FD = None


_TELEGRAM_BOT_TOKEN_RE = re.compile(r"/bot\d+:[^/\s\"']+")
_POLLING_RETRY_BASE_SECONDS = 3.0
_POLLING_RETRY_MAX_SECONDS = 60.0
_POLLING_CONFLICT_RETRY_SECONDS = 30.0
_POLLING_BOOTSTRAP_RETRIES = -1


class _SecretRedactionFilter(logging.Filter):
    """Redacts secrets that can appear inside third-party log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TELEGRAM_BOT_TOKEN_RE.sub("/bot<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    """Formatter-level redaction, so handler output is scrubbed even after formatting."""

    def format(self, record: logging.LogRecord) -> str:
        return _TELEGRAM_BOT_TOKEN_RE.sub("/bot<redacted>", super().format(record))


def _configure_logging() -> None:
    """
    Configure unified logging for console and files.

    Files:
    - logs/bot.log: all INFO+ events.
    - logs/errors.log: ERROR+ events only.
    - logs/llm_responses.log: полные промпты/ответы текстовой LLM и расширенные ответы image API (``bot.llm``).
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    redaction_filter = _SecretRedactionFilter()
    root.addFilter(redaction_filter)

    fmt = _RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    console.addFilter(redaction_filter)
    root.addHandler(console)

    logs_dir = os.getenv("BOT_LOG_DIR", "logs").strip() or "logs"
    os.makedirs(logs_dir, exist_ok=True)

    all_file = RotatingFileHandler(
        os.path.join(logs_dir, "bot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    all_file.setLevel(logging.INFO)
    all_file.setFormatter(fmt)
    all_file.addFilter(redaction_filter)
    root.addHandler(all_file)

    err_file = RotatingFileHandler(
        os.path.join(logs_dir, "errors.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    err_file.setLevel(logging.ERROR)
    err_file.setFormatter(fmt)
    err_file.addFilter(redaction_filter)
    root.addHandler(err_file)

    media_file = RotatingFileHandler(
        os.path.join(logs_dir, "media_generation.log"),
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    media_file.setLevel(logging.INFO)
    media_file.setFormatter(fmt)
    media_file.addFilter(redaction_filter)
    media_file.addFilter(
        lambda record: record.name.startswith("bot.image") or record.name.startswith("bot.video")
    )
    root.addHandler(media_file)

    llm_logger = get_llm_io_logger()
    llm_logger.handlers.clear()
    llm_logger.setLevel(logging.INFO)
    llm_logger.propagate = True
    llm_file = RotatingFileHandler(
        os.path.join(logs_dir, "llm_responses.log"),
        maxBytes=30 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    llm_file.setLevel(logging.INFO)
    llm_file.setFormatter(fmt)
    llm_file.addFilter(redaction_filter)
    llm_logger.addHandler(llm_file)


def _run_polling_with_retries(token: str) -> None:
    """Restarts long polling after transient Telegram polling failures."""
    retry_delay = _POLLING_RETRY_BASE_SECONDS
    bot_logger = logging.getLogger("bot")

    while True:
        application = build_application(token)
        try:
            application.run_polling(bootstrap_retries=_POLLING_BOOTSTRAP_RETRIES)
            return
        except Conflict:
            bot_logger.warning(
                "Telegram polling conflict: another getUpdates request is active; "
                "retrying in %.1f seconds.",
                _POLLING_CONFLICT_RETRY_SECONDS,
                exc_info=True,
            )
            time.sleep(_POLLING_CONFLICT_RETRY_SECONDS)
            retry_delay = _POLLING_RETRY_BASE_SECONDS
        except (NetworkError, TimedOut):
            bot_logger.warning(
                "Telegram polling stopped due to a network error; restarting in %.1f seconds.",
                retry_delay,
                exc_info=True,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _POLLING_RETRY_MAX_SECONDS)
        except Exception:
            logging.exception("Bot polling stopped with an error.")
            raise
        else:
            retry_delay = _POLLING_RETRY_BASE_SECONDS


def upgrade_database_to_head() -> None:
    """Apply Alembic migrations before the bot starts serving updates."""
    project_root = Path(__file__).resolve().parent
    config = AlembicConfig(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.attributes["skip_logging_config"] = True
    alembic_command.upgrade(config, "head")


def _acquire_process_lock() -> None:
    """Prevents multiple bot processes from polling the same token in parallel."""
    global _LOCK_FD
    if _LOCK_FD is not None:
        return

    lock_fd = _LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        print("Bot is already running. Exiting.")
        sys.exit(1)
    _LOCK_FD = lock_fd


def main() -> None:
    """
    Запускает бота.

    Действия по шагам:
    1. ``load_env_file()`` — подмешивает ключи из ``.env`` в ``os.environ`` (если ещё не заданы).
    2. Читает ``TELEGRAM_BOT_TOKEN`` — без него Telegram API недоступен.
    3. ``build_application(token)`` — регистрирует команды ``/start``, ``/generate`` и обработчик текста.
    4. ``run_polling()`` — бесконечный цикл опроса обновлений от Telegram.
    """
    _acquire_process_lock()
    load_env_file()
    _configure_logging()
    bot_logger = logging.getLogger("bot")
    bot_logger.info("Bot startup initialized.")
    if (os.getenv(DATABASE_URL_ENV_NAME) or "").strip():
        try:
            bot_logger.info("Applying database migrations.")
            upgrade_database_to_head()
            health_check()
        except Exception:
            bot_logger.exception("Database migration or health-check failed; aborting bot startup.")
            raise
        bot_logger.info("Database migrations are current and health-check passed.")
    token = os.getenv(TOKEN_ENV_NAME)
    if not token:
        raise RuntimeError(f"Missing environment variable: {TOKEN_ENV_NAME}")
    bot_logger.info("Starting Telegram polling.")
    _run_polling_with_retries(token)


if __name__ == "__main__":
    main()
