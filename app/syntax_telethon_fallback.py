"""Telethon transport for Syntax fallback video generation."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
from pathlib import Path
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
from .llm_utils import GEMINI_MODEL_PROFILE_EDIT, extract_json_object, gemini_generate_sync
from .generation_modes import SYNTAX_PROVIDER_COMMANDS, SYNTAX_PROVIDER_KLING, SYNTAX_PROVIDER_VEO
from .pipeline_llm_steps import RUSSIAN_YO_RULE

logger = logging.getLogger(__name__)

SYNTAX_PROMPT_DEFAULT_MAX_CHARS = 2000
SYNTAX_PROMPT_HARD_MAX_CHARS = 2000
SYNTAX_MEDIA_DOWNLOAD_TIMEOUT_SEC_ENV = "SYNTAX_MEDIA_DOWNLOAD_TIMEOUT_SEC"

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


_SESSION_FILE_LOCKS: dict[_SessionCandidate, Any] = {}


def _try_lock_session_process(candidate: _SessionCandidate) -> bool:
    root = Path(os.getenv("SYNTAX_SESSION_LOCK_DIR") or Path(__file__).resolve().parent.parent / "runtime" / "syntax_session_locks")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256((candidate.kind + ":" + candidate.value).encode()).hexdigest()
    handle = (root / (digest + ".lock")).open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _SESSION_FILE_LOCKS[candidate] = handle
    return True


def _acquire_syntax_session_candidate(timeout_sec: int | None) -> _SessionCandidate | None:
    candidates = _syntax_session_candidates()
    if not candidates:
        return None
    deadline = time.monotonic() + timeout_sec if timeout_sec is not None and timeout_sec > 0 else None
    with _SESSION_POOL_CONDITION:
        while True:
            for candidate in candidates:
                if candidate not in _SESSION_POOL_IN_USE and _try_lock_session_process(candidate):
                    _SESSION_POOL_IN_USE.add(candidate)
                    return candidate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                _SESSION_POOL_CONDITION.wait(timeout=min(remaining, 0.5))
            else:
                _SESSION_POOL_CONDITION.wait(timeout=0.5)


def _release_syntax_session_candidate(candidate: _SessionCandidate | None) -> None:
    if candidate is None:
        return
    with _SESSION_POOL_CONDITION:
        _SESSION_POOL_IN_USE.discard(candidate)
        handle = _SESSION_FILE_LOCKS.pop(candidate, None)
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
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
    text = str(prompt or "")
    if not text.strip():
        return None
    match = re.search(r"\blang\s*[:=]\s*([a-z]{2,8})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return f"lang:{match.group(1).lower()}"


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


def _normalize_syntax_provider(provider: str | None, model: str | None = None) -> str:
    raw = str(provider or "").strip().lower()
    model_raw = str(model or "").strip().lower()
    if raw in {"kling", "klimg", "клинг", "kling3", "kling_3_0"} or model_raw.startswith("kling"):
        return SYNTAX_PROVIDER_KLING
    return SYNTAX_PROVIDER_VEO


def _syntax_button_text(button: Any) -> str:
    return str(getattr(button, "text", "") or getattr(getattr(button, "button", None), "text", "") or "").strip()


def _syntax_button_url(button: Any) -> str | None:
    url = str(getattr(button, "url", "") or "").strip()
    if url:
        return url
    raw_button = getattr(button, "button", None)
    url = str(getattr(raw_button, "url", "") or "").strip()
    return url or None


def _syntax_message_buttons(message: Any) -> list[Any]:
    buttons = getattr(message, "buttons", None) or []
    out: list[Any] = []
    for row in buttons:
        if isinstance(row, (list, tuple)):
            out.extend(row)
        else:
            out.append(row)
    return out


def _syntax_button_is_configure(button: Any) -> bool:
    text = _syntax_button_text(button).lower()
    return "настро" in text or "settings" in text or "configure" in text


def _syntax_find_configure_button(message: Any) -> Any | None:
    for button in _syntax_message_buttons(message):
        if _syntax_button_is_configure(button):
            return button
    return None


async def _wait_for_syntax_configure_button(
    client: Any,
    target: Any,
    *,
    reply_anchor: int,
    poll: float,
    deadline: float,
) -> tuple[Any | None, Any | None]:
    while time.monotonic() < deadline:
        latest_raw = await client.get_messages(target, limit=1)
        latest = _first_message_from_get_messages(latest_raw)
        if latest is not None and not getattr(latest, "out", False):
            mid = int(getattr(latest, "id", 0) or 0)
            button = _syntax_find_configure_button(latest)
            if mid > reply_anchor and button is not None:
                return latest, button
        async for msg in client.iter_messages(target, limit=20):
            if msg is None or getattr(msg, "out", False):
                continue
            mid = int(getattr(msg, "id", 0) or 0)
            if mid <= reply_anchor:
                continue
            button = _syntax_find_configure_button(msg)
            if button is not None:
                return msg, button
        await asyncio.sleep(max(0.5, min(poll, 5.0)))
    return None, None


async def _wait_for_latest_syntax_configure_button(
    client: Any,
    target: Any,
    *,
    reply_anchor: int,
    poll: float,
    attempts: int = 6,
    require_prompt_saved: bool = False,
    deadline: float | None = None,
) -> tuple[Any | None, Any | None]:
    """Wait for this prompt's settings acknowledgement without resending it.

    Callers with a known WebView flow use a time budget. The bounded probe
    mode remains available to legacy transports that generate without a form.
    """
    attempt = 0
    while (time.monotonic() < deadline if deadline is not None else attempt < max(1, min(int(attempts), 10))):
        attempt += 1
        latest_raw = await client.get_messages(target, limit=20 if deadline is not None else 5)
        if latest_raw is None:
            latest_messages: list[Any] = []
        elif isinstance(latest_raw, (list, tuple)):
            latest_messages = list(latest_raw)
        else:
            latest_messages = [latest_raw]
        for message in latest_messages:
            if message is None or getattr(message, "out", False):
                continue
            mid = int(getattr(message, "id", 0) or 0)
            if mid <= reply_anchor:
                continue
            if require_prompt_saved:
                message_text = str(getattr(message, "raw_text", "") or getattr(message, "text", "") or "").lower()
                if not re.search(r"промпт\s+сохран|prompt\s+saved", message_text):
                    continue
                reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
                if deadline is not None and reply_to is not None and int(reply_to) != reply_anchor:
                    continue  # Delayed acknowledgement of another language/request.
            button = _syntax_find_configure_button(message)
            if button is not None:
                return message, button
        delay = max(0.5, min(float(poll), 5.0))
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.monotonic()))
        if delay > 0 and (deadline is not None or attempt < attempts):
            await asyncio.sleep(delay)
    logger.warning("syntax.configure_ack_timeout prompt_message_id=%s probes=%s", reply_anchor, attempt)
    return None, None


async def _syntax_configure_button_webview_url(
    client: Any,
    target: Any,
    bot_entity: Any,
    message: Any,
    button: Any,
) -> str | None:
    raw_button = getattr(button, "button", None)
    raw_url = str(getattr(raw_button, "url", "") or getattr(button, "url", "") or "").strip()
    raw_type = type(raw_button).__name__ if raw_button is not None else ""
    if raw_url and "WebView" in raw_type:
        try:
            from telethon import functions, types

            result = await client(
                functions.messages.RequestWebViewRequest(
                    peer=target,
                    bot=bot_entity,
                    platform="macos",
                    url=raw_url,
                    reply_to=types.InputReplyToMessage(reply_to_msg_id=int(getattr(message, "id", 0) or 0)),
                )
            )
            url = str(getattr(result, "url", "") or "").strip()
            if url:
                return url
        except Exception as exc:
            logger.info("syntax.kling.request_webview_url_failed err=%s", exc)
    url = raw_url or _syntax_button_url(button)
    if url:
        return url
    if raw_button is not None:
        try:
            from telethon import functions

            result = await client(functions.bots.RequestWebViewButtonRequest(user_id=bot_entity, button=raw_button))
            url = str(getattr(result, "url", "") or "").strip()
            if url:
                return url
        except Exception as exc:
            logger.info("syntax.kling.webview_button_url_failed err=%s", exc)
    click = getattr(button, "click", None)
    if callable(click):
        try:
            clicked = await click()
            if isinstance(clicked, str) and clicked.strip().startswith(("http://", "https://")):
                return clicked.strip()
            url = str(getattr(clicked, "url", "") or "").strip()
            if url:
                return url
        except Exception as exc:
            logger.info("syntax.kling.configure_click_failed err=%s", exc)
    return None


def _syntax_option_already_selected_sync(page: Any, value: str) -> bool:
    """Return True when the updated Syntax UI already has the requested option selected."""
    return bool(
        page.evaluate(
            r"""({ value }) => {
                const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const wanted = norm(value);
                const selected = el => {
                    if (!el) return false;
                    if (el.selected === true || el.checked === true) return true;
                    if (norm(el.getAttribute('aria-selected')) === 'true') return true;
                    if (norm(el.getAttribute('aria-checked')) === 'true') return true;
                    if (['selected', 'checked', 'active', 'on'].includes(norm(el.getAttribute('data-state')))) return true;
                    const cls = norm(el.className);
                    return /(^|\s)(is-selected|selected|is-active|active)(\s|$)/.test(cls);
                };
                const controls = [...document.querySelectorAll(
                    '[role="option"], option, [aria-selected], [aria-checked], [data-state], .is-selected, .selected, .is-active'
                )];
                return controls.some(el => {
                    const text = norm(el.textContent || el.value || el.getAttribute('data-value'));
                    return selected(el) && (text === wanted || text.includes(wanted));
                });
            }""",
            {"value": value},
        )
    )


def _click_syntax_option_sync(page: Any, label: str, value: str, *, timeout_ms: int = 5000) -> None:
    label_re = re.compile(re.escape(label), re.IGNORECASE)
    value_re = re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
    with contextlib.suppress(Exception):
        if _syntax_option_already_selected_sync(page, value):
            logger.info("syntax.kling.option_already_selected label=%s value=%s", label, value)
            return

    def _click_visible_option() -> bool:
        return bool(
            page.evaluate(
                """({ value }) => {
                    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                    const visible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                    };
                    const wanted = norm(value).toLowerCase();
                    const options = [...document.querySelectorAll('[role="option"], li, button, [role="button"]')];
                    const exact = options.find(el => visible(el) && norm(el.textContent).toLowerCase() === wanted);
                    const partial = options.find(el => visible(el) && norm(el.textContent).toLowerCase().includes(wanted));
                    const found = exact || partial;
                    if (!found) return false;
                    const state = String(found.getAttribute('aria-selected') || found.getAttribute('aria-checked') || found.getAttribute('data-state') || '').toLowerCase();
                    const cls = norm(found.className).toLowerCase();
                    if (found.selected === true || found.checked === true || ['true', 'selected', 'checked', 'active', 'on'].includes(state) || /(^|\\s)(is-selected|selected|is-active|active)(\\s|$)/.test(cls)) {
                        return true;
                    }
                    found.click();
                    return true;
                }""",
                {"value": value},
            )
        )

    with contextlib.suppress(Exception):
        clicked = page.evaluate(
            """({ label, value }) => {
                const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                const contains = (hay, needle) => norm(hay).toLowerCase().includes(norm(needle).toLowerCase());
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const labels = [...document.querySelectorAll('body *')].filter(el => visible(el) && contains(el.textContent, label));
                for (const labelEl of labels) {
                    const scope = labelEl.closest('.syntx-field, .syntx-form-field, .syntx-custom-select, label, div') || labelEl.parentElement;
                    const candidates = [
                        ...(scope ? [...scope.querySelectorAll('[role="combobox"], select, input, button')] : []),
                        ...[...document.querySelectorAll('[role="combobox"], select, input, button')].filter(el => {
                            const lr = labelEl.getBoundingClientRect();
                            const er = el.getBoundingClientRect();
                            return visible(el) && er.top >= lr.top && er.top - lr.top < 180;
                        }),
                    ];
                    const trigger = candidates.find(visible);
                    if (!trigger) continue;
                    trigger.click();
                    return true;
                }
                return false;
            }""",
            {"label": label, "value": value},
        )
        if clicked:
            page.wait_for_timeout(250)
            if _click_visible_option():
                return
    with contextlib.suppress(Exception):
        field = page.get_by_label(label_re).first
        field.select_option(label=value, timeout=timeout_ms)
        return
    with contextlib.suppress(Exception):
        field = page.get_by_label(label_re).first
        field.fill(value, timeout=timeout_ms)
        return
    with contextlib.suppress(Exception):
        trigger = page.locator(
            f"xpath=//*[contains(normalize-space(.), '{label}')]/following::*[self::select or self::input or self::button or @role='combobox'][1]"
        ).first
        tag = (trigger.evaluate("el => el.tagName") or "").lower()
        if tag == "select":
            trigger.select_option(label=value, timeout=timeout_ms)
            return
        trigger.click(timeout=timeout_ms)
        page.wait_for_timeout(250)
        if _click_visible_option():
            return
    page.get_by_text(value_re).last.click(timeout=timeout_ms)


def _try_click_syntax_option_sync(page: Any, label: str, value: str, *, timeout_ms: int = 5000) -> bool:
    try:
        _click_syntax_option_sync(page, label, value, timeout_ms=timeout_ms)
        return True
    except Exception as exc:
        logger.info("syntax.kling.option_not_set label=%s value=%s err=%s", label, value, exc)
        return False


def _set_syntax_kling_duration_slider_sync(page: Any, duration_sec: int) -> bool:
    """Set the numeric duration slider used by the current Syntax Kling UI."""
    return bool(
        page.evaluate(
            """({ duration }) => {
                const ranges = [...document.querySelectorAll('input[type="range"]')];
                if (!ranges.length) return false;
                const target = ranges.find(el => {
                    const min = Number(el.min || 0);
                    const max = Number(el.max || 0);
                    return min <= duration && max >= duration;
                });
                if (!target) return false;
                const setValue = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype,
                    'value'
                )?.set;
                if (setValue) setValue.call(target, String(duration));
                else target.value = String(duration);
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
                return Number(target.value) === Number(duration);
            }""",
            {"duration": max(1, int(duration_sec))},
        )
    )


def _set_syntax_kling_duration_sync(page: Any, duration_sec: int, *, timeout_ms: int = 5000) -> int:
    """Set Kling duration in either the current slider UI or the legacy select UI."""
    requested = max(1, int(duration_sec))
    with contextlib.suppress(Exception):
        if _set_syntax_kling_duration_slider_sync(page, requested):
            page.wait_for_timeout(250)
            logger.info("syntax.kling.duration_slider_set value=%s", requested)
            return requested
    if _try_click_syntax_option_sync(
        page,
        "Длительность",
        f"{requested} сек",
        timeout_ms=timeout_ms,
    ):
        return requested
    raise RuntimeError(f"Syntax Kling duration could not be set to {requested} sec.")


def _click_syntax_ratio_by_dom_sync(page: Any, aspect_ratio: str, *, timeout_ms: int = 5000) -> bool:
    ratio = _normalize_syntax_aspect_ratio(aspect_ratio)
    return bool(
        page.evaluate(
            """({ ratio }) => {
                const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const clickEl = el => {
                    if (!el) return false;
                    el.scrollIntoView({ block: 'center', inline: 'center' });
                    el.click();
                    return true;
                };
                const descriptor = el => [
                    norm(el.textContent),
                    norm(el.getAttribute('aria-label')),
                    norm(el.getAttribute('title')),
                    norm(el.getAttribute('value')),
                    norm(el.getAttribute('data-value')),
                    norm(el.getAttribute('data-testid')),
                    norm(el.className),
                ].join(' ').toLowerCase();
                const targetTerms = ratio === '16:9'
                    ? ['16:9', '16 / 9', '16x9', 'landscape', 'horizontal', 'горизонт', 'альбом', 'широк']
                    : ['9:16', '9 / 16', '9x16', 'portrait', 'vertical', 'вертик', 'портрет'];
                const labelRe = /Соотношение сторон|Aspect ratio|Aspect/i;
                const clickableSelector = 'button, [role="button"], [role="option"], [role="radio"], label, input, [tabindex], li';
                const allClickables = [...document.querySelectorAll(clickableSelector)].filter(visible);
                const textMatch = allClickables.find(el => targetTerms.some(term => descriptor(el).includes(term)));
                if (textMatch) return clickEl(textMatch);

                const labelNodes = [...document.querySelectorAll('body *')].filter(el => visible(el) && labelRe.test(norm(el.textContent)));
                const roots = [];
                for (const labelNode of labelNodes) {
                    let cur = labelNode;
                    for (let i = 0; i < 5 && cur; i += 1) {
                        const rect = cur.getBoundingClientRect();
                        if (rect.width >= 120 && rect.height >= 40 && rect.height <= 420) {
                            roots.push(cur);
                            break;
                        }
                        cur = cur.parentElement;
                    }
                }
                if (!roots.length) roots.push(document.body);
                const wantedWide = ratio === '16:9';
                for (const root of roots) {
                    const scoped = [...root.querySelectorAll(clickableSelector)].filter(visible);
                    const scopedTextMatch = scoped.find(el => targetTerms.some(term => descriptor(el).includes(term)));
                    if (scopedTextMatch) return clickEl(scopedTextMatch);
                    for (const el of scoped) {
                        const shapes = [el, ...el.querySelectorAll('svg, img, span, div')].filter(visible);
                        for (const shape of shapes) {
                            const r = shape.getBoundingClientRect();
                            if (r.width < 10 || r.height < 6 || r.width * r.height > 20000) continue;
                            const shapeRatio = r.width / r.height;
                            if ((wantedWide && shapeRatio >= 1.35) || (!wantedWide && shapeRatio <= 0.74)) {
                                return clickEl(el);
                            }
                        }
                    }
                }
                return false;
            }""",
            {"ratio": ratio},
        )
    )


def _syntax_visible_controls_snapshot_sync(page: Any) -> str:
    with contextlib.suppress(Exception):
        snapshot = page.evaluate(
            """() => {
                const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const controls = [...document.querySelectorAll('button, [role="button"], [role="option"], [role="radio"], select, input, textarea, label')]
                    .filter(visible)
                    .slice(0, 80)
                    .map(el => norm([
                        el.tagName,
                        el.getAttribute('role') || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('value') || '',
                        el.textContent || '',
                    ].join(' ')))
                    .filter(Boolean);
                return controls.join(' | ');
            }"""
        )
        return str(snapshot or "")[:1200]
    return ""


def _normalize_syntax_aspect_ratio(value: str | None) -> str:
    raw = str(value or "").strip()
    return "16:9" if raw == "16:9" else "9:16"


def _set_syntax_aspect_ratio_sync(page: Any, aspect_ratio: str, *, timeout_ms: int = 5000) -> None:
    ratio = _normalize_syntax_aspect_ratio(aspect_ratio)
    opposite_ratio = "9:16" if ratio == "16:9" else "16:9"
    if _syntax_page_contains_aspect_ratio_sync(page, ratio) and not _syntax_page_contains_aspect_ratio_sync(page, opposite_ratio):
        return
    labels = ("Соотношение сторон", "Aspect ratio", "Aspect")
    for label in labels:
        if _try_click_syntax_option_sync(page, label, ratio, timeout_ms=timeout_ms):
            return
    with contextlib.suppress(Exception):
        if _click_syntax_ratio_by_dom_sync(page, ratio, timeout_ms=timeout_ms):
            page.wait_for_timeout(300)
            if _syntax_page_contains_aspect_ratio_sync(page, ratio):
                return
    controls = _syntax_visible_controls_snapshot_sync(page)
    suffix = f" Visible controls: {controls}" if controls else ""
    raise RuntimeError(f"Syntax aspect ratio option was not set to {ratio}.{suffix}")


def _click_syntax_settings_apply_sync(page: Any, *, timeout_ms: int = 5000) -> bool:
    apply_re = re.compile("Сохранить|Save|Применить|Apply|Готово|Done|OK|ОК", re.IGNORECASE)
    for role in ("button",):
        with contextlib.suppress(Exception):
            page.get_by_role(role, name=apply_re).last.click(timeout=timeout_ms)
            page.wait_for_timeout(800)
            return True
    with contextlib.suppress(Exception):
        clicked = page.evaluate(
            """() => {
                const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const re = /Сохранить|Save|Применить|Apply|Готово|Done|OK|ОК/i;
                const candidates = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(el => visible(el) && re.test(norm(el.textContent)));
                const target = candidates[candidates.length - 1];
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if clicked:
            page.wait_for_timeout(800)
            return True
    return False


def _syntax_page_contains_aspect_ratio_sync(page: Any, aspect_ratio: str) -> bool:
    ratio = _normalize_syntax_aspect_ratio(aspect_ratio)
    with contextlib.suppress(Exception):
        return bool(
            page.evaluate(
                """({ ratio }) => {
                    const text = String(document.body ? document.body.innerText : '');
                    return text.includes(ratio);
                }""",
                {"ratio": ratio},
            )
        )
    return False


def _syntax_model_settings_probe_attempts() -> int:
    raw = (os.getenv("SYNTAX_MODEL_SETTINGS_PROBE_ATTEMPTS") or "").strip()
    return max(1, min(8, int(raw))) if raw.isdigit() else 5


def _sanitize_syntax_webview_error(error: Exception) -> str:
    text = str(error)
    text = re.sub(r"https://bot\.syntx\.ai/\S+", "[syntx-webview-url]", text)
    text = re.sub(r"token=[^&#\s]+", "token=[redacted]", text)
    text = re.sub(r"tgWebAppData=[^&#\s]+", "tgWebAppData=[redacted]", text)
    return text


def _enable_syntax_audio_sync(page: Any, *, timeout_ms: int = 5000) -> None:
    audio_re = re.compile("Аудио|Audio|Звуки|Sounds", re.IGNORECASE)
    for role in ("switch", "checkbox"):
        with contextlib.suppress(Exception):
            control = page.get_by_role(role, name=audio_re).last
            checked = control.get_attribute("aria-checked", timeout=1000)
            if str(checked or "").lower() not in {"true", "1"}:
                control.click(timeout=timeout_ms)
            return
    with contextlib.suppress(Exception):
        control = page.locator(
            "xpath=//*[contains(normalize-space(.), 'Аудио') or contains(normalize-space(.), 'Audio')]/following::*[@role='switch' or @role='checkbox' or self::input[@type='checkbox']][1]"
        ).first
        checked = control.get_attribute("aria-checked", timeout=1000) or control.get_attribute("checked", timeout=1000)
        if str(checked or "").lower() not in {"true", "1", "checked"}:
            control.click(timeout=timeout_ms)
        return
    page.get_by_text(audio_re).last.click(timeout=timeout_ms)


def _syntax_audio_checked_state_sync(control: Any) -> bool | None:
    for attr in ("aria-checked", "checked", "data-state"):
        with contextlib.suppress(Exception):
            raw = str(control.get_attribute(attr, timeout=1000) or "").strip().lower()
            if raw in {"true", "1", "checked", "on"}:
                return True
            if raw in {"false", "0", "unchecked", "off"}:
                return False
    with contextlib.suppress(Exception):
        return bool(control.is_checked(timeout=1000))
    return None


def _disable_syntax_audio_sync(page: Any, *, timeout_ms: int = 5000) -> None:
    """Explicitly disable Kling audio or block the paid launch if state is ambiguous."""
    audio_re = re.compile("Аудио|Audio|Звуки|Sounds", re.IGNORECASE)
    ambiguous_control_found = False
    controls: list[Any] = []
    for role in ("switch", "checkbox"):
        with contextlib.suppress(Exception):
            controls.append(page.get_by_role(role, name=audio_re).last)
    with contextlib.suppress(Exception):
        controls.append(
            page.locator(
                "xpath=//*[contains(normalize-space(.), 'Аудио') or contains(normalize-space(.), 'Audio')]/following::*[@role='switch' or @role='checkbox' or self::input[@type='checkbox']][1]"
            ).first
        )
    for control in controls:
        state = _syntax_audio_checked_state_sync(control)
        if state is None:
            ambiguous_control_found = True
            continue
        if state:
            control.click(timeout=timeout_ms)
            page.wait_for_timeout(300)
            verified = _syntax_audio_checked_state_sync(control)
            if verified is not False:
                raise RuntimeError("Syntax audio switch could not be verified as disabled; generation was not started.")
        return
    if ambiguous_control_found:
        raise RuntimeError("Syntax audio switch state is ambiguous; generation was not started.")
    raise RuntimeError("Syntax audio switch was not found; generation was not started.")


_KLING_SUBMISSION_STARTED_RE = re.compile(
    r"(?:генерация|видео|задание|запрос)\s+(?:успешно\s+)?(?:запущен[ао]?|отправлен[ао]?|добавлен[ао]?\s+в\s+очередь|поставлен[ао]?\s+в\s+очередь|ожидает\s+выполнения|зарегистрирован[ао]?)|generating\s+video|video\s+(?:queued|submitted)|generation\s+(?:started|submitted)",
    re.IGNORECASE,
)
_KLING_SUBMISSION_BUTTON_PROGRESS_RE = re.compile(
    r"обраб|очеред|ожид|созда[её]т|processing|queued|in queue|creating|please wait",
    re.IGNORECASE,
)
_KLING_SUBMISSION_BLOCKED_RE = re.compile(
    r"ошиб|error|failed|fail|лимит|limit|недостат|insufficient|заполн|invalid|try again|попроб",
    re.IGNORECASE,
)
_KLING_SUBMISSION_ALREADY_SENT_RE = re.compile(
    r"(?:эта\s+)?генерац(?:ия|ии)\s+уже\s+отправлен|generation\s+(?:was\s+)?already\s+(?:sent|submitted)|already\s+submitted",
    re.IGNORECASE,
)


class SyntaxVeoQueueFullError(RuntimeError):
    """The final Veo click was rejected because both account queue slots are busy."""


def _regex_match_count(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _match in pattern.finditer(re.sub(r"\s+", " ", str(text or ""))))


def _syntax_kling_submission_state_from_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    # Syntax uses this toast when the same WebView launch was accepted by an
    # earlier click.  It is an idempotency acknowledgement, not a rejection.
    if _KLING_SUBMISSION_ALREADY_SENT_RE.search(normalized):
        return "started"
    if _KLING_SUBMISSION_BLOCKED_RE.search(normalized):
        return "blocked"
    if _KLING_SUBMISSION_STARTED_RE.search(normalized):
        return "started"
    return ""


def _syntax_kling_submission_state_after_change(before_text: str, after_text: str) -> str:
    """Classify only status text introduced by the submit click.

    Veo can render a permanent queue-limit warning before the button is
    clicked.  Reclassifying the whole page after any Livewire update made that
    old warning look like a fresh rejection even when the click was accepted.
    """

    before = re.sub(r"\s+", " ", str(before_text or "")).strip()
    after = re.sub(r"\s+", " ", str(after_text or "")).strip()
    if not after or after == before:
        return ""
    if _KLING_SUBMISSION_ALREADY_SENT_RE.search(after):
        return "started"
    if _regex_match_count(_KLING_SUBMISSION_BLOCKED_RE, after) > _regex_match_count(
        _KLING_SUBMISSION_BLOCKED_RE,
        before,
    ):
        return "blocked"
    if _regex_match_count(_KLING_SUBMISSION_STARTED_RE, after) > _regex_match_count(
        _KLING_SUBMISSION_STARTED_RE,
        before,
    ):
        return "started"
    return ""


def _syntax_kling_submit_confirm_timeout_sec() -> float:
    raw = (os.getenv("SYNTAX_KLING_SUBMIT_CONFIRM_SEC") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 60.0
    return max(10.0, min(value, 120.0))


def _syntax_kling_submit_snapshot_sync(page: Any) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """() => {
                const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
                const visible = el => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const generateRe = /Сгенерировать|Generate/i;
                const buttons = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(el => visible(el) && generateRe.test(norm(el.innerText)))
                    .map(el => ({
                        text: norm(el.innerText),
                        disabled: Boolean(el.disabled) || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
                    }));
                return {
                    text: norm(document.body ? document.body.innerText : ''),
                    generateButtons: buttons,
                };
            }"""
        )
        or {}
    )


