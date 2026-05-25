"""Telethon transport for Syntax fallback video generation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sqlite3
import re
import time
from typing import Any
from uuid import uuid4

from .syntax_getvideo_download import fetch_mp4_bytes_from_getvideo_page
from .syntax_image_prepare import prepare_syntax_frame_bytes

logger = logging.getLogger(__name__)

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover - optional dependency handling
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]


def _normalize_username(raw: str) -> str:
    clean = (raw or "").strip()
    if not clean:
        return "syntxaibot"
    return clean[1:] if clean.startswith("@") else clean


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


def _cap_prompt(text: str, *, max_len: int = 2000) -> tuple[str, bool]:
    clean = (text or "").strip()
    if len(clean) <= max_len:
        return clean, False
    return clean[:max_len], True


def _syntax_prompt_max_chars() -> int:
    raw = (os.getenv("SYNTAX_PROMPT_MAX_CHARS") or "3900").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3900
    return max(1000, min(4096, value))


def _strip_visual_context_for_syntax(prompt: str) -> str:
    """
    Remove verbose image-only continuity blocks before sending to Syntax.

    Syntax accepts a much shorter Telegram text than Vertex. The attached start/end
    frames already carry visual constants, so the text should prioritize action,
    style, safety constraints and the exact speech phrase.
    """
    text = (prompt or "").strip()
    if not text:
        return ""
    # text = re.sub(
    #     r"\n+Визуальные константы предметов:.*?(?=(?:\n(?:voice_lock_full:|veo3_|kitchen_physics_rule:|CHARACTERS SPEAKS:|lang:|$)))",
    #     "\n",
    #     text,
    #     flags=re.IGNORECASE | re.DOTALL,
    # )
    text = re.sub(
        r"\bAll visual elements,\s*including:.*?(?=(?:\s(?:voice_lock_full:|veo3_|kitchen_physics_rule:|CHARACTERS SPEAKS:|lang:|$)))",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # text = re.sub(
    #     r"Для стартового кадра обязательно подробно опиши:.*?состояние обработки\.?",
    #     " ",
    #     text,
    #     flags=re.IGNORECASE | re.DOTALL,
    # )
    replacements = {
        r"veo3_framing:.*?(?=\s(?:veo3_|kitchen_physics_rule:|voice_lock_full:|CHARACTERS SPEAKS:|$))": (
            "veo3_framing: close/medium-close cartoon shot; grandma and all characters fully visible; "
            "accurate lip sync; gentle camera movement."
        ),
        r"veo3_cartoon_character_rule:.*?(?=\s(?:veo3_|kitchen_physics_rule:|voice_lock_full:|CHARACTERS SPEAKS:|$))": (
            "veo3_cartoon_character_rule: cartoon grandmother and cartoon cat; no realism or photorealism."
        ),
        r"kitchen_physics_rule:.*?(?=\s(?:veo3_|voice_lock_full:|CHARACTERS SPEAKS:|$))": (
            "kitchen_physics_rule: cookware/appliances are functional and physically plausible; "
            "pots on burners, blender on base with lid."
        ),
        r"voice_lock_full:.*?(?=\s(?:CHARACTERS SPEAKS:|$))": (
            "voice_lock_full: warm elderly grandmother voice, clear soft articulation."
        ),
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", text).strip()


def _tail_characters_speaks(prompt: str) -> str:
    match = re.search(r"CHARACTERS SPEAKS:\s*.*$", prompt or "", flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def _prepare_prompt_for_syntax(prompt: str, *, max_len: int | None = None) -> tuple[str, bool]:
    limit = max_len if max_len is not None else _syntax_prompt_max_chars()
    original = (prompt or "").strip()
    compact = _strip_visual_context_for_syntax(original)
    if len(compact) <= limit:
        return compact, compact != original

    speech_tail = _tail_characters_speaks(compact)
    head = compact
    if speech_tail:
        idx = compact.lower().rfind("characters speaks:")
        head = compact[:idx].strip()
    room_for_head = limit - len(speech_tail) - (2 if speech_tail else 0)
    if room_for_head < 200:
        speech_tail = speech_tail[: max(0, limit - 3)].rstrip() + "..."
        return speech_tail, True
    head = head[:room_for_head].rstrip(" ,.;")
    prepared = f"{head}\n{speech_tail}".strip() if speech_tail else head
    return prepared[:limit], True


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

    session_path = (os.getenv("TELETHON_SESSION_PATH") or "").strip()
    session_string = (os.getenv("TELETHON_STRING_SESSION") or "").strip()
    if not session_path and not session_string:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Missing Telethon session: set TELETHON_SESSION_PATH or TELETHON_STRING_SESSION.",
            attempt_id=attempt_id,
        )

    if timeout_sec is not None and int(timeout_sec) > 0:
        timeout = int(timeout_sec)
    else:
        env_raw = (os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "").strip()
        timeout = int(env_raw) if env_raw.isdigit() and int(env_raw) > 0 else 25 * 60
    poll = _poll_interval_sec()
    bot_username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))
    session_obj: Any = StringSession(session_string) if session_string and StringSession else session_path

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

    prompt_text, prompt_truncated = _prepare_prompt_for_syntax(prompt)
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
                if prompt_truncated:
                    last_info = "Prompt exceeded 2000 chars and was truncated before send."

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
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=(
                    "Сессия Telethon занята другим процессом (database is locked). "
                    "Остановите другие процессы с тем же TELETHON_SESSION_PATH и повторите."
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


def run_syntax_telethon_fallback(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None = None,
    timeout_sec: int | None = None,
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
        )
    )
