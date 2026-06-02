"""Telethon transport for Syntax fallback video generation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sqlite3
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .syntax_getvideo_download import fetch_mp4_bytes_from_getvideo_page
from .syntax_image_prepare import prepare_syntax_frame_bytes
from .llm_utils import extract_json_object, gemini_generate_sync

logger = logging.getLogger(__name__)

SYNTAX_PROMPT_DEFAULT_MAX_CHARS = 2000
SYNTAX_PROMPT_HARD_MAX_CHARS = 2000

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover - optional dependency handling
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class _SessionCandidate:
    kind: str
    value: str


_SESSION_POOL_CONDITION = threading.Condition()
_SESSION_POOL_IN_USE: set[_SessionCandidate] = set()


def _normalize_username(raw: str) -> str:
    clean = (raw or "").strip()
    if not clean:
        return "syntxaibot"
    return clean[1:] if clean.startswith("@") else clean


def _split_env_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _syntax_session_candidates() -> list[_SessionCandidate]:
    candidates: list[_SessionCandidate] = []
    seen: set[_SessionCandidate] = set()
    for value in _split_env_list(os.getenv("TELETHON_STRING_SESSIONS")):
        candidate = _SessionCandidate("string", value)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    single_string = (os.getenv("TELETHON_STRING_SESSION") or "").strip()
    if single_string:
        candidate = _SessionCandidate("string", single_string)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    for value in _split_env_list(os.getenv("TELETHON_SESSION_PATHS")):
        candidate = _SessionCandidate("path", value)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    single_path = (os.getenv("TELETHON_SESSION_PATH") or "").strip()
    if single_path:
        candidate = _SessionCandidate("path", single_path)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def _acquire_syntax_session_candidate(timeout_sec: int | None) -> _SessionCandidate | None:
    candidates = _syntax_session_candidates()
    if not candidates:
        return None
    deadline = time.monotonic() + timeout_sec if timeout_sec is not None and timeout_sec > 0 else None
    with _SESSION_POOL_CONDITION:
        while True:
            for candidate in candidates:
                if candidate not in _SESSION_POOL_IN_USE:
                    _SESSION_POOL_IN_USE.add(candidate)
                    return candidate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                _SESSION_POOL_CONDITION.wait(timeout=remaining)
            else:
                _SESSION_POOL_CONDITION.wait()


def _release_syntax_session_candidate(candidate: _SessionCandidate | None) -> None:
    if candidate is None:
        return
    with _SESSION_POOL_CONDITION:
        _SESSION_POOL_IN_USE.discard(candidate)
        _SESSION_POOL_CONDITION.notify()


@dataclass(frozen=True, slots=True)
class _BatchRequestState:
    key: str
    attempt_id: str
    prompt_text: str
    prompt_shortened: bool
    expected_lang_token: str | None
    start_image: bytes
    end_image: bytes | None


def _result(
    *,
    success: bool,
    video_bytes: bytes | None,
    video_uri: str | None,
    provider_message: str,
    attempt_id: str,
) -> Any:
    from .syntax_video_fallback import FallbackResult

    return FallbackResult(
        success=success,
        video_bytes=video_bytes,
        video_uri=video_uri,
        provider_message=provider_message,
        attempt_id=attempt_id,
    )


def _extract_text(message: Any) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "").strip()


def _first_word(text: str) -> str:
    match = re.search(r"\S+", text or "")
    return match.group(0).strip().lower() if match else ""


def _expected_lang_token_from_prompt(prompt: str) -> str | None:
    first = _first_word(prompt)
    if not first:
        return None
    if first.startswith("lang:"):
        return first
    if first.startswith("lang="):
        return "lang:" + first.split("=", 1)[1]
    return None


def _extract_mp4_uri(text: str) -> str | None:
    match = re.search(r"https?://\S+\.mp4(?:\?\S*)?", text, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _extract_getvideo_uri(text: str) -> str | None:
    m = re.search(r"https?://getvideo\.syntxai\.net/\S+", text, flags=re.IGNORECASE)
    return m.group(0).rstrip(").,]}\"'") if m else None


def _urls_from_message_entities(message: Any) -> list[str]:
    out: list[str] = []
    entities = getattr(message, "entities", None) or []
    for ent in entities:
        cls = type(ent).__name__
        url = getattr(ent, "url", None)
        if cls == "MessageEntityTextUrl" and url:
            out.append(str(url).strip())
        if cls == "MessageEntityUrl":
            t = _extract_text(message)
            off = int(getattr(ent, "offset", 0) or 0)
            ln = int(getattr(ent, "length", 0) or 0)
            frag = t[off : off + ln] if ln else ""
            if frag.startswith("http"):
                out.append(frag.strip())
    return out


def _collect_urls_from_message(message: Any) -> list[str]:
    text = _extract_text(message)
    urls: list[str] = []
    urls.extend(_urls_from_message_entities(message))
    urls.extend(re.findall(r"https?://\S+", text))
    mp4 = _extract_mp4_uri(text)
    if mp4:
        urls.append(mp4)
    gv = _extract_getvideo_uri(text)
    if gv:
        urls.append(gv)
    # Dedup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = u.strip().rstrip(").,]}\"'")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _image_ext(data: bytes) -> str:
    """Best-effort extension for Telegram upload compatibility."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return "jpg"


