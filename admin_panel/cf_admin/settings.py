"""Django settings for the Content Fabric admin panel."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file
from app.db import database_url_is_test_safe, running_under_pytest

load_env_file()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


PUBLIC_ADMIN = _env_bool("DJANGO_PUBLIC_ADMIN", False)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "content-fabric-local-admin")
if PUBLIC_ADMIN and SECRET_KEY == "content-fabric-local-admin":
    raise RuntimeError("Set a strong DJANGO_SECRET_KEY before enabling DJANGO_PUBLIC_ADMIN.")

DEBUG = _env_bool("DJANGO_DEBUG", not PUBLIC_ADMIN)
ALLOWED_HOSTS = _env_csv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

public_base_url = os.getenv("CONTENT_FABRIC_PUBLIC_BASE_URL", "").strip()
if public_base_url:
    parsed_public_base_url = urlparse(public_base_url)
    if parsed_public_base_url.hostname and parsed_public_base_url.hostname not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(parsed_public_base_url.hostname)

CSRF_TRUSTED_ORIGINS = _env_csv("DJANGO_CSRF_TRUSTED_ORIGINS")
public_origin = public_base_url.rstrip("/")
if public_origin and public_origin not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(public_origin)

ADMIN_URL_PATH = os.getenv("DJANGO_ADMIN_PATH", "admin").strip("/") or "admin"

USE_X_FORWARDED_HOST = _env_bool("DJANGO_USE_X_FORWARDED_HOST", PUBLIC_ADMIN)
if _env_bool("DJANGO_SECURE_PROXY_SSL_HEADER", PUBLIC_ADMIN):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", PUBLIC_ADMIN)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", PUBLIC_ADMIN)
SESSION_COOKIE_HTTPONLY = True
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "pipeline_admin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cf_admin.urls"
WSGI_APPLICATION = "cf_admin.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = os.getenv("DJANGO_STATIC_ROOT", str(PROJECT_ROOT / "runtime" / "django_static"))

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
    if raw and running_under_pytest() and not database_url_is_test_safe(raw):
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("CONTENT_FABRIC_PYTEST_DJANGO_DB", str(PROJECT_ROOT / "runtime" / "pytest_django.sqlite3")),
        }
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

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_IGNORE_RESULT = os.getenv("CELERY_TASK_IGNORE_RESULT", "1") == "1"
CELERY_TIMEZONE = os.getenv("CELERY_TIMEZONE", "Europe/Moscow")
CELERY_BROKER_CONNECTION_TIMEOUT = float(os.getenv("CELERY_BROKER_CONNECTION_TIMEOUT", "5"))
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_connect_timeout": CELERY_BROKER_CONNECTION_TIMEOUT,
    "socket_timeout": CELERY_BROKER_CONNECTION_TIMEOUT,
}
CELERY_BEAT_SCHEDULER = os.getenv(
    "CELERY_BEAT_SCHEDULER",
    "django_celery_beat.schedulers:DatabaseScheduler",
)

try:
    from celery.schedules import crontab
except Exception:
    CELERY_BEAT_SCHEDULE = {}
else:
    CELERY_BEAT_SCHEDULE = {
        "marketer-daily-0900": {
            "task": "pipeline_admin.marketer_daily",
            "schedule": crontab(hour=9, minute=0),
            "options": {"queue": "marketer", "expires": 3600},
        },
        "scheduled-generation-nightly-0100": {
            "task": "pipeline_admin.process_due_scheduled_generations",
            "schedule": crontab(hour=1, minute=0),
            "kwargs": {"limit": int(os.getenv("SCHEDULED_GENERATION_NIGHTLY_LIMIT", "0") or 0)},
        },
        "publish-due-social-videos": {
            "task": "pipeline_admin.publish_due_publication_jobs",
            "schedule": 60.0,
            "kwargs": {"limit": int(os.getenv("PUBLICATION_WORKER_BATCH_SIZE", "20") or 20)},
        },
        "refresh-expiring-instagram-tokens": {
            "task": "pipeline_admin.refresh_expiring_instagram_tokens",
            "schedule": crontab(hour=3, minute=30),
        },
    }
