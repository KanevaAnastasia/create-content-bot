"""Optional Celery app for Content Fabric admin tasks."""

from __future__ import annotations

import os


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

try:
    from celery import Celery
except ImportError:  # Celery is optional in local/dev installs.
    app = None
else:
    app = Celery("cf_admin")
    app.config_from_object("django.conf:settings", namespace="CELERY")
    app.autodiscover_tasks()
