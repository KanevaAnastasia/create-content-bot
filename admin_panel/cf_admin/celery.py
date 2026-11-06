"""Optional Celery app for Content Fabric admin tasks."""

from __future__ import annotations

import os


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

try:
    from celery import Celery
except ImportError:  # Celery is optional in local/dev installs.
    app = None
else:
    from kombu import Exchange, Queue

    app = Celery("cf_admin")
    app.config_from_object("django.conf:settings", namespace="CELERY")
    # Existing workers keep serving publication tasks; the new analytics queue
    # is only consumed by workers restarted with this version of the application.
    app.conf.task_queues = (
        Queue("celery", Exchange("celery"), routing_key="celery"),
        Queue("marketer", Exchange("marketer"), routing_key="marketer"),
    )
    app.autodiscover_tasks()
