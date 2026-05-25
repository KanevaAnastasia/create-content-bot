"""
Минимальный слой подключения к PostgreSQL через SQLAlchemy (sync).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.env_utils import load_env_file


DATABASE_URL_ENV_NAME = "DATABASE_URL"


class Base(DeclarativeBase):
    """Базовый declarative-класс для ORM-моделей проекта."""


def get_database_url() -> str:
    """
    Возвращает URL подключения к БД из ``DATABASE_URL``.

    Raises:
        RuntimeError: если переменная окружения не задана.
    """
    # Подгружаем .env, чтобы SessionLocal() работал из REPL/скриптов без ручного load_env_file().
    load_env_file()
    database_url = (os.getenv(DATABASE_URL_ENV_NAME) or "").strip()
    if not database_url:
        raise RuntimeError(
            "Missing environment variable: DATABASE_URL. "
            "Example: postgresql+psycopg2://user:password@localhost:5432/content_fabric"
        )
    return database_url


def create_db_engine(database_url: str | None = None) -> Engine:
    """
    Создаёт sync SQLAlchemy engine.

    Args:
        database_url: явный URL подключения; если не передан, берётся из env.
    """
    url = database_url or get_database_url()
    return create_engine(url, future=True, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Ленивая singleton-инициализация SQLAlchemy engine."""
    return create_db_engine()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    """Ленивая singleton-инициализация фабрики sync-сессий."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)


def SessionLocal(**kwargs: Any) -> Session:
    """
    Совместимый alias для создания sync-сессии SQLAlchemy.

    Возвращает новую ``Session`` при каждом вызове.
    """
    return get_session_factory()(**kwargs)


def create_tables(engine: Engine | None = None) -> None:
    """
    Создаёт все таблицы, объявленные в ORM-моделях.

    Если engine не передан, используется ``get_engine()``.
    """
    # Импорт нужен для регистрации классов моделей в metadata перед create_all.
    from app import models as _models  # noqa: F401

    Base.metadata.create_all(bind=engine or get_engine())


def health_check(engine: Engine | None = None) -> bool:
    """
    Проверяет, что соединение с БД открывается и отвечает на простой запрос.
    """
    target = engine or get_engine()
    with target.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True
