"""
Обёртки над OpenAI / Google Gemini и разбор ответов модели.

Модели часто возвращают JSON внутри markdown-блоков ```json ... ``` или с пояснительным текстом;
функции ``extract_json_*`` вытаскивают валидный JSON из «грязного» вывода.

Все вызовы API идут через ``safe_llm_call`` → глобальная очередь ``submit_llm`` (увеличенный троттлинг).

Исчерпание **квоты** (``quotaValue``, ``RESOURCE_EXHAUSTED``): без retry, сразу ``GeminiQuotaError``.
**429 / rate limit**: экспоненциальный backoff с джиттером (и подсказкой ``retry in Xs`` из ответа, если есть).
"""

from __future__ import annotations

import json
import base64
import requests
import logging
import os
import random
import re
import contextvars
import ssl
import threading
import time
import hashlib
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from typing import Any, Callable, TypeVar

from .constants import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_CREATIVE_MODEL_FALLBACKS_ENV_NAME,
    GEMINI_EDIT_MODEL_FALLBACKS_ENV_NAME,
    GEMINI_KEY_ENV_NAME,
    GEMINI_KEYS_ENV_NAME,
    GEMINI_MODEL_FALLBACKS_ENV_NAME,
    GEMINI_MODEL_ENV_NAME,
)
from .llm_logging import get_llm_io_logger
from .llm_queue import LLMCallTimeoutError, resolve_llm_generate_timeout_sec, submit_llm
from .writing_style import with_writing_style
from .openai_requests import text_request
from .openai_usage import record_usage, operation_name
from .text_settings import GUEST_TEXT_MODEL, LEGACY_GUEST_TEXT, selected_text_model

logger = logging.getLogger("bot")
_llm_io = get_llm_io_logger()

T = TypeVar("T")
_RUNTIME_NOTICE_LOCK = threading.Lock()
_RUNTIME_NOTICES: list[str] = []
_PREFER_VERTEX_API: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "prefer_vertex_api",
    default=False,
)
_VERTEX_CACHE_LOCK = threading.Lock()
_VERTEX_MODEL_CACHE: dict[tuple[int, int, str, str, str], Any] = {}
_VERTEX_SAFETY_SETTINGS_CACHE: dict[tuple[int, int, int, int], list[Any]] = {}


class GeminiQuotaError(RuntimeError):
    """Исчерпан лимит Gemini (квота / преходящий rate limit после повторов)."""


class GeminiAccessError(RuntimeError):
    """Общий отказ доступа к endpoint Vertex, не зависящий от модели."""


def _is_html_access_denied(error: BaseException) -> bool:
    text = str(error).lower()
    return bool(re.search(r"\b403\b", text)) and (
        "<!doctype html" in text or "<html" in text
    )


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
    if isinstance(e, LLMCallTimeoutError) or _is_transient_transport_retryable(e):
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
LLM_TRANSPORT_MAX_ATTEMPTS = 3
LLM_TRANSPORT_BACKOFF_INITIAL_SEC = 2.0
LLM_TRANSPORT_BACKOFF_MAX_SEC = 20.0
_MAX_USER_VISIBLE_LLM_ERROR_LEN = 1200
GEMINI_MODEL_PROFILE_CREATIVE = "creative"
GEMINI_MODEL_PROFILE_EDIT = "edit"
TEXT_MODEL_PROFILE_SHORT = "short"
_DEFAULT_CREATIVE_MODEL_FALLBACKS = ("gemini-2.5-pro", "gemini-2.5-flash-lite")
_DEFAULT_EDIT_MODEL_FALLBACKS = ("gemini-2.5-flash-lite", "gemini-2.5-pro")


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


