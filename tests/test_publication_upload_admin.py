from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponseRedirect
from django.test import RequestFactory


ADMIN_DIR = Path(__file__).resolve().parents[1] / "admin_panel"
sys.path.insert(0, str(ADMIN_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

import django

django.setup()

from pipeline_admin import admin as admin_module
from pipeline_admin.publication_upload_admin import PublicationUploadForm


def _empty_copy() -> dict[str, str]:
    return {key: "" for key, _label in admin_module.SOCIAL_COPY_FIELD_LABELS}


def test_custom_upload_form_requires_copy_for_each_selected_target() -> None:
    form = PublicationUploadForm(
        data={
            "project_slug": "grandma_kitchen",
            "target_keys": ["youtube:ru"],
            "video_variant": "clean",
            "video_source_ru": "upload",
        },
        files={"custom_video_ru": SimpleUploadedFile("video.mp4", b"video")},
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid() is False
    assert "youtube_ru_title" in form.errors
    assert "youtube_ru_description" in form.errors


def test_custom_upload_form_accepts_one_video_per_language_and_cover() -> None:
    form = PublicationUploadForm(
        data={
            "project_slug": "grandma_kitchen",
            "target_keys": ["instagram:ru", "youtube:en"],
            "video_variant": "clean",
            "video_source_ru": "upload",
            "video_source_en": "upload",
            "instagram_ru": "Описание",
            "youtube_en_title": "Title",
            "youtube_en_description": "Description",
        },
        files={
            "custom_video_ru": SimpleUploadedFile("ru.mp4", b"ru"),
            "custom_video_en": SimpleUploadedFile("en.mp4", b"en"),
            "cover_ru": SimpleUploadedFile("cover.png", b"png"),
        },
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["video_source_ru"] == "upload"
    assert form.cleaned_data["video_source_en"] == "upload"


def test_generation_publication_panel_has_single_choice_controls(monkeypatch) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setattr(admin_module, "_publication_copy_from_run", lambda _run: _empty_copy())
    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [])
    monkeypatch.setattr(
        admin_module,
        "_publication_video_source_choices",
        lambda _run: {
            "ru": (("artifact:ru-1", "RU one"), ("artifact:ru-2", "RU two")),
            "en": (("artifact:en-1", "EN one"),),
        },
    )

    class _EmptyJobs:
        def order_by(self, *_args):
            return []

    monkeypatch.setattr(admin_module.PublicationJob, "objects", _EmptyJobs())
    html = str(admin_module._publication_prep_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert 'type="radio" name="video_source_ru"' in html
    assert html.count('name="video_source_ru"') == 2
    assert "Для каждого языка можно выбрать не больше одной версии" in html


def test_standalone_upload_page_is_available_from_admin(monkeypatch) -> None:
    monkeypatch.setattr(
        admin_module,
        "_publication_project_choices",
        lambda: (("grandma_kitchen", "Grandma Kitchen"), ("discipline", "Discipline")),
    )
    request = RequestFactory().get("/admin/pipeline_admin/publicationjob/prepare/")
    modeladmin = SimpleNamespace(admin_site=None)

    response = admin_module.PublicationJobAdmin.prepare_view(modeladmin, request)
    response.render()
    html = response.content.decode()

    assert response.status_code == 200
    assert "Заливка видео" in html
    assert "Русская версия" in html
    assert "English version" in html
    assert "Опубликовать выбранное" in html


def test_standalone_upload_redirects_generation_links(monkeypatch) -> None:
    run_id = uuid.uuid4()

    class _Runs:
        def filter(self, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(id=run_id)

    monkeypatch.setattr(admin_module.GenerationRun, "objects", _Runs())
    request = RequestFactory().get(f"/admin/pipeline_admin/publicationjob/prepare/?run_id={run_id}")
    modeladmin = SimpleNamespace(admin_site=None)

    response = admin_module.PublicationJobAdmin.prepare_view(modeladmin, request)

    assert isinstance(response, HttpResponseRedirect)
    assert str(run_id) in response.url
