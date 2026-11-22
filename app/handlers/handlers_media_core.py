"""Media/reference helper functions extracted from handlers."""

import asyncio
import base64
import contextvars
import hashlib
import io
import os
import re
from typing import Any, Awaitable, Callable, TypeVar

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from ..assets import ASSETS
from ..character_catalog import sanitize_character_names_and_keys
from ..constants import ELEVENLABS_VOICE_ID_ENV_NAME, STATE_READY_FOR_PRODUCTION
from ..generation_modes import SYNTAX_PROVIDER_VEO
from ..llm_utils import (
    GEMINI_MODEL_PROFILE_EDIT,
    GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE,
    GeminiQuotaError,
    gemini_generate_sync,
    pop_runtime_notices,
    reset_prefer_vertex_api,
    set_prefer_vertex_api,
)
from ..scene_utils import normalize_recipe_steps, extract_recipe_steps_text
from ..pipeline import (
    GRANDMA_VOICE_LOCK_FULL,
    VIDEO_PROMPT_VISUAL_SOURCE_GENERATION_RULE,
    append_veo3_video_framing_constraints,
    _NO_SUBTITLES_MUSIC_RULE,
    _ensure_named_grandma_voice_lock,
    VOICE_CONTINUITY_RULE,
)
from ..pipeline_llm_steps import RUSSIAN_YO_RULE
from ..multilingual import language_name, normalize_language_code
from .handlers_media_core_recipe import (
    _normalize_user_recipe_steps,
    _is_obvious_placeholder_recipe,
    _try_parse_direct_recipe_from_user_text,
)

from ..state import SESSIONS
from ..syntax_fallback_output import video_bytes_from_fallback_result
from ..syntax_video_fallback import BatchFallbackRequest, try_generate_via_syntax, try_generate_via_syntax_batch
from ..vertex_images import generate_image_vertex
from ..video_generation import generate_video_bytes
from .handlers_keyboards import safe_reply_text, safe_send_message

logger = __import__('logging').getLogger("bot")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VIDEO_HIGH_LOAD_RETRY_DELAY_SEC = 15
VIDEO_HIGH_LOAD_MAX_ATTEMPTS = 3
SYNTAX_FALLBACK_DEFAULT_TIMEOUT_SEC = 15 * 60
SYNTAX_FALLBACK_TIMEOUT_GRACE_SEC = 30
SYNTAX_FALLBACK_DOWNLOAD_TIMEOUT_SEC = 300
SERVICE_UNAVAILABLE_RETRY_COUNT = 20
SERVICE_UNAVAILABLE_RETRY_DELAY_SEC = 15
AI_RATE_LIMIT_RETRY_COUNT = 3
AI_RATE_LIMIT_RETRY_DELAY_SEC = 60
SCENE_MEDIA_SUBSTEP_START = "start_prompt"
SCENE_MEDIA_SUBSTEP_END = "end_prompt"
SCENE_MEDIA_SUBSTEP_VIDEO = "video_prompt"
_DEBOUNCE_SEQ: dict[int, int] = {}
T = TypeVar("T")
_CHARACTER_ALIASES: dict[str, str] = {
    "grandma": "grandma",
    "бабушка": "grandma",
    "babushka": "grandma",
    "cat": "cat",
    "кот": "cat",
    "тимоша": "cat",
    "тимофей": "cat",
    "koshka": "cat",
    "zaya": "zaya",
    "white cat": "zaya",
    "white cartoon cat": "zaya",
    "зая": "zaya",
    "заю": "zaya",
    "заи": "zaya",
    "белая кошка": "zaya",
    "белая кошечка": "zaya",
    "markiz": "markiz",
    "маркиз": "markiz",
    "маркиза": "markiz",
    "черно-белый кот": "markiz",
    "чёрно-белый кот": "markiz",
    "granddaughter": "granddaughter",
    "внучка": "granddaughter",
    "daughter": "daughter",
    "дочь": "daughter",
    "grandson": "grandson",
    "внук": "grandson",
    "grandfather": "grandfather",
    "дедушка": "grandfather",
    "dog": "dog",
    "собака": "dog",
}

_LOCATION_ALIASES: dict[str, str] = {
    "kitchen": "kitchen",
    "кухня": "kitchen",
    "sink": "sink",
    "мойка": "sink",
    "раковина": "sink",
    "stove": "stove",
    "плита": "stove",
    "serving": "serving",
    "подача": "serving",
    "fridge": "fridge",
    "холодильник": "fridge",
    "cutting": "cutting",
    "разделка": "cutting",
    "разделочная зона": "cutting",
}
_VOICE_RULES_BY_LANGUAGE = {
    "ru": "all spoken phrases and voiceover are in Russian language only",
    "en": "all spoken phrases and voiceover are in English language only",
}
_NON_REALISM_RULE = "visual style is strictly non-photorealistic, non-realistic, and cartoon; no realism or photorealism"
_CARTOON_CHARACTER_RUNTIME_RULE = (
    "grandmother is a cartoon grandmother; every human or animal character is cartoon; "
    "the cat is a cartoon cat"
)
_VIDEO_PROMPT_VISUAL_SOURCE_RULE = (
    "visual_source_lock: use the provided start/end frame or uploaded source image as the complete visual source of truth; "
    "describe only motion, actions, speech, lipsync, reactions, and camera movement; "
    "do not describe location, background, static dish layout, serving composition, or ingredient inventory; "
    "do not introduce new food, garnish, topping, side dish, tableware, decor, or scenery"
)
_VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG = {
    "ru": "Russian language",
    "en": "English language",
}
_LAST_VIDEO_PROVIDER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "last_video_provider",
    default=None,
)


def _voice_lock_for_language(target_language: str) -> str:
    """Return language-adjusted voice lock text for grandma."""
    lang = (target_language or "ru").strip().lower()
    token = _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG.get(lang, _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG["en"])
    accent = "neutral Russian accent" if lang == "ru" else f"neutral {language_name(lang)} accent"
    return GRANDMA_VOICE_LOCK_FULL.replace("Russian language", token).replace("neutral Russian accent", accent)


def get_last_video_provider() -> str | None:
    return _LAST_VIDEO_PROVIDER.get()


def _sanitize_video_prompt_language_references(video_prompt: str, target_language: str) -> str:
    """Remove stale speech-language instructions after prompt reuse or edits."""
    prompt = str(video_prompt or "").strip()
    lang = normalize_language_code(target_language) or "ru"
    label = language_name(lang)
    token = _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG.get(lang, f"{label} language")
    stale_lang = "en" if lang == "ru" else "ru"
    stale_label = language_name(stale_lang)
    stale_token = _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG.get(stale_lang, f"{stale_label} language")
    replacements = {
        stale_token: token,
        f"{stale_label} line": f"{label} line",
        f"spoken {stale_label} speech": f"spoken {label} speech",
        f"{stale_label} speech": f"{label} speech",
        f"neutral {stale_label} accent": f"neutral {label} accent",
        _VOICE_RULES_BY_LANGUAGE.get(stale_lang, ""): (
            _VOICE_RULES_BY_LANGUAGE.get(lang, f"all spoken phrases and voiceover are in {label} language only")
        ),
    }
    out = prompt
    for source, target in replacements.items():
        out = out.replace(source, target)
    return out


def enforce_video_prompt_visual_source_rule(video_prompt: str) -> str:
    """Strip internal visual-source instructions before sending/storing runtime video prompts."""
    vp = (video_prompt or "").strip()
    if not vp:
        return ""
    out = vp
    escaped_rule = re.escape(VIDEO_PROMPT_VISUAL_SOURCE_GENERATION_RULE)
    out = re.sub(escaped_rule, "", out, flags=re.IGNORECASE)
    visual_source_patterns = (
        r"\s*\.?\s*visual_source_lock:\s*use the provided start/end frame or uploaded source image as the complete visual source of truth;\s*describe only motion, actions, speech, lipsync, reactions, and camera movement;\s*do not describe location, background, static dish layout, serving composition, or ingredient inventory;\s*do not introduce new food, garnish, topping, side dish, tableware, decor, or scenery\.?",
        r"\s*\.?\s*visual_source_lock:\s*[^\n]*",
    )
    for pattern in visual_source_patterns:
        out = re.sub(pattern, "", out, flags=re.IGNORECASE).strip()
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"\s+\.", ".", out).strip()
    return out


def _normalize_character_key(value: str) -> str:
    key = (value or "").strip().lower()
    return _CHARACTER_ALIASES.get(key, key)


def _normalize_location_key(value: str) -> str:
    key = (value or "").strip().lower()
    return _LOCATION_ALIASES.get(key, key)


