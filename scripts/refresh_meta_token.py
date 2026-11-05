#!/usr/bin/env python3
"""Exchange one project's Meta token for a long-lived token without printing secrets."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

# Keep the script directly executable from the repository root as documented by
# its shebang, not only through ``python -m``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publication_config import project_env
from app.publish_secret_store import set_project_secrets


GRAPH_TOKEN_URL = "https://graph.facebook.com/v25.0/oauth/access_token"


def exchange_meta_token(
    project_slug: str,
    *,
    language: str | None = None,
    http_get=requests.get,
) -> int | None:
    suffix = f"_{language.strip().upper()}" if language else ""
    app_id = project_env(project_slug, f"INSTAGRAM_APP_ID{suffix}")
    app_secret = project_env(project_slug, f"INSTAGRAM_APP_SECRET{suffix}")
    access_token = project_env(project_slug, f"INSTAGRAM_ACCESS_TOKEN{suffix}")
    missing = [
        key
        for key, value in (
            (f"INSTAGRAM_APP_ID{suffix}", app_id),
            (f"INSTAGRAM_APP_SECRET{suffix}", app_secret),
            (f"INSTAGRAM_ACCESS_TOKEN{suffix}", access_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing project-scoped Meta settings: {', '.join(missing)}")

    try:
        response = http_get(
            GRAPH_TOKEN_URL,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": access_token,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Meta token exchange request failed ({type(exc).__name__}).") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Meta token exchange returned HTTP {response.status_code} without JSON.") from exc
    if response.status_code >= 400 or not payload.get("access_token"):
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        detail = ", ".join(str(part) for part in (error_type, error_code) if part)
        raise RuntimeError(
            f"Meta token exchange failed with HTTP {response.status_code}"
            f"{f' ({detail})' if detail else ''}."
        )

    expires_in = payload.get("expires_in")
    expires_in_seconds = int(expires_in) if expires_in is not None else None
    values = {f"INSTAGRAM_ACCESS_TOKEN{suffix}": str(payload["access_token"])}
    if expires_in_seconds:
        values[f"INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT{suffix}"] = str(
            int(datetime.now(UTC).timestamp()) + expires_in_seconds
        )
    set_project_secrets(project_slug, values)
    return expires_in_seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", choices=("grandma_kitchen", "discipline"))
    parser.add_argument("--language", choices=("ru", "en"))
    args = parser.parse_args()
    expires_in = exchange_meta_token(args.project, language=args.language)
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        print(f"Saved long-lived Meta token (expires near {expires_at.date().isoformat()}; value hidden).")
    else:
        print("Saved exchanged Meta token (expiry not returned; value hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
