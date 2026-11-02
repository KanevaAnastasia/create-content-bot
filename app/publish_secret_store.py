"""Atomic local storage for project-scoped publishing credentials."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Mapping

from .publication_config import project_env_name


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _quoted_env_value(value: str) -> str:
    if not value:
        raise ValueError("Secret value cannot be empty.")
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("Secret value must be a single line.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def set_env_values(path: Path, values: Mapping[str, str]) -> None:
    """Atomically update several .env values without exposing them in output."""
    normalized = {str(name).strip(): str(value).strip() for name, value in values.items()}
    if not normalized:
        return
    for name, value in normalized.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"Invalid environment variable name: {name!r}")
        _quoted_env_value(value)

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(normalized)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in pending:
            out.append(f"{key}={_quoted_env_value(pending.pop(key))}")
        else:
            out.append(line)
    if pending and out and out[-1].strip():
        out.append("")
    out.extend(f"{name}={_quoted_env_value(value)}" for name, value in pending.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".env.publish.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.chmod(path, 0o600)
    os.environ.update(normalized)


def set_project_secrets(project_slug: str, values: Mapping[str, str]) -> None:
    set_env_values(
        ENV_PATH,
        {project_env_name(project_slug, key): value for key, value in values.items()},
    )