def _mask_secret_tail(value: str) -> str:
    if not value:
        return "-"
    tail = value[-4:] if len(value) >= 4 else value
    return f"***{tail}"


def _normalize_veo_duration_sec(raw_seconds: int) -> int:
    """Нормализует длительность под ограничения `veo3_fast` (6/8 сек)."""
    allowed = (6, 8)
    return min(allowed, key=lambda x: abs(x - int(raw_seconds)))


def _parse_video_command_args(raw_text: str) -> tuple[int, str]:
    """
    Разбирает `/video <seconds> <prompt>`.

    Returns:
        (duration_sec, prompt), где duration_sec нормализован к 6/8.
    Raises:
        ValueError: если формат команды неверный.
    """
    text = (raw_text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        raise ValueError('Формат: /video <секунды> <промпт>. Пример: /video 6 Бабушка замешивает тесто.')
    try:
        sec_raw = int(parts[1])
    except Exception as e:
        raise ValueError("Секунды должны быть числом (например 6 или 8).") from e
    if sec_raw <= 0:
        raise ValueError("Секунды должны быть положительным числом.")
    prompt = parts[2].strip()
    if not prompt:
        raise ValueError("После секунд укажите video prompt.")
    return _normalize_veo_duration_sec(sec_raw), prompt


def _bump_debounce_generation(chat_id: int) -> None:
    """Сбрасывает ожидающий debounce on_text (например после /generate)."""
    _DEBOUNCE_SEQ[chat_id] = _DEBOUNCE_SEQ.get(chat_id, 0) + 1


def _user_error_message(e: Exception, *, domain: str = "gemini") -> str:
    """
    Maps low-level exceptions to user-facing messages.

    Args:
        e: original exception.
        domain: logical source of error (`gemini`, `video`, ...).
    """
    if isinstance(e, GeminiQuotaError):
        return str(e)
    text = str(e)
    low = text.lower()
    if "RESOURCE_EXHAUSTED" in text or "quotaValue" in text:
        if domain == "video":
            return "Лимит Vertex Video API временно превышен. Подождите немного и повторите запрос."
        return GEMINI_QUOTA_EXHAUSTED_USER_MESSAGE
    if "429" in text:
        if domain == "video":
            return "Лимит Vertex Video API временно превышен (429). Подождите немного и повторите запрос."
        return "Лимит Gemini временно превышен. Подождите немного и повторите запрос."
    if domain == "video" and "404" in text and (
        "aiplatform.googleapis.com" in low
        or "predictlongrunning" in low
        or "<!doctype html>" in low
        or "<html" in low
    ):
        if "locations/global" in low or "global-aiplatform.googleapis.com" in low:
            return (
                "Vertex Video API вернул 404: Veo video endpoint недоступен в регионе global. "
                "Для видео используйте us-central1 или задайте VERTEX_VIDEO_LOCATION."
            )
        return (
            "Vertex Video API вернул 404: проверьте регион и доступность модели Veo в этом проекте."
        )
    if "403" in text and ("permission_denied" in low or "permission denied" in low):
        if domain == "video":
            return (
                "Нет прав на вызов Vertex Video API (403). Проверьте GOOGLE_CLOUD_PROJECT, регион, "
                "включение Vertex AI API и доступ к модели."
            )
        if "denied access" in low or "contact support" in low:
            return (
                "Google отклонил доступ к Gemini для этого проекта (403 PERMISSION_DENIED: denied access). "
                "Это на стороне аккаунта/проекта: проверьте Vertex AI в Cloud Console "
                "(console.cloud.google.com) "
                "(включён Gemini API, биллинг, регион, ограничения организации). "
                "Если в консоли всё ок — напишите в поддержку Google Cloud."
            )
        return (
            "Нет прав на вызов Gemini (403). Проверьте API-ключ, проект GCP и что Gemini API включён."
        )
    compact = text.replace("\n", " ").strip()
    if len(compact) > 1200:
        return compact[:1200] + "…"
    return compact


def _is_service_unavailable_retryable(e: Exception) -> bool:
    text = str(e).lower()
    return "503" in text or "unavailable" in text or "high demand" in text


def _is_ai_rate_limit_retryable(e: Exception) -> bool:
    if isinstance(e, GeminiQuotaError):
        return True
    text = str(e)
    low = text.lower()
    return (
        "429" in text
        or "resource_exhausted" in low
        or "quota" in low
        or "rate limit" in low
        or "too many requests" in low
        or "слишком много запросов" in low
    )


class ServiceUnavailableRetriesExhaustedError(RuntimeError):
    """Все retry временных ошибок модели исчерпаны."""


async def _send_chat_action_safely(
    context: ContextTypes.DEFAULT_TYPE | None,
    *,
    chat_id: int | None,
    action_name: str,
) -> None:
    """Best-effort typing indicator; Telegram hiccups must not abort generation."""
    if context is None or chat_id is None:
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning("%s typing indicator failed, continuing: %s", action_name, e)


async def _send_llm_retry_notice_safely(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE | None,
    *,
    chat_id: int | None,
    text: str,
) -> None:
    """Best-effort user notice for long LLM retries, even outside message handlers."""
    try:
        if update is not None and update.message is not None:
            await safe_reply_text(update.message, text)
            return
        if context is not None and chat_id is not None:
            await safe_send_message(context.bot, chat_id=chat_id, text=text)
    except Exception as e:
        logger.warning("LLM retry notice failed, continuing: %s", e)


async def _call_with_503_retries(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: int | None,
    action_name: str,
    op: Callable[[], Awaitable[T]],
    *,
    retry_ai_rate_limit: bool = False,
) -> T:
    """
    Выполняет LLM-операцию с retry на временные сбои:
    - 503/UNAVAILABLE/high demand: короткие повторы;
    - 429/quota/rate limit: без внешних минутных повторов по умолчанию,
      чтобы не складывать retry поверх retry самого провайдера.
    """
    session = SESSIONS.get(chat_id) or {}
    prefer_vertex = bool(session.get("prefer_vertex_api"))
    service_attempt = 0
    rate_attempt = 0
    while True:
        try:
            await _send_chat_action_safely(context, chat_id=chat_id, action_name=action_name)
            token = set_prefer_vertex_api(prefer_vertex)
            try:
                result = await op()
            finally:
                reset_prefer_vertex_api(token)
            for note in pop_runtime_notices():
                await _send_llm_retry_notice_safely(update, context, chat_id=chat_id, text=note)
            return result
        except Exception as e:
            if _is_service_unavailable_retryable(e) and service_attempt < SERVICE_UNAVAILABLE_RETRY_COUNT:
                service_attempt += 1
                logger.warning(
                    "%s got 503/unavailable, retry %s/%s after %ss: %s",
                    action_name,
                    service_attempt,
                    SERVICE_UNAVAILABLE_RETRY_COUNT,
                    SERVICE_UNAVAILABLE_RETRY_DELAY_SEC,
                    e,
                )
                await _send_llm_retry_notice_safely(
                    update,
                    context,
                    chat_id=chat_id,
                    text=(
                        "Сервис модели сейчас перегружен. "
                        f"Повторяю через {SERVICE_UNAVAILABLE_RETRY_DELAY_SEC} секунд "
                        f"(попытка {service_attempt}/{SERVICE_UNAVAILABLE_RETRY_COUNT})."
                    )
                )
                await asyncio.sleep(SERVICE_UNAVAILABLE_RETRY_DELAY_SEC)
                continue
            if _is_service_unavailable_retryable(e):
                raise ServiceUnavailableRetriesExhaustedError(
                    "Сервис модели перегружен слишком долго. Попробуйте позже."
                ) from e
            if retry_ai_rate_limit and _is_ai_rate_limit_retryable(e) and rate_attempt < AI_RATE_LIMIT_RETRY_COUNT:
                rate_attempt += 1
                logger.warning(
                    "%s got AI rate limit, retry %s/%s after %ss: %s",
                    action_name,
                    rate_attempt,
                    AI_RATE_LIMIT_RETRY_COUNT,
                    AI_RATE_LIMIT_RETRY_DELAY_SEC,
                    e,
                )
                await _send_llm_retry_notice_safely(
                    update,
                    context,
                    chat_id=chat_id,
                    text=(
                        "Уперлась в лимит AI: слишком много запросов подряд. "
                        f"Подожду {AI_RATE_LIMIT_RETRY_DELAY_SEC} секунд и попробую сама "
                        f"(попытка {rate_attempt}/{AI_RATE_LIMIT_RETRY_COUNT})."
                    )
                )
                await asyncio.sleep(AI_RATE_LIMIT_RETRY_DELAY_SEC)
                continue
            if _is_ai_rate_limit_retryable(e):
                if not retry_ai_rate_limit:
                    raise ServiceUnavailableRetriesExhaustedError(
                        "Лимит AI: слишком много запросов подряд. Попробуйте позже или введите перевод вручную."
                    ) from e
                raise ServiceUnavailableRetriesExhaustedError(
                    "Лимит AI держится слишком долго. Я уже попробовала несколько раз; повторите позже."
                ) from e
            raise


def _elevenlabs_voice_warning() -> str | None:
    voice_id = (os.getenv(ELEVENLABS_VOICE_ID_ENV_NAME) or "").strip()
    if voice_id:
        return None
    return (
        "Предупреждение: не задан ELEVENLABS_VOICE_ID. "
        "Для сцен с провайдером озвучки elevenlabs поставится заглушка voice_id."
    )


_BASE_FRAME_CHARACTERS = frozenset({"grandma", "cat"})


def _character_mentions_in_prompt(prompt: str) -> set[str]:
    """
    Грубое множество персонажей, явно упомянутых в тексте промпта кадра (RU/EN).
    """
    low = (prompt or "").lower()
    found: set[str] = set()
    if "бабуш" in low or "grandma" in low:
        found.add("grandma")
    if "кот" in low or "кош" in low or "cat" in low:
        found.add("cat")
    if "granddaughter" in low or "внуч" in low:
        found.add("granddaughter")
    if "grandson" in low or "внук" in low:
        found.add("grandson")
    if "daughter" in low or "дочь" in low or "дочер" in low:
        found.add("daughter")
    if "grandfather" in low or "grandpa" in low or "дед" in low or "дедуш" in low:
        found.add("grandfather")
    if "husband" in low or "муж" in low:
        found.add("husband")
    if "dog" in low or "собак" in low:
        found.add("dog")
    if any(token in low for token in ("zaya", "зая", "заю", "заи", "зайка", "белая кошка", "белая кошечка")):
        found.add("zaya")
    if any(token in low for token in ("markiz", "маркиз", "маркиза", "черно-белый кот", "чёрно-белый кот")):
        found.add("markiz")
    return found


def _scene_character_keys(scene: dict) -> set[str]:
    chars = scene.get("characters")
    if not isinstance(chars, list):
        return set(_BASE_FRAME_CHARACTERS)
    out = {_normalize_character_key(str(c)) for c in chars if str(c).strip()}
    return out if out else set(_BASE_FRAME_CHARACTERS)


def _end_frame_image_warranted(scene: dict) -> bool:
    """
    Конечный кадр рисуем только если в сцене есть персонаж вне пары бабушка+кот,
    которого нет в тексте стартового кадра (вошёл в кадр к финалу сцены).
    """
    start_src = (
        str(scene.get("start_frame_prompt", "")).strip()
        or str(scene.get("start_frame_prompt_ru", "")).strip()
    )
    mentioned = _character_mentions_in_prompt(start_src)
    in_scene = _scene_character_keys(scene)
    extra = in_scene - mentioned
    extra -= _BASE_FRAME_CHARACTERS
    return bool(extra)


def _first_prompt_from_lang_map(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for language in ("ru", "en"):
        prompt = str(value.get(language) or "").strip()
        if prompt:
            return prompt
    for item in value.values():
        prompt = str(item or "").strip()
        if prompt:
            return prompt
    return ""


def _candidate_video_prompt(scene: dict, *, approved_only: bool) -> str:
    return _first_prompt_from_lang_map(_candidate_video_prompt_by_lang(scene, approved_only=approved_only))


def _candidate_video_prompt_by_lang(scene: dict, *, approved_only: bool) -> dict[str, str]:
    candidates_by_lang = scene.get("video_candidates_by_lang")
    if not isinstance(candidates_by_lang, dict):
        return {}
    candidate_groups: list[tuple[str, list[Any]]] = []
    for language in ("ru", "en"):
        candidates = candidates_by_lang.get(language)
        if isinstance(candidates, list):
            candidate_groups.append((language, candidates))
    for candidates in candidates_by_lang.values():
        if isinstance(candidates, list) and not any(candidates is group for _lang, group in candidate_groups):
            candidate_groups.append(("", candidates))
    out: dict[str, str] = {}
    for group_language, candidates in candidate_groups:
        for candidate in reversed(candidates):
            if not isinstance(candidate, dict):
                continue
            if approved_only and not bool(candidate.get("approved_for_production")):
                continue
            prompt_by_lang = candidate.get("video_prompt_by_lang")
            if not isinstance(prompt_by_lang, dict):
                continue
            if group_language:
                prompt = str(prompt_by_lang.get(group_language) or "").strip()
                if prompt and group_language not in out:
                    out[group_language] = prompt
            for raw_language, raw_prompt in prompt_by_lang.items():
                language = str(raw_language or "").strip().lower()
                prompt = str(raw_prompt or "").strip()
                if language and prompt and language not in out:
                    out[language] = prompt
            if group_language and group_language in out:
                break
    return out


def _approved_candidate_video_prompt(scene: dict) -> str:
    return _candidate_video_prompt(scene, approved_only=True)


def _latest_candidate_video_prompt(scene: dict) -> str:
    return _candidate_video_prompt(scene, approved_only=False)


def _video_prompt_by_lang_with_candidate_fallback(scene: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    mapping = scene.get("video_prompt_by_lang")
    if isinstance(mapping, dict):
        for raw_language, raw_prompt in mapping.items():
            language = str(raw_language or "").strip().lower()
            prompt = str(raw_prompt or "").strip()
            if language and prompt:
                out[language] = prompt
    legacy_prompt = str(scene.get("video_prompt", "")).strip()
    current_language = str(scene.get("video_prompt_current_language") or "").strip().lower() or "ru"
    if legacy_prompt and current_language not in out:
        out[current_language] = legacy_prompt
    for fallback in (
        _candidate_video_prompt_by_lang(scene, approved_only=True),
        _candidate_video_prompt_by_lang(scene, approved_only=False),
    ):
        for language, prompt in fallback.items():
            if language not in out and prompt:
                out[language] = prompt
    return out


def _video_context_as_start_frame_prompt(scene: dict, video_prompt: str) -> str:
    text = str(video_prompt or "").strip()
    text = re.sub(r"^\s*lang\s*:\s*[a-z]{2}\b", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\bclip duration\s*:\s*[^.\n]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\blanguage\s*:\s*[^\n.]+(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bvoice(?:over)?(?:_language|_lock_full(?:_grandma)?)?\s*:\s*[^\n]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCHARACTERS?\s+SPEAKS?\s*:[^\n]*(?:\n|$)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bspeech_timing_plan\s*:\s*(?:\n\s*-\s*[^\n]+)+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\ball spoken phrases[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\baccurate lip sync[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:her|his|their)?\s*lips?\s+sync(?:hronize|hronise)?[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\blipsync[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcamera(?:\s+\w+){0,5}\s*(?:maintains|smoothly|subtly|gently|tracks|pans|zooms|shifts|follows)[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bno background music[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bno on-screen text[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bnatural-fast spoken delivery[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfinish the spoken line[^\n.]*(?:\.|\n)", " ", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    if len(text) > 1200:
        text = text[:1200].rsplit(" ", 1)[0].strip()

    story_scene = str(scene.get("generation_mode") or "").strip().lower() == "story"
    scene_text = sanitize_character_names_and_keys(
        str(
            (scene.get("action") or scene.get("text") or scene.get("recipe_step"))
            if story_scene
            else (scene.get("text") or scene.get("action") or scene.get("recipe_step"))
            or ""
        ).strip()
    )
    aspect = str(scene.get("aspect_ratio") or "").strip()
    frame_rule = (
        "Горизонтальный 16:9, landscape frame, крупный план, не medium shot."
        if aspect == "16:9"
        else "Вертикальный 9:16, крупный план, не medium shot."
    )
    prompt_parts = [f"Стартовый кадр, статичное изображение до начала движения. {frame_rule}"]
    if story_scene:
        characters = [str(item).strip() for item in scene.get("characters") or [] if str(item).strip()]
        prompt_parts.append(
            "В кадре только явно перечисленные персонажи: "
            + (", ".join(characters) if characters else "людей и животных нет")
            + ". Не добавлять бабушку, кота или других героев из текста озвучки."
        )
        era_style = str(scene.get("era_style") or "").strip()
        if era_style:
            prompt_parts.append(f"Эпоха и техника изображения: {era_style}.")
        shot_type = str(scene.get("shot_type") or "").strip()
        if shot_type:
            prompt_parts.append(f"План: {shot_type}.")
        location = str(scene.get("location") or "").strip()
        if location:
            prompt_parts.append(f"Локация: {location}.")
        lighting = str(scene.get("lighting") or "").strip()
        if lighting:
            prompt_parts.append(f"Свет: {lighting}.")
        color_palette = str(scene.get("color_palette") or "").strip()
        if color_palette:
            prompt_parts.append(f"Цветовая гамма: {color_palette}.")
    else:
        prompt_parts.append(
            "Бабушка, рыжий кот и все дополнительно упомянутые персонажи полностью видны в кадре; без дублей персонажей."
        )
    if scene_text:
        prompt_parts.append(f"Сюжет сцены: {scene_text.rstrip('. ')}.")
    if text:
        prompt_parts.append(f"Визуальная композиция: {sanitize_character_names_and_keys(text)}")
    prompt_parts.append("Без озвучки, без синхронизации губ, без таймингов, без текста на экране, без субтитров, без водяных знаков.")
    return " ".join(part.strip() for part in prompt_parts if part.strip())


def _image_scene_prompt(scene: dict) -> str:
    start = str(scene.get("start_frame_prompt", "")).strip() or str(scene.get("start_frame_prompt_ru", "")).strip()
    if start:
        return start
    if str(scene.get("generation_mode") or "").strip().lower() == "story" and str(
        scene.get("action") or scene.get("text") or scene.get("recipe_step") or ""
    ).strip():
        # Story video prompts may describe motion, transitions, or an older
        # approved composition. START must reflect only the current visual beat.
        return _video_context_as_start_frame_prompt(scene, "")
    fallback = str(scene.get("video_prompt", "")).strip()
    if fallback:
        return _video_context_as_start_frame_prompt(scene, fallback)
    fallback = _first_prompt_from_lang_map(scene.get("video_prompt_by_lang"))
    if fallback:
        return _video_context_as_start_frame_prompt(scene, fallback)
    fallback = _approved_candidate_video_prompt(scene)
    if fallback:
        return _video_context_as_start_frame_prompt(scene, fallback)
    fallback = _latest_candidate_video_prompt(scene)
    if fallback:
        return _video_context_as_start_frame_prompt(scene, fallback)
    if str(scene.get("text") or scene.get("action") or scene.get("recipe_step") or "").strip():
        return _video_context_as_start_frame_prompt(scene, "")
    raise RuntimeError("В сцене нет промпта для генерации изображения.")


def _image_end_scene_prompt(scene: dict) -> str | None:
    if not scene.get("end_frame_requested"):
        return None
    end_prompt = str(scene.get("end_frame_prompt", "")).strip() or str(scene.get("end_frame_prompt_ru", "")).strip()
    if not end_prompt or end_prompt.lower() == "пусто":
        return None
    continuity_block = (
        "Референс: приложенный стартовый кадр — полная база композиции. "
        "Текст ниже задаёт ТОЛЬКО отличия финала от старта (дельта): кто вошёл в кадр, что сдвинулось, степень готовности еды. "
        "Запрещено описывать весь кадр заново «с нуля». Всё, что уже есть на старте, сохраняется; новые элементы — только из дельты."
    )
    return f"{end_prompt}\n\n{continuity_block}"


def _encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_b64(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(value)
    except Exception:
        return None


def _is_video_policy_error(e: Exception) -> bool:
    low = str(e).lower()
    return "policy" in low or "safety" in low or "content violation" in low or "moderation" in low


def _is_video_safety_error(e: Exception) -> bool:
    low = str(e).lower()
    return "finish_reason=safety" in low or "safety" in low


def _is_video_prompt_filtered_error(e: Exception) -> bool:
    """Detect RAI filtering where provider suggests rephrasing prompt."""
    low = str(e).lower()
    return (
        "raimediafilteredcount" in low
        or "raimediafilteredreasons" in low
        or ("filtered out" in low and "usage guidelines" in low)
        or ("violated" in low and "usage guidelines" in low)
    )


def _is_video_invalid_image_error(e: Exception) -> bool:
    low = str(e).lower()
    return (
        ("image" in low and ("invalid" in low or "unsupported" in low or "corrupt" in low))
        or "bad reference image" in low
        or "reference image rejected" in low
    )


def _is_video_high_load_error(e: Exception) -> bool:
    low = str(e).lower()
    return (
        '"code":8' in low
        or "'code': 8" in low
        or "code=8" in low
        or "experiencing high load" in low
        or "please try again later" in low
    )


def _is_video_model_unavailable_error(e: Exception) -> bool:
    """Model is unavailable in region (404 NOT_FOUND publisher model)."""
    low = str(e).lower()
    return (
        "http 404" in low
        or '"code": 404' in low
        or '"code":404' in low
        or "status\": \"not_found" in low
        or "publisher model" in low and ("was not found" in low or "does not have access" in low)
    )


def _is_video_vertex_quota_like_error(e: Exception) -> bool:
    """Quota / rate-limit style failures — Syntax fallback must not run."""
    if isinstance(e, GeminiQuotaError):
        return True
    text = str(e)
    low = text.lower()
    return (
        "resource_exhausted" in low
        or "resource exhausted" in low
        or "quota exceeded" in low
        or "quotavalue" in low
        or "429" in text
    )


def _is_syntax_fallback_trigger_error(e: Exception) -> bool:
    """
    True only for Vertex policy / safety / RAI-filter blocks.

    Excludes high load, per-region model 404, invalid reference images, and quota-like errors.
    """
    if (
        _is_video_high_load_error(e)
        or _is_video_model_unavailable_error(e)
        or _is_video_invalid_image_error(e)
        or _is_video_vertex_quota_like_error(e)
    ):
        return False
    return (
        _is_video_policy_error(e)
        or _is_video_safety_error(e)
        or _is_video_prompt_filtered_error(e)
    )


def _syntax_fallback_env_enabled() -> bool:
    """True when ``SYNTAX_FALLBACK_ENABLED`` is a common truthy token (1/true/yes/on)."""
    raw = (os.getenv("SYNTAX_FALLBACK_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _syntax_fallback_mode() -> str:
    """
    Legacy mode parser for ``SYNTAX_FALLBACK_MODE``.

    ``auto`` is returned only for explicit ``auto`` token; anything else maps to ``manual``.
    Current video fallback path runs automatically even if mode is ``manual``.
    """
    raw = (os.getenv("SYNTAX_FALLBACK_MODE") or "manual").strip().lower()
    return "auto" if raw == "auto" else "manual"


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer env value, falling back to ``default``."""
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


class SyntaxFallbackPendingConfirmation(Exception):
    """
    Vertex video was blocked by policy/safety after regional retries and Syntax fallback is enabled
    in ``manual`` mode: the handler should ask the user and call :func:`run_syntax_fallback_from_session_payload` only after approval.
    """

    def __init__(
        self,
        *,
        prompt: str,
        model: str,
        duration_sec: int,
        start_image: bytes,
        end_image: bytes | None,
        quality: str,
        aspect_ratio: str,
        audio_enabled: bool,
        vertex_error_message: str,
        syntax_provider: str | None = None,
    ) -> None:
        super().__init__(vertex_error_message)
        self.prompt = prompt
        self.model = model
        self.duration_sec = duration_sec
        self.start_image = start_image
        self.end_image = end_image
        self.quality = quality
        self.aspect_ratio = aspect_ratio
        self.audio_enabled = audio_enabled
        self.vertex_error_message = vertex_error_message
        self.syntax_provider = syntax_provider or SYNTAX_PROVIDER_VEO


class VideoProviderChoiceRequired(Exception):
    """
    Syntax was the only automatic path and did not return a video.

    The caller must ask the user whether to retry Syntax or try the reserve Vertex path.
    """

    def __init__(
        self,
        *,
        prompt: str,
        model: str,
        duration_sec: int,
        start_image: bytes,
        end_image: bytes | None,
        quality: str,
        aspect_ratio: str,
        audio_enabled: bool,
        syntax_error_message: str,
        syntax_provider: str | None = None,
    ) -> None:
        super().__init__(syntax_error_message)
        self.prompt = prompt
        self.model = model
        self.duration_sec = duration_sec
        self.start_image = start_image
        self.end_image = end_image
        self.quality = quality
        self.aspect_ratio = aspect_ratio
        self.audio_enabled = audio_enabled
        self.syntax_error_message = syntax_error_message
        self.syntax_provider = syntax_provider or SYNTAX_PROVIDER_VEO


class SyntaxFallbackDispatchError(RuntimeError):
    """Syntax request could not be dispatched to Telegram."""


def encode_syntax_fallback_pending_for_session(exc: SyntaxFallbackPendingConfirmation) -> dict[str, Any]:
    """Serializes a pending Syntax attempt for ``SESSIONS`` / scene JSON (base64 images)."""
    end = exc.end_image
    return {
        "schema": 1,
        "prompt": exc.prompt,
        "model": exc.model,
        "duration_sec": exc.duration_sec,
        "quality": exc.quality,
        "aspect_ratio": exc.aspect_ratio,
        "audio_enabled": exc.audio_enabled,
        "syntax_provider": exc.syntax_provider,
        "start_image_b64": base64.b64encode(exc.start_image).decode("ascii"),
        "end_image_b64": base64.b64encode(end).decode("ascii") if end else "",
        "vertex_error_message": (exc.vertex_error_message or "")[:8000],
    }


def _vertex_fallback_duration_sec(model: str, duration_sec: int) -> int:
    """
    Reserve Vertex path for Veo uses 8 seconds, which is the safe image-to-video duration.
    """
    raw_model = (model or "").strip().lower()
    if raw_model.startswith("veo"):
        return 8
    return int(duration_sec)


def encode_video_provider_choice_for_session(exc: VideoProviderChoiceRequired) -> dict[str, Any]:
    """Serializes a pending provider-choice prompt for session/scene state."""
    end = exc.end_image
    vertex_duration_sec = _vertex_fallback_duration_sec(exc.model, exc.duration_sec)
    return {
        "schema": 1,
        "prompt": exc.prompt,
        "model": exc.model,
        "duration_sec": exc.duration_sec,
        "vertex_duration_sec": vertex_duration_sec,
        "quality": exc.quality,
        "aspect_ratio": exc.aspect_ratio,
        "audio_enabled": exc.audio_enabled,
        "syntax_provider": exc.syntax_provider,
        "start_image_b64": base64.b64encode(exc.start_image).decode("ascii"),
        "end_image_b64": base64.b64encode(end).decode("ascii") if end else "",
        "syntax_error_message": (exc.syntax_error_message or "")[:8000],
    }


def video_provider_choice_parse_user_selection(text: str) -> str | None:
    """Parses explicit user choice after Syntax failed: retry Syntax or try Vertex."""
    low = (text or "").strip().lower()
    if low in {
        "syntax",
        "syntx",
        "synthx",
        "ещё раз syntax",
        "еще раз syntax",
        "повторить syntax",
        "retry syntax",
        "syntax retry",
    }:
        return "syntax"
    if low in {
        "vertex",
        "veo",
        "try vertex",
        "резервный vertex",
        "vertex fallback",
    }:
        return "vertex"
    return None


def _decode_fallback_payload_image(payload: dict[str, Any], *keys: str, required: bool = True) -> bytes | None:
    for key in keys:
        raw = str(payload.get(key) or "").strip()
        if raw:
            return base64.b64decode(raw)
    if required:
        raise KeyError(keys[0] if keys else "image_b64")
    return None


async def run_syntax_fallback_from_session_payload(payload: dict[str, Any]) -> bytes | None:
    """Runs Syntax fallback using a dict from :func:`encode_syntax_fallback_pending_for_session`."""
    start_im = _decode_fallback_payload_image(payload, "start_image_b64", "start_frame_image_b64")
    end_im = _decode_fallback_payload_image(payload, "end_image_b64", "end_frame_image_b64", required=False)
    prompt = enforce_video_prompt_visual_source_rule(str(payload.get("prompt") or "")).strip()
    if not prompt:
        raise SyntaxFallbackDispatchError("prompt для Syntax пустой; отправка остановлена.")
    return await _attempt_syntax_video_fallback_bytes(
        prompt=prompt,
        model=str(payload["model"]),
        duration_sec=int(payload["duration_sec"]),
        start_image=start_im,
        end_image=end_im,
        raise_on_dispatch_failure=True,
        syntax_provider=str(payload.get("syntax_provider") or SYNTAX_PROVIDER_VEO),
        aspect_ratio=str(payload.get("aspect_ratio") or "9:16"),
        audio_enabled=bool(payload.get("audio_enabled", True)),
    )


async def run_vertex_fallback_from_session_payload(payload: dict[str, Any]) -> bytes:
    """Runs the reserve Vertex path from a serialized pending payload."""
    start_im = _decode_fallback_payload_image(payload, "start_image_b64", "start_frame_image_b64")
    end_im = _decode_fallback_payload_image(payload, "end_image_b64", "end_frame_image_b64", required=False)
    prompt = enforce_video_prompt_visual_source_rule(str(payload.get("prompt") or "")).strip()
    if not prompt:
        raise RuntimeError("empty video prompt")
    return await asyncio.to_thread(
        generate_video_bytes,
        prompt=prompt,
        model=str(payload["model"]),
        duration_sec=int(payload.get("vertex_duration_sec") or payload["duration_sec"]),
        start_image=start_im,
        end_image=end_im,
        quality=str(payload.get("quality") or "720"),
        aspect_ratio=str(payload.get("aspect_ratio") or "9:16"),
        audio_enabled=bool(payload.get("audio_enabled", True)),
    )


def syntax_fallback_parse_user_confirmation(text: str) -> bool | None:
    """``True`` / ``False`` for clear yes/no replies; ``None`` if the user should be prompted again."""
    low = (text or "").strip().lower()
    if low in {"да", "yes", "y", "ок", "ok", "okay", "approve", "запуск", "syntax", "+"}:
        return True
    if low in {"нет", "no", "n", "отмена", "cancel", "не надо", "decline", "-"}:
        return False
    return None


async def _attempt_syntax_video_fallback_bytes(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None,
    raise_on_dispatch_failure: bool = False,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
) -> bytes | None:
    """Runs Syntax adapter in a worker thread; returns bytes or None."""
    if not _syntax_fallback_env_enabled():
        return None
    from ..syntax_durable import enabled_for_scene
    from ..syntax_telethon_fallback import _normalize_syntax_provider
    if enabled_for_scene() and _normalize_syntax_provider(syntax_provider, model) == SYNTAX_PROVIDER_VEO:
        from ..state import SESSIONS
        from ..syntax_durable import current_dialogue
        active = SESSIONS.get(current_dialogue().update.effective_chat.id) or {}
        match = re.search(r"lang\s*[:=]\s*(ru|en)\b", prompt, flags=re.IGNORECASE)
        languages = active.get("target_languages") or ["ru"]
        language = match.group(1).lower() if match else languages[min(int(active.get("scene_media_language_index") or 0), len(languages) - 1)]
        values = await _attempt_syntax_video_fallback_batch(
            requests=[BatchFallbackRequest(key=language, prompt=prompt, start_image=start_image, end_image=end_image)],
            model=model, duration_sec=duration_sec, timeout_sec=timeout_sec,
            acquire_timeout_sec=acquire_timeout_sec, syntax_provider=syntax_provider,
            aspect_ratio=aspect_ratio, audio_enabled=audio_enabled,
        )
        return values.get(language)
    timeout_sec = int(timeout_sec or _positive_int_env("SYNTAX_FALLBACK_TIMEOUT_SEC", SYNTAX_FALLBACK_DEFAULT_TIMEOUT_SEC))
    lock_timeout_sec = int(acquire_timeout_sec or _positive_int_env("SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC", timeout_sec))
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                try_generate_via_syntax,
                prompt=prompt,
                model=model,
                duration_sec=duration_sec,
                start_image=start_image,
                end_image=end_image,
                timeout_sec=timeout_sec,
                acquire_timeout_sec=lock_timeout_sec,
                syntax_provider=syntax_provider,
                aspect_ratio=aspect_ratio,
                audio_enabled=audio_enabled,
            ),
            timeout=lock_timeout_sec + timeout_sec + SYNTAX_FALLBACK_TIMEOUT_GRACE_SEC,
        )
    except TimeoutError:
        logger.warning(
            "video.syntax_fallback call_timeout acquire_timeout_sec=%s timeout_sec=%s",
            lock_timeout_sec,
            timeout_sec,
        )
        return None
    if not result.success:
        provider_message = str(result.provider_message or "")
        logger.warning(
            "video.syntax_fallback not_used attempt_id=%s msg=%s",
            result.attempt_id,
            provider_message[:400],
        )
        if raise_on_dispatch_failure and _syntax_failure_happened_before_dispatch(provider_message):
            raise SyntaxFallbackDispatchError(_syntax_dispatch_error_user_message(provider_message))
        return None
    try:
        payload = await asyncio.wait_for(
            asyncio.to_thread(video_bytes_from_fallback_result, result),
            timeout=SYNTAX_FALLBACK_DOWNLOAD_TIMEOUT_SEC + SYNTAX_FALLBACK_TIMEOUT_GRACE_SEC,
        )
    except TimeoutError:
        logger.warning(
            "video.syntax_fallback download_timeout attempt_id=%s timeout_sec=%s",
            result.attempt_id,
            SYNTAX_FALLBACK_DOWNLOAD_TIMEOUT_SEC,
        )
        return None
    except Exception as e:
        logger.warning(
            "video.syntax_fallback download_failed attempt_id=%s err=%s",
            result.attempt_id,
            str(e)[:400],
        )
        return None
    logger.info(
        "video.syntax_fallback ok attempt_id=%s bytes=%s",
        result.attempt_id,
        len(payload),
    )
    return payload


def _syntax_failure_happened_before_dispatch(provider_message: str) -> bool:
    low = str(provider_message or "").lower()
    return any(
        marker in low
        for marker in (
            "telethon is not installed",
            "missing telegram_api_id/telegram_api_hash",
            "missing telethon session",
            "no free telethon session",
            "syntax veo not submitted:",
            "telethon stringsession is unavailable",
            "telethon session is not authorized",
            "syntax frame prepare failed",
            "syntax prompt is empty",
            "syntax prompt is longer than",
        )
    )


def _syntax_dispatch_error_user_message(provider_message: str) -> str:
    low = str(provider_message or "").lower()
    if "syntax prompt is empty" in low:
        return "prompt для Syntax пустой; отправка остановлена."
    if "syntax prompt is longer than" in low:
        return (
            "prompt превышает лимит Syntax, а автоматически сократить его сейчас не удалось. "
            "Сократите prompt вручную и повторите."
        )
    if "no free telethon session" in low:
        return "Telethon-сессия Syntax занята другой попыткой. Дождитесь её завершения и повторите."
    if "syntax veo launch unconfirmed:" in low:
        return "После трёх попыток Syntx не подтвердил запуск. Результат запуска неизвестен; новая генерация автоматически не отправлена."
    if "syntax veo not submitted:" in low and "menu acknowledgement" in low:
        return "Syntx не ответил на выбор инструмента даже после повторной команды. Кадр и промпт ещё не отправлены, генерация не запущена."
    if "syntax veo not submitted:" in low and "prompt acknowledgement" in low:
        return "Syntx не прислал подтверждение сохранения промпта и кнопку настроек за время ожидания. Генерация ещё не запущена; кадр и промпт сохранены."
    if "syntax veo not submitted:" in low:
        stages = {
            "clear_attachments": "удаление прежних кадров",
            "upload_frame": "передача кадра",
            "confirm_upload": "подтверждение загрузки кадра",
            "verify_persisted_frame": "проверка сохранения кадра в Syntx",
            "commit_prompt": "сохранение промпта",
            "set_aspect_ratio": "выбор формата кадра",
            "form_ready": "готовность формы к запуску",
        }
        for stage, label in stages.items():
            if f"этап={stage}" in low:
                return f"Syntx: не завершён этап «{label}» после трёх попыток. Кнопку генерации не нажимали; фраза и кадр сохранены для повтора."
    if "syntax veo not submitted:" in low:
        return "Не удалось открыть или подготовить форму Syntax. Генерация не запущена; сессия освобождена для повтора."
    if "not authorized" in low:
        return "Telethon-сессия Syntax не авторизована."
    if "missing telethon session" in low:
        return "не настроена Telethon-сессия для Syntax."
    if "missing telegram_api_id/telegram_api_hash" in low:
        return "не настроены TELEGRAM_API_ID/TELEGRAM_API_HASH для Syntax."
    if "telethon is not installed" in low or "stringsession is unavailable" in low:
        return "транспорт Telethon для Syntax сейчас недоступен."
    if "syntax frame prepare failed" in low:
        return "не удалось подготовить кадр для отправки в Syntax."
    return "транспорт Syntax недоступен до отправки запроса."


async def _attempt_syntax_video_fallback_batch(
    *,
    requests: list[BatchFallbackRequest],
    model: str,
    duration_sec: int,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
) -> dict[str, bytes]:
    """Runs one batched Syntax session and returns only successful mp4 payloads by request key."""
    if not _syntax_fallback_env_enabled() or not requests:
        return {}
    timeout_sec = int(timeout_sec or _positive_int_env("SYNTAX_FALLBACK_TIMEOUT_SEC", SYNTAX_FALLBACK_DEFAULT_TIMEOUT_SEC))
    lock_timeout_sec = int(acquire_timeout_sec or _positive_int_env("SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC", timeout_sec))

    from ..syntax_durable import enabled_for_scene, run_batch as run_durable_batch
    if enabled_for_scene():
        results = await run_durable_batch(
            requests=requests, model=model, duration_sec=duration_sec,
            timeout_sec=timeout_sec, acquire_timeout_sec=lock_timeout_sec,
            syntax_provider=syntax_provider, aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
        )
    else:
        partial_results: dict[str, Any] = {}
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    try_generate_via_syntax_batch,
                    requests=requests,
                    result_sink=partial_results,
                    model=model,
                    duration_sec=duration_sec,
                    timeout_sec=timeout_sec,
                    acquire_timeout_sec=lock_timeout_sec,
                    syntax_provider=syntax_provider,
                    aspect_ratio=aspect_ratio,
                    audio_enabled=audio_enabled,
                ),
                timeout=lock_timeout_sec + timeout_sec + SYNTAX_FALLBACK_TIMEOUT_GRACE_SEC,
            )
        except TimeoutError:
            logger.warning(
                "video.syntax_batch call_timeout acquire_timeout_sec=%s timeout_sec=%s requests=%s",
                lock_timeout_sec,
                timeout_sec,
                len(requests),
            )
            results = partial_results.copy()
            logger.info("video.syntax_batch retained_after_timeout keys=%s",
                        sorted(key for key, result in results.items() if result.success))
    output: dict[str, bytes] = {}
    payload_hashes: dict[str, str] = {}
    for key, result in results.items():
        if not getattr(result, "success", False):
            logger.warning(
                "video.syntax_batch not_used key=%s attempt_id=%s msg=%s",
                key,
                getattr(result, "attempt_id", ""),
                str(getattr(result, "provider_message", ""))[:400],
            )
            continue
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(video_bytes_from_fallback_result, result),
                timeout=SYNTAX_FALLBACK_DOWNLOAD_TIMEOUT_SEC + SYNTAX_FALLBACK_TIMEOUT_GRACE_SEC,
            )
        except Exception as e:
            logger.warning(
                "video.syntax_batch download_failed key=%s attempt_id=%s err=%s",
                key,
                getattr(result, "attempt_id", ""),
                str(e)[:400],
            )
            continue
        digest = hashlib.sha256(payload).hexdigest()
        duplicate_key = payload_hashes.get(digest)
        if duplicate_key is not None:
            logger.warning(
                "video.syntax_batch duplicate_payload_discarded key=%s first_key=%s attempt_id=%s sha256=%s",
                key,
                duplicate_key,
                getattr(result, "attempt_id", ""),
                digest[:16],
            )
            continue
        normalized_key = str(key)
        payload_hashes[digest] = normalized_key
        output[normalized_key] = payload
    if not output and results and all(
        not getattr(result, "success", False)
        and _syntax_failure_happened_before_dispatch(str(getattr(result, "provider_message", "")))
        for result in results.values()
    ):
        first = next(iter(results.values()))
        raise SyntaxFallbackDispatchError(_syntax_dispatch_error_user_message(first.provider_message))
    return output


def _video_region_candidates() -> list[str]:
    """Build ordered unique Vertex regions: primary + optional fallbacks from env."""
    primary = (
        os.getenv("VERTEX_VIDEO_LOCATION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    ).strip() or "us-central1"
    if primary.lower() == "global":
        primary = "us-central1"
    raw = (os.getenv("VERTEX_VIDEO_LOCATION_FALLBACKS") or "").strip()
    fallbacks = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
    out: list[str] = []
    for region in [primary, *fallbacks]:
        if region and region not in out:
            out.append(region)
    return out


async def _generate_video_bytes_with_high_load_retry(
    update: Update,
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None,
    quality: str,
    aspect_ratio: str,
    audio_enabled: bool,
    skip_initial_syntax: bool = False,
    disable_syntax_fallback: bool = False,
    force_vertex_fallback: bool = False,
    syntax_timeout_sec: int | None = None,
    syntax_acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
) -> bytes:
    """Generate video through the current policy: auto Syntax, manual Vertex fallback."""

    prompt = enforce_video_prompt_visual_source_rule(prompt).strip()
    if not prompt:
        raise RuntimeError("empty video prompt")
    syntax_enabled = _syntax_fallback_env_enabled() and not disable_syntax_fallback
    if not force_vertex_fallback and syntax_enabled and not skip_initial_syntax:
        if update.message is not None:
            await safe_reply_text(
                update.message,
                "Сначала пробую генерацию через Syntax. В Vertex автоматически не ухожу."
            )
        syntax_bytes = await _attempt_syntax_video_fallback_bytes(
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            timeout_sec=syntax_timeout_sec,
            acquire_timeout_sec=syntax_acquire_timeout_sec,
            syntax_provider=syntax_provider or SYNTAX_PROVIDER_VEO,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
        )
        if syntax_bytes is not None:
            _LAST_VIDEO_PROVIDER.set("telegram")
            if update.message is not None:
                await safe_reply_text(update.message, "Видео получено через Syntax.")
            return syntax_bytes
        raise VideoProviderChoiceRequired(
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            quality=quality,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
            syntax_error_message="Syntax не вернул видео.",
            syntax_provider=syntax_provider or SYNTAX_PROVIDER_VEO,
        )
    if not force_vertex_fallback and syntax_enabled and skip_initial_syntax:
        raise VideoProviderChoiceRequired(
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            quality=quality,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
            syntax_error_message="Syntax уже был запущен для этой сцены, но видео не вернул.",
            syntax_provider=syntax_provider or SYNTAX_PROVIDER_VEO,
        )
    if not force_vertex_fallback and not syntax_enabled and not disable_syntax_fallback:
        raise VideoProviderChoiceRequired(
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            quality=quality,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
            syntax_error_message="Syntax сейчас недоступен, поэтому нужен явный выбор: повторить Syntax позже или запустить Vertex вручную.",
            syntax_provider=syntax_provider or SYNTAX_PROVIDER_VEO,
        )
    if not force_vertex_fallback:
        # ``disable_syntax_fallback`` is an internal transport switch, not
        # permission to spend Vertex quota.  Vertex video generation is only
        # allowed after an explicit user choice has been translated into
        # ``force_vertex_fallback=True`` by the provider-choice handler.
        raise VideoProviderChoiceRequired(
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            quality=quality,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
            syntax_error_message=(
                "Syntax отключён для этого запуска. Vertex не будет запущен без явного выбора пользователя."
            ),
            syntax_provider=syntax_provider or SYNTAX_PROVIDER_VEO,
        )
    if force_vertex_fallback and model.strip().lower().startswith("veo") and update.message is not None:
        await safe_reply_text(
            update.message,
            f"Запускаю резервный Vertex/Veo путь на {_vertex_fallback_duration_sec(model, duration_sec)} сек."
        )

    last_err: Exception | None = None
    regions = _video_region_candidates()
    total_attempts = len(regions) * VIDEO_HIGH_LOAD_MAX_ATTEMPTS
    global_attempt = 0
    for region in regions:
        for region_attempt in range(VIDEO_HIGH_LOAD_MAX_ATTEMPTS):
            global_attempt += 1
            try:
                out = await asyncio.to_thread(
                    generate_video_bytes,
                    prompt=prompt,
                    model=model,
                    duration_sec=_vertex_fallback_duration_sec(model, duration_sec),
                    start_image=start_image,
                    end_image=end_image,
                    quality=quality,
                    aspect_ratio=aspect_ratio,
                    audio_enabled=audio_enabled,
                    location=region,
                )
                _LAST_VIDEO_PROVIDER.set("veo")
                return out
            except Exception as e:
                last_err = e
                model_unavailable = _is_video_model_unavailable_error(e)
                should_retry_by_region = (
                    _is_video_high_load_error(e)
                    or _is_video_policy_error(e)
                    or _is_video_safety_error(e)
                    or _is_video_prompt_filtered_error(e)
                    or model_unavailable
                )
                if not should_retry_by_region:
                    raise
                if global_attempt >= total_attempts:
                    raise
                wait_sec = 0 if model_unavailable else VIDEO_HIGH_LOAD_RETRY_DELAY_SEC * (region_attempt + 1)
                same_region_retry_left = (region_attempt < VIDEO_HIGH_LOAD_MAX_ATTEMPTS - 1) and not model_unavailable
                next_region = region if same_region_retry_left else regions[(regions.index(region) + 1) % len(regions)]
                logger.warning(
                    "video.region.retry attempt=%s/%s region=%s wait=%ss next_region=%s error=%s",
                    global_attempt,
                    total_attempts,
                    region,
                    wait_sec,
                    next_region,
                    str(e),
                )
                if update.message is not None:
                    if model_unavailable:
                        await update.message.reply_text(
                            f"Модель видео недоступна в регионе {region} (404). "
                            f"Переключаюсь на регион {next_region}."
                        )
                    elif same_region_retry_left:
                        await update.message.reply_text(
                            f"Vertex Video API отклонил генерацию в регионе {region}. "
                            f"Жду {wait_sec} сек и пробую снова в этом же регионе."
                        )
                    else:
                        await update.message.reply_text(
                            f"Vertex Video API отклонил генерацию в регионе {region}. "
                            f"Жду {wait_sec} сек и переключаюсь на регион {next_region}."
                        )
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)
                if model_unavailable:
                    # Для 404 по модели бессмысленно повторять в том же регионе.
                    break
    if last_err is not None:
        raise last_err
    raise RuntimeError("Video generation failed: retry loop exited unexpectedly.")


async def _regenerate_video_prompt_from_error(scene: dict, error_text: str) -> str:
    """
    Предлагает безопасный видео-промпт после policy-модерации.
    """
    target_language = normalize_language_code(str(scene.get("video_prompt_current_language", "ru"))) or "ru"
    prompt_map = scene.get("video_prompt_by_lang")
    current = (
        str(prompt_map.get(target_language) or "").strip()
        if isinstance(prompt_map, dict)
        else ""
    ) or str(scene.get("video_prompt", "")).strip()
    mode = str(scene.get("generation_mode") or scene.get("mode") or "").strip().lower()
    model = str(scene.get("model") or "").strip().lower()
    speechless_story = mode in {"story", "silence"} or model.startswith("kling")
    if speechless_story:
        meta = scene.get("prompt_meta_json") if isinstance(scene.get("prompt_meta_json"), dict) else {}
        labels = scene.get("story_character_labels") or meta.get("story_character_labels") or {}
        character_names = ", ".join(str(value) for value in labels.values() if str(value).strip())
        action = str(scene.get("action") or meta.get("action") or "").strip()
        prompt = (
            "Rewrite the current silent image-to-video motion prompt so it is clear and safe for Kling.\n"
            "Return only one finished English prompt, without explanations or headings.\n"
            "Preserve the exact characters, identities, action, camera movement and object continuity from the current prompt "
            "and approved start frame. Never replace them with a grandmother, cat, kitchen characters or characters from another project.\n"
            "Describe only motion, reactions and camera movement. No dialogue, speech, voiceover, lip sync, audio, music, "
            "ambient sound, on-screen text, subtitles, logos or watermark.\n"
            "Do not duplicate duration/model/no-audio boilerplate; the application adds it once.\n"
            f"Scene characters: {character_names or '(use only the characters already present)'}.\n"
            f"Scene action: {action or '(preserve the current action)'}.\n\n"
            f"PROVIDER ERROR:\n{error_text}\n\n"
            f"CURRENT VIDEO PROMPT:\n{current}\n"
        )
        raw = await asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
        return str(raw or "").strip()
    speech = str(scene.get("text", "")).strip()
    speech_hint = (
        f"\nРЕПЛИКА СЦЕНЫ (поле text) — в итоговом prompt ОБЯЗАТЕЛЬНО одна подстрока вида "
        f"`CHARACTERS SPEAKS: ` сразу после двоеточия и пробела, затем ЭТОТ ТЕКСТ ДОСЛОВНО, без перефразирования:\n{speech}\n"
        if speech
        else ""
    )
    prompt = (
        "Перепиши video prompt для генерации ролика так, чтобы пройти модерацию политики.\n"
        "Верни только один готовый prompt, без пояснений, заголовков и служебных меток.\n"
        "Не копируй в ответ строки `ТЕКУЩИЙ VIDEO PROMPT`, `ФИДБЕК ПОЛЬЗОВАТЕЛЯ` или `ТЕКСТ ОШИБКИ ПРОВАЙДЕРА`.\n"
        "Сохрани действие сцены, персонажей, стиль референсов и предметную непрерывность кадра.\n"
        "Video prompt должен быть только про движение, действия, речь, lipsync, реакцию персонажей и движение камеры. "
        "Не описывай местность, фон, статичную раскладку блюда, состав блюда или список ингредиентов: это берётся из start/end frame. "
        "Не добавляй новые визуальные продукты, гарниры, посуду или декор.\n"
        "Стиль кадра должен быть не-реалистичным: без реализма и без фотореализма.\n"
        "Бабушка и кот обязательно мультяшные; при необходимости мягко уточни cartoon style без громоздких служебных блоков.\n"
        "Запрети любые субтитры/текст в кадре и любую фоновую музыку/саундтрек.\n"
        "Если указана реплика сцены выше — твой ответ ДОЛЖЕН содержать подстроку `CHARACTERS SPEAKS: ` + дословная реплика; "
        "код её не допишет.\n"
        "Для озвучки: произносить реплику дословно, без добавленных слов и без пропусков; финал обычной повествовательной фразы "
        "должен звучать завершенно и спокойно, без вопросительной/повышающейся интонации.\n"
        f"{RUSSIAN_YO_RULE}\n"
        "Обязательно включи точный фрагмент: "
        f"voice_lock_full: {GRANDMA_VOICE_LOCK_FULL}\n"
        "Обязательно явно укажи язык озвучки в соответствии с целевым языком сцены.\n"
        "Соблюдай физическую правдоподобность кухни и не добавляй лишних персонажей или лишних параллельных действий.\n"
        "Убери потенциально рискованные/запрещенные формулировки.\n\n"
        f"ТЕКСТ ОШИБКИ ПРОВАЙДЕРА:\n{error_text}\n"
        f"{speech_hint}\n"
        f"\nТЕКУЩИЙ VIDEO PROMPT:\n{current}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
    return enforce_video_prompt_voice_requirements(raw.strip(), scene, target_language=target_language)


def _build_video_prompt_rewrite_llm_prompt(
    prompt_text: str,
    error_text: str,
    *,
    phrases: str | None,
) -> str:
    """
    Собирает пользовательское сообщение для Gemini при переписывании video prompt после отклонения.
    Включает полный текст ошибки и (если есть) реплики, которые нельзя потерять.
    """
    stripped_prompt = prompt_text.strip()
    err = (error_text or "").strip()
    p = (phrases or "").strip()
    phrases_block = p if p else "(отдельный список реплик не передан.)"
    return (
        "Перепиши video prompt для генерации ролика так, чтобы пройти модерацию политики (RAI/safety).\n"
        "Верни только один готовый prompt, без пояснений.\n"
        "Сохрани действие сцены и визуальный стиль, убери рискованные формулировки.\n"
        "Video prompt должен быть только про движение, действия, речь, lipsync, реакцию персонажей и движение камеры. "
        "Не описывай местность, фон, статичную раскладку блюда, состав блюда или список ингредиентов: это берётся из start/end frame. "
        "Не добавляй новые визуальные продукты, гарниры, посуду или декор.\n"
        "Стиль кадра должен быть не-реалистичным: без реализма и без фотореализма.\n"
        "Бабушка и кот обязательно мультяшные; при необходимости мягко уточни cartoon style без громоздких служебных блоков.\n"
        "Запрети любые субтитры/текст в кадре и любую фоновую музыку/саундтрек.\n"
        "Ориентируйся на раздел «ПОЧЕМУ ОТКЛОНЕНО»: исправь те части промпта, на которые указывает "
        "ошибка (filtered reasons, policy, safety, коды модерации и т.п.).\n"
        "Если в «ФРАЗЫ И РЕПЛИКИ» есть текст — сохрани смысл и формулировки в итоговом описании сцены.\n\n"
        "Обязательно включи точный фрагмент: "
        f"voice_lock_full: {GRANDMA_VOICE_LOCK_FULL}\n"
        "Соблюдай физическую правдоподобность кухни и не добавляй лишних персонажей или лишних параллельных действий.\n"
        "Обязательно явно укажи, что все фразы и озвучка на русском языке.\n"
        f"{RUSSIAN_YO_RULE}\n"
        "Если в «ФРАЗЫ И РЕПЛИКИ» есть непустой текст — итоговый prompt ОБЯЗАН содержать подстроку "
        "`CHARACTERS SPEAKS: ` + тот же текст дословно (символ в символ); без этого озвучка по сценарию пропадёт; код не дописывает.\n\n"
        f"ИСХОДНЫЙ VIDEO PROMPT:\n{stripped_prompt}\n\n"
        f"ФРАЗЫ И РЕПЛИКИ (сохранить по смыслу):\n{phrases_block}\n\n"
        f"ПОЧЕМУ ОТКЛОНЕНО (полный текст ошибки провайдера / модерации):\n{err}\n"
    )


async def _regenerate_video_prompt_from_text(
    prompt_text: str,
    error_text: str,
    *,
    phrases: str | None = None,
) -> str:
    """
    Переписывает сырой video prompt после отклонения модерацией.

    В Gemini уходит исходный промпт, полный текст ошибки и при необходимости реплики для озвучки.
    """
    stripped = prompt_text.strip()
    logger.info("[video prompt rewrite] rejected prompt:\n%s", stripped)
    llm_prompt = _build_video_prompt_rewrite_llm_prompt(
        prompt_text, error_text, phrases=phrases
    )
    raw = await asyncio.to_thread(gemini_generate_sync, llm_prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
    return enforce_video_prompt_voice_requirements(raw.strip(), None)


def enforce_video_prompt_voice_requirements(
    video_prompt: str,
    scene: dict[str, Any] | None = None,
    *,
    target_language: str = "ru",
) -> str:
    """
    Добавляет в video prompt обязательные ограничения по голосу, если модель их опустила:
    блок ``voice_lock_full`` и явное требование целевого языка озвучки.

    Для ``veo3*`` дописывает ``VEO3_FAST_VIDEO_FRAMING_RULE`` (полная бабушка и все персонажи в кадре,
    крупный план, липсинк, смена ракурса).

    Строку ``CHARACTERS SPEAKS:`` с репликой формирует только LLM (см. промпты production / rewrite).
    """
    vp = (video_prompt or "").strip()
    lang = (target_language or "ru").strip().lower()
    lang_rule = _VOICE_RULES_BY_LANGUAGE.get(lang, _VOICE_RULES_BY_LANGUAGE["en"])
    required_lock = _voice_lock_for_language(lang)
    if not vp:
        vp = (
            f"voice_lock_full: {required_lock}. {lang_rule}. {_NON_REALISM_RULE}. "
            f"{_CARTOON_CHARACTER_RUNTIME_RULE}. {_NO_SUBTITLES_MUSIC_RULE}."
        )
    else:
        source_token = _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG["en" if lang == "ru" else "ru"]
        target_token = _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG.get(lang, _VOICE_LOCK_LANGUAGE_TOKEN_BY_LANG["en"])
        vp = _sanitize_video_prompt_language_references(vp.replace(source_token, target_token), lang)
        if "voice_lock_full:" not in vp.lower():
            vp = f"{vp} voice_lock_full: {required_lock}".strip()
        if lang_rule.lower() not in vp.lower():
            vp = f"{vp}. {lang_rule}".strip()
        if "photoreal" not in vp.lower() and "no realism" not in vp.lower():
            vp = f"{vp}. {_NON_REALISM_RULE}".strip()
        if "cartoon grandmother" not in vp.lower() or "cartoon cat" not in vp.lower():
            vp = f"{vp}. {_CARTOON_CHARACTER_RUNTIME_RULE}".strip()
        if _NO_SUBTITLES_MUSIC_RULE.lower() not in vp.lower():
            vp = f"{vp}. {_NO_SUBTITLES_MUSIC_RULE}".strip()
    vp = enforce_video_prompt_visual_source_rule(vp)
    vp = _ensure_named_grandma_voice_lock(vp, scene or {"voice_needed": True}, lang)
    if VOICE_CONTINUITY_RULE.lower() not in vp.lower():
        vp = f"{vp}\n{VOICE_CONTINUITY_RULE}"
    return _sanitize_video_prompt_language_references(append_veo3_video_framing_constraints(vp, scene), lang)


async def _regenerate_image_prompt_from_video_error(scene: dict, error_text: str) -> str:
    """
    Предлагает новый start-frame prompt, если видео-генератор отклонил входное изображение.
    """
    current = str(scene.get("start_frame_prompt", "")).strip()
    continuity = ", ".join(str(x) for x in (scene.get("continuity_anchors") or []) if str(x).strip())
    prompt = (
        "Перепиши промпт стартового кадра так, чтобы изображение стало валидным для video generator.\n"
        "Верни только один готовый prompt на русском, без пояснений.\n"
        f"{RUSSIAN_YO_RULE}\n"
        "Сохрани персонажей, основное действие и предметную непрерывность: посуда, кастрюля/сковорода, "
        "плита или рабочая поверхность, ингредиенты и состояние еды должны оставаться узнаваемыми.\n"
        "Избегай сложных артефактов, размытий, лишних объектов, логотипов и текста поверх кадра.\n\n"
        f"CONTINUITY ANCHORS: {continuity or '-'}\n"
        f"ТЕКУЩИЙ START PROMPT:\n{current}\n\n"
        f"ТЕКСТ ОШИБКИ VIDEO GENERATOR:\n{error_text}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt, model_profile=GEMINI_MODEL_PROFILE_EDIT)
    return raw.strip()
