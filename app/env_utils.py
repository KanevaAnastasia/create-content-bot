"""
Загрузка переменных из файла ``.env`` в корне проекта.

Формат строк: ``KEY=value`` (опционально в кавычках). Строки с ``#`` считаются комментариями.
Не перезаписывает переменные, переданные процессу извне. Значения, которые этот модуль
ранее загрузил из ``.env``, обновляются при изменении файла — это позволяет долгоживущим
воркерам подхватывать обновлённые OAuth-токены без перезапуска.
"""

import logging
import os
import threading
from pathlib import Path


_ENV_LOCK = threading.RLock()
_FILE_MANAGED_KEYS: set[str] = set()


def load_env_file() -> None:
    """
    Читает ``<корень_проекта>/.env`` и для каждой пары KEY=VALUE выставляет ``os.environ[KEY]``.

    Уже существующие переменные считаются внешней конфигурацией и сохраняются. Исключение —
    ключи, которые этот модуль сам загрузил из файла раньше: их значения синхронизируются с
    текущим содержимым ``.env``.

    Побочные эффекты: изменение ``os.environ`` процесса.
    При ошибке чтения файла логирует исключение и не падает (бот может работать только из env ОС).
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        with _ENV_LOCK:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    continue
                if key not in os.environ or key in _FILE_MANAGED_KEYS:
                    os.environ[key] = value
                    _FILE_MANAGED_KEYS.add(key)
    except OSError:
        logging.exception("Failed to read .env")