def _syntax_bot_message_is_hard_reject(text: str, _urls: list[str]) -> bool:
    """
    Syntax иногда отвечает финальным сообщением об ошибке / фильтрах (без видео).

    Такие ответы нужно завершать сразу, а не ждать таймаут.
    """
    raw = text or ""
    low = raw.lower()
    if re.search(r"veo(?:\s*3)?\W{0,12}на\s+обнов", low, flags=re.IGNORECASE):
        return True
    veo_update_markers = (
        "veo на обнов",
        "veo3 на обнов",
        "veo 3 на обнов",
        "veo обнов",
        "veo3 обнов",
        "veo is updating",
        "veo3 is updating",
        "veo 3 is updating",
        "veo maintenance",
        "veo3 maintenance",
        "veo 3 maintenance",
        "veo temporarily unavailable",
        "veo3 temporarily unavailable",
        "veo 3 temporarily unavailable",
    )
    if any(m in low for m in veo_update_markers):
        return True
    if "произошла ошибка" in low and ("попробуйте" in low or "еще раз" in low or "ещё раз" in low):
        return True
    if "\u274c" in raw and ("ошибк" in low or "фильтр" in low or "filter" in low):
        return True
    markers = (
        "отклонен",
        "отклонён",
        "нарушает правила",
        "заблокирован",
        "не прошло модерац",
        "content policy",
        "safety",
    )
    return any(m in low for m in markers)


def _text_looks_like_syntax_pending_status(text: str) -> bool:
    """Syntax often *edits* the same message from status to final link — keep re-reading it."""
    low = (text or "").lower()
    markers = (
        "ожидает выполнения",
        "ожидайте",
        "генерац",
        "обрабатывается",
        "в очереди",
        "high load",
        "высокая нагрузка",
        "veo3",
        "veo 3",
    )
    return any(m in low for m in markers)