def _syntax_kling_form_ready_timeout_ms() -> int:
    """Allow Syntax to finish ingesting an uploaded reference frame."""
    raw = (os.getenv("SYNTAX_KLING_FORM_READY_SEC") or "").strip()
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 120
    return max(30_000, min(seconds * 1000, 300_000))


def _wait_for_syntax_kling_form_ready_sync(page: Any, *, timeout_ms: int | None = None) -> None:
    """Wait until pending reactive setting updates cannot swallow submit."""
    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        return
    wait_for_function(
        """() => {
            const button = document.querySelector('#btn_submit');
            if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
            const guard = button.closest('[x-data*="busy"]');
            if (guard && guard.classList.contains('syntx-submit--busy')) return false;
            const fieldset = button.closest('.syntx-model-layout')?.querySelector('fieldset');
            if (fieldset && fieldset.disabled) return false;
            return true;
        }""",
        timeout=max(timeout_ms or 0, _syntax_kling_form_ready_timeout_ms()),
    )


def _click_syntax_kling_generate_sync(
    page: Any,
    *,
    timeout_ms: int = 10_000,
    wait_for_form_ready: bool = True,
) -> str:
    generate_re = re.compile("Сгенерировать|Generate", re.IGNORECASE)
    before = _syntax_kling_submit_snapshot_sync(page)
    before_text = str(before.get("text") or "")
    before_buttons = before.get("generateButtons")
    before_button_texts = {
        str(item.get("text") or "").strip()
        for item in (before_buttons if isinstance(before_buttons, list) else [])
        if isinstance(item, dict)
    }
    submit_responses: list[tuple[int, str]] = []

    def _record_response(response: Any) -> None:
        with contextlib.suppress(Exception):
            request = response.request
            method = str(request.method or "").upper()
            response_url = str(response.url or "").split("?", 1)[0]
            if method == "POST" and any(marker in response_url.lower() for marker in ("/livewire/", "/submit", "/generate")):
                submit_responses.append((int(response.status), response_url))

    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
    if wait_for_form_ready:
        _wait_for_syntax_kling_form_ready_sync(page)
    with contextlib.suppress(Exception):
        page.on("response", _record_response)

    try:
        button = page.locator("#btn_submit")
        visible_label = ""
        with contextlib.suppress(Exception):
            visible_label = str(button.inner_text(timeout=timeout_ms) or "").strip()
        if visible_label and not generate_re.search(visible_label):
            raise RuntimeError(f"Syntax visible submit button has unexpected label: {visible_label}")
        logger.info("syntax.kling.click_visible_generate label=%s", visible_label or "<unavailable>")
        button.click(timeout=timeout_ms)

        deadline = time.monotonic() + _syntax_kling_submit_confirm_timeout_sec()
        last_text = ""
        last_buttons: list[Any] = []
        progress_button_seen = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(700)
            snapshot = _syntax_kling_submit_snapshot_sync(page)
            last_text = str(snapshot.get("text") or "")
            buttons = snapshot.get("generateButtons")
            last_buttons = buttons if isinstance(buttons, list) else []
            current_button_texts = {
                str(item.get("text") or "").strip()
                for item in last_buttons
                if isinstance(item, dict)
            }
            progressed_button_texts = current_button_texts - before_button_texts
            button_progress_now = any(
                _KLING_SUBMISSION_BUTTON_PROGRESS_RE.search(text)
                for text in progressed_button_texts
            )
            text_changed = bool(last_text and last_text != before_text)
            state = (
                _syntax_kling_submission_state_after_change(before_text, last_text)
                if text_changed
                else ""
            )
            if state == "blocked":
                excerpt = last_text[:500].strip()
                if _regex_match_count(_SYNTAX_VEO_QUEUE_LIMIT_RE, last_text) > _regex_match_count(
                    _SYNTAX_VEO_QUEUE_LIMIT_RE,
                    before_text,
                ):
                    raise SyntaxVeoQueueFullError(excerpt)
                raise RuntimeError(f"Syntax Kling UI rejected generation after Generate click: {excerpt}")
            if state == "started":
                return "started"
            if text_changed:
                logger.info("syntax.kling.submit_changed_without_known_status text=%s", last_text[:300])

            # The current Syntx Livewire UI first updates only the nested button
            # text (for example, ``Сгенерировать Обработка...``).  That is
            # acknowledgement of the click, not proof that the paid generation
            # was submitted. Keep the webview alive until a task status appears.
            if button_progress_now:
                if not progress_button_seen:
                    progress_button_seen = True
                    logger.info(
                        "syntax.kling.submit_button_progress_seen buttons=%s; waiting_for_task_status=true",
                        sorted(progressed_button_texts),
                    )

            if submit_responses:
                failed_statuses = [status for status, _response_url in submit_responses if status >= 400]
                if failed_statuses:
                    raise RuntimeError(f"Syntax Kling submit request failed with HTTP {failed_statuses[-1]}.")
                # The visible button is a nested Livewire component.  Its child
                # acknowledgement must be followed by the parent component's
                # actual generation-submission response.
                if len(submit_responses) < 2:
                    continue
                page.wait_for_timeout(1200)
                final_snapshot = _syntax_kling_submit_snapshot_sync(page)
                final_text = str(final_snapshot.get("text") or "")
                final_state = _syntax_kling_submission_state_after_change(before_text, final_text)
                if final_state == "blocked":
                    if _regex_match_count(_SYNTAX_VEO_QUEUE_LIMIT_RE, final_text) > _regex_match_count(
                        _SYNTAX_VEO_QUEUE_LIMIT_RE,
                        before_text,
                    ):
                        raise SyntaxVeoQueueFullError(final_text[:500].strip())
                    raise RuntimeError(
                        f"Syntax Kling UI rejected generation after Generate click: {final_text[:500].strip()}"
                    )
                if final_state == "started":
                    return "started"
                # HTTP 200 also acknowledges settings/child component updates.
                # Only a provider task status proves that generation was accepted.
                logger.debug("syntax.kling.submit_response_without_task_status responses=%s", len(submit_responses))

        logger.warning(
            "syntax.kling.submit_not_confirmed responses=%s progress_seen=%s buttons=%s text=%s",
            submit_responses,
            progress_button_seen,
            last_buttons,
            last_text[:500],
        )
        raise SyntaxSubmissionUnconfirmedError(
            "Syntax UI did not confirm generation start after Generate click. "
            "Запуск не подтверждён; новое платное задание автоматически не создаю."
        )
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("response", _record_response)



