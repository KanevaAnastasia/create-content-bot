"""
Обёртки над Google Gemini и разбор ответов модели.

Модели часто возвращают JSON внутри markdown-блоков ```json ... ``` или с пояснительным текстом;
функции ``extract_json_*`` вытаскивают валидный JSON из «грязного» вывода.

Все вызовы API идут через ``safe_llm_call`` → глобальная очередь ``submit_llm`` (увеличенный троттлинг).

Исчерпание **квоты** (``quotaValue``, ``RESOURCE_EXHAUSTED``): без retry, сразу ``GeminiQuotaError``.
**429 / rate limit**: экспоненциальный backoff с джиттером (и подсказкой ``retry in Xs`` из ответа, если есть).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import contextvars
import threading
import time
import hashlib
from typing import Any, Callable, TypeVar

from .constants import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_KEY_ENV_NAME,
    GEMINI_KEYS_ENV_NAME,
    GEMINI_MODEL_FALLBACKS_ENV_NAME,
    GEMINI_MODEL_ENV_NAME,
)
from .llm_logging import get_llm_io_logger
from .llm_queue import LLMCallTimeoutError, submit_llm

logger = logging.getLogger("bot")
_llm_io = get_llm_io_logger()

T = TypeVar("T")
_RUNTIME_NOTICE_LOCK = threading.Lock()
_RUNTIME_NOTICES: list[str] = []
_PREFER_VERTEX_API: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "prefer_vertex_api",
    default=False,
)


class GeminiQuotaError(RuntimeError):
    """Исчерпан лимит Gemini (квота / преходящий rate limit после повторов)."""


# Сообщение в Telegram при исчерпании дневной/проектной квоты (retry бессмысленен).
GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE = (
    "Лимит генераций на сегодня исчерпан 😅\n"
    "Попробуй позже или я могу переключиться на другую модель"
)
GEMINI_RATE_LIMIT_EXHAUSTED_USER_MESSAGE = (
    "Лимит AI: слишком много запросов подряд. Подождите минуту и отправьте сообщение снова."
)


def _mask_key(api_key: str) -> str:
    """
    Безопасный идентификатор ключа для логов (без утечки полного значения).
    """
    if not api_key:
        return "<empty>"
    tail = api_key[-4:] if len(api_key) >= 4 else api_key
    return f"***{tail}"


def _push_runtime_notice(message: str) -> None:
    """Кладёт служебное уведомление для UI (например о смене транспорта)."""
    msg = (message or "").strip()
    if not msg:
        return
    with _RUNTIME_NOTICE_LOCK:
        _RUNTIME_NOTICES.append(msg)


def pop_runtime_notices() -> list[str]:
    """
    Возвращает и очищает накопленные служебные уведомления для UI.
    """
    with _RUNTIME_NOTICE_LOCK:
        if not _RUNTIME_NOTICES:
            return []
        out = list(_RUNTIME_NOTICES)
        _RUNTIME_NOTICES.clear()
        return out


def set_prefer_vertex_api(enabled: bool) -> contextvars.Token:
    """
    Временный runtime-флаг: предпочитать VERTEX_API_KEY в текущем контексте запроса.
    """
    return _PREFER_VERTEX_API.set(bool(enabled))


def reset_prefer_vertex_api(token: contextvars.Token) -> None:
    """
    Сбрасывает runtime-флаг, установленный через set_prefer_vertex_api().
    """
    _PREFER_VERTEX_API.reset(token)


def _is_quota_exhausted_no_retry(e: BaseException) -> bool:
    """
    Квота исчерпана — повтор запроса не поможет (в отличие от кратковременного rate limit).
    """
    s = str(e).lower()
    return "quotavalue" in s or "resource_exhausted" in s or "resource exhausted" in s


def _parse_remaining_quota(error_text: str) -> str | None:
    """Пытается вытащить из текста ошибки число remaining / quotaValue (если API его отдал)."""
    patterns = (
        r'["\']?quotaValue["\']?\s*[:=]\s*["\']?(\d+)',
        r'"remaining"\s*:\s*"?(\d+)"?',
        r"remaining_quota\s*[:=]\s*(\d+)",
        r"quota[_\s]*remaining\s*[:=]\s*(\d+)",
        r"limit:\s*(\d+)\s*/\s*(\d+)",  # иногда limit / used
    )
    for pat in patterns:
        m = re.search(pat, error_text, flags=re.IGNORECASE)
        if m:
            return m.group(1) if len(m.groups()) == 1 else f"{m.group(1)}/{m.group(2)}"
    return None


def _log_gemini_quota_exhausted(e: BaseException) -> None:
    """
    Логирует событие исчерпания квоты единообразно и безопасно.

    Args:
        e: Исключение от клиента Gemini.
    Returns:
        None.
    """
    text = str(e)
    remaining = _parse_remaining_quota(text)
    snippet = text[:1200].replace("\n", " ")
    if remaining is not None:
        logger.warning(
            "Gemini quota exhausted (no retry). remaining_quota=%s | error_snippet=%s",
            remaining,
            snippet,
        )
    else:
        logger.warning(
            "Gemini quota exhausted (no retry). remaining_quota=n/a | error_snippet=%s",
            snippet,
        )


def _is_transient_rate_limit_retryable(e: BaseException) -> bool:
    """Кратковременный лимит: имеет смысл подождать и повторить (не квота RESOURCE_EXHAUSTED)."""
    if _is_quota_exhausted_no_retry(e):
        return False
    s = str(e).lower()
    if "rate limit" in s or "too many requests" in s:
        return True
    if "429" in s:
        return True
    return False


def _is_retryable_with_other_key_or_model(e: BaseException) -> bool:
    """
    Ошибки, при которых имеет смысл попробовать следующую модель из fallback.
    """
    if isinstance(e, GeminiQuotaError):
        return False
    if isinstance(e, LLMCallTimeoutError):
        return True
    s = str(e).lower()
    # Смена имени модели обрабатывается отдельно (break на следующую модель).
    if "404" in s or "not found" in s or "unsupported" in s:
        return False
    if "403" in s or "401" in s:
        return True
    if "permission_denied" in s or "permission denied" in s:
        return True
    if "denied access" in s:
        return True
    if "gemini returned empty response" in s:
        return True
    return False


def _extract_retry_seconds_from_error(error_text: str) -> float | None:
    """Подсказка API вида «Please retry in 12.5s» — нижняя граница ожидания."""
    m = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s", error_text, flags=re.IGNORECASE)
    if m:
        return max(1.0, float(m.group(1)))
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]([0-9]+)s['\"]", error_text, flags=re.IGNORECASE)
    if m:
        return max(1.0, float(m.group(1)))
    return None


# Экспоненциальный backoff при 429 / rate limit (не при исчерпании квоты).
LLM_BACKOFF_INITIAL_SEC = 5.0
LLM_BACKOFF_MAX_SEC = 120.0
LLM_RATE_LIMIT_MAX_ATTEMPTS = 5
_MAX_USER_VISIBLE_LLM_ERROR_LEN = 1200


def _backoff_delay_before_retry(
    attempt_index: int,
    error: BaseException,
    *,
    backoff_initial_sec: float,
    backoff_max_sec: float,
) -> float:
    """
    Задержка перед следующей попыткой: max(exponential, hint_from_api), затем лёгкий jitter.
    ``attempt_index`` — индекс неудачной попытки (0 после первого 429).
    """
    exp = min(backoff_max_sec, backoff_initial_sec * (2**attempt_index))
    hint = _extract_retry_seconds_from_error(str(error))
    delay = max(exp, hint) if hint is not None else exp
    jitter = delay * random.uniform(0.85, 1.15)
    return min(backoff_max_sec, jitter)


def safe_llm_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = LLM_RATE_LIMIT_MAX_ATTEMPTS,
    backoff_initial_sec: float = LLM_BACKOFF_INITIAL_SEC,
    backoff_max_sec: float = LLM_BACKOFF_MAX_SEC,
) -> T:
    """
    Выполняет ``fn`` через ``submit_llm``.

    При **исчерпании квоты** (``quotaValue`` / ``RESOURCE_EXHAUSTED``): сразу ``GeminiQuotaError``
    без повторов, в лог — ``remaining_quota`` если удалось распарсить.

    При **429 / rate limit**: до ``max_attempts`` попыток с **экспоненциальным backoff**
    (``backoff_initial_sec * 2^attempt``, не выше ``backoff_max_sec``), плюс джиттер и подсказка из ответа API.

    Выполнение ``fn`` ставится в глобальную LLM-очередь: параллельных обращений к API нет.
    """
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return submit_llm(fn)
        except Exception as e:
            last_err = e
            if _is_quota_exhausted_no_retry(e):
                _log_gemini_quota_exhausted(e)
                raise GeminiQuotaError(GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE) from e
            if _is_transient_rate_limit_retryable(e):
                if attempt < max_attempts - 1:
                    wait_sec = _backoff_delay_before_retry(
                        attempt,
                        e,
                        backoff_initial_sec=backoff_initial_sec,
                        backoff_max_sec=backoff_max_sec,
                    )
                    logger.warning(
                        "LLM rate limit / 429, exponential backoff: wait %.1fs (attempt %s/%s)",
                        wait_sec,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(wait_sec)
                    continue
                raise GeminiQuotaError(GEMINI_RATE_LIMIT_EXHAUSTED_USER_MESSAGE) from e
            raise
    raise RuntimeError(f"LLM call failed after {max_attempts} attempts") from last_err


def _extract_text_from_vertex_response(resp: Any) -> str | None:
    """
    Извлекает текст из ответа Vertex SDK, включая multi-part candidates.
    """
    # Fast path: стандартное поле `resp.text`.
    try:
        text = getattr(resp, "text", None)
    except Exception:
        text = None
    if isinstance(text, str) and text.strip():
        return text

    # Fallback: склеиваем text-части первого кандидата.
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return None
    first = candidates[0]
    content = getattr(first, "content", None)
    parts = getattr(content, "parts", None) or []
    chunks: list[str] = []
    for part in parts:
        t = getattr(part, "text", None)
        if isinstance(t, str) and t.strip():
            chunks.append(t)
    if chunks:
        return "\n".join(chunks).strip()
    return None


def _truncate_error_text(text: str, *, max_len: int = _MAX_USER_VISIBLE_LLM_ERROR_LEN) -> str:
    """Ограничивает текст ошибки до безопасной длины для Telegram."""
    compact = (text or "").replace("\n", " ").strip()
    if len(compact) <= max_len:
        return compact
    return compact[:max_len] + "…"


def gemini_generate_sync(prompt: str) -> str:
    """
    Синхронный вызов Gemini (исполняется из async-кода через ``asyncio.to_thread``).

    Параметры:
        prompt: полный текст инструкции для модели.

    Возвращает:
        Строку ответа модели (обычно текст или JSON как текст).

    Исключения:
        RuntimeError — нет Vertex-конфига, нет пакета ``google-cloud-aiplatform``, пустой ответ.
        GeminiQuotaError — квота исчерпана или лимит после повторов (см. ``safe_llm_call``).

    Логика:
        1) Создает Vertex AI клиент (`vertexai=True, project, location`).
        2) Собирает список моделей: primary + fallbacks.
        3) Для каждой модели вызывает ``safe_llm_call``.
        4) На retryable ошибках пробует следующую модель.
        5) Если все попытки исчерпаны — поднимает подробную ошибку с историей попыток.
    """
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1").strip() or "us-central1"
    if not project:
        raise RuntimeError("Missing environment variable: GOOGLE_CLOUD_PROJECT")
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import (  # type: ignore
            GenerativeModel,
            HarmBlockThreshold,
            HarmCategory,
            SafetySetting,
        )
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-cloud-aiplatform") from e
    vertexai.init(project=project, location=location)
    primary_model = (os.getenv(GEMINI_MODEL_ENV_NAME) or "").strip() or DEFAULT_GEMINI_MODEL
    fallback_models_raw = (os.getenv(GEMINI_MODEL_FALLBACKS_ENV_NAME) or "").strip()
    fallback_models = [m.strip() for m in fallback_models_raw.split(",") if m.strip()] if fallback_models_raw else []
    model_ids: list[str] = []
    for m in [primary_model, *fallback_models]:
        if m and m not in model_ids:
            model_ids.append(m)
    transports: list[tuple[str, Any]] = [("vertexai_sdk", None)]
    prompt_snapshot = str(prompt)
    context_fingerprint = hashlib.sha1(prompt_snapshot.encode("utf-8")).hexdigest()[:10]

    last_quota_error: BaseException | None = None
    last_non_quota_error: BaseException | None = None
    attempt_notes: list[str] = []

    for transport_idx, (transport_note, _transport_client) in enumerate(transports):
        if transport_idx > 0:
            _push_runtime_notice(
                f"Переключаю Gemini transport на {transport_note} после ошибок/квот предыдущего транспорта."
            )
        logger.info("Gemini transport selected: %s", transport_note)
        for model_pos, model_id in enumerate(model_ids, start=1):
            logger.info("Gemini model selection: trying model %s/%s = %s", model_pos, len(model_ids), model_id)

            def _call() -> str:
                _llm_io.info(
                    "[Gemini] model=%s transport=%s ctx=%s prompt_chars=%s\n---------- PROMPT ----------\n%s\n---------- END PROMPT ----------",
                    model_id,
                    transport_note,
                    context_fingerprint,
                    len(prompt_snapshot),
                    prompt_snapshot,
                )
                model = GenerativeModel(model_id)
                safety_settings = [
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                        threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                        threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                        threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
                    ),
                    SafetySetting(
                        category=HarmCategory.HARM_CATEGORY_HARASSMENT,
                        threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
                    ),
                ]
                resp = model.generate_content(prompt_snapshot, safety_settings=safety_settings)
                text = _extract_text_from_vertex_response(resp)
                if not text:
                    raise RuntimeError("Gemini returned empty response text.")
                _llm_io.info(
                    "[Gemini] model=%s transport=%s ctx=%s response_chars=%s\n---------- RESPONSE ----------\n%s\n---------- END RESPONSE ----------",
                    model_id,
                    transport_note,
                    context_fingerprint,
                    len(text),
                    text,
                )
                return text

            try:
                return safe_llm_call(_call)
            except GeminiQuotaError as e:
                if str(e) == GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE:
                    line = f"transport={transport_note} model={model_id}: quota exhausted"
                    logger.warning("Gemini %s", line)
                    attempt_notes.append(line)
                    last_quota_error = e
                    continue
                raise
            except Exception as e:
                err_text = str(e).lower()
                last_non_quota_error = e
                err_one_line = _truncate_error_text(str(e), max_len=500)
                note = f"transport={transport_note} model={model_id}: {err_one_line}"
                if ("404" in err_text or "not found" in err_text or "unsupported" in err_text) and model_pos < len(model_ids):
                    logger.warning(
                        "Gemini model unavailable: %s | %s — пробуем следующую модель.",
                        note,
                        e,
                    )
                    attempt_notes.append(note)
                    continue
                if _is_retryable_with_other_key_or_model(e):
                    logger.warning(
                        "Gemini ошибка (пробуем следующую модель/transport): %s | exception=%s",
                        note,
                        e,
                    )
                    attempt_notes.append(note)
                    continue
                attempt_notes.append(note)
                raise

    if last_quota_error is not None:
        tail = ""
        if attempt_notes:
            tail_text = "\n\nПопытки:\n" + "\n".join(attempt_notes[-25:])
            if len(tail_text) > 2000:
                tail_text = tail_text[:2000] + "…"
            tail = tail_text
        raise GeminiQuotaError(GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE + tail) from last_quota_error
    summary = "\n".join(attempt_notes[-30:]) if attempt_notes else "(нет деталей)"
    if len(summary) > 2800:
        summary = summary[:2800] + "…"
    raise RuntimeError(
        "Gemini (Vertex): не удалось получить ответ ни по одной модели.\n\n"
        "Попытки:\n"
        f"{summary}"
    ) from last_non_quota_error


def extract_json_array(text: str) -> list[dict[str, Any]]:
    """
    Извлекает из ответа модели **первый** JSON-массив (список объектов сцен и т.п.).

    Args:
        text: Сырой ответ модели (может включать markdown/пояснения).
    Returns:
        Первый корректный JSON-массив.
    Raises:
        ValueError: Если массив не найден/невалиден.

    Алгоритм:
    1. Убирает обёртку ```json ... ``` если есть.
    2. Пытается ``json.loads`` всего текста.
    3. Если не вышло — берёт подстроку от первой ``[`` до последней ``]`` и парсит её.
    """
    cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE))
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
    except Exception:
        pass
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model output doesn't contain a JSON array.")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("Extracted JSON is not a list.")
    return data  # type: ignore[return-value]


def extract_json_object(text: str) -> dict[str, Any]:
    """
    Извлекает из ответа модели **первый** JSON-объект (например ``{"concepts": [...]}``).

    Args:
        text: Сырой ответ модели (может включать markdown/пояснения).
    Returns:
        Первый корректный JSON-объект.
    Raises:
        ValueError: Если объект не найден/невалиден.

    Логика аналогична ``extract_json_array``, но ищет пару ``{`` ... ``}``.
    """
    cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE))
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model output doesn't contain a JSON object.")
    obj = json.loads(cleaned[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("Extracted JSON is not an object.")
    return obj
