#!/usr/bin/env python3
"""Verify and optionally save a project-specific TikTok connection (no posting)."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Project
from app.publish_secret_store import set_project_secrets
from app.zernio_publisher import ZernioClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--account-id")
    parser.add_argument("--username", help="Expected TikTok @handle, not the display name")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        if session.scalar(select(Project).where(Project.slug == args.project)) is None:
            parser.error("Unknown project")
    if args.save and not (args.account_id and args.username):
        parser.error("Saving requires both --account-id and --username")
    key = getpass.getpass("Zernio API key (hidden): ").strip()
    client = ZernioClient(args.project, api_key=key)
    if args.account_id and args.username:
        account = client.verify_account(args.account_id, args.username)
        print(f"Verified TikTok @{account['username']} for project {args.project}")
    else:
        for account in client.accounts():
            if account.get("platform") == "tiktok":
                print(f"{account.get('_id')}: @{account.get('username')} ({account.get('displayName', '')})")
    if args.save:
        set_project_secrets(args.project, {
            "ZERNIO_API_KEY": key,
            "ZERNIO_TIKTOK_ACCOUNT_ID": args.account_id,
            "ZERNIO_TIKTOK_USERNAME": args.username.lstrip("@"),
            "TIKTOK_PROVIDER": "zernio",
        })
        print("Saved project-scoped credentials. No video was posted. Reload the publication worker.")


if __name__ == "__main__":
    main()
