"""
Загрузка переменных из файла ``.env`` в корне проекта.

Формат строк: ``KEY=value`` (опционально в кавычках). Строки с ``#`` считаются комментариями.
Не перезаписывает уже установленные в окружении переменные — удобно для переопределения при деплое.
"""

import logging
import os
from pathlib import Path


def load_env_file() -> None:
    """
    Читает ``<корень_проекта>/.env`` и для каждой пары KEY=VALUE выставляет ``os.environ[KEY]``,
    если KEY ещё не был задан.

    Побочные эффекты: изменение ``os.environ`` процесса.
    При ошибке чтения файла логирует исключение и не падает (бот может работать только из env ОС).
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        logging.exception("Failed to read .env")