def _fill_and_commit_syntax_kling_prompt_sync(page: Any, prompt: str, *, timeout_ms: int = 10_000) -> None:
    """Replace stale persisted text and commit Livewire's blur-bound prompt before submit."""
    textarea = page.locator("textarea").first
    textarea.fill(prompt, timeout=timeout_ms)
    # Syntx keeps the last prompt in the webview and currently binds this
    # textarea on blur.  ``fill`` alone updates the DOM but can leave the
    # parent Livewire component with the previous prompt, so Generate only
    # acknowledges its nested button without creating a task.
    committed = False
    with contextlib.suppress(Exception):
        textarea.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
            }"""
        )
        committed = True
    if not committed:
        with contextlib.suppress(Exception):
            textarea.press("Tab", timeout=timeout_ms)
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
    value = str(textarea.input_value(timeout=timeout_ms) or "")
    if value != prompt:
        raise RuntimeError("Syntax Kling prompt field did not keep the requested prompt.")
    _wait_for_syntax_kling_form_ready_sync(page, timeout_ms=max(timeout_ms, 30_000))


def _upload_syntax_kling_start_frame_sync(page: Any, start_image: bytes, *, timeout_ms: int = 30_000) -> None:
    """Attach the start frame to Syntax's own Image-to-video form.

    Sending an image to the Telegram chat only makes it available to the
    WebApp's picker; it does not select it.  The current Syntax UI requires a
    real file in its ``input[type=file]`` before it enables Generate.
    """
    if not start_image:
        raise RuntimeError("Syntax Kling start frame is empty.")
    with contextlib.suppress(Exception):
        already_attached = bool(
            page.evaluate(
                """() => [...document.querySelectorAll('button, [role=button]')].some(el => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return /^(Remove file|Удалить файл)/i.test(text);
                })"""
            )
        )
        if already_attached:
            logger.info("syntax.kling.start_frame_already_attached source=telegram_picker")
            return
    extension = _image_ext(start_image)
    mime_type = "image/png" if extension == "png" else "image/jpeg"
    file_input = page.locator("input[type=file]").first
    file_input.set_input_files(
        {
            "name": f"start_frame.{extension}",
            "mimeType": mime_type,
            "buffer": start_image,
        },
        timeout=timeout_ms,
    )
    # Syntax's Livewire component can replace the native input immediately
    # after accepting the file, so checking ``input.files`` afterwards is
    # racy and falsely reports a failed upload. The form-ready wait below
    # observes the authoritative UI state instead.
    with contextlib.suppress(Exception):
        page.wait_for_timeout(800)
    logger.info("syntax.kling.start_frame_transfer_completed bytes=%s extension=%s", len(start_image), extension)


_KEYFRAME_FILES_SELECTOR = 'input[type="file"][x-ref="fileInput"][multiple]'


def _wait_for_syntax_keyframes_count_sync(
    page: Any, expected: int | None, *, timeout: int, capacity: int | None = 2,
) -> int:
    deadline = time.monotonic() + timeout / 1000
    count = None
    while time.monotonic() < deadline:
        text = page.evaluate(
            "selector => document.querySelector(selector)?.parentElement?.parentElement?.innerText",
            _KEYFRAME_FILES_SELECTOR,
        )
        match = re.search(r"(?:Загружено|Uploaded)\s*\(\s*(\d+)\s*/\s*([1-9]\d*)\s*\)", text or "", re.I)
        if match and (capacity is None or int(match.group(2)) == capacity):
            count = int(match.group(1))
            if expected is None or count == expected:
                return count
        page.wait_for_timeout(250)
    raise TimeoutError(f"Syntax Kling frame count: expected={expected}, observed={count}.")


def _wait_for_syntax_kling_keyframes_sync(page: Any) -> None:
    _wait_for_syntax_keyframes_count_sync(page, 2, timeout=60_000)


def _wait_for_syntax_kling_mode_sync(page: Any, mode: str) -> None:
    page.wait_for_function(
        """mode => {
            const isSelected = text => String(text || '').trim().toLowerCase() === mode.toLowerCase();
            return [...document.querySelectorAll('select, button, [role=combobox], [role=button]')].some(control => {
                if (!control.getClientRects().length || control.closest('[role=listbox], [role=menu]')) return false;
                if (control instanceof HTMLSelectElement) return isSelected(control.selectedOptions[0]?.text);
                return control.getAttribute('aria-expanded') !== 'true' && isSelected(control.innerText);
            });
        }""",
        arg=mode,
        timeout=15_000,
    )


def _syntax_kling_has_mode_selector_sync(page: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(page.get_by_text(re.compile(r"^(?:Режим|Mode)$", re.I)).first.is_visible())
    return False


def _upload_syntax_kling_frames_sync(page: Any, frames: list[tuple[str, bytes]], *, capacity: int | None) -> None:
    """Transfer ordered frames and verify their accepted previews before submitting."""
    # The other file input belongs to the hidden Telegram media picker.
    field = page.locator(_KEYFRAME_FILES_SELECTOR)
    uploader = field.locator("xpath=../..")
    count = _wait_for_syntax_keyframes_count_sync(page, None, timeout=30_000, capacity=capacity)
    while count:
        uploader.get_by_role("button", name=re.compile(r"^(Remove file|Удалить файл)$", re.I)).first.click(timeout=10_000)
        count -= 1
        _wait_for_syntax_keyframes_count_sync(page, count, timeout=15_000, capacity=capacity)
    for expected, (role, image) in enumerate(frames, 1):
        extension = _image_ext(image)
        file = {"name": f"{role}_frame.{extension}",
                "mimeType": "image/png" if extension == "png" else "image/jpeg", "buffer": image}
        # Syntx may accept only one file from a batch. Wait for the first to
        # finish before adding the second through the same form input.
        field.set_input_files(file, timeout=30_000)
        try:
            _wait_for_syntax_keyframes_count_sync(page, expected, timeout=30_000, capacity=capacity)
        except TimeoutError:
            page.wait_for_load_state("networkidle", timeout=15_000)
            observed = _wait_for_syntax_keyframes_count_sync(page, None, timeout=5_000, capacity=capacity)
            if observed == expected - 1:
                logger.warning("syntax.kling.keyframe_upload_retry role=%s confirmed=%s", role, observed)
                field.set_input_files(file, timeout=30_000)
                _wait_for_syntax_keyframes_count_sync(page, expected, timeout=30_000, capacity=capacity)
            elif observed != expected:
                raise
        logger.info("syntax.kling.keyframe_upload_confirmed role=%s count=%s", role, expected)


def _upload_syntax_kling_keyframes_sync(page: Any, start_image: bytes, end_image: bytes) -> None:
    _upload_syntax_kling_frames_sync(page, [("start", start_image), ("end", end_image)], capacity=2)
    logger.info("syntax.kling.keyframes_upload_confirmed")


def _fill_syntax_kling_webapp_sync(
    *,
    url: str,
    prompt: str,
    start_image: bytes,
    duration_sec: int,
    quality: str,
    aspect_ratio: str,
    version: str,
    audio_enabled: bool,
    timeout_sec: int,
    keyframe_end_image: bytes | None = None,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "Kling Syntax UI flow requires Playwright in the bot environment. "
            "Install the python package and browser runtime before using 📼 Kling."
        ) from exc

    timeout_ms = max(10_000, int(timeout_sec) * 1000)
    with sync_playwright() as p:
        # The bundled Playwright Chromium occasionally starts successfully but
        # loses its DevTools socket on a Telegram WebView navigation. In that
        # case retry the complete flow through installed Chrome, not merely a
        # new page in the already-disconnected browser.
        browser_launches = (
            ("playwright-chromium", {"headless": True}),
            ("system-chrome", {"channel": "chrome", "headless": True}),
        )
        last_error: Exception | None = None
        for browser_name, launch_kwargs in browser_launches:
            browser: Any | None = None
            try:
                browser = p.chromium.launch(**launch_kwargs)
                last_goto_error: Exception | None = None
                page: Any | None = None
                for attempt in range(3):
                    candidate = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True)
                    try:
                        candidate.goto(url, wait_until="commit", timeout=timeout_ms)
                        page = candidate
                        break
                    except Exception as exc:
                        last_goto_error = exc
                        logger.info(
                            "syntax.kling.webapp_goto_retry browser=%s attempt=%s err=%s",
                            browser_name,
                            attempt + 1,
                            _sanitize_syntax_webview_error(exc),
                        )
                        with contextlib.suppress(Exception):
                            candidate.close()
                        # A failed WebView navigation can leave Chromium's page
                        # transport disconnected. Retry in a fresh page rather
                        # than reusing that poisoned page.
                        time.sleep(1.5 * (attempt + 1))
                if page is None:
                    if last_goto_error is not None:
                        raise last_goto_error
                    raise RuntimeError("Syntax Kling webview did not open.")
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30_000))

                mode = ""
                try:
                    if keyframe_end_image or _syntax_kling_has_mode_selector_sync(page):
                        mode = "Keyframes" if keyframe_end_image else "Image to video"
                        _click_syntax_option_sync(page, "Модель", "Kling 3.0")
                        _click_syntax_option_sync(page, "Режим", mode)
                        _wait_for_syntax_kling_mode_sync(page, mode)
                        if keyframe_end_image:
                            _upload_syntax_kling_keyframes_sync(page, start_image, keyframe_end_image)
                        else:
                            _upload_syntax_kling_frames_sync(page, [("start", start_image)], capacity=None)
                    else:
                        _upload_syntax_kling_start_frame_sync(page, start_image)
                    _fill_and_commit_syntax_kling_prompt_sync(page, prompt)
                except Exception as exc:
                    controls = _syntax_visible_controls_snapshot_sync(page)
                    suffix = f" Visible controls: {controls}" if controls else ""
                    reason = _sanitize_syntax_webview_error(exc)
                    raise RuntimeError(f"Syntax Kling form did not become ready: {reason}.{suffix}") from exc

                if not mode:
                    _click_syntax_option_sync(page, "Версия", version)
                    page.wait_for_timeout(1200)
                _click_syntax_option_sync(page, "Качество", quality)
                _set_syntax_kling_duration_sync(page, duration_sec)

                if audio_enabled:
                    _enable_syntax_audio_sync(page)
                else:
                    _disable_syntax_audio_sync(page)

                if mode:
                    _wait_for_syntax_kling_mode_sync(page, mode)
                    if keyframe_end_image:
                        _wait_for_syntax_kling_keyframes_sync(page)
                    else:
                        _wait_for_syntax_keyframes_count_sync(page, 1, timeout=30_000, capacity=None)
                elif _syntax_kling_has_mode_selector_sync(page):
                    raise RuntimeError("Kling mode controls appeared late. Image upload and mode could not be verified.")
                try:
                    _click_syntax_kling_generate_sync(page)
                except Exception as exc:
                    if keyframe_end_image and "ERR_SOCKET_NOT_CONNECTED" in str(exc):
                        raise SyntaxSubmissionUnconfirmedError(
                            "Connection to Syntx was lost during the Keyframes submission."
                        ) from exc
                    raise
                return
            except Exception as exc:
                last_error = exc
                disconnected = "ERR_SOCKET_NOT_CONNECTED" in str(exc)
                # A missing Playwright browser executable is another expected
                # reason to use the installed Chrome. Once a browser was
                # actually opened, only retry its disconnected DevTools-socket
                # failure; configuration errors must surface unchanged.
                can_fallback_to_system_chrome = (
                    browser_name == "playwright-chromium"
                    and (browser is None or disconnected)
                )
                if not can_fallback_to_system_chrome:
                    raise
                logger.warning(
                    "syntax.kling.webapp_retry_system_chrome browser_started=%s err=%s",
                    browser is not None,
                    _sanitize_syntax_webview_error(exc),
                )
            finally:
                if browser is not None:
                    with contextlib.suppress(Exception):
                        browser.close()
        if last_error is not None:
            raise last_error


def _fill_syntax_model_settings_webapp_sync(
    *,
    url: str,
    model_label: str | None = None,
    duration_sec: int,
    quality: str,
    aspect_ratio: str,
    timeout_sec: int,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "Syntax model settings UI flow requires Playwright in the bot environment. "
            "Install the python package and browser runtime before using Syntax model settings."
        ) from exc

    timeout_ms = max(10_000, int(timeout_sec) * 1000)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception:
            browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True)
            last_goto_error: Exception | None = None
            for attempt in range(3):
                try:
                    page.goto(url, wait_until="commit", timeout=timeout_ms)
                    last_goto_error = None
                    break
                except Exception as exc:
                    last_goto_error = exc
                    logger.info(
                        "syntax.settings.webapp_goto_retry attempt=%s err=%s",
                        attempt + 1,
                        _sanitize_syntax_webview_error(exc),
                    )
                    page.wait_for_timeout(1500 * (attempt + 1))
            if last_goto_error is not None:
                raise last_goto_error
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30_000))

            if model_label and not _try_click_syntax_option_sync(page, "Модель", model_label):
                raise RuntimeError(f"Syntax model option was not set to {model_label}.")
            _try_click_syntax_option_sync(page, "Качество", quality)
            _try_click_syntax_option_sync(page, "Длительность", f"{int(duration_sec)} сек")
            _set_syntax_aspect_ratio_sync(page, aspect_ratio)
            if not _syntax_page_contains_aspect_ratio_sync(page, aspect_ratio):
                raise RuntimeError(f"Syntax settings page does not show selected aspect ratio {aspect_ratio}.")
            _click_syntax_settings_apply_sync(page)
        finally:
            browser.close()


_SYNTAX_VEO_QUEUE_LIMIT_RE = re.compile(
    r"достигнут\s+лимит\s+очереди\s+veo|veo\s+queue\s+limit|maximum\s+active\s+generations",
    re.IGNORECASE,
)


_SYNTAX_REMOVE_FILE_SELECTOR = "button[aria-label='Remove file'], button[aria-label='Удалить файл']"


def _syntax_veo_uploaded_reference_count_sync(page: Any) -> int:
    """Read attachment evidence; -1 means unknown, never an empty form."""
    label_count = None
    with contextlib.suppress(Exception):
        text = str(page.locator("body").inner_text() or "")
        match = re.search(r"(?:Загружен{1,2}[оаы]|Uploaded)\s*\(\s*(\d+)\s*/", text, flags=re.IGNORECASE)
        if match:
            label_count = int(match.group(1))
    controls_count = None
    with contextlib.suppress(Exception):
        controls = page.locator(_SYNTAX_REMOVE_FILE_SELECTOR)
        controls_count = sum(controls.nth(i).is_visible() for i in range(controls.count()))
    if controls_count:
        # A stale text counter must not hide an additional attached frame.
        if label_count is not None and label_count != controls_count:
            return -1
        return controls_count
    if label_count is not None:
        return label_count
    # No label and no attachment controls is empty only on a recognizable form.
    with contextlib.suppress(Exception):
        if controls_count == 0 and page.locator("input[type=file]").count() > 0:
            return 0
    return -1


def _syntax_veo_queue_is_full_sync(page: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(_SYNTAX_VEO_QUEUE_LIMIT_RE.search(str(page.locator("body").inner_text() or "")))
    return False


def _wait_for_syntax_veo_uploaded_count_sync(
    page: Any,
    expected: int,
    *,
    timeout_ms: int,
) -> None:
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if _syntax_veo_uploaded_reference_count_sync(page) == expected:
            return
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Syntax Veo uploaded reference count did not become {expected}; "
        f"observed={_syntax_veo_uploaded_reference_count_sync(page)} (-1 means unknown)."
    )


def _clear_syntax_veo_uploaded_references_sync(page: Any, *, timeout_ms: int = 10_000) -> None:
    """Remove stale Telegram picker items so retries never accumulate frames."""

    for _attempt in range(4):
        count_before = _syntax_veo_uploaded_reference_count_sync(page)
        if count_before == 0:
            return
        if count_before < 0:
            raise RuntimeError("Syntax Veo attachment state is unknown; refusing to add another frame.")
        buttons = page.locator(_SYNTAX_REMOVE_FILE_SELECTOR)
        visible_button: Any | None = None
        for index in range(buttons.count()):
            candidate = buttons.nth(index)
            with contextlib.suppress(Exception):
                if candidate.is_visible():
                    visible_button = candidate
                    break
        if visible_button is None:
            raise RuntimeError("Syntax Veo uploaded references cannot be cleared safely.")
        visible_button.click(timeout=timeout_ms)
        # Removing one Telegram picker item triggers several Livewire updates
        # and can take around ten seconds even after the click is accepted.
        try:
            _wait_for_syntax_veo_uploaded_count_sync(
                page,
                count_before - 1,
                timeout_ms=max(timeout_ms, 15_000),
            )
        except RuntimeError:
            # Livewire can accept the remove request while leaving the current
            # page on the old upload counter. Reload the same authenticated
            # WebView and retry from its authoritative server-side state.
            logger.warning(
                "syntax.veo.remove_reference_stale_counter attempt=%s count_before=%s",
                _attempt + 1,
                count_before,
            )
            with contextlib.suppress(Exception):
                page.reload(wait_until="commit", timeout=max(timeout_ms, 30_000))
                page.wait_for_load_state("networkidle", timeout=max(timeout_ms, 15_000))
            continue
    if _syntax_veo_uploaded_reference_count_sync(page) != 0:
        raise RuntimeError("Syntax Veo still contains stale uploaded references.")


def _wait_for_syntax_veo_queue_slot_sync(page: Any, *, timeout_ms: int) -> None:
    """Wait for an account queue slot before the paid Veo submit click."""

    deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
    while _syntax_veo_queue_is_full_sync(page):
        if time.monotonic() >= deadline:
            raise RuntimeError("Syntax Veo queue limit is still active; generation was not started.")
        page.wait_for_timeout(10_000)
        with contextlib.suppress(Exception):
            page.reload(wait_until="commit", timeout=min(timeout_ms, 60_000))
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30_000))


def _submit_syntax_veo_generate_sync(page: Any, *, timeout_ms: int) -> str:
    """Click first, then wait/retry only after a real queue rejection.

    A permanent "maximum active generations" banner can include the current
    form's already-submitted job.  Waiting for that banner to disappear before
    the first click deadlocks the bot.  The WebView launch is idempotent: a
    repeated accepted click returns "Эта генерация уже отправлена".
    """

    for attempt in range(3):
        # Retry the same draft, never create a fresh paid request here.
        text = ""
        with contextlib.suppress(Exception):
            text = str(_syntax_kling_submit_snapshot_sync(page).get("text") or "")
        if _KLING_SUBMISSION_ALREADY_SENT_RE.search(text):
            return "started"
        try:
            return _click_syntax_kling_generate_sync(
                page, timeout_ms=15_000, wait_for_form_ready=False,
            )
        except SyntaxVeoQueueFullError:
            if attempt == 2:
                raise
            logger.info("syntax.veo.submit_queue_rejected waiting_for_slot=true")
            _wait_for_syntax_veo_queue_slot_sync(page, timeout_ms=timeout_ms)
        except Exception as exc:
            # Explicit rejection is terminal. Retry only failed UI interaction
            # or missing acknowledgement, on the same idempotent WebView.
            retryable = any(marker in str(exc).lower() for marker in (
                "timeout", "timed out", "did not confirm generation", "detached",
            ))
            if attempt == 2 or not retryable:
                raise
            logger.warning("syntax.veo.submit_retry attempt=%s error=%s",
                           attempt + 1, _sanitize_syntax_webview_error(exc))
            page.wait_for_timeout(1500 * (attempt + 1))
    raise RuntimeError("Syntax Veo submit attempts exhausted.")


def _open_syntax_veo_page_sync(playwright: Any, url: str, timeout_ms: int) -> tuple[Any, Any]:
    """Retry a broken bundled browser before any generation can be submitted."""
    for attempt, channel in enumerate((None, "chrome", "chrome", "chrome")):
        browser = None
        try:
            kwargs = {"headless": True}
            if channel:
                kwargs["channel"] = channel
            browser = playwright.chromium.launch(**kwargs)
            page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True)
            page.goto(url, wait_until="commit", timeout=min(timeout_ms, 60_000))
            return browser, page
        except Exception as exc:
            launch_failed = browser is None
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.close()
            retryable = "ERR_SOCKET_NOT_CONNECTED" in str(exc) or (channel is None and launch_failed)
            if attempt == 3 or not retryable:
                raise
            logger.warning("syntax.veo.webapp_retry_system_chrome err=%s", _sanitize_syntax_webview_error(exc))
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Syntax Veo webview did not open.")


class SyntaxSubmissionUnconfirmedError(RuntimeError):
    """A click was attempted but no provider task acknowledgement appeared."""


class SyntaxVeoNotSubmittedError(RuntimeError):
    """The WebView failed before the Generate action was attempted."""


def _prepare_syntax_veo_form_sync(page: Any, *, start_image: bytes, prompt: str, aspect_ratio: str) -> None:
    """Prepare one live form; uploads are transient and must not be reloaded."""
    stage = "inspect_attachments"
    for attempt in range(1, 4):
        try:
            stage = "clear_attachments"
            _clear_syntax_veo_uploaded_references_sync(page)
            stage = "upload_frame"
            _upload_syntax_kling_start_frame_sync(page, start_image)
            stage = "confirm_upload"
            _wait_for_syntax_veo_uploaded_count_sync(page, 1, timeout_ms=60_000)
            stage = "commit_prompt"
            _fill_and_commit_syntax_kling_prompt_sync(page, prompt)
            stage = "set_aspect_ratio"
            _set_syntax_aspect_ratio_sync(page, aspect_ratio)
            if not _syntax_page_contains_aspect_ratio_sync(page, aspect_ratio):
                raise RuntimeError("Requested aspect ratio is not visible")
            stage = "form_ready"
            _wait_for_syntax_kling_form_ready_sync(page)
            if _syntax_veo_uploaded_reference_count_sync(page) != 1:
                raise RuntimeError("Attachment state changed while preparing the form")
            logger.info("syntax.veo.prepared attempt=%s frame_attached=true", attempt)
            return
        except Exception as exc:
            logger.warning("syntax.veo.prepare_failed stage=%s attempt=%s count=%s error=%s",
                           stage, attempt, _syntax_veo_uploaded_reference_count_sync(page),
                           _sanitize_syntax_webview_error(exc))
            if attempt == 3:
                raise SyntaxVeoNotSubmittedError(
                    f"Подготовка формы не завершена: этап={stage}, попыток={attempt}. "
                    + _sanitize_syntax_webview_error(exc)
                ) from exc
            # Same draft, before Generate: safe to reload and replace its frame.
            try:
                page.reload(wait_until="commit", timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                logger.warning("syntax.veo.prepare_reload_failed attempt=%s", attempt)


def _fill_syntax_veo_webapp_sync(
    *,
    url: str,
    prompt: str,
    start_image: bytes,
    duration_sec: int,
    aspect_ratio: str,
    timeout_sec: int,
) -> None:
    """Configure and launch the current Syntax Veo WebView.

    Telegram supplies the prompt and uploaded frame to this page.  We verify
    and re-commit those values, apply the requested duration/aspect ratio, and
    click the visible Generate button.  A genuine queue rejection is retried
    after a slot becomes available; an already-submitted form is accepted.
    """

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "Syntax Veo settings flow requires Playwright and a Chromium runtime."
        ) from exc

    timeout_ms = max(30_000, int(timeout_sec) * 1000)
    with sync_playwright() as p:
        try:
            browser, page = _open_syntax_veo_page_sync(p, url, timeout_ms)
        except Exception as exc:
            raise SyntaxVeoNotSubmittedError(_sanitize_syntax_webview_error(exc)) from exc
        submit_attempted = False
        try:
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30_000))

            _prepare_syntax_veo_form_sync(
                page, start_image=start_image, prompt=prompt, aspect_ratio=aspect_ratio,
            )

            # Click once even if the page already shows the account-wide queue
            # banner: the current form may itself be one of those active jobs.
            # Syntax deduplicates this WebView launch server-side.
            queue_wait_ms = max(30_000, timeout_ms - 60_000)
            submit_attempted = True
            _submit_syntax_veo_generate_sync(page, timeout_ms=queue_wait_ms)
        except Exception as exc:
            if not submit_attempted:
                raise SyntaxVeoNotSubmittedError(_sanitize_syntax_webview_error(exc)) from exc
            raise
        finally:
            browser.close()


async def _try_configure_syntax_model_settings(
    client: Any,
    target: Any,
    *,
    bot_entity: Any,
    reply_anchor: int,
    model_label: str | None = None,
    duration_sec: int,
    quality: str,
    aspect_ratio: str,
    poll: float,
    timeout: int,
    required: bool = False,
) -> str:
    configure_message: Any | None = None
    configure_button: Any | None = None
    deadline = time.monotonic() + max(8.0, min(float(timeout), float(_syntax_model_settings_probe_attempts()) * 2.0))
    configure_message, configure_button = await _wait_for_syntax_configure_button(
        client,
        target,
        reply_anchor=reply_anchor,
        poll=max(0.5, min(poll, 2.0)),
        deadline=deadline,
    )
    if configure_message is None or configure_button is None:
        if required:
            raise RuntimeError("Syntax model settings button was not found; refusing to dispatch video with unknown aspect ratio.")
        return ""
    webview_url = await _syntax_configure_button_webview_url(client, target, bot_entity, configure_message, configure_button)
    if not webview_url:
        if required:
            raise RuntimeError("Syntax model settings button did not expose a webview URL; refusing to dispatch video with unknown aspect ratio.")
        return ""
    ratio = _normalize_syntax_aspect_ratio(aspect_ratio)
    try:
        await asyncio.to_thread(
            _fill_syntax_model_settings_webapp_sync,
            url=webview_url,
            model_label=model_label,
            duration_sec=duration_sec,
            quality=quality,
            aspect_ratio=ratio,
            timeout_sec=max(20, min(timeout, 60)),
        )
    except Exception as exc:
        logger.warning(
            "syntax.settings.configure_failed aspect_ratio=%s err=%s",
            ratio,
            _sanitize_syntax_webview_error(exc),
        )
        message = f"Syntax model settings were not changed to {ratio}: {_sanitize_syntax_webview_error(exc)}"
        if required:
            raise RuntimeError(message) from exc
        return message
    return f"Syntax model settings applied: aspect_ratio={ratio}."


def _syntax_veo_model_label(model: str | None) -> str | None:
    normalized = str(model or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"veo_3_1_lite", "veo3_1_lite", "veo31_lite"}:
        return "Veo3.1 Lite"
    return None


async def _run_syntax_kling_configure_flow(
    client: Any,
    target: Any,
    *,
    bot_entity: Any,
    prompt: str,
    start_image: bytes,
    duration_sec: int,
    aspect_ratio: str,
    reply_anchor: int,
    sent_ids: set[int],
    attempt_id: str,
    poll: float,
    deadline: float,
    timeout: int,
    initial_last_info: str,
    audio_enabled: bool = True,
    keyframe_end_image: bytes | None = None,
) -> Any:
    configure_message, configure_button = await _wait_for_syntax_configure_button(
        client,
        target,
        reply_anchor=reply_anchor,
        poll=poll,
        deadline=deadline,
    )
    if configure_message is None or configure_button is None:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Syntax Kling did not show the Настроить button after image upload.",
            attempt_id=attempt_id,
        )
    configure_mid = int(getattr(configure_message, "id", 0) or 0)
    if configure_mid:
        sent_ids.add(configure_mid)
    webview_url = await _syntax_configure_button_webview_url(client, target, bot_entity, configure_message, configure_button)
    if not webview_url:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Syntax Kling Настроить button did not expose a webview URL.",
            attempt_id=attempt_id,
        )
    try:
        await asyncio.to_thread(
            _fill_syntax_kling_webapp_sync,
            url=webview_url,
            prompt=prompt,
            start_image=start_image,
            duration_sec=duration_sec,
            quality="720",
            aspect_ratio=aspect_ratio,
            version="3.0",
            audio_enabled=audio_enabled,
            timeout_sec=max(30, min(timeout, 180)),
            **({"keyframe_end_image": keyframe_end_image} if keyframe_end_image else {}),
        )
    except SyntaxSubmissionUnconfirmedError as exc:
        # Generate may have reached Syntx even when the WebView never acknowledges it.
        # Keep listening to the existing request; never submit another paid job here.
        initial_last_info = f"Syntax Kling launch unconfirmed: {_sanitize_syntax_webview_error(exc)}"
        logger.warning("syntax.kling.ui_unconfirmed attempt_id=%s; waiting_for_telegram_result=true", attempt_id)
    except Exception as exc:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message=f"Syntax Kling UI configure failed: {_sanitize_syntax_webview_error(exc)}",
            attempt_id=attempt_id,
        )
    return await _poll_syntax_bot_until_video(
        client,
        target,
        reply_anchor=reply_anchor,
        sent_ids=sent_ids,
        attempt_id=attempt_id,
        poll=poll,
        deadline=time.monotonic() + timeout,
        initial_last_info=initial_last_info,
        timeout=timeout,
        expected_lang_token=_expected_lang_token_from_prompt(prompt),
    )


async def _run_syntax_veo_configure_flow(
    client: Any,
    target: Any,
    *,
    bot_entity: Any,
    configure_message: Any,
    configure_button: Any,
    prompt: str,
    start_image: bytes,
    duration_sec: int,
    aspect_ratio: str,
    reply_anchor: int,
    sent_ids: set[int],
    attempt_id: str,
    poll: float,
    deadline: float,
    timeout: int,
    initial_last_info: str,
    expected_lang_token: str | None,
) -> Any:
    """Launch Veo through Syntax's mandatory Settings WebView, then poll chat."""

    configure_mid = int(getattr(configure_message, "id", 0) or 0)
    if configure_mid:
        sent_ids.add(configure_mid)
    configure_error = await _submit_syntax_veo_webview(
        client,
        target,
        bot_entity=bot_entity,
        configure_message=configure_message,
        configure_button=configure_button,
        prompt=prompt,
        start_image=start_image,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        timeout_sec=max(30, int(deadline - time.monotonic())),
    )
    if configure_error:
        if configure_error.startswith("Syntax Veo not submitted:"):
            return _result(success=False, video_bytes=None, video_uri=None,
                           provider_message=configure_error, attempt_id=attempt_id)
        initial_last_info = configure_error
        logger.warning(
            "syntax.veo.ui_unconfirmed attempt_id=%s; waiting_for_telegram_result=true error=%s",
            attempt_id, configure_error,
        )
    return await _poll_syntax_bot_until_video(
        client,
        target,
        reply_anchor=reply_anchor,
        sent_ids=sent_ids,
        attempt_id=attempt_id,
        poll=poll,
        deadline=deadline,
        initial_last_info=initial_last_info,
        timeout=timeout,
        expected_lang_token=expected_lang_token,
    )


