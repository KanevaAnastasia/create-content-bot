"""One-off Syntx WebApp probe for the "клинг" comparison run."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file
from app.syntax_telethon_fallback import _normalize_username

try:
    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Telethon is required: {exc}") from exc


def _session_obj() -> Any:
    string_session = (os.getenv("TELETHON_STRING_SESSION") or "").strip()
    if string_session:
        return StringSession(string_session)
    path = (os.getenv("TELETHON_SESSION_PATH") or "").strip()
    if path:
        return path
    paths = [x.strip() for x in (os.getenv("TELETHON_SESSION_PATHS") or "").split(",") if x.strip()]
    if paths:
        return paths[0]
    strings = [x.strip() for x in (os.getenv("TELETHON_STRING_SESSIONS") or "").split(",") if x.strip()]
    if strings:
        return StringSession(strings[0])
    raise RuntimeError("Missing TELETHON_SESSION_PATH or TELETHON_STRING_SESSION")


def _button_rows(message: Any) -> list[list[Any]]:
    buttons = getattr(message, "buttons", None) or []
    return [list(row or []) for row in buttons]


def _button_payload(button: Any) -> dict[str, Any]:
    raw = getattr(button, "button", button)
    payload: dict[str, Any] = {
        "text": str(getattr(button, "text", None) or getattr(raw, "text", "") or ""),
        "type": type(raw).__name__,
    }
    url = getattr(raw, "url", None)
    if url:
        payload["url_host"] = str(url).split("/", 3)[2] if "://" in str(url) else ""
        payload["has_url"] = True
    data = getattr(raw, "data", None)
    if data:
        payload["has_data"] = True
    return payload


def _find_configure_button(message: Any) -> Any | None:
    for row in _button_rows(message):
        for button in row:
            text = str(getattr(button, "text", "") or getattr(getattr(button, "button", None), "text", "") or "")
            if "настро" in text.lower() or "configure" in text.lower():
                return button
    return None


async def _request_webview_url(client: Any, target: Any, bot_input: Any, message: Any, button: Any) -> str | None:
    raw = getattr(button, "button", button)
    direct_url = str(getattr(raw, "url", "") or "").strip()
    if isinstance(raw, (types.KeyboardButtonWebView, types.KeyboardButtonSimpleWebView)) and direct_url:
        result = await client(
            functions.messages.RequestWebViewRequest(
                peer=target,
                bot=bot_input,
                platform="macos",
                url=direct_url,
                reply_to=types.InputReplyToMessage(reply_to_msg_id=int(message.id)),
            )
        )
        return str(getattr(result, "url", "") or "").strip() or direct_url
    if direct_url:
        return direct_url
    if isinstance(raw, types.KeyboardButtonCallback):
        answer = await button.click()
        return str(getattr(answer, "url", "") or "").strip() or None
    return None


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--request-text", default="")
    parser.add_argument("--video-prompt", default="")
    parser.add_argument("--video-prompt-file", default="")
    parser.add_argument("--url-out", default="/private/tmp/syntx_kling_webview_url.txt")
    parser.add_argument("--inspect-out", default="/private/tmp/syntx_kling_last_message.json")
    parser.add_argument("--wait-sec", type=float, default=90.0)
    args = parser.parse_args()

    load_env_file()
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id.isdigit() or not api_hash:
        raise RuntimeError("Missing TELEGRAM_API_ID/TELEGRAM_API_HASH")

    image_path = Path(args.image)
    if not image_path.is_file():
        raise RuntimeError(f"Missing image: {image_path}")
    video_prompt = str(args.video_prompt or "").strip()
    if args.video_prompt_file:
        text = Path(args.video_prompt_file).read_text(encoding="utf-8")
        marker = "## Video prompt"
        next_marker = "## Syntx settings"
        if marker in text:
            text = text.split(marker, 1)[1]
        if next_marker in text:
            text = text.split(next_marker, 1)[0]
        video_prompt = text.strip()
    if not video_prompt:
        raise RuntimeError("Provide --video-prompt or --video-prompt-file")

    client = TelegramClient(_session_obj(), int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized")
        username = _normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot"))
        target = await client.get_entity(username)
        bot_input = await client.get_input_entity(target)

        start_id = 0
        latest = await client.get_messages(target, limit=1)
        if latest:
            start_id = int(latest[0].id)

        image_data = image_path.read_bytes()
        image_file = io.BytesIO(image_data)
        image_file.name = image_path.name
        sent_image = await client.send_file(target, file=image_file, force_document=False)
        request_text = str(args.request_text or "").strip()
        sent_request = None
        if request_text:
            sent_request = await client.send_message(target, request_text, reply_to=int(sent_image.id))

        deadline = time.monotonic() + args.wait_sec
        found = None
        while time.monotonic() < deadline:
            async for msg in client.iter_messages(target, limit=20):
                mid = int(getattr(msg, "id", 0) or 0)
                if mid <= start_id or getattr(msg, "out", False):
                    continue
                if _find_configure_button(msg):
                    found = msg
                    break
            if found is not None:
                break
            await asyncio.sleep(3)

        recent_messages: list[dict[str, Any]] = []
        async for msg in client.iter_messages(target, limit=12):
            recent_messages.append(
                {
                    "id": int(getattr(msg, "id", 0) or 0),
                    "out": bool(getattr(msg, "out", False)),
                    "text": str(getattr(msg, "raw_text", "") or getattr(msg, "message", "") or "")[:1000],
                    "buttons": [[_button_payload(btn) for btn in row] for row in _button_rows(msg)],
                }
            )

        if found is None:
            Path(args.inspect_out).write_text(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "wait_configure_button",
                        "sent_image_id": int(sent_image.id),
                        "sent_request_id": int(sent_request.id) if sent_request is not None else None,
                        "recent_messages": recent_messages,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "wait_configure_button",
                        "sent_image_id": int(sent_image.id),
                        "sent_request_id": int(sent_request.id) if sent_request is not None else None,
                        "inspect_out": args.inspect_out,
                    },
                    ensure_ascii=False,
                )
            )
            return 1

        button = _find_configure_button(found)
        rows = [[_button_payload(btn) for btn in row] for row in _button_rows(found)]
        inspect_payload = {
            "message_id": int(found.id),
            "text": str(getattr(found, "raw_text", "") or getattr(found, "message", "") or "")[:4000],
            "buttons": rows,
            "recent_messages": recent_messages,
            "target_settings": {
                "quality": "720",
                "version": "3",
                "duration_sec": 3,
                "aspect_ratio": "9:16",
                "video_prompt": video_prompt,
            },
        }
        Path(args.inspect_out).write_text(json.dumps(inspect_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        url = await _request_webview_url(client, target, bot_input, found, button)
        if not url:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "request_webview_url",
                        "message_id": int(found.id),
                        "buttons": rows,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        Path(args.url_out).write_text(url, encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": True,
                    "message_id": int(found.id),
                    "url_out": args.url_out,
                    "inspect_out": args.inspect_out,
                    "buttons": rows,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
