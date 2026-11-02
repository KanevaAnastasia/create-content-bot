"""Syntax Seedance webview transport for copy-mode video generation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from .syntax_getvideo_download import fetch_mp4_bytes_from_getvideo_page
from .syntax_telethon_fallback import (
    StringSession,
    TelegramClient,
    _acquire_syntax_session_candidate,
    _collect_urls_from_message,
    _normalize_username,
    _release_syntax_session_candidate,
    _syntax_configure_button_webview_url,
    _syntax_session_candidates,
)
from .syntax_video_fallback import FallbackResult

logger = logging.getLogger(__name__)

SYNTAX_PROVIDER_SEEDANCE = "seedance"
SEEDANCE_COMMAND = "🧿 Seedance"
SEEDANCE_MIN_DURATION_SEC = 4
SEEDANCE_MAX_DURATION_SEC = 15
SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS = 3
SEEDANCE_REFERENCE_UPLOAD_TIMEOUT_SEC = 30


def run_syntax_seedance_copy_mode(
    *,
    prompt: str,
    reference_paths: list[str | Path],
    duration_sec: int,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    required_reference_indices: list[int] | None = None,
) -> FallbackResult:
    """Generate one Seedance clip via Syntax webview using image/video references."""

    attempt_id = uuid4().hex
    return asyncio.run(
        _run_syntax_seedance_copy_mode_async(
            attempt_id=attempt_id,
            prompt=prompt,
            reference_paths=[str(Path(path)) for path in reference_paths],
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            acquire_timeout_sec=acquire_timeout_sec,
            required_reference_indices=required_reference_indices,
        )
    )


async def _run_syntax_seedance_copy_mode_async(
    *,
    attempt_id: str,
    prompt: str,
    reference_paths: list[str],
    duration_sec: int,
    timeout_sec: int | None,
    acquire_timeout_sec: int | None,
    required_reference_indices: list[int] | None,
) -> FallbackResult:
    if TelegramClient is None:
        return _result(False, None, "Telethon is not installed; Syntax Seedance is unavailable.", attempt_id)
    api_id_raw = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id_raw.isdigit() or not api_hash:
        return _result(False, None, "Missing TELEGRAM_API_ID/TELEGRAM_API_HASH for Syntax Seedance.", attempt_id)
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return _result(False, None, "Seedance prompt is empty; refusing to dispatch.", attempt_id)
    existing_paths = [str(Path(path)) for path in reference_paths if Path(path).exists()]
    if not existing_paths:
        return _result(False, None, "Seedance references are missing; refusing to dispatch.", attempt_id)

    timeout = int(timeout_sec or int(os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "900"))
    poll = max(10.0, float(os.getenv("SYNTAX_FALLBACK_POLL_SEC") or "10"))
    duration = max(SEEDANCE_MIN_DURATION_SEC, min(SEEDANCE_MAX_DURATION_SEC, int(duration_sec or SEEDANCE_MIN_DURATION_SEC)))
    bot_username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))

    session_candidate = _acquire_syntax_session_candidate(acquire_timeout_sec)
    if session_candidate is None:
        provider_message = (
            "Missing Telethon session: set TELETHON_SESSION_PATH / TELETHON_STRING_SESSION."
            if not _syntax_session_candidates()
            else "No free Telethon session became available before timeout."
        )
        return _result(False, None, provider_message, attempt_id)
    session_obj: Any = StringSession(session_candidate.value) if session_candidate.kind == "string" else session_candidate.value

    client = TelegramClient(session_obj, int(api_id_raw), api_hash)
    start_id = 0
    try:
        await client.connect()
        target = await client.get_entity(bot_username)
        bot_input = await client.get_input_entity(target)
        latest = await client.get_messages(target, limit=1)
        latest_msg = _first_message(latest)
        start_id = int(getattr(latest_msg, "id", 0) or 0) if latest_msg is not None else 0
        await client.send_message(target, "/video")
        await asyncio.sleep(1.0)
        await client.send_message(target, SEEDANCE_COMMAND)
        await asyncio.sleep(2.0)
        url = await _find_seedance_webview_url(client, target, bot_input, start_id=start_id)
        if not url:
            return _result(False, None, "Seedance configure webview URL was not found.", attempt_id)
        submit_timeout = max(60, int(os.getenv("SYNTAX_SEEDANCE_WEBVIEW_SUBMIT_TIMEOUT_SEC") or "300"))
        try:
            submit_state = await asyncio.wait_for(
                asyncio.to_thread(
                    _fill_seedance_webview_and_submit,
                    url,
                    prompt_text,
                    existing_paths,
                    duration,
                    required_reference_indices,
                ),
                timeout=submit_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("syntax_seedance.webview_submit_timeout timeout=%s", submit_timeout)
            return _result(False, None, f"Seedance webview submit timed out after {submit_timeout}s.", attempt_id)
        except Exception as exc:
            logger.warning("syntax_seedance.webview_failed err=%s", _compact_playwright_error(exc))
            return _result(False, None, f"Seedance webview failed: {_compact_playwright_error(exc)}", attempt_id)
        poll_result = await _poll_seedance_result(
            client,
            target,
            start_id=start_id,
            poll=poll,
            timeout=timeout,
        )
        if poll_result.video_bytes:
            info = " ".join(x for x in (submit_state, poll_result.provider_message) if x).strip()
            return FallbackResult(True, poll_result.video_bytes, poll_result.video_uri, info or "Seedance ok.", attempt_id)
        info = " ".join(x for x in (submit_state, poll_result.provider_message) if x).strip()
        return FallbackResult(False, None, poll_result.video_uri, info or "Seedance did not return a video.", attempt_id)
    except Exception as exc:
        logger.exception("syntax_seedance.transport_failed start_id=%s", start_id)
        return _result(False, None, f"Seedance transport error: {exc}", attempt_id)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        _release_syntax_session_candidate(session_candidate)


async def _find_seedance_webview_url(client: Any, target: Any, bot_input: Any, *, start_id: int) -> str | None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        async for msg in client.iter_messages(target, limit=25):
            if msg is None or getattr(msg, "out", False):
                continue
            mid = int(getattr(msg, "id", 0) or 0)
            if mid <= start_id:
                continue
            text = str(getattr(msg, "raw_text", "") or getattr(msg, "message", "") or "")
            if "Seedance" not in text and "динамичные видео" not in text:
                continue
            for row in getattr(msg, "buttons", None) or []:
                for button in row:
                    label = str(getattr(button, "text", "") or getattr(getattr(button, "button", None), "text", "") or "")
                    if "Настро" in label:
                        logger.info("syntax_seedance.configure_webview_found msg_id=%s", mid)
                        return await _syntax_configure_button_webview_url(client, target, bot_input, msg, button)
        await asyncio.sleep(1.5)
    return None


def _fill_seedance_webview_and_submit(
    url: str,
    prompt: str,
    reference_paths: list[str],
    duration_sec: int,
    required_reference_indices: list[int] | None = None,
) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception:
            browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True)
        try:
            logger.info("syntax_seedance.webview_opening refs=%s duration=%s", len(reference_paths), duration_sec)
            _goto_seedance_webview(page, url)
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=30_000)
            _wait_for_seedance_form(page)
            logger.info("syntax_seedance.form_ready")
            required = (
                set(range(len(reference_paths)))
                if required_reference_indices is None
                else {int(index) for index in required_reference_indices}
            )
            uploaded_count = 0
            skipped: list[int] = []
            for zero_index, path in enumerate(reference_paths):
                index = zero_index + 1
                logger.info("syntax_seedance.uploading_reference index=%s path_suffix=%s", index, Path(path).suffix.lower())
                uploaded = _upload_seedance_reference(
                    page,
                    path,
                    minimum=uploaded_count + 1,
                    index=index,
                )
                if uploaded:
                    uploaded_count += 1
                    logger.info("syntax_seedance.reference_uploaded index=%s uploaded_count=%s", index, uploaded_count)
                    continue
                if zero_index in required:
                    raise RuntimeError(
                        f"Seedance required reference {index} failed to upload after "
                        f"{SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS} attempts: {Path(path).name}"
                    )
                skipped.append(index)
                logger.warning(
                    "syntax_seedance.optional_reference_skipped index=%s path_suffix=%s uploaded_count=%s",
                    index,
                    Path(path).suffix.lower(),
                    uploaded_count,
                )
            if uploaded_count <= 0:
                raise RuntimeError("Seedance did not upload any references.")
            _set_textarea_value(page, prompt)
            _set_seedance_duration(page, duration_sec)
            _wait_for_prompt_bound(page, min_chars=min(len(prompt), 20))
            logger.info("syntax_seedance.prompt_bound chars=%s", len(prompt))
            _dismiss_feedback_modal(page)
            clicked = _click_seedance_generate_button(page)
            logger.info("syntax_seedance.generate_click_result clicked=%s", clicked)
            page.wait_for_timeout(2500)
            if clicked and _dismiss_feedback_modal(page):
                with contextlib.suppress(Exception):
                    _click_seedance_generate_button(page)
            page.wait_for_timeout(8000)
            skipped_text = f" skipped={','.join(map(str, skipped))}" if skipped else ""
            return f"Seedance webview submitted files={uploaded_count} duration={duration_sec}.{skipped_text}"
        finally:
            browser.close()


def _goto_seedance_webview(page: Any, url: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 5):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function(
                "() => Boolean(window.Telegram && window.Telegram.WebApp)",
                timeout=15_000,
            )
            logger.info("syntax_seedance.webview_opened attempt=%s", attempt)
            return
        except Exception as exc:
            last_error = exc
            logger.info("syntax_seedance.webview_goto_retry attempt=%s err=%s", attempt, _compact_playwright_error(exc))
            with contextlib.suppress(Exception):
                page.goto("about:blank", wait_until="commit", timeout=10_000)
            page.wait_for_timeout(2500 * attempt)
    raise RuntimeError(f"Seedance webview did not open after retries: {_compact_playwright_error(last_error)}")


def _compact_playwright_error(exc: Exception | None) -> str:
    text = str(exc or "").strip()
    if "net::" in text:
        match = re.search(r"net::[A-Z0-9_:-]+", text)
        return match.group(0) if match else text[:300]
    if "Page.goto" in text:
        return text.split("\n", 1)[0][:300]
    return text[:300] or "unknown error"


def _wait_for_seedance_form(page: Any) -> None:
    deadline = time.monotonic() + 90
    last = ""
    while time.monotonic() < deadline:
        last = page.locator("body").inner_text(timeout=5000)
        if (
            ("Промпт" in last or "Prompt" in last)
            and ("Сгенерировать" in last or "Generate" in last)
            and ("Длительность" in last or "Duration" in last)
        ):
            return
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Seedance form did not become ready: {last[:1000]}")


def _upload_seedance_reference(page: Any, path: str, *, minimum: int, index: int) -> bool:
    last_error: Exception | None = None
    for attempt in range(1, SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS + 1):
        try:
            _set_seedance_local_file(page, path)
            _wait_for_uploaded_count(
                page,
                minimum,
                timeout_sec=SEEDANCE_REFERENCE_UPLOAD_TIMEOUT_SEC,
            )
            return True
        except Exception as exc:
            last_error = exc
            logger.warning(
                "syntax_seedance.reference_upload_retry index=%s attempt=%s/%s err=%s",
                index,
                attempt,
                SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS,
                _compact_playwright_error(exc),
            )
            if attempt < SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS:
                page.wait_for_timeout(1500 * attempt)
    logger.warning(
        "syntax_seedance.reference_upload_exhausted index=%s err=%s",
        index,
        _compact_playwright_error(last_error),
    )
    return False


def _set_seedance_local_file(page: Any, path: str) -> None:
    page.evaluate(
        """() => {
            const input = document.querySelector('input[x-ref="fileInput"]');
            if (!input || !input.parentElement) {
                throw new Error('Seedance primary upload dropzone was not found');
            }
            input.parentElement.click();
        }"""
    )
    page.wait_for_timeout(500)
    page.set_input_files('input[x-ref="mediaFileInput"]', path)


def _wait_for_uploaded_count(page: Any, minimum: int, *, timeout_sec: float = 180) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last = ""
    while time.monotonic() < deadline:
        last = page.locator("body").inner_text(timeout=5000)
        uploaded_count = _uploaded_count_from_body(last)
        if uploaded_count is not None and uploaded_count >= minimum:
            return
        page.wait_for_timeout(1500)
    raise RuntimeError(f"Seedance upload count did not reach {minimum}: {last[:1200]}")


def _uploaded_count_from_body(body: str) -> int | None:
    """Read Syntax's current upload counter without depending on its changing limit."""
    match = re.search(r"Загруженн[оа]\s*\((\d+)\s*/\s*\d+\)", str(body or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _set_textarea_value(page: Any, prompt: str) -> None:
    textarea = page.locator("textarea").first
    textarea.evaluate(
        """(el, value) => {
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
            setter.call(el, value);
            el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
            el.dispatchEvent(new Event("change", {bubbles: true}));
            el.blur();
        }""",
        prompt,
    )


def _set_seedance_duration(page: Any, duration_sec: int) -> None:
    page.evaluate(
        """(value) => {
            for (const el of document.querySelectorAll("input[type=number], input[type=range]")) {
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
                setter.call(el, String(value));
                el.dispatchEvent(new Event("input", {bubbles: true}));
                el.dispatchEvent(new Event("change", {bubbles: true}));
            }
        }""",
        int(duration_sec),
    )


def _wait_for_prompt_bound(page: Any, *, min_chars: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        lengths = page.evaluate("() => Array.from(document.querySelectorAll('textarea')).map((e) => e.value.length)")
        if any(int(length or 0) >= min_chars for length in lengths):
            return
        page.wait_for_timeout(500)
    raise RuntimeError("Seedance prompt did not bind to textarea.")


def _click_seedance_generate_button(page: Any) -> bool:
    deadline = time.monotonic() + 45
    last_state = ""
    while time.monotonic() < deadline:
        state = page.evaluate(
            """() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const submit = buttons.reverse().find((button) => {
                    const text = (button.innerText || button.textContent || '').trim();
                    return (button.name === 'submit' || button.id === 'btn_submit' || /Сгенерировать|Generate/i.test(text))
                        && button.offsetParent !== null;
                });
                if (!submit) return 'missing';
                const cls = String(submit.className || '').toLowerCase();
                if (submit.disabled || submit.getAttribute('aria-busy') === 'true' || /loading|disabled/.test(cls)) {
                    return 'busy';
                }
                submit.scrollIntoView({block: 'center', inline: 'center'});
                submit.click();
                return 'clicked';
            }"""
        )
        last_state = str(state or "")
        if last_state == "clicked":
            return True
        if last_state == "busy":
            page.wait_for_timeout(1500)
            continue
        page.wait_for_timeout(1000)
    logger.warning("syntax_seedance.generate_button_unavailable last_state=%s", last_state)
    return False


def _dismiss_feedback_modal(page: Any) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000)
        if "Ваше мнение" not in body:
            return False
        close_buttons = page.locator("button").filter(has_text=re.compile(r"^\s*[×xX]\s*$"))
        if close_buttons.count():
            close_buttons.last.click(force=True, timeout=5000)
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


async def _poll_seedance_result(
    client: Any,
    target: Any,
    *,
    start_id: int,
    poll: float,
    timeout: int,
) -> FallbackResult:
    deadline = time.monotonic() + timeout
    last_info = ""
    while time.monotonic() < deadline:
        async for msg in client.iter_messages(target, limit=40):
            if msg is None or getattr(msg, "out", False):
                continue
            mid = int(getattr(msg, "id", 0) or 0)
            if mid <= start_id:
                continue
            text = str(getattr(msg, "raw_text", "") or getattr(msg, "message", "") or "")
            if _message_looks_like_video(msg):
                data = await client.download_media(msg, file=bytes)
                if isinstance(data, bytes) and data:
                    return FallbackResult(True, data, None, "Seedance video downloaded from Telegram.", "")
            urls = _collect_urls_from_message(msg)
            data, uri, info = await _download_seedance_urls(urls)
            if data:
                return FallbackResult(True, data, uri, info, "")
            if info or text:
                last_info = (info or text[:2000]).strip()
            low = text.lower()
            if any(marker in low for marker in ("ошиб", "недостаточно", "error", "failed", "не удалось")):
                return FallbackResult(False, None, None, text[:3000], "")
        await asyncio.sleep(poll)
    return FallbackResult(False, None, None, f"Seedance timed out after {timeout}s. Last: {last_info}", "")


async def _download_seedance_urls(urls: list[str]) -> tuple[bytes | None, str | None, str]:
    for url in urls:
        low = url.lower()
        try:
            if "getvideo.syntxai.net/" in low:
                data = await asyncio.to_thread(fetch_mp4_bytes_from_getvideo_page, url)
                return data, url, "Downloaded mp4 from Syntax getvideo page."
            if ".mp4" in low:
                data = await asyncio.to_thread(_download_direct_mp4, url)
                return data, url, f"Downloaded direct Seedance mp4: {url}"
        except Exception as exc:
            logger.info("syntax_seedance.url_download_failed url=%s err=%s", url, exc)
            continue
    return None, None, ""


def _download_direct_mp4(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 content-fabric-bot"}, timeout=180)
    resp.raise_for_status()
    data = resp.content
    if not data or len(data) < 1024:
        raise RuntimeError("Downloaded Seedance video payload is empty or too small.")
    return data


def _message_looks_like_video(message: Any) -> bool:
    if getattr(message, "video", None) is not None:
        return True
    document = getattr(message, "document", None)
    if document is None:
        return False
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    for attr in getattr(document, "attributes", None) or []:
        if str(getattr(attr, "file_name", "") or "").lower().endswith(".mp4"):
            return True
    return False


def _first_message(got: Any) -> Any | None:
    if got is None:
        return None
    if isinstance(got, (list, tuple)):
        return got[0] if got else None
    return got if getattr(got, "id", None) is not None else None


def _result(success: bool, video_bytes: bytes | None, provider_message: str, attempt_id: str) -> FallbackResult:
    return FallbackResult(success, video_bytes, None, provider_message, attempt_id)