def _syntax_prompt_max_chars() -> int:
    raw = (os.getenv("SYNTAX_PROMPT_MAX_CHARS") or str(SYNTAX_PROMPT_DEFAULT_MAX_CHARS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return SYNTAX_PROMPT_DEFAULT_MAX_CHARS
    return max(1000, min(SYNTAX_PROMPT_HARD_MAX_CHARS, value))


def _shorten_prompt_for_syntax_with_gemini_sync(prompt: str, *, max_len: int) -> str | None:
    original = (prompt or "").strip()
    if not original:
        return ""
    payload = {
        "max_chars": max_len,
        "current_prompt": original,
        "requirements": [
            "Return a complete Syntax video prompt, not a summary.",
            "Keep the core action, camera/motion, style, safety/physics constraints and voice/lip-sync instructions.",
            "If CHARACTERS SPEAKS is present, preserve that marker and the spoken phrase as accurately as possible.",
            "The returned prompt must be at most max_chars characters.",
        ],
    }
    instruction = (
        "You rewrite video-generation prompts for Syntax, which has a strict text limit.\n"
        'Return ONLY a JSON object: {"prompt":"..."} without markdown.\n'
        "The prompt value must be a complete ready-to-send video prompt and must not exceed max_chars.\n\n"
        "INPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    for attempt in (1, 2):
        extra = "" if attempt == 1 else "\n\nYour previous answer was too long or invalid. Compress more aggressively."
        try:
            raw = gemini_generate_sync(instruction + extra)
            obj = extract_json_object(raw)
        except Exception as exc:
            logger.warning("syntax_prompt_shorten.gemini failed attempt=%s err=%s", attempt, exc)
            continue
        shortened = obj.get("prompt")
        if not isinstance(shortened, str):
            continue
        shortened = shortened.strip()
        if shortened and len(shortened) <= max_len:
            return shortened
        logger.warning(
            "syntax_prompt_shorten.invalid_length attempt=%s chars=%s limit=%s",
            attempt,
            len(shortened),
            max_len,
        )
    return None


def _prepare_prompt_for_syntax(prompt: str, *, max_len: int | None = None) -> tuple[str, bool] | None:
    limit = max_len if max_len is not None else _syntax_prompt_max_chars()
    original = (prompt or "").strip()
    if len(original) <= limit:
        return original, False
    shortened = _shorten_prompt_for_syntax_with_gemini_sync(original, max_len=limit)
    if shortened is None:
        return None
    return shortened, True


def _syntax_allow_loose_correlation() -> bool:
    """
    Если не strict — любое входящее от бота **после** нашего последнего ``id`` считается ответом на попытку.

    У Syntax часто нет ``reply_to`` на кадр/промпт; достаточно смотреть последние сообщения в1:1 чате.
    """
    raw = (os.getenv("SYNTAX_STRICT_REPLY_MATCH") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def _poll_interval_sec() -> float:
    raw = (os.getenv("SYNTAX_FALLBACK_POLL_SEC") or "10").strip()
    try:
        sec = float(raw)
    except ValueError:
        return 10.0
    return max(10.0, min(120.0, sec))


def _message_looks_like_video(message: Any) -> bool:
    if getattr(message, "video", None) is not None:
        return True
    document = getattr(message, "document", None)
    if document is None:
        return False
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if mime == "video/mp4":
        return True
    attrs = getattr(document, "attributes", None) or []
    for attr in attrs:
        file_name = str(getattr(attr, "file_name", "") or "").lower()
        if file_name.endswith(".mp4"):
            return True
    return False


async def _try_download_from_urls(urls: list[str]) -> tuple[bytes | None, str | None, str]:
    """Returns (video_bytes, video_uri, provider_message)."""
    for url in urls:
        if url.lower().endswith(".mp4"):
            return None, url, f"Syntax returned direct mp4 link: {url}"
        if "getvideo.syntxai.net/" in url.lower():
            try:
                data = await asyncio.to_thread(fetch_mp4_bytes_from_getvideo_page, url)
                return data, url, "Downloaded mp4 from Syntax getvideo page."
            except Exception:
                # Page may still be preparing; keep polling.
                continue
    return None, None, ""


async def _process_bot_message(
    client: Any,
    *,
    message: Any,
    sent_ids: set[int],
    expected_lang_token: str | None = None,
) -> tuple[bytes | None, str | None, str, bool, bool] | None:
    """
    Handle one incoming bot message for this attempt.

    Returns:
        ``None`` — не относится к нашей цепочке (можно больше не рассматривать).
        Кортеж ``(video_bytes, video_uri, info, retry_later, hard_reject)``:
        при ``hard_reject=True`` — окончательный отказ Syntax (фильтры, ошибка).
    """
    if message is None:
        return None
    if getattr(message, "out", False):
        return None
    mid = int(getattr(message, "id", 0) or 0)
    reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    our_high = max(sent_ids) if sent_ids else 0
    correlated = bool(reply_id and reply_id in sent_ids)
    if not correlated:
        if not (_syntax_allow_loose_correlation() and mid > our_high):
            return None
        text_for_lang = _extract_text(message)
        if expected_lang_token and text_for_lang:
            if expected_lang_token not in text_for_lang.lower():
                return None

    if _message_looks_like_video(message):
        data = await client.download_media(message, file=bytes)
        if isinstance(data, bytes) and data:
            return data, None, "Video downloaded from Syntax bot.", False, False
        return None, None, "Syntax sent a video attachment but download failed.", True, False

    text = _extract_text(message)
    urls = _collect_urls_from_message(message)
    if _syntax_bot_message_is_hard_reject(text, urls):
        body = (text[:4500] if text else "Syntax отклонил запрос.").strip()
        return None, None, body, False, True

    vbytes, vuri, msg = await _try_download_from_urls(urls)
    if vbytes or vuri:
        return vbytes, vuri, msg or "Syntax returned a video link.", False, False
    if urls:
        hint = (msg or text or "Syntax link present; video not ready yet.").strip()
        return None, None, (hint or "Waiting for video file.")[:2000], True, False
    if text and ("прямая ссылка" in text.lower() or "getvideo.syntxai.net" in text.lower()):
        return None, None, text[:2000], True, False
    if text and _text_looks_like_syntax_pending_status(text):
        return None, None, text[:2000], True, False
    if text:
        return None, None, text[:2000], False, False
    return None, None, "", False, False


async def _syntax_dispatch_bot_reply(
    client: Any,
    msg: Any,
    *,
    reply_anchor: int,
    sent_ids: set[int],
    processed: set[int],
    pending_refetch: set[int],
    attempt_id: str,
    last_holder: list[str],
    expected_lang_token: str | None,
) -> Any | None:
    """Apply one bot ``Message``; on success returns ``FallbackResult``."""
    mid = int(getattr(msg, "id", 0) or 0)
    if mid <= reply_anchor:
        return None
    out = await _process_bot_message(
        client,
        message=msg,
        sent_ids=sent_ids,
        expected_lang_token=expected_lang_token,
    )
    if out is None:
        if mid not in pending_refetch:
            processed.add(mid)
        return None
    vbytes, vuri, pmsg, retry_later, hard_reject = out
    prev = last_holder[0]
    if hard_reject:
        processed.add(mid)
        pending_refetch.discard(mid)
        combined = (
            f"{pmsg}\n\n"
            "Syntax не вернул видео. Можно повторить с другим промптом или использовать основной пайплайн (Vertex)."
        ).strip()
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=combined[:8000],
            attempt_id=attempt_id,
        )
    if vbytes or vuri:
        processed.add(mid)
        pending_refetch.discard(mid)
        msg_out = " ".join(x for x in (pmsg, prev) if x).strip()
        return _result(
            success=True,
            video_bytes=vbytes,
            video_uri=vuri,
            provider_message=msg_out or (pmsg or "Syntax fallback ok."),
            attempt_id=attempt_id,
        )
    if retry_later:
        pending_refetch.add(mid)
        if pmsg:
            last_holder[0] = pmsg
        return None
    processed.add(mid)
    pending_refetch.discard(mid)
    if pmsg:
        last_holder[0] = pmsg
    return None


def _first_message_from_get_messages(got: Any) -> Any | None:
    """Normalize Telethon ``get_messages(..., ids=…)`` result: one ``Message`` or a sequence."""
    if got is None:
        return None
    if isinstance(got, (list, tuple)):
        return got[0] if len(got) > 0 else None
    if getattr(got, "id", None) is not None:
        return got
    try:
        return got[0] if got else None
    except (TypeError, IndexError, KeyError):
        return None


async def _poll_syntax_bot_until_video(
    client: Any,
    target: Any,
    *,
    reply_anchor: int,
    sent_ids: set[int],
    attempt_id: str,
    poll: float,
    deadline: float,
    initial_last_info: str,
    timeout: int,
    expected_lang_token: str | None,
) -> Any:
    """Scan bot replies every ``poll`` seconds; re-fetch edited status messages by id."""
    processed: set[int] = set(sent_ids)
    pending_refetch: set[int] = set()
    last_holder = [initial_last_info]

    our_high = max(sent_ids) if sent_ids else 0
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        logger.info(
            "syntax_fallback.poll attempt_id=%s poll_sec=%s remaining_sec=%.0f last=%s",
            attempt_id,
            poll,
            remaining,
            (last_holder[0] or "")[:120],
        )
        latest_raw = await client.get_messages(target, limit=1)
        latest = _first_message_from_get_messages(latest_raw)
        if latest is not None and not getattr(latest, "out", False):
            lmid = int(getattr(latest, "id", 0) or 0)
            if lmid > our_high and lmid not in processed:
                done = await _syntax_dispatch_bot_reply(
                    client,
                    latest,
                    reply_anchor=reply_anchor,
                    sent_ids=sent_ids,
                    processed=processed,
                    pending_refetch=pending_refetch,
                    attempt_id=attempt_id,
                    last_holder=last_holder,
                    expected_lang_token=expected_lang_token,
                )
                if done is not None:
                    return done
        async for msg in client.iter_messages(target, limit=40):
            mid = int(getattr(msg, "id", 0) or 0)
            if mid <= reply_anchor or mid in processed:
                continue
            done = await _syntax_dispatch_bot_reply(
                client,
                msg,
                reply_anchor=reply_anchor,
                sent_ids=sent_ids,
                processed=processed,
                pending_refetch=pending_refetch,
                attempt_id=attempt_id,
                last_holder=last_holder,
                expected_lang_token=expected_lang_token,
            )
            if done is not None:
                return done
        for mid in list(pending_refetch):
            if mid in processed:
                pending_refetch.discard(mid)
                continue
            batch = await client.get_messages(target, ids=mid)
            fresh = _first_message_from_get_messages(batch)
            if fresh is None:
                pending_refetch.discard(mid)
                continue
            done = await _syntax_dispatch_bot_reply(
                client,
                fresh,
                reply_anchor=reply_anchor,
                sent_ids=sent_ids,
                processed=processed,
                pending_refetch=pending_refetch,
                attempt_id=attempt_id,
                last_holder=last_holder,
                expected_lang_token=expected_lang_token,
            )
            if done is not None:
                return done
        await asyncio.sleep(poll)

    tail = last_holder[0]
    return _result(
        success=False,
        video_bytes=None,
        video_uri=None,
        provider_message=(
            f"Syntax fallback timed out after {timeout}s (poll={poll}s). Last: {tail}".strip()
        ),
        attempt_id=attempt_id,
    )


async def _run_fallback_async(
    *,
    attempt_id: str,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None,
    timeout_sec: int | None,
    acquire_timeout_sec: int | None,
) -> Any:
    _ = model, duration_sec
    if TelegramClient is None:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Telethon is not installed; Syntax fallback transport is unavailable.",
            attempt_id=attempt_id,
        )

    api_id_raw = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id_raw.isdigit() or not api_hash:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Missing TELEGRAM_API_ID/TELEGRAM_API_HASH for Syntax Telethon fallback.",
            attempt_id=attempt_id,
        )

    session_candidate = _acquire_syntax_session_candidate(acquire_timeout_sec)
    if session_candidate is None:
        candidates = _syntax_session_candidates()
        if not candidates:
            return _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=(
                    "Missing Telethon session: set TELETHON_SESSION_PATH / TELETHON_STRING_SESSION "
                    "or multi-session TELETHON_SESSION_PATHS / TELETHON_STRING_SESSIONS."
                ),
                attempt_id=attempt_id,
            )
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=(
                "No free Telethon session became available before timeout. "
                "Add more sessions or increase SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC."
            ),
            attempt_id=attempt_id,
        )
    if session_candidate.kind == "string":
        if StringSession is None:
            _release_syntax_session_candidate(session_candidate)
            return _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Telethon StringSession is unavailable in this environment.",
                attempt_id=attempt_id,
            )
        session_obj: Any = StringSession(session_candidate.value)
    else:
        session_obj = session_candidate.value

    try:
        if timeout_sec is not None and int(timeout_sec) > 0:
            timeout = int(timeout_sec)
        else:
            env_raw = (os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "").strip()
            timeout = int(env_raw) if env_raw.isdigit() and int(env_raw) > 0 else 15 * 60
        poll = _poll_interval_sec()
        bot_username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))
    except Exception:
        _release_syntax_session_candidate(session_candidate)
        raise

    try:
        start_image = prepare_syntax_frame_bytes(start_image)
        if end_image:
            end_image = prepare_syntax_frame_bytes(end_image)
    except Exception as prep_err:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=f"Syntax frame prepare failed: {prep_err}",
            attempt_id=attempt_id,
        )

    prepared_prompt = await asyncio.to_thread(_prepare_prompt_for_syntax, prompt)
    if prepared_prompt is None:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=(
                f"Syntax prompt is longer than {_syntax_prompt_max_chars()} chars, "
                "and Gemini could not shorten it to the Syntax limit."
            ),
            attempt_id=attempt_id,
        )
    prompt_text, prompt_shortened = prepared_prompt
    expected_lang_token = _expected_lang_token_from_prompt(prompt_text)
    sent_ids: set[int] = set()
    last_info = ""

    try:
        client = TelegramClient(session_obj, int(api_id_raw), api_hash)
        try:
            await client.connect()
            is_authorized = getattr(client, "is_user_authorized", None)
            if callable(is_authorized):
                if not await is_authorized():
                    return _result(
                        success=False,
                        video_bytes=None,
                        video_uri=None,
                        provider_message=(
                            "Telethon session is not authorized. "
                            "Run tools/telethon_login.py once to create an authorized session."
                        ),
                        attempt_id=attempt_id,
                    )
            try:
                target = await client.get_entity(bot_username)
                start_file = io.BytesIO(start_image)
                start_file.name = f"start_frame.{_image_ext(start_image)}"
                start_msg = await client.send_file(target, file=start_file, force_document=False)
                sent_ids.add(int(start_msg.id))
                reply_anchor = int(start_msg.id)
                if end_image:
                    end_file = io.BytesIO(end_image)
                    end_file.name = f"end_frame.{_image_ext(end_image)}"
                    end_msg = await client.send_file(target, file=end_file, force_document=False)
                    sent_ids.add(int(end_msg.id))
                    reply_anchor = int(end_msg.id)

                request_message = await client.send_message(target, prompt_text, reply_to=reply_anchor)
                sent_ids.add(int(request_message.id))
                if prompt_shortened:
                    last_info = (
                        f"Prompt exceeded Syntax limit ({_syntax_prompt_max_chars()} chars) "
                        "and was shortened with Gemini before send."
                    )

                deadline = time.monotonic() + timeout
                return await _poll_syntax_bot_until_video(
                    client,
                    target,
                    reply_anchor=reply_anchor,
                    sent_ids=sent_ids,
                    attempt_id=attempt_id,
                    poll=poll,
                    deadline=deadline,
                    initial_last_info=last_info,
                    timeout=timeout,
                    expected_lang_token=expected_lang_token,
                )
            except Exception as exc:
                return _result(
                    success=False,
                    video_bytes=None,
                    video_uri=None,
                    provider_message=f"Syntax fallback transport error: {exc}",
                    attempt_id=attempt_id,
                )
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            _release_syntax_session_candidate(session_candidate)
    except sqlite3.OperationalError as exc:
        _release_syntax_session_candidate(session_candidate)
        if "locked" in str(exc).lower():
            return _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=(
                    "Сессия Telethon занята другим процессом (database is locked). "
                    "Остановите другие процессы с тем же TELETHON_SESSION_PATH/TELETHON_SESSION_PATHS и повторите."
                ),
                attempt_id=attempt_id,
            )
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=f"Syntax Telethon SQLite error: {exc}",
            attempt_id=attempt_id,
        )
    except Exception as exc:
        _release_syntax_session_candidate(session_candidate)
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=f"Syntax fallback transport error: {exc}",
            attempt_id=attempt_id,
        )