def _is_transient_transport_retryable(e: BaseException) -> bool:
    """Network/SSL transport glitches where retrying the same LLM request is reasonable."""
    current: BaseException | None = e
    while current is not None:
        if isinstance(current, (TimeoutError, ConnectionError, ssl.SSLError, RequestsConnectionError, RequestsTimeout)):
            return True
        current = current.__cause__ if isinstance(current.__cause__, BaseException) else None

    s = str(e).lower()
    retry_markers = (
        "ssl",
        "ssleoferror",
        "unexpected_eof",
        "eof occurred in violation of protocol",
        "max retries exceeded",
        "connection reset",
        "connection aborted",
        "remote end closed connection",
        "temporarily unavailable",
        "timed out",
        "timeout",
    )
    service_markers = (
        "aiplatform.googleapis.com",
        "generative language",
        "google.api_core",
        "httpsconnectionpool",
    )
    return any(marker in s for marker in retry_markers) and (
        any(marker in s for marker in service_markers)
        or "generatecontent" in s
        or isinstance(e, (TimeoutError, ConnectionError))
    )


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
    transport_attempt = 0
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
            if _is_transient_transport_retryable(e):
                transport_attempt += 1
                if transport_attempt < LLM_TRANSPORT_MAX_ATTEMPTS:
                    wait_sec = _backoff_delay_before_retry(
                        transport_attempt - 1,
                        e,
                        backoff_initial_sec=LLM_TRANSPORT_BACKOFF_INITIAL_SEC,
                        backoff_max_sec=LLM_TRANSPORT_BACKOFF_MAX_SEC,
                    )
                    logger.warning(
                        "LLM transport error, retrying same request: wait %.1fs (attempt %s/%s) err=%s",
                        wait_sec,
                        transport_attempt,
                        LLM_TRANSPORT_MAX_ATTEMPTS,
                        _truncate_error_text(str(e), max_len=500),
                    )
                    time.sleep(wait_sec)
                    continue
            raise
    raise RuntimeError(f"LLM call failed after {max_attempts} attempts") from last_err


def _vertex_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _vertex_value(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value)


