from __future__ import annotations

from django.apps import AppConfig


class PipelineAdminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pipeline_admin"
    verbose_name = "Content Fabric Pipeline"

    def ready(self) -> None:
        # Admin autodiscovery has registered the model admins by this point.
        from .publication_upload_admin import configure_publication_upload_admin

        configure_publication_upload_admin()
