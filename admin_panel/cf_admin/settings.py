"""Django settings for the local Content Fabric admin panel."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file

load_env_file()

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "content-fabric-local-admin")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "pipeline_admin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "cf_admin.urls"
WSGI_APPLICATION = "cf_admin.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]


def _database_from_url() -> dict[str, str]:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(PROJECT_ROOT / "content_fabric_admin.sqlite3"),
        }
    parsed = urlparse(raw.replace("postgresql+psycopg2://", "postgresql://", 1))
    if parsed.scheme.startswith("sqlite"):
        if parsed.path in {"/:memory:", ":memory:"}:
            name = ":memory:"
        elif parsed.netloc:
            name = f"//{parsed.netloc}{parsed.path}"
        else:
            name = parsed.path or ":memory:"
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": name}
    if parsed.scheme.startswith("postgres"):
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username or "",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
        }
    raise RuntimeError(f"Unsupported DATABASE_URL for Django admin: {parsed.scheme}")


DATABASES = {"default": _database_from_url()}