async def _submit_syntax_veo_webview(
    client: Any,
    target: Any,
    *,
    bot_entity: Any,
    configure_message: Any,
    configure_button: Any,
    prompt: str,
    start_image: bytes,
    duration_sec: int,
    aspect_ratio: str,
    timeout_sec: int,
) -> str | None:
    """Submit one Veo draft and return as soon as Livewire accepts Generate."""

    webview_url = await _syntax_configure_button_webview_url(
        client,
        target,
        bot_entity,
        configure_message,
        configure_button,
    )
    if not webview_url:
        return "Syntax Veo not submitted: Настроить и запустить button did not expose a WebView URL."
    try:
        await asyncio.to_thread(
            _fill_syntax_veo_webapp_sync,
            url=webview_url,
            prompt=prompt,
            start_image=start_image,
            duration_sec=8,
            aspect_ratio=aspect_ratio,
            timeout_sec=max(30, int(timeout_sec)),
        )
    except SyntaxSubmissionUnconfirmedError as exc:
        return f"Syntax Veo launch unconfirmed: {_sanitize_syntax_webview_error(exc)}"
    except SyntaxVeoNotSubmittedError as exc:
        return f"Syntax Veo not submitted: {_sanitize_syntax_webview_error(exc)}"
    except Exception as exc:
        return f"Syntax Veo UI configure failed: {_sanitize_syntax_webview_error(exc)}"
    return None


