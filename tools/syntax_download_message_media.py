"""Download media from a Syntx message id."""

from __future__ import annotations

import argparse
import asyncio
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


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    load_env_file()
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id.isdigit() or not api_hash:
        raise RuntimeError("Missing TELEGRAM_API_ID/TELEGRAM_API_HASH")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(_session_obj(), int(api_id), api_hash)
    await client.connect()
    try:
        target = await client.get_entity(_normalize_username(os.getenv("SYNTAX_BOT_USERNAME", "syntxaibot")))
        msg = await client.get_messages(target, ids=int(args.message_id))
        if msg is None:
            raise RuntimeError(f"Message not found: {args.message_id}")
        data = await client.download_media(msg, file=bytes)
        if not isinstance(data, bytes) or not data:
            raise RuntimeError(f"Message has no downloadable media: {args.message_id}")
        out.write_bytes(data)
        print(f"{out.resolve()} {len(data)}")
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
