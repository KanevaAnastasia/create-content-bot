"""
Глобальная очередь LLM: один worker, последовательное выполнение, троттлинг между запросами.

Все вызовы API к моделям должны идти через ``submit_llm`` (используется из ``safe_llm_call``).
Параллельных запросов к LLM нет — следующая задача стартует только после паузы между запросами.

Каждый вызов оборачивается в **дедлайн** (``GEMINI_GENERATE_TIMEOUT_SEC``): если Vertex SDK
зависает в ``generate_content``, клиент получает ошибку и может перейти к fallback-модели,
вместо бесконечного молчания в чате.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import random
import threading
import time
from typing import Any, Callable, TypeVar

from .constants import GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME

logger = logging.getLogger("bot")

T = TypeVar("T")

# Очередь задач: (callable, Future для результата). Имя по ТЗ — «глобальная очередь».
llm_queue: queue.Queue[tuple[Callable[[], Any], concurrent.futures.Future[Any]]] = queue.Queue()

# Интервал между завершением одного запроса и стартом следующего (секунды, случайный в диапазоне).
LLM_THROTTLE_MIN_SEC = 5.0
LLM_THROTTLE_MAX_SEC = 9.0

_DEFAULT_GENERATE_TIMEOUT_SEC = 180.0
_MIN_GENERATE_TIMEOUT_SEC = 30.0
_MAX_GENERATE_TIMEOUT_SEC = 600.0


class LLMCallTimeoutError(RuntimeError):
    """
    Истёк лимит ожидания ответа модели: ``generate_content`` не завершился до дедлайна.

    Обрабатывается в ``gemini_generate_sync`` как повод перейти к следующей модели из fallbacks.
    """


def resolve_llm_generate_timeout_sec() -> float:
    """
    Читает верхнюю границу ожидания одного вызова генерации (секунды) из окружения.

    Returns:
        Число секунд в диапазоне [30, 600]; при некорректном значении — дефолт 180.
    """
    raw = (os.getenv(GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME) or "").strip()
    if not raw:
        return _DEFAULT_GENERATE_TIMEOUT_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_GENERATE_TIMEOUT_SEC
    return max(_MIN_GENERATE_TIMEOUT_SEC, min(v, _MAX_GENERATE_TIMEOUT_SEC))


def _run_llm_call_with_deadline(fn: Callable[[], T], timeout_sec: float) -> T:
    """
    Выполняет ``fn`` в отдельном потоке и ждёт не дольше ``timeout_sec``.

    При таймауте поднимает ``LLMCallTimeoutError``; исходный поток остаётся daemon и может
    ещё какое-то время висеть на сети — это лучше, чем блокировать всю очередь LLM навсегда.
    """
    _unset = object()
    result: Any = _unset
    err: Exception | None = None

    def runner() -> None:
        nonlocal result, err
        try:
            result = fn()
        except Exception as e:
            err = e

    t = threading.Thread(target=runner, daemon=True, name="llm-generate-call")
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        logger.error(
            "LLM generate_content exceeded %.0f s timeout (call continues in background daemon thread)",
            timeout_sec,
        )
        raise LLMCallTimeoutError(
            f"Ответ модели не пришёл за {timeout_sec:.0f} с. Попробуйте ещё раз или проверьте "
            f"{GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME} / список fallback-моделей."
        )
    if err is not None:
        raise err
    if result is _unset:
        raise RuntimeError("LLM call finished without result or exception.")
    return result


def _worker_loop() -> None:
    while True:
        fn, fut = llm_queue.get()
        deadline = resolve_llm_generate_timeout_sec()
        try:
            try:
                out = _run_llm_call_with_deadline(fn, deadline)
                fut.set_result(out)
            except Exception as e:
                fut.set_exception(e)
        finally:
            llm_queue.task_done()
        delay = random.uniform(LLM_THROTTLE_MIN_SEC, LLM_THROTTLE_MAX_SEC)
        time.sleep(delay)


_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="llm-queue-worker")
        t.start()
        _worker_started = True
        logger.info(
            "LLM queue worker started (throttle %.1f–%.1f s, generate timeout %.0f s)",
            LLM_THROTTLE_MIN_SEC,
            LLM_THROTTLE_MAX_SEC,
            resolve_llm_generate_timeout_sec(),
        )


def submit_llm(fn: Callable[[], T]) -> T:
    """
    Ставит ``fn`` в глобальную очередь и блокируется до результата.
    Выполняется строго по одному запросу с паузой после каждого.
    """
    _ensure_worker()
    fut: concurrent.futures.Future[T] = concurrent.futures.Future()
    llm_queue.put((fn, fut))
    return fut.result()
