from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import path


urlpatterns = [
    path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls),
]
