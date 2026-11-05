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
from pipeline_admin import publication_upload_admin as upload_admin
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


def test_final_video_panel_has_single_choice_controls_next_to_versions(monkeypatch) -> None:
    run_id = uuid.uuid4()
    artifacts = [
        SimpleNamespace(
            id=uuid.uuid4(),
            language=language,
            artifact_type="final_clean",
            label=label,
            created_at=None,
        )
        for language, label in (("ru", "RU one"), ("ru", "RU two"), ("en", "EN one"))
    ]
    monkeypatch.setattr(
        admin_module,
        "_publication_all_final_artifacts",
        lambda _run: artifacts,
    )
    monkeypatch.setattr(admin_module, "_publication_artifact_video_payload", lambda artifact: {"video_path": str(artifact.id)})
    monkeypatch.setattr(admin_module, "_artifact_video_preview", lambda artifact: f"<video>{artifact.label}</video>")
    monkeypatch.setattr(admin_module, "_generation_artifact_link", lambda artifact, _label: f"open-{artifact.id}")
    monkeypatch.setattr(upload_admin, "_original_final_videos_panel", lambda _run: "<div>build controls</div>")

    html = str(admin_module._final_videos_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert 'type="radio" name="cf_publish_source_ru"' in html
    assert html.count('name="cf_publish_source_ru"') == 3
    assert html.count('name="cf_publish_source_en"') == 2
    assert html.count(" checked") == 2
    assert html.count('value="" checked') == 2
    assert "Не выбирать видео этого языка" in html
    assert "Для каждого языка можно выбрать не больше одной версии" in html
    assert "Перейти к заливке с выбранными версиями" in html
    assert "<form" not in html


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


def test_generation_upload_preserves_explicit_empty_language_selection(monkeypatch) -> None:
    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, pk=run_id, human_title="Видео")
    monkeypatch.setattr(admin_module, "_publication_project_choices", lambda: (("grandma_kitchen", "Grandma"),))
    monkeypatch.setattr(admin_module, "_project_slug_for_publication_run", lambda _run: "grandma_kitchen")
    monkeypatch.setattr(
        admin_module,
        "_publication_video_source_choices",
        lambda _run: {"ru": (("artifact:ru-1", "RU one"),), "en": (("artifact:en-1", "EN one"),)},
    )
    monkeypatch.setattr(
        admin_module,
        "_publication_post_production_from_final_artifacts",
        lambda _run: {"renders_by_language": {"ru": {"clean": {"video_path": "/tmp/ru.mp4"}}}},
    )
    monkeypatch.setattr(admin_module, "all_final_artifacts_delivered", lambda _run_id: True)
    monkeypatch.setattr(admin_module, "_publication_copy_from_run", lambda _run: _empty_copy())
    monkeypatch.setattr(admin_module, "_publication_recipe_from_draft", lambda _run: "")
    monkeypatch.setattr(admin_module, "_publication_recipe_for_run", lambda _run: "recipe")
    request = RequestFactory().get("/?video_source_ru=&video_source_en=")
    modeladmin = SimpleNamespace(
        get_object=lambda _request, _object_id: run,
        message_user=lambda *_args, **_kwargs: None,
        admin_site=None,
    )

    response = admin_module.GenerationRunAdmin.queue_publications_view(modeladmin, request, str(run_id))
    form = response.context_data["form"]

    assert form["video_source_ru"].value() == ""
    assert form["video_source_en"].value() == ""
