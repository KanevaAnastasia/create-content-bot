#!/usr/bin/env python3
"""Safely set one project-scoped publishing secret in the local .env file."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def env_name(project_slug: str, key: str) -> str:
    project = re.sub(r"[^A-Z0-9]+", "_", project_slug.upper()).strip("_")
    setting = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
    if not project or not setting:
        raise ValueError("Project and key must contain letters or digits.")
    return f"PUBLISH_{project}_{setting}"


def set_env_value(path: Path, name: str, value: str) -> None:
    if not value:
        raise ValueError("Secret value cannot be empty.")
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("Secret value must be a single line.")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f'{name}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
    pattern = re.compile(rf"^{re.escape(name)}=")
    updated = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line):
            if not updated:
                out.append(replacement)
                updated = True
            continue
        out.append(line)
    if not updated:
        if out and out[-1].strip():
            out.append("")
        out.append(replacement)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", choices=("grandma_kitchen", "discipline", "shared"))
    parser.add_argument("key")
    parser.add_argument("--value", help="Avoid this for real secrets; omit it to use the hidden prompt.")
    parser.add_argument("--generate", action="store_true", help="Generate a new random value without printing it.")
    args = parser.parse_args()
    if args.value is not None and args.generate:
        parser.error("--value and --generate cannot be used together")
    name = (
        re.sub(r"[^A-Z0-9]+", "_", args.key.upper()).strip("_")
        if args.project == "shared"
        else env_name(args.project, args.key)
    )
    value = secrets.token_urlsafe(48) if args.generate else (
        args.value if args.value is not None else getpass.getpass(f"{name}: ")
    )
    set_env_value(ENV_PATH, name, value.strip())
    print(f"Saved {name} in {ENV_PATH} (value hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
