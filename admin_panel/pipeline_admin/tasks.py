"""Background tasks for the Django admin app."""

from __future__ import annotations

import uuid

from app.scheduled_generation import process_due_scheduled_generations, process_scheduled_generation_item
from app.social_publishers import publish_due_publication_jobs, publish_publication_jobs

try:
    from cf_admin.celery import app as celery_app
except Exception:
    celery_app = None


def _process_scheduled_generation_item(item_id: str) -> str:
    import asyncio

    return asyncio.run(process_scheduled_generation_item(uuid.UUID(str(item_id))))


if celery_app is not None:

    @celery_app.task(name="pipeline_admin.process_scheduled_generation_item")
    def process_scheduled_generation_item_task(item_id: str) -> str:
        return _process_scheduled_generation_item(item_id)

    @celery_app.task(name="pipeline_admin.process_due_scheduled_generations")
    def process_due_scheduled_generations_task(limit: int = 0) -> list[tuple[str, str]]:
        return process_due_scheduled_generations(limit=max(0, int(limit or 0)))

    @celery_app.task(name="pipeline_admin.publish_publication_jobs")
    def publish_publication_jobs_task(job_ids: list[str]) -> list[dict]:
        return publish_publication_jobs(job_ids)

    @celery_app.task(name="pipeline_admin.publish_due_publication_jobs")
    def publish_due_publication_jobs_task(limit: int = 20) -> list[dict]:
        return publish_due_publication_jobs(limit=max(1, int(limit or 20)))

else:

    class _UnavailableTask:
        def delay(self, *_args, **_kwargs):
            raise RuntimeError("Celery is not installed/configured.")

    process_scheduled_generation_item_task = _UnavailableTask()
    process_due_scheduled_generations_task = _UnavailableTask()
    publish_publication_jobs_task = _UnavailableTask()
    publish_due_publication_jobs_task = _UnavailableTask()