async def _run_batch_fallback_async(
    *,
    requests: list[Any],
    model: str,
    duration_sec: int,
    timeout_sec: int | None,
    acquire_timeout_sec: int | None,
) -> dict[str, Any]:
    _ = model, duration_sec
    results: dict[str, Any] = {}
    if not requests:
        return results
    if TelegramClient is None:
        for request in requests:
            results[str(request.key)] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Telethon is not installed; Syntax fallback transport is unavailable.",
                attempt_id=uuid4().hex,
            )
        return results

    api_id_raw = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id_raw.isdigit() or not api_hash:
        for request in requests:
            results[str(request.key)] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Missing TELEGRAM_API_ID/TELEGRAM_API_HASH for Syntax Telethon fallback.",
                attempt_id=uuid4().hex,
            )
        return results

    session_candidate = _acquire_syntax_session_candidate(acquire_timeout_sec)
    if session_candidate is None:
        provider_message = (
            "Missing Telethon session: set TELETHON_SESSION_PATH / TELETHON_STRING_SESSION "
            "or multi-session TELETHON_SESSION_PATHS / TELETHON_STRING_SESSIONS."
            if not _syntax_session_candidates()
            else "No free Telethon session became available before timeout. "
            "Add more sessions or increase SYNTAX_FALLBACK_LOCK_TIMEOUT_SEC."
        )
        for request in requests:
            results[str(request.key)] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=provider_message,
                attempt_id=uuid4().hex,
            )
        return results

    if session_candidate.kind == "string":
        if StringSession is None:
            _release_syntax_session_candidate(session_candidate)
            for request in requests:
                results[str(request.key)] = _result(
                    success=False,
                    video_bytes=None,
                    video_uri=None,
                    provider_message="Telethon StringSession is unavailable in this environment.",
                    attempt_id=uuid4().hex,
                )
            return results
        session_obj: Any = StringSession(session_candidate.value)
    else:
        session_obj = session_candidate.value

    if timeout_sec is not None and int(timeout_sec) > 0:
        timeout = int(timeout_sec)
    else:
        env_raw = (os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "").strip()
        timeout = int(env_raw) if env_raw.isdigit() and int(env_raw) > 0 else 15 * 60
    poll = _poll_interval_sec()
    bot_username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))

    ready: list[_BatchRequestState] = []
    for request in requests:
        key = str(request.key)
        attempt_id = uuid4().hex
        try:
            start_image = prepare_syntax_frame_bytes(request.start_image)
            end_image = prepare_syntax_frame_bytes(request.end_image) if request.end_image else None
        except Exception as prep_err:
            results[key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=f"Syntax frame prepare failed: {prep_err}",
                attempt_id=attempt_id,
            )
            continue
        prepared_prompt = await asyncio.to_thread(_prepare_prompt_for_syntax, request.prompt)
        if prepared_prompt is None:
            results[key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=(
                    f"Syntax prompt is longer than {_syntax_prompt_max_chars()} chars, "
                    "and Gemini could not shorten it to the Syntax limit."
                ),
                attempt_id=attempt_id,
            )
            continue
        prompt_text, prompt_shortened = prepared_prompt
        ready.append(
            _BatchRequestState(
                key=key,
                attempt_id=attempt_id,
                prompt_text=prompt_text,
                prompt_shortened=prompt_shortened,
                expected_lang_token=_expected_lang_token_from_prompt(prompt_text),
                start_image=start_image,
                end_image=end_image,
            )
        )

    if not ready:
        _release_syntax_session_candidate(session_candidate)
        return results

    client = TelegramClient(session_obj, int(api_id_raw), api_hash)
    try:
        await client.connect()
        is_authorized = getattr(client, "is_user_authorized", None)
        if callable(is_authorized) and not await is_authorized():
            for request in ready:
                results[request.key] = _result(
                    success=False,
                    video_bytes=None,
                    video_uri=None,
                    provider_message=(
                        "Telethon session is not authorized. "
                        "Run tools/telethon_login.py once to create an authorized session."
                    ),
                    attempt_id=request.attempt_id,
                )
            return results

        target = await client.get_entity(bot_username)
        request_runtime: list[tuple[_BatchRequestState, int, set[int], str]] = []
        for request in ready:
            sent_ids: set[int] = set()
            start_file = io.BytesIO(request.start_image)
            start_file.name = f"start_frame.{_image_ext(request.start_image)}"
            start_msg = await client.send_file(target, file=start_file, force_document=False)
            sent_ids.add(int(start_msg.id))
            reply_anchor = int(start_msg.id)
            if request.end_image:
                end_file = io.BytesIO(request.end_image)
                end_file.name = f"end_frame.{_image_ext(request.end_image)}"
                end_msg = await client.send_file(target, file=end_file, force_document=False)
                sent_ids.add(int(end_msg.id))
                reply_anchor = int(end_msg.id)
            request_message = await client.send_message(target, request.prompt_text, reply_to=reply_anchor)
            sent_ids.add(int(request_message.id))
            last_info = ""
            if request.prompt_shortened:
                last_info = (
                    f"Prompt exceeded Syntax limit ({_syntax_prompt_max_chars()} chars) "
                    "and was shortened with Gemini before send."
                )
            request_runtime.append((request, reply_anchor, sent_ids, last_info))

        deadline = time.monotonic() + timeout
        pending_states = {
            request.key: {
                "request": request,
                "reply_anchor": reply_anchor,
                "sent_ids": sent_ids,
                "processed": set(sent_ids),
                "pending_refetch": set(),
                "last_holder": [last_info],
            }
            for request, reply_anchor, sent_ids, last_info in request_runtime
        }

        async def _consume_for_batch_message(message: Any) -> None:
            if message is None or getattr(message, "out", False):
                return
            for key in list(pending_states.keys()):
                state = pending_states.get(key)
                if state is None:
                    continue
                mid = int(getattr(message, "id", 0) or 0)
                if mid <= state["reply_anchor"] or mid in state["processed"]:
                    continue
                out = await _process_bot_message(
                    client,
                    message=message,
                    sent_ids=state["sent_ids"],
                    expected_lang_token=state["request"].expected_lang_token,
                )
                if out is None:
                    continue
                vbytes, vuri, pmsg, retry_later, hard_reject = out
                if hard_reject:
                    state["processed"].add(mid)
                    state["pending_refetch"].discard(mid)
                    combined = (
                        f"{pmsg}\n\n"
                        "Syntax не вернул видео. Можно повторить с другим промптом или использовать основной пайплайн (Vertex)."
                    ).strip()
                    results[key] = _result(
                        success=False,
                        video_bytes=None,
                        video_uri=None,
                        provider_message=combined[:8000],
                        attempt_id=state["request"].attempt_id,
                    )
                    pending_states.pop(key, None)
                    return
                if vbytes or vuri:
                    state["processed"].add(mid)
                    state["pending_refetch"].discard(mid)
                    msg_out = " ".join(x for x in (pmsg, state["last_holder"][0]) if x).strip()
                    results[key] = _result(
                        success=True,
                        video_bytes=vbytes,
                        video_uri=vuri,
                        provider_message=msg_out or (pmsg or "Syntax fallback ok."),
                        attempt_id=state["request"].attempt_id,
                    )
                    pending_states.pop(key, None)
                    return
                if retry_later:
                    state["pending_refetch"].add(mid)
                    if pmsg:
                        state["last_holder"][0] = pmsg
                    return
                state["processed"].add(mid)
                state["pending_refetch"].discard(mid)
                if pmsg:
                    state["last_holder"][0] = pmsg
                return

        while pending_states and time.monotonic() < deadline:
            latest_raw = await client.get_messages(target, limit=1)
            latest = _first_message_from_get_messages(latest_raw)
            await _consume_for_batch_message(latest)
            async for msg in client.iter_messages(target, limit=40):
                await _consume_for_batch_message(msg)
                if not pending_states:
                    break
            for key in list(pending_states.keys()):
                state = pending_states.get(key)
                if state is None:
                    continue
                for mid in list(state["pending_refetch"]):
                    if mid in state["processed"]:
                        state["pending_refetch"].discard(mid)
                        continue
                    batch = await client.get_messages(target, ids=mid)
                    fresh = _first_message_from_get_messages(batch)
                    await _consume_for_batch_message(fresh)
                    if key not in pending_states:
                        break
            if pending_states:
                await asyncio.sleep(poll)

        for key, state in pending_states.items():
            tail = state["last_holder"][0]
            results[key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=(
                    f"Syntax fallback timed out after {timeout}s (poll={poll}s). Last: {tail}".strip()
                ),
                attempt_id=state["request"].attempt_id,
            )
        return results
    except sqlite3.OperationalError as exc:
        provider_message = (
            "Сессия Telethon занята другим процессом (database is locked). "
            "Остановите другие процессы с тем же TELETHON_SESSION_PATH/TELETHON_SESSION_PATHS и повторите."
            if "locked" in str(exc).lower()
            else f"Syntax Telethon SQLite error: {exc}"
        )
        for request in ready:
            results[request.key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=provider_message,
                attempt_id=request.attempt_id,
            )
        return results
    except Exception as exc:
        for request in ready:
            results[request.key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=f"Syntax fallback transport error: {exc}",
                attempt_id=request.attempt_id,
            )
        return results
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        _release_syntax_session_candidate(session_candidate)


def run_syntax_telethon_fallback(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None = None,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
) -> Any:
    """Run Syntax fallback transport and return unified fallback result."""
    attempt_id = uuid4().hex
    return asyncio.run(
        _run_fallback_async(
            attempt_id=attempt_id,
            prompt=prompt,
            model=model,
            duration_sec=duration_sec,
            start_image=start_image,
            end_image=end_image,
            timeout_sec=timeout_sec,
            acquire_timeout_sec=acquire_timeout_sec,
        )
    )


def run_syntax_telethon_fallback_batch(
    *,
    requests: list[Any],
    model: str,
    duration_sec: int,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Run one Telethon session that sends multiple Syntax requests before waiting for replies."""
    return asyncio.run(
        _run_batch_fallback_async(
            requests=requests,
            model=model,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            acquire_timeout_sec=acquire_timeout_sec,
        )
    )