def _vertex_to_mapping(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        return obj
    to_dict = _vertex_attr(obj, "to_dict")
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:
            return None
        if isinstance(data, dict):
            return data
    return None


def _extract_text_from_vertex_mapping(data: dict[str, Any]) -> str | None:
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                chunks.append(t)
        if chunks:
            break
    if chunks:
        return "\n".join(chunks).strip()
    return None


def _summarize_vertex_safety_ratings(ratings: Any) -> str:
    out: list[str] = []
    for rating in ratings or []:
        category = _vertex_value(_vertex_attr(rating, "category"))
        probability = _vertex_value(_vertex_attr(rating, "probability"))
        blocked = _vertex_attr(rating, "blocked")
        bits = [bit for bit in (category, probability) if bit]
        if blocked is not None:
            bits.append(f"blocked={blocked}")
        if bits:
            out.append("/".join(bits))
    return ", ".join(out[:8])


def _summarize_empty_vertex_response(resp: Any) -> str:
    """
    Vertex SDK can return no text while still carrying a finish/block reason.
    Make that reason visible so fallback failures are actionable.
    """
    details: list[str] = []
    data = _vertex_to_mapping(resp)
    if data is not None:
        prompt_feedback_data = data.get("prompt_feedback") or data.get("promptFeedback") or {}
        if isinstance(prompt_feedback_data, dict):
            block_reason = _vertex_value(
                prompt_feedback_data.get("block_reason") or prompt_feedback_data.get("blockReason")
            )
            block_message = _vertex_value(
                prompt_feedback_data.get("block_reason_message") or prompt_feedback_data.get("blockReasonMessage")
            )
            if block_reason:
                details.append(f"prompt_block_reason={block_reason}")
            if block_message:
                details.append(f"prompt_block_message={block_message}")
        candidates_data = data.get("candidates") or []
        details.append(f"candidates={len(candidates_data)}")
        if candidates_data and isinstance(candidates_data[0], dict):
            first_data = candidates_data[0]
            finish_reason = _vertex_value(first_data.get("finish_reason") or first_data.get("finishReason"))
            finish_message = _vertex_value(first_data.get("finish_message") or first_data.get("finishMessage"))
            if finish_reason:
                details.append(f"finish_reason={finish_reason}")
            if finish_message:
                details.append(f"finish_message={finish_message}")
            content_data = first_data.get("content") or {}
            parts_data = content_data.get("parts") if isinstance(content_data, dict) else []
            details.append(f"parts={len(parts_data or [])}")
        return "; ".join(details)

    prompt_feedback = _vertex_attr(resp, "prompt_feedback")
    if prompt_feedback is not None:
        block_reason = _vertex_value(_vertex_attr(prompt_feedback, "block_reason"))
        block_message = _vertex_value(_vertex_attr(prompt_feedback, "block_reason_message"))
        if block_reason:
            details.append(f"prompt_block_reason={block_reason}")
        if block_message:
            details.append(f"prompt_block_message={block_message}")
        safety = _summarize_vertex_safety_ratings(_vertex_attr(prompt_feedback, "safety_ratings"))
        if safety:
            details.append(f"prompt_safety={safety}")

    candidates = _vertex_attr(resp, "candidates") or []
    details.append(f"candidates={len(candidates)}")
    if candidates:
        first = candidates[0]
        finish_reason = _vertex_value(_vertex_attr(first, "finish_reason"))
        finish_message = _vertex_value(_vertex_attr(first, "finish_message"))
        if finish_reason:
            details.append(f"finish_reason={finish_reason}")
        if finish_message:
            details.append(f"finish_message={finish_message}")
        safety = _summarize_vertex_safety_ratings(_vertex_attr(first, "safety_ratings"))
        if safety:
            details.append(f"safety={safety}")
        content = _vertex_attr(first, "content")
        parts = _vertex_attr(content, "parts") or []
        details.append(f"parts={len(parts)}")
        non_text_parts = [
            name
            for part in parts
            for name in ("inline_data", "file_data", "function_call", "function_response")
            if _vertex_attr(part, name) is not None
        ]
        if non_text_parts:
            details.append("non_text_parts=" + ",".join(non_text_parts[:8]))
    return "; ".join(details)


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
    data = _vertex_to_mapping(resp)
    if data is not None:
        mapped_text = _extract_text_from_vertex_mapping(data)
        if mapped_text:
            return mapped_text

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


def _split_model_list(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _model_ids_for_profile(profile: str | None) -> list[str]:
    primary_model = (os.getenv(GEMINI_MODEL_ENV_NAME) or "").strip() or DEFAULT_GEMINI_MODEL
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile == GEMINI_MODEL_PROFILE_CREATIVE:
        fallbacks = _split_model_list(os.getenv(GEMINI_CREATIVE_MODEL_FALLBACKS_ENV_NAME))
        if not fallbacks:
            fallbacks = list(_DEFAULT_CREATIVE_MODEL_FALLBACKS)
    elif normalized_profile in {GEMINI_MODEL_PROFILE_EDIT, TEXT_MODEL_PROFILE_SHORT}:
        fallbacks = _split_model_list(os.getenv(GEMINI_EDIT_MODEL_FALLBACKS_ENV_NAME))
        if not fallbacks:
            fallbacks = list(_DEFAULT_EDIT_MODEL_FALLBACKS)
    else:
        fallbacks = _split_model_list(os.getenv(GEMINI_MODEL_FALLBACKS_ENV_NAME))

    model_ids: list[str] = []
    for model in [primary_model, *fallbacks]:
        if model and model not in model_ids:
            model_ids.append(model)
    return model_ids


def _guess_image_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _guess_media_mime_type(data: bytes) -> str:
    if data.startswith(b"\x00\x00\x00") and data[4:8] == b"ftyp":
        return "video/mp4"
    return _guess_image_mime_type(data)


def _dedupe_binary_payloads(payloads: list[bytes] | None) -> list[bytes]:
    """
    Preserves order while dropping exact duplicate media payloads before upload.
    """
    out: list[bytes] = []
    seen: set[str] = set()
    for payload in payloads or []:
        if not isinstance(payload, bytes) or not payload:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        out.append(payload)
    return out


def _load_vertex_generative_sdk() -> tuple[Any, Any]:
    try:
        import vertexai  # type: ignore
        from vertexai import generative_models as vertex_generative_models  # type: ignore
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-cloud-aiplatform") from e
    return vertexai, vertex_generative_models


def uses_gemini_developer_api() -> bool:
    return (os.getenv("GEMINI_BACKEND") or "vertex").strip().lower() == "developer"


class _GeminiDeveloperModel:
    """Send Gemini-native media checks to the API-key project."""

    def __init__(self, model_id: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model_id):
            raise ValueError("Invalid Gemini model ID")
        self.model_id = model_id

    def generate_content(self, contents, *, generation_config=None, safety_settings=None):
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("Missing environment variable: GEMINI_API_KEY")
        parts = contents if isinstance(contents, list) else [contents]
        payload = {"contents": [{"role": "user", "parts": [
            {"text": part} if isinstance(part, str) else part.to_dict() for part in parts
        ]}]}
        if safety_settings:
            payload["safetySettings"] = [setting.to_dict() for setting in safety_settings]
        if generation_config:
            payload["generationConfig"] = generation_config
        with requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_id}:generateContent",
            headers={"x-goog-api-key": key}, json=payload,
            timeout=(15, resolve_llm_generate_timeout_sec()), allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                try:
                    message = str(response.json()["error"]["message"]).replace(key, "[redacted]")
                except (KeyError, ValueError, TypeError):
                    message = ""
                raise RuntimeError(f"Gemini API HTTP {response.status_code} {message[:1000]}")
            return response.json()


def get_vertex_generative_model(project: str, location: str, model_id: str) -> Any:
    """Select the configured Gemini backend, caching Vertex models per project."""
    if uses_gemini_developer_api():
        return _GeminiDeveloperModel(model_id)
    vertexai, vertex_generative_models = _load_vertex_generative_sdk()
    GenerativeModel = getattr(vertex_generative_models, "GenerativeModel", None)
    if GenerativeModel is None:
        raise RuntimeError("Vertex Gemini SDK does not expose GenerativeModel.")
    cache_key = (id(vertex_generative_models), id(GenerativeModel), project, location, model_id)
    with _VERTEX_CACHE_LOCK:
        model = _VERTEX_MODEL_CACHE.get(cache_key)
        if model is None:
            vertexai.init(project=project, location=location)
            model = GenerativeModel(model_id)
            _VERTEX_MODEL_CACHE[cache_key] = model
        return model


def get_vertex_safety_settings() -> list[Any]:
    """
    Builds Vertex Gemini safety settings once per SDK class set.
    """
    try:
        from vertexai import generative_models as vertex_generative_models  # type: ignore
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-cloud-aiplatform") from e
    HarmBlockThreshold = getattr(vertex_generative_models, "HarmBlockThreshold", None)
    HarmCategory = getattr(vertex_generative_models, "HarmCategory", None)
    SafetySetting = getattr(vertex_generative_models, "SafetySetting", None)
    if HarmBlockThreshold is None or HarmCategory is None or SafetySetting is None:
        raise RuntimeError("Vertex Gemini SDK does not expose safety setting classes.")
    cache_key = (id(vertex_generative_models), id(SafetySetting), id(HarmCategory), id(HarmBlockThreshold))
    with _VERTEX_CACHE_LOCK:
        cached = _VERTEX_SAFETY_SETTINGS_CACHE.get(cache_key)
        if cached is not None:
            return cached
        settings = [
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
        _VERTEX_SAFETY_SETTINGS_CACHE[cache_key] = settings
        return settings


def vertex_part_from_text(project: str, location: str, text: str) -> Any:
    _vertexai, vertex_generative_models = _load_vertex_generative_sdk()
    Part = getattr(vertex_generative_models, "Part", None)
    if Part is None:
        raise RuntimeError("Vertex Gemini SDK does not support image parts in this version.")
    return Part.from_text(text)


def vertex_part_from_data(project: str, location: str, data: bytes) -> Any:
    _vertexai, vertex_generative_models = _load_vertex_generative_sdk()
    Part = getattr(vertex_generative_models, "Part", None)
    if Part is None:
        raise RuntimeError("Vertex Gemini SDK does not support image parts in this version.")
    return Part.from_data(data=data, mime_type=_guess_media_mime_type(data))


def text_llm_provider() -> str:
    """Select text backend; preserve Gemini for installations without an OpenAI key."""
    if selected_text_model():
        return "openai"
    provider = (os.getenv("TEXT_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        provider = "openai" if (os.getenv("OPENAI_API_KEY") or "").strip() else "gemini"
    if provider not in {"openai", "gemini"}:
        raise RuntimeError("TEXT_LLM_PROVIDER must be openai or gemini")
    return provider


def openai_text_model(model_profile: str | None = None) -> str:
    """Short operations have their own model; admin selection controls complex work."""
    if LEGACY_GUEST_TEXT.get():
        return GUEST_TEXT_MODEL
    if model_profile == TEXT_MODEL_PROFILE_SHORT:
        return (os.getenv("OPENAI_SHORT_MODEL") or "").strip() or "gpt-6-luna"
    model = (os.getenv("OPENAI_MODEL") or "").strip() or "gpt-6.2-sol"
    if model_profile in {GEMINI_MODEL_PROFILE_CREATIVE, GEMINI_MODEL_PROFILE_EDIT}:
        model = (os.getenv(f"OPENAI_{model_profile.upper()}_MODEL") or "").strip() or model
    return selected_text_model() or model


def openai_generate_sync(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    rate_limit_max_attempts: int | None = None,
    model_profile: str | None = None,
    response_schema: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Generate text through Responses API and the shared bounded retry queue."""
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing environment variable: OPENAI_API_KEY")
    model = openai_text_model(model_profile)
    content: list[dict[str, Any]] = []
    operation = operation_name()
    for payload in _dedupe_binary_payloads(images):
        mime = _guess_media_mime_type(payload)
        if not mime.startswith("image/"):
            raise ValueError("OpenAI text generation accepts images, not video payloads")
        encoded = base64.b64encode(payload).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})

    payload = text_request(prompt, model=model, profile=model_profile, images=content,
                           response_schema=response_schema, max_output_tokens=max_output_tokens)

    def _call() -> str:
        logger.info("OpenAI text generation model=%s profile=%s", model, model_profile)
        with requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            allow_redirects=False,
            timeout=(15, 150),
        ) as response:
            if not response.ok:
                # Never expose upstream messages: auth errors may echo a credential.
                if response.status_code == 429:
                    try:
                        code = response.json().get("error", {}).get("code")
                    except (ValueError, AttributeError):
                        code = None
                    if code == "insufficient_quota":
                        raise GeminiQuotaError("OpenAI: исчерпан баланс или лимит API. Пополните баланс или переключите TEXT_LLM_PROVIDER=gemini.")
                if response.status_code >= 500:
                    raise ConnectionError(f"OpenAI HTTP {response.status_code}")
                raise RuntimeError(f"OpenAI HTTP {response.status_code}")
            data = response.json()
        record_usage(event_id=data.get("id"), model=data.get("model") or model,
                     kind="text", usage=data.get("usage"), profile=model_profile,
                     operation=operation, status=data.get("status", "unknown"), prompt=prompt)
        if data.get("status") != "completed":
            raise RuntimeError("OpenAI did not complete the response")
        text = "\n".join(
            part["text"]
            for item in data.get("output", []) if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text" and isinstance(part.get("text"), str)
        ).strip()
        if not text:
            raise RuntimeError("OpenAI returned an empty response or refusal")
        return text

    kwargs = {} if rate_limit_max_attempts is None else {"max_attempts": rate_limit_max_attempts}
    return safe_llm_call(_call, **kwargs)


def gemini_generate_sync(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    rate_limit_max_attempts: int | None = None,
    model_profile: str | None = None,
    editorial_plain_text: bool = False,
    response_schema: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Compatibility entry point for all text generation, routed by TEXT_LLM_PROVIDER.

    Gemini remains selectable explicitly; errors never silently switch providers.
    Both backends attach the full writing policy to the generation request.
    editorial_plain_text is retained for callers; it no longer starts a second
    model call that rewrites the generated result.
    """
    kwargs = dict(images=images, rate_limit_max_attempts=rate_limit_max_attempts, model_profile=model_profile)
    if text_llm_provider() == "openai":
        if response_schema is not None:
            kwargs['response_schema'] = response_schema
        if max_output_tokens is not None:
            kwargs['max_output_tokens'] = max_output_tokens
        return openai_generate_sync(prompt, **kwargs)
    # Gemini keeps its existing request format; callers still validate the JSON.
    return _gemini_generate_sync(prompt, **kwargs)


def _gemini_generate_sync(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    rate_limit_max_attempts: int | None = None,
    model_profile: str | None = None,
) -> str:
    """
    Синхронный вызов Gemini (исполняется из async-кода через ``asyncio.to_thread``).

    Параметры:
        prompt: полный текст инструкции для модели.
        rate_limit_max_attempts: override for 429/rate-limit retry attempts.
        model_profile: optional routing profile. ``creative`` uses flash -> pro -> lite;
            ``edit`` uses flash -> lite -> pro. Empty uses the legacy global fallback list.

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
    if not project and not uses_gemini_developer_api():
        raise RuntimeError("Missing environment variable: GOOGLE_CLOUD_PROJECT")
    _load_vertex_generative_sdk()
    model_ids = _model_ids_for_profile(model_profile)
    transport = "gemini_api" if uses_gemini_developer_api() else "vertexai_sdk"
    transports: list[tuple[str, Any]] = [(transport, None)]
    prompt_snapshot = with_writing_style(prompt)
    image_payloads = _dedupe_binary_payloads(images)
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

            disable_thinking_after_disconnect = False

            def _call() -> str:
                nonlocal disable_thinking_after_disconnect
                _llm_io.info(
                    "[Gemini] model=%s transport=%s ctx=%s prompt_chars=%s\n---------- PROMPT ----------\n%s\n---------- END PROMPT ----------",
                    model_id,
                    transport_note,
                    context_fingerprint,
                    len(prompt_snapshot),
                    prompt_snapshot,
                )
                model = get_vertex_generative_model(project, location, model_id)
                safety_settings = get_vertex_safety_settings()
                request: Any = prompt_snapshot
                if image_payloads:
                    request = [vertex_part_from_text(project, location, prompt_snapshot)]
                    request.extend(vertex_part_from_data(project, location, image) for image in image_payloads)
                generation_kwargs: dict[str, Any] = {}
                if disable_thinking_after_disconnect:
                    generation_kwargs["generation_config"] = {"thinking_config": {"thinking_budget": 0}}
                try:
                    resp = model.generate_content(request, safety_settings=safety_settings, **generation_kwargs)
                except Exception as error:
                    if model_id == "gemini-2.5-flash" and _is_transient_transport_retryable(error):
                        disable_thinking_after_disconnect = True
                        logger.warning("Gemini Flash transport failed; next attempt will use thinking_budget=0")
                    raise
                text = _extract_text_from_vertex_response(resp)
                if not text:
                    details = _summarize_empty_vertex_response(resp)
                    suffix = f" ({details})" if details else ""
                    raise RuntimeError(f"Gemini returned empty response text{suffix}.")
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
                kwargs = {}
                if rate_limit_max_attempts is not None:
                    kwargs["max_attempts"] = max(1, int(rate_limit_max_attempts))
                return safe_llm_call(_call, **kwargs)
            except GeminiQuotaError as e:
                line = f"transport={transport_note} model={model_id}: quota/rate limit exhausted"
                logger.warning("Gemini %s", line)
                attempt_notes.append(line)
                last_quota_error = e
                continue
            except Exception as e:
                if _is_html_access_denied(e):
                    logger.warning("Vertex endpoint returned HTML 403; stopping model fallback")
                    raise GeminiAccessError(
                        "Google Vertex AI отклонил запрос: HTTP 403 (отказ доступа). "
                        "Сервис вернул HTML вместо ответа API; смена модели не поможет. "
                        "Нужно проверить сетевой доступ сервера к Google Vertex AI "
                        "и ограничения доступа в Google Cloud. "
                        "Точная причина в ответе Google не указана."
                    ) from e
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
        f"Gemini ({transport}): не удалось получить ответ ни по одной модели.\n\n"
        "Попытки:\n"
        f"{summary}"
    ) from last_non_quota_error


def _strip_json_markdown_fence(text: str) -> str:
    return re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE))


def _repair_unquoted_pause_tokens_in_json_strings(text: str) -> str:
    """
    Repairs a common LLM slip in scene JSON:
    `"text": "first phrase" [пауза] "second phrase"`.

    The pause token is valid for our spoken text, but it must stay inside the
    JSON string. Keep this targeted to text-like fields so unrelated arrays are
    not rewritten.
    """
    field_names = (
        "text",
        "speech",
        "recipe_step",
        "video_prompt",
        "start_frame_prompt",
        "start_frame_prompt_ru",
        "end_frame_prompt",
        "end_frame_prompt_ru",
    )
    fields = "|".join(re.escape(name) for name in field_names)
    pattern = re.compile(
        rf'("(?P<field>{fields})"\s*:\s*)"(?P<left>(?:[^"\\]|\\.)*)"\s*'
        r"(?P<pause>\[\s*пауза\s*\])\s*"
        r'"(?P<right>(?:[^"\\]|\\.)*)"',
        flags=re.IGNORECASE,
    )

    repaired = text
    while True:
        updated = pattern.sub(
            lambda m: (
                f'{m.group(1)}"'
                f'{m.group("left").rstrip()} [пауза] {m.group("right").lstrip()}'
                f'"'
            ),
            repaired,
        )
        if updated == repaired:
            return repaired
        repaired = updated


def _extract_first_json_value(cleaned: str, start_char: str, expected_type: type) -> Any:
    decoder = json.JSONDecoder()
    for match in re.finditer(re.escape(start_char), cleaned):
        candidate = cleaned[match.start() :]
        try:
            value, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, expected_type):
            return value
    raise ValueError(f"Model output doesn't contain a JSON {expected_type.__name__}.")


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
    3. Если не вышло — декодирует первый завершённый JSON-массив из текста.
    """
    cleaned = _strip_json_markdown_fence(text)
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
    except Exception:
        pass
    repaired = _repair_unquoted_pause_tokens_in_json_strings(cleaned)
    if repaired != cleaned:
        try:
            data = json.loads(repaired)
            if isinstance(data, list):
                return data  # type: ignore[return-value]
        except Exception:
            pass
    data = _extract_first_json_value(cleaned, "[", list)
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

    Логика аналогична ``extract_json_array``, но ищет первый завершённый объект.
    """
    cleaned = _strip_json_markdown_fence(text)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    obj = _extract_first_json_value(cleaned, "{", dict)
    return obj
