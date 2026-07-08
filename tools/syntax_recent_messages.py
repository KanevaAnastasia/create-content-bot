"""Print recent Syntx bot messages without exposing secrets."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file
from app.syntax_telethon_fallback import _normalize_username

from telethon import TelegramClient
from telethon.sessions import StringSession


def _session_obj() -> Any:
    if (value := (os.getenv("TELETHON_STRING_SESSION") or "").strip()):
        return StringSession(value)
    if (value := (os.getenv("TELETHON_SESSION_PATH") or "").strip()):
        return value
    values = [x.strip() for x in (os.getenv("TELETHON_SESSION_PATHS") or "").split(",") if x.strip()]
    if values:
        return values[0]
    values = [x.strip() for x in (os.getenv("TELETHON_STRING_SESSIONS") or "").split(",") if x.strip()]
    if values:
        return StringSession(values[0])
    raise RuntimeError("Missing Telethon session")


def _button_payload(button: Any) -> dict[str, Any]:
    raw = getattr(button, "button", button)
    return {
        "text": str(getattr(button, "text", None) or getattr(raw, "text", "") or ""),
        "type": type(raw).__name__,
        "has_url": bool(getattr(raw, "url", None)),
        "has_data": bool(getattr(raw, "data", None)),
    }


async def main_async() -> int:
    load_env_file()
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id.isdigit() or not api_hash:
        raise RuntimeError("Missing TELEGRAM_API_ID/TELEGRAM_API_HASH")
    client = TelegramClient(_session_obj(), int(api_id), api_hash)
    await client.connect()
    try:
        target = await client.get_entity(_normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot")))
        out = []
        async for msg in client.iter_messages(target, limit=20):
            out.append(
                {
                    "id": int(getattr(msg, "id", 0) or 0),
                    "out": bool(getattr(msg, "out", False)),
                    "text": str(getattr(msg, "raw_text", "") or getattr(msg, "message", "") or "")[:2000],
                    "buttons": [
                        [_button_payload(button) for button in row]
                        for row in (getattr(msg, "buttons", None) or [])
                    ],
                    "has_video": bool(getattr(msg, "video", None)),
                    "has_document": bool(getattr(msg, "document", None)),
                }
            )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
