from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import path

from pipeline_admin.views import publication_media_view


urlpatterns = [
    path("publication-media/<uuid:job_id>/<str:token>/video.mp4", publication_media_view, name="publication_media"),
    path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls),
]
