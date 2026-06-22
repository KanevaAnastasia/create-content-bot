from __future__ import annotations

from django.apps import AppConfig


class PipelineAdminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pipeline_admin"
    verbose_name = "Content Fabric Pipeline"
