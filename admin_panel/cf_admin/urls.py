from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import path

from pipeline_admin.views import publication_media_view
from pipeline_admin.marketer import marketer_view


urlpatterns = [
    path(f"{settings.ADMIN_URL_PATH}/marketer/", admin.site.admin_view(marketer_view), name="marketer"),
    path("publication-media/<uuid:job_id>/<str:token>/video.mp4", publication_media_view, name="publication_media"),
    path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls),
]
