#!/usr/bin/env python3
"""Exchange one project's Meta token for a long-lived token without printing secrets."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import requests

from app.publication_config import project_env
from scripts.set_publish_secret import ENV_PATH, env_name, set_env_value


GRAPH_TOKEN_URL = "https://graph.facebook.com/v25.0/oauth/access_token"


def exchange_meta_token(
    project_slug: str,
    *,
    http_get=requests.get,
) -> int | None:
    app_id = project_env(project_slug, "INSTAGRAM_APP_ID")
    app_secret = project_env(project_slug, "INSTAGRAM_APP_SECRET")
    access_token = project_env(project_slug, "INSTAGRAM_ACCESS_TOKEN")
    missing = [
        key
        for key, value in (
            ("INSTAGRAM_APP_ID", app_id),
            ("INSTAGRAM_APP_SECRET", app_secret),
            ("INSTAGRAM_ACCESS_TOKEN", access_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing project-scoped Meta settings: {', '.join(missing)}")

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

    set_env_value(
        ENV_PATH,
        env_name(project_slug, "INSTAGRAM_ACCESS_TOKEN"),
        str(payload["access_token"]),
    )
    expires_in = payload.get("expires_in")
    return int(expires_in) if expires_in is not None else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", choices=("grandma_kitchen", "discipline"))
    args = parser.parse_args()
    expires_in = exchange_meta_token(args.project)
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        print(f"Saved long-lived Meta token (expires near {expires_at.date().isoformat()}; value hidden).")
    else:
        print("Saved exchanged Meta token (expiry not returned; value hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
