"""
Логирование полных промптов/ответов LLM в канал ``bot.llm``.

Обработчики настраиваются в ``main._configure_logging``: отдельный файл ``llm_responses.log``
плюс ``propagate=True`` — те же записи попадают в консоль и ``bot.log``.
"""

from __future__ import annotations

import logging

_LLM_IO_LOGGER_NAME = "bot.llm"


def get_llm_io_logger() -> logging.Logger:
    """Логгер для полного текста запросов/ответов текстовых и мультимодальных моделей."""
    return logging.getLogger(_LLM_IO_LOGGER_NAME)