async def _wait_for_syntax_menu_reply(client: Any, target: Any, *, after_id: int,
                                      expected_button: str, timeout_sec: float = 60) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        messages = await client.get_messages(target, limit=20)
        if messages is not None and not isinstance(messages, (list, tuple)):
            messages = [messages]
        for message in messages or []:
            if getattr(message, "out", False) or int(getattr(message, "id", 0) or 0) <= after_id:
                continue
            for button in _syntax_message_buttons(message):
                if (expected_button == "configure" and _syntax_button_is_configure(button)) or (
                    _syntax_button_text(button).casefold() == expected_button.casefold()
                ):
                    return True
        await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    return False


async def _send_syntax_video_preflight(client: Any, target: Any, *, provider: str | None, model: str) -> int:
    # These are menu selections, not the paid WebView Generate button.
    # Confirm each screen before sending the next selection or any media.
    provider_kind = _normalize_syntax_provider(provider, model)
    command = SYNTAX_PROVIDER_COMMANDS[provider_kind]
    steps = [("/video", command), (command, "Сгенерировать" if provider_kind == SYNTAX_PROVIDER_VEO else "configure")]
    if provider_kind == SYNTAX_PROVIDER_VEO:
        steps.append(("Сгенерировать", "configure"))
    last_id = 0
    for text, expected_button in steps:
        first_id = 0
        for attempt in range(2):
            message = await client.send_message(target, text)
            last_id = int(message.id)
            first_id = first_id or last_id
            if await _wait_for_syntax_menu_reply(client, target, after_id=first_id, expected_button=expected_button):
                logger.info("syntax.menu_confirmed command=%s attempt=%s", text, attempt + 1)
                break
            logger.warning("syntax.menu_retry command=%s attempt=%s", text, attempt + 1)
        else:
            raise SyntaxVeoNotSubmittedError(
                f"Syntax Veo not submitted: menu acknowledgement missing for {text!r}; no frame or prompt sent."
            )
    return last_id


