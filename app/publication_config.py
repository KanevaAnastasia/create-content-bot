"""Project-scoped environment configuration for social publishing."""

from __future__ import annotations

import os
import re

from .env_utils import load_env_file


def project_env_prefix(project_slug: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(project_slug or "").upper()).strip("_")
    return f"PUBLISH_{normalized}_" if normalized else "PUBLISH_"


def project_env_name(project_slug: str, key: str) -> str:
    return f"{project_env_prefix(project_slug)}{str(key or '').strip().upper()}"


def project_env(project_slug: str, key: str, default: str = "") -> str:
    """Return only a project-scoped publishing setting.

    Publishing credentials must never fall back to an unscoped value: the same
    process serves several independent social identities.
    """
    load_env_file()
    return (os.getenv(project_env_name(project_slug, key)) or default or "").strip()


def shared_env(key: str, default: str = "") -> str:
    """Return a non-account-specific publishing setting."""
    load_env_file()
    return (os.getenv(str(key or "").strip().upper()) or default or "").strip()
