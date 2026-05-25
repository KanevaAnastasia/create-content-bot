"""Interactive Telethon login helper for Syntax fallback.

Usage:
    .venv/bin/python tools/telethon_login.py --phone +1234567890

The script authorizes a Telegram user account with ``TELEGRAM_API_ID`` and
``TELEGRAM_API_HASH`` from env, then prints/stores a reusable session.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")
    return value


async def _run_login(args: argparse.Namespace) -> int:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception as exc:
        raise RuntimeError(
            "Telethon dependency is missing. Install project dependencies first."
        ) from exc

    api_id_raw = _required_env("TELEGRAM_API_ID")
    api_hash = _required_env("TELEGRAM_API_HASH")
    if not api_id_raw.isdigit():
        raise RuntimeError("TELEGRAM_API_ID must be numeric.")

    session_dir = os.path.dirname(os.path.abspath(args.session_path))
    if session_dir and not os.path.isdir(session_dir):
        os.makedirs(session_dir, exist_ok=True)

    async with TelegramClient(args.session_path, int(api_id_raw), api_hash) as client:
        if not await client.is_user_authorized():
            sent = await client.send_code_request(args.phone)
            code = input("Enter Telegram login code: ").strip()
            try:
                await client.sign_in(
                    phone=args.phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash,
                )
            except Exception as first_exc:
                if "SESSION_PASSWORD_NEEDED" not in str(first_exc):
                    raise
                password = input("Two-step verification password: ")
                await client.sign_in(password=password)
        if not await client.is_user_authorized():
            raise RuntimeError("Authorization failed; session was not created.")
        print(f"Telethon session saved: {os.path.abspath(args.session_path)}")
        if args.print_string_session:
            print("StringSession:")
            print(StringSession.save(client.session))
    return 0


def main() -> int:
    # Reuse project .env loader so TELEGRAM_API_ID / TELEGRAM_API_HASH
    # are available when running the helper directly.
    load_env_file()
    parser = argparse.ArgumentParser(description="Create Telethon session for Syntax fallback.")
    parser.add_argument("--phone", required=True, help="Phone number in international format.")
    parser.add_argument(
        "--session-path",
        default="token/syntax_telethon.session",
        help="Path for Telethon session file (default: token/syntax_telethon.session).",
    )
    parser.add_argument(
        "--print-string-session",
        action="store_true",
        help="Print StringSession after successful login.",
    )
    args = parser.parse_args()
    return asyncio.run(_run_login(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:
        print(str(err), file=sys.stderr)
        raise SystemExit(1)