def _syntax_bot_requires_tool_selection(text: str) -> bool:
    low = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return (
        "не выбран инструмент" in low
        or "инструмент для работы с чат-ботом не выбран" in low
    )


def _syntax_bot_message_is_hard_reject(text: str, _urls: list[str]) -> bool:
    """
    Syntax иногда отвечает финальным сообщением об ошибке / фильтрах (без видео).

    Такие ответы нужно завершать сразу, а не ждать таймаут.
    """
    raw = text or ""
    low = raw.lower()
    if _syntax_bot_requires_tool_selection(raw):
        return True
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
        "заблокировала запрос",
        "не соответствует стандартам поведения",
        "не прошло модерац",
        "content policy",
        "safety",
    )
    return any(m in low for m in markers)


def _text_looks_like_syntax_pending_status(text: str) -> bool:
    """Syntax often *edits* the same message from status to final link — keep re-reading it."""
    low = (text or "").lower()
    # Setup acknowledgements also contain "генерация", but are not jobs.
    if re.search(r"(?:промпт|файл)\s+сохран|prompt\s+saved|file\s+saved|чтобы\s+начать\s+генерацию", low):
        return False
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
            RUSSIAN_YO_RULE,
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
    try:
        raw = gemini_generate_sync(
            instruction,
            rate_limit_max_attempts=1,
            model_profile=GEMINI_MODEL_PROFILE_EDIT,
        )
        obj = extract_json_object(raw)
    except Exception as exc:
        logger.warning("syntax_prompt_shorten.gemini failed err=%s", exc)
        return None
    shortened = obj.get("prompt")
    if not isinstance(shortened, str):
        return None
    shortened = shortened.strip()
    if shortened and len(shortened) <= max_len:
        return shortened
    logger.warning(
        "syntax_prompt_shorten.invalid_length chars=%s limit=%s",
        len(shortened),
        max_len,
    )
    return None


def _shorten_prompt_for_syntax_locally(prompt: str, *, max_len: int) -> str:
    """Deterministic network-free fallback that preserves action and speech instructions."""
    original = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if len(original) <= max_len:
        return original

    speech_match = re.search(r"\bCHARACTERS?\s+SPEAKS?\s*:", original, flags=re.IGNORECASE)
    voice_matches = list(re.finditer(r"\bvoice_lock_full\s*:", original, flags=re.IGNORECASE))
    voice_match = voice_matches[-1] if voice_matches else None
    if voice_match and (speech_match is None or voice_match.start() < speech_match.start()):
        suffix_start = voice_match.start()
    elif speech_match:
        suffix_start = speech_match.start()
    else:
        suffix_start = max(0, len(original) - min(900, max_len // 2))
    suffix = original[suffix_start:].strip()
    suffix_budget = min(len(suffix), max(500, min(1100, max_len * 3 // 5)))
    if len(suffix) > suffix_budget:
        suffix = suffix[-suffix_budget:].lstrip()

    separator = " "
    prefix_budget = max_len - len(separator) - len(suffix)
    prefix = original[: max(0, prefix_budget)].rstrip()
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0].rstrip()

    shortened = f"{prefix}{separator}{suffix}".strip()
    if len(shortened) > max_len:
        shortened = shortened[:max_len].rstrip()
    return shortened


def _prepare_prompt_for_syntax(prompt: str, *, max_len: int | None = None) -> tuple[str, bool] | None:
    limit = max_len if max_len is not None else _syntax_prompt_max_chars()
    original = (prompt or "").strip()
    if not original:
        return None
    token = _expected_lang_token_from_prompt(original)
    if token:
        body = re.sub(r"^\s*lang\s*[:=]\s*[a-z]{2,8}\b\s*", "", original, flags=re.IGNORECASE)
        original = f"{token} {body}".strip()
    if len(original) <= limit:
        return original, False
    body_limit = limit - len(token) - 1 if token else limit
    if body_limit <= 0:
        return None
    shortened = _shorten_prompt_for_syntax_with_gemini_sync(original, max_len=body_limit)
    if shortened is None:
        shortened = _shorten_prompt_for_syntax_locally(original, max_len=body_limit)
        logger.warning(
            "syntax_prompt_shorten.local_fallback original_chars=%s shortened_chars=%s limit=%s",
            len(original),
            len(shortened),
            limit,
        )
    if shortened and token:
        shortened = re.sub(r"^\s*lang\s*[:=]\s*[a-z]{2,8}\b\s*", "", shortened, flags=re.IGNORECASE)
        shortened = f"{token} {shortened}".strip()
    if not shortened or len(shortened) > limit:
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


def _syntax_media_download_timeout_sec() -> float:
    raw = str(os.getenv(SYNTAX_MEDIA_DOWNLOAD_TIMEOUT_SEC_ENV) or "").strip()
    if not raw:
        return 300.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 300.0


async def _download_telegram_media_bytes(client: Any, message: Any) -> bytes | None:
    timeout_sec = _syntax_media_download_timeout_sec()
    document = getattr(message, "document", None)
    document_id = getattr(document, "id", None)
    size = getattr(document, "size", None)
    if isinstance(document_id, int) and isinstance(size, int) and size > 0:
        # Persist complete chunks, so a timeout or executor restart can resume.
        root = Path(__file__).resolve().parent.parent / "runtime" / "syntax_media"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / f"{document_id}-{size}.part"
        with (root / f"{document_id}-{size}.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            for attempt in range(3):
                with path.open("r+b" if path.exists() else "w+b") as output:
                    output.seek(0, os.SEEK_END)
                    offset = output.tell()
                    if offset == size:
                        return path.read_bytes()
                    # Telegram offsets must be aligned; only discard a partial block.
                    offset = (offset // 4096) * 4096 if offset < size else 0
                    output.truncate(offset)
                    output.seek(offset)
                    stream = client.iter_download(message.media, offset=offset, request_size=512 * 1024)
                    try:
                        while offset < size:
                            # This is an inactivity deadline, not a whole-file deadline.
                            chunk = await asyncio.wait_for(stream.__anext__(), timeout=timeout_sec)
                            if not chunk or offset + len(chunk) > size:
                                raise ValueError("Invalid Telegram media chunk size")
                            output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                            offset += len(chunk)
                        logger.info("syntax.download_complete message_id=%s bytes=%s", message.id, size)
                        return path.read_bytes()
                    except (TimeoutError, ConnectionError, OSError, StopAsyncIteration) as exc:
                        logger.warning("syntax.download_resume message_id=%s offset=%s size=%s attempt=%s error=%s",
                                       message.id, offset, size, attempt + 1, type(exc).__name__)
                    finally:
                        await stream.close()
            return None
    try:
        data = await asyncio.wait_for(client.download_media(message, file=bytes), timeout=timeout_sec)
    except TimeoutError:
        logger.warning(
            "syntax.download_media_timeout message_id=%s timeout_sec=%s",
            int(getattr(message, "id", 0) or 0),
            timeout_sec,
        )
        return None
    if isinstance(data, bytes) and data:
        return data
    return None


async def _try_download_from_urls(urls: list[str]) -> tuple[bytes | None, str | None, str]:
    """Returns (video_bytes, video_uri, provider_message)."""
    for url in urls:
        if url.lower().endswith(".mp4"):
            return None, url, f"Syntax returned direct mp4 link: {url}"
        if "getvideo.syntxai.net/" in url.lower():
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(fetch_mp4_bytes_from_getvideo_page, url),
                    timeout=_syntax_media_download_timeout_sec(),
                )
                return data, url, "Downloaded mp4 from Syntax getvideo page."
            except TimeoutError:
                logger.warning("syntax.getvideo_download_timeout url=%s", url)
                continue
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
    require_lang_token_for_loose: bool = True,
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
    response_token = _expected_lang_token_from_prompt(_extract_text(message))
    if expected_lang_token and response_token and response_token != expected_lang_token:
        return None
    mid = int(getattr(message, "id", 0) or 0)
    reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    our_high = max(sent_ids) if sent_ids else 0
    correlated = bool((reply_id and reply_id in sent_ids) or mid in sent_ids)
    loose_missing_lang_token = False
    if not correlated:
        if not (_syntax_allow_loose_correlation() and mid > our_high):
            return None
        text_for_lang = _extract_text(message)
        if require_lang_token_for_loose and expected_lang_token:
            if re.search(r"\blang\s*[:=]\s*[a-z]{2,8}\b", text_for_lang, flags=re.IGNORECASE):
                if expected_lang_token not in text_for_lang.lower():
                    return None
            loose_missing_lang_token = expected_lang_token not in text_for_lang.lower()

    if _message_looks_like_video(message):
        if loose_missing_lang_token:
            return None
        data = await _download_telegram_media_bytes(client, message)
        if data:
            return data, None, "Video downloaded from Syntax bot.", False, False
        return None, None, "Syntax sent a video attachment but download failed or timed out.", True, False

    text = _extract_text(message)
    urls = _collect_urls_from_message(message)
    if _syntax_bot_message_is_hard_reject(text, urls):
        body = (text[:4500] if text else "Syntax отклонил запрос.").strip()
        return None, None, body, False, True

    vbytes, vuri, msg = await _try_download_from_urls(urls)
    if vbytes or vuri:
        if loose_missing_lang_token:
            return None
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


def _syntax_message_revision(message: Any) -> tuple[Any, ...]:
    """A Telegram message ID is stable when Syntax edits a status into a result."""
    document = getattr(message, "document", None)
    return (
        str(getattr(message, "edit_date", "") or ""),
        _extract_text(message),
        tuple(_collect_urls_from_message(message)),
        _message_looks_like_video(message),
        getattr(document, "id", None),
    )


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
    processed_revisions: dict[int, tuple[Any, ...]] | None = None,
) -> Any | None:
    """Apply one bot ``Message``; on success returns ``FallbackResult``."""
    mid = int(getattr(msg, "id", 0) or 0)
    if mid <= reply_anchor:
        return None
    if processed_revisions is not None:
        processed_revisions[mid] = _syntax_message_revision(msg)
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


async def _capture_syntax_submission_status(client: Any, target: Any, *, after_id: int) -> set[int]:
    """Bind the status while this request still owns the Syntax webview."""
    latest = _first_message_from_get_messages(await client.get_messages(target, limit=1))
    if latest is None or getattr(latest, "out", False) or _message_looks_like_video(latest):
        return set()
    mid = int(getattr(latest, "id", 0) or 0)
    text = _extract_text(latest)
    if mid <= after_id or _syntax_find_configure_button(latest) is not None:
        return set()
    # This is the new response to the current submission, captured before
    # dispatching the next language. Bind even an unknown status or an
    # immediate failure: Syntx later edits this same ID without reply_to/lang.
    if re.search(r"(?:промпт|файл)\s+сохран|prompt\s+saved|file\s+saved", text, re.IGNORECASE):
        return set()
    return {mid}


def _batch_message_candidate_keys(message: Any, pending_states: dict[str, dict[str, Any]]) -> list[str]:
    """Route a batch reply to the request it can actually be correlated with."""
    mid = int(getattr(message, "id", 0) or 0)
    reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    text = _extract_text(message).lower()

    explicit_token = _expected_lang_token_from_prompt(text)
    if explicit_token:
        return [key for key, state in pending_states.items()
                if state["request"].expected_lang_token == explicit_token]

    if reply_id:
        matched = [
            key
            for key, state in pending_states.items()
            if int(reply_id) in state["sent_ids"]
            or int(reply_id) in state["pending_refetch"]
        ]
        if matched:
            return matched

    edited_status_matches = [
        key
        for key, state in pending_states.items()
        if mid in state.get("submission_status_ids", set())
        or mid in state["pending_refetch"] or mid in state.get("processed_revisions", {})
    ]
    if edited_status_matches:
        return edited_status_matches

    token_matches = [
        key
        for key, state in pending_states.items()
        if state["request"].expected_lang_token
        and state["request"].expected_lang_token in text
    ]
    if token_matches:
        return token_matches

    if len(pending_states) == 1:
        return list(pending_states)

    if (
        _text_looks_like_syntax_pending_status(text)
        or _syntax_bot_message_is_hard_reject(text, _collect_urls_from_message(message))
    ):
        unanchored = [
            key
            for key, state in pending_states.items()
            if mid > int(state["reply_anchor"])
            and not state["pending_refetch"]
        ]
        if unanchored:
            return unanchored[:1]

    # A bare video in a multi-request chat has no reliable language identity.
    return []


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
    processed_revisions: dict[int, tuple[Any, ...]] = {}

    def needs_processing(message: Any) -> bool:
        mid = int(getattr(message, "id", 0) or 0)
        return mid not in sent_ids and (
            mid not in processed or processed_revisions.get(mid) != _syntax_message_revision(message)
        )

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
            if lmid > our_high and needs_processing(latest):
                done = await _syntax_dispatch_bot_reply(
                    client,
                    latest,
                    reply_anchor=reply_anchor,
                    sent_ids=sent_ids,
                    processed=processed,
                processed_revisions=processed_revisions,
                    pending_refetch=pending_refetch,
                    attempt_id=attempt_id,
                    last_holder=last_holder,
                    expected_lang_token=expected_lang_token,
                )
                if done is not None:
                    return done
        async for msg in client.iter_messages(target, limit=40):
            mid = int(getattr(msg, "id", 0) or 0)
            if mid <= reply_anchor or not needs_processing(msg):
                continue
            done = await _syntax_dispatch_bot_reply(
                client,
                msg,
                reply_anchor=reply_anchor,
                sent_ids=sent_ids,
                processed=processed,
                processed_revisions=processed_revisions,
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
                processed_revisions=processed_revisions,
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
    syntax_provider: str | None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
    kling_keyframes: bool = False,
) -> Any:
    if kling_keyframes and (not end_image or _normalize_syntax_provider(syntax_provider, model) != SYNTAX_PROVIDER_KLING):
        return _result(success=False, video_bytes=None, video_uri=None, attempt_id=attempt_id,
                       provider_message="Kling Keyframes requires two approved frames and the Kling provider.")
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

    if timeout_sec is not None and int(timeout_sec) > 0:
        timeout = int(timeout_sec)
    else:
        env_raw = (os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "").strip()
        timeout = int(env_raw) if env_raw.isdigit() and int(env_raw) > 0 else 15 * 60
    poll = _poll_interval_sec()
    bot_username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))

    prompt_source = str(prompt or "").strip()
    if not prompt_source:
        return _result(
            success=False,
            video_bytes=None,
            video_uri=None,
            provider_message="Syntax prompt is empty; refusing to dispatch.",
            attempt_id=attempt_id,
        )

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

    prepared_prompt = await asyncio.to_thread(_prepare_prompt_for_syntax, prompt_source)
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
    provider_kind = _normalize_syntax_provider(syntax_provider, model)

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
                preflight_anchor = await _send_syntax_video_preflight(
                    client,
                    target,
                    provider=syntax_provider,
                    model=model,
                )
                if provider_kind == SYNTAX_PROVIDER_KLING:
                    # Current Syntax exposes the Configure WebApp immediately
                    # after /kling.  Prefer it before sending a Telegram photo:
                    # a chat photo is a *picker candidate*, not a form upload,
                    # and can replace the real file with a failed thumbnail.
                    configure_message, _configure_button = await _wait_for_syntax_configure_button(
                        client,
                        target,
                        reply_anchor=preflight_anchor,
                        poll=max(0.5, min(poll, 2.0)),
                        deadline=time.monotonic() + 12.0,
                    )
                    if configure_message is not None:
                        deadline = time.monotonic() + timeout
                        return await _run_syntax_kling_configure_flow(
                            client,
                            target,
                            bot_entity=target,
                            prompt=prompt_text,
                            start_image=start_image,
                            duration_sec=duration_sec,
                            aspect_ratio=aspect_ratio,
                            reply_anchor=preflight_anchor,
                            sent_ids=sent_ids,
                            attempt_id=attempt_id,
                            poll=poll,
                            deadline=deadline,
                            timeout=timeout,
                            initial_last_info=last_info,
                            audio_enabled=audio_enabled,
                            **({"keyframe_end_image": end_image} if kling_keyframes else {}),
                        )

                start_file = io.BytesIO(start_image)
                start_file.name = f"start_frame.{_image_ext(start_image)}"
                start_msg = await client.send_file(target, file=start_file, force_document=False)
                sent_ids.add(int(start_msg.id))
                reply_anchor = int(start_msg.id)
                if provider_kind == SYNTAX_PROVIDER_KLING:
                    if end_image and not kling_keyframes:
                        last_info = "Kling Syntax UI uses the start image only; end frame was not sent."
                    if prompt_shortened:
                        last_info = " ".join(
                            x
                            for x in (
                                last_info,
                                f"Prompt exceeded Syntax limit ({_syntax_prompt_max_chars()} chars) "
                                "and was shortened automatically before UI send.",
                            )
                            if x
                        ).strip()
                    deadline = time.monotonic() + timeout
                    return await _run_syntax_kling_configure_flow(
                        client,
                        target,
                        bot_entity=target,
                        prompt=prompt_text,
                        start_image=start_image,
                        duration_sec=duration_sec,
                        aspect_ratio=aspect_ratio,
                        reply_anchor=reply_anchor,
                        sent_ids=sent_ids,
                        attempt_id=attempt_id,
                        poll=poll,
                        deadline=deadline,
                        timeout=timeout,
                        initial_last_info=last_info,
                        audio_enabled=audio_enabled,
                        **({"keyframe_end_image": end_image} if kling_keyframes else {}),
                    )
                if end_image:
                    end_file = io.BytesIO(end_image)
                    end_file.name = f"end_frame.{_image_ext(end_image)}"
                    end_msg = await client.send_file(target, file=end_file, force_document=False)
                    sent_ids.add(int(end_msg.id))
                    reply_anchor = int(end_msg.id)

                if prompt_shortened:
                    last_info = (
                        f"Prompt exceeded Syntax limit ({_syntax_prompt_max_chars()} chars) "
                        "and was shortened automatically before send."
                    )
                request_message = await client.send_message(target, prompt_text, reply_to=reply_anchor)
                sent_ids.add(int(request_message.id))

                deadline = time.monotonic() + timeout
                if provider_kind == SYNTAX_PROVIDER_VEO:
                    configure_message, configure_button = await _wait_for_latest_syntax_configure_button(
                        client,
                        target,
                        reply_anchor=int(request_message.id),
                        poll=2.0,
                        require_prompt_saved=True,
                        deadline=time.monotonic() + min(timeout, 180),
                    )
                    deadline = time.monotonic() + timeout
                    if configure_message is not None and configure_button is not None:
                        return await _run_syntax_veo_configure_flow(
                            client,
                            target,
                            bot_entity=target,
                            configure_message=configure_message,
                            configure_button=configure_button,
                            prompt=prompt_text,
                            start_image=start_image,
                            duration_sec=duration_sec,
                            aspect_ratio=aspect_ratio,
                            reply_anchor=reply_anchor,
                            sent_ids=sent_ids,
                            attempt_id=attempt_id,
                            poll=poll,
                            deadline=deadline,
                            timeout=timeout,
                            initial_last_info=last_info,
                            expected_lang_token=expected_lang_token,
                        )
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
    syntax_provider: str | None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
    result_sink: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = model
    results: dict[str, Any] = result_sink if result_sink is not None else {}
    if not requests:
        return results
    if _normalize_syntax_provider(syntax_provider, model) == SYNTAX_PROVIDER_KLING:
        for request in requests:
            key = str(request.key)
            results[key] = await _run_fallback_async(
                attempt_id=uuid4().hex,
                prompt=str(request.prompt or ""),
                model=model,
                duration_sec=duration_sec,
                start_image=request.start_image,
                end_image=request.end_image,
                timeout_sec=timeout_sec,
                acquire_timeout_sec=acquire_timeout_sec,
                syntax_provider=syntax_provider,
                aspect_ratio=aspect_ratio,
                audio_enabled=audio_enabled,
            )
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
        prompt_source = str(request.prompt or "").strip()
        if not prompt_source:
            results[key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message="Syntax prompt is empty; refusing to dispatch.",
                attempt_id=attempt_id,
            )
            continue
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
        prepared_prompt = await asyncio.to_thread(_prepare_prompt_for_syntax, prompt_source)
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
        for request in ready:
            if request.key in results:
                continue
            results[request.key] = _result(
                success=False,
                video_bytes=None,
                video_uri=None,
                provider_message=provider_message,
                attempt_id=request.attempt_id,
            )
        return results

    if session_candidate.kind == "string":
        if StringSession is None:
            _release_syntax_session_candidate(session_candidate)
            for request in ready:
                results[request.key] = _result(
                    success=False,
                    video_bytes=None,
                    video_uri=None,
                    provider_message="Telethon StringSession is unavailable in this environment.",
                    attempt_id=request.attempt_id,
                )
            return results
        session_obj: Any = StringSession(session_candidate.value)
    else:
        session_obj = session_candidate.value

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
        submission_status_ids: dict[str, set[int]] = {}
        for request in ready:
            try:
                await _send_syntax_video_preflight(client, target, provider=syntax_provider, model=model)
            except SyntaxVeoNotSubmittedError as exc:
                results[request.key] = _result(success=False, video_bytes=None, video_uri=None,
                    provider_message=str(exc), attempt_id=request.attempt_id)
                continue
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
            last_info = ""
            if request.prompt_shortened:
                last_info = (
                    f"Prompt exceeded Syntax limit ({_syntax_prompt_max_chars()} chars) "
                    "and was shortened automatically before send."
                )
            request_message = await client.send_message(target, request.prompt_text, reply_to=reply_anchor)
            sent_ids.add(int(request_message.id))
            configure_message, configure_button = await _wait_for_latest_syntax_configure_button(
                client,
                target,
                reply_anchor=int(request_message.id),
                poll=2.0,
                require_prompt_saved=True,
                deadline=time.monotonic() + min(timeout, 180),
            )
            if configure_message is None or configure_button is None:
                results[request.key] = _result(
                    success=False,
                    video_bytes=None,
                    video_uri=None,
                    provider_message="Syntax Veo not submitted: prompt acknowledgement and settings did not arrive before the configure deadline.",
                    attempt_id=request.attempt_id,
                )
                continue
            configure_mid = int(getattr(configure_message, "id", 0) or 0)
            if configure_mid:
                sent_ids.add(configure_mid)
            configure_error = await _submit_syntax_veo_webview(
                client,
                target,
                bot_entity=target,
                configure_message=configure_message,
                configure_button=configure_button,
                prompt=request.prompt_text,
                start_image=request.start_image,
                duration_sec=duration_sec,
                aspect_ratio=aspect_ratio,
                timeout_sec=timeout,
            )
            if configure_error:
                if configure_error.startswith("Syntax Veo not submitted:"):
                    results[request.key] = _result(
                        success=False, video_bytes=None, video_uri=None,
                        provider_message=configure_error, attempt_id=request.attempt_id,
                    )
                    continue
                # A WebView error does not prove that generation failed: the
                # click may have reached Syntx, or the user may finish the form.
                # Keep the request correlated and collect its Telegram result
                # without submitting (and potentially charging) a second job.
                last_info = configure_error
                logger.warning(
                    "syntax_batch.veo_ui_unconfirmed key=%s attempt_id=%s; waiting_for_telegram_result=true error=%s",
                    request.key, request.attempt_id, configure_error,
                )
            submission_status_ids[request.key] = await _capture_syntax_submission_status(
                client, target, after_id=max(sent_ids),
            )
            logger.info("syntax_batch.status_bound key=%s message_ids=%s",
                        request.key, sorted(submission_status_ids[request.key]))
            logger.info("syntax_batch.veo_waiting key=%s attempt_id=%s ui_confirmed=%s",
                        request.key, request.attempt_id, not bool(configure_error))
            request_runtime.append((request, reply_anchor, sent_ids, last_info))

        if not request_runtime:
            return results
        deadline = time.monotonic() + timeout
        pending_states = {
            request.key: {
                "request": request,
                "reply_anchor": reply_anchor,
                "sent_ids": sent_ids,
                "processed": set(sent_ids),
                "processed_revisions": {},
                "pending_refetch": set(submission_status_ids.get(request.key, set())),
                "submission_status_ids": submission_status_ids.get(request.key, set()),
                "last_holder": [last_info],
            }
            for request, reply_anchor, sent_ids, last_info in request_runtime
        }

        consumed_video_ids: set[int] = set()

        async def _consume_for_batch_message(message: Any) -> None:
            if message is None or getattr(message, "out", False):
                return
            if int(getattr(message, "id", 0) or 0) in consumed_video_ids:
                return
            candidate_keys = _batch_message_candidate_keys(message, pending_states)
            if not candidate_keys and _message_looks_like_video(message):
                if all(int(getattr(message, "id", 0) or 0) <= state["reply_anchor"]
                       for state in pending_states.values()):
                    return
                logger.warning(
                    "syntax_batch.unmatched_video message_id=%s pending_keys=%s",
                    int(getattr(message, "id", 0) or 0),
                    ",".join(pending_states),
                )
                return
            for key in candidate_keys:
                state = pending_states.get(key)
                if state is None:
                    continue
                mid = int(getattr(message, "id", 0) or 0)
                revision = _syntax_message_revision(message)
                if mid <= state["reply_anchor"] or mid in state["sent_ids"]:
                    continue
                if mid in state["processed"] and state["processed_revisions"].get(mid) == revision:
                    continue
                correlation_ids = (set(state["sent_ids"]) | set(state["pending_refetch"])
                                   | set(state.get("submission_status_ids", set())))
                if mid in state["pending_refetch"] or mid in state["processed_revisions"]:
                    correlation_ids.add(mid)
                out = await _process_bot_message(
                    client,
                    message=message,
                    sent_ids=correlation_ids,
                    expected_lang_token=state["request"].expected_lang_token,
                    require_lang_token_for_loose=True,
                )
                if out is None:
                    continue
                state["processed_revisions"][mid] = revision
                vbytes, vuri, pmsg, retry_later, hard_reject = out
                if hard_reject:
                    logger.warning("syntax_batch.hard_reject key=%s message_id=%s edited=%s",
                                   key, mid, bool(getattr(message, "edit_date", None)))
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
                    consumed_video_ids.add(mid)
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
                    logger.info("syntax_batch.video_received key=%s message_id=%s attempt_id=%s",
                                key, mid, state["request"].attempt_id)
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
            messages_by_id: dict[int, Any] = {}
            latest_raw = await client.get_messages(target, limit=1)
            latest = _first_message_from_get_messages(latest_raw)
            if latest is not None:
                messages_by_id[int(getattr(latest, "id", 0) or 0)] = latest
            async for msg in client.iter_messages(target, limit=40):
                if msg is not None:
                    messages_by_id[int(getattr(msg, "id", 0) or 0)] = msg
            # Telethon yields newest first; batch status messages must be assigned in chat order.
            for _mid, msg in sorted(messages_by_id.items()):
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
            if request.key in results:
                continue
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
            if request.key in results:
                continue
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
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
    kling_keyframes: bool = False,
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
            syntax_provider=syntax_provider,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
            **({"kling_keyframes": True} if kling_keyframes else {}),
        )
    )


def run_syntax_telethon_fallback_batch(
    *,
    requests: list[Any],
    model: str,
    duration_sec: int,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
    result_sink: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one Telethon session that sends multiple Syntax requests before waiting for replies."""
    return asyncio.run(
        _run_batch_fallback_async(
            requests=requests,
            result_sink=result_sink,
            model=model,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            acquire_timeout_sec=acquire_timeout_sec,
            syntax_provider=syntax_provider,
            aspect_ratio=aspect_ratio,
            audio_enabled=audio_enabled,
        )
    )
