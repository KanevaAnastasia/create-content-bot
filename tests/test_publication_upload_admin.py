from __future__ import annotations

import os
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
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


def test_marketer_keeps_trial_identity_separate():
    from pipeline_admin.marketer import job_dict
    job = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(),
        project=SimpleNamespace(slug="grandma_kitchen"), bundle_key="bundle-trial",
        platform="instagram", language="ru", account_key="instagram:ru",
        title="Trial", description="Trial text", published_at=None,
        external_post_id="trial-id", payload_json={"instagram_trial": True})
    row = job_dict(job)
    assert row["instagram_trial"] is True
    assert row["external_post_id"] == "trial-id"
    assert row["bundle_key"] == "bundle-trial"


def test_missing_copy_never_duplicates_source_script_into_en(monkeypatch):
    monkeypatch.setattr(admin_module, "_latest_chat_session_for_run", lambda run: (None, {}))
    monkeypatch.setattr(admin_module, "_publication_copy_from_draft", lambda run: {})
    monkeypatch.setattr(admin_module, "_publication_copy_from_latest_jobs", lambda run: {})
    result = upload_admin._original_publication_copy_from_run(
        SimpleNamespace(script="Русский рецепт, не описание для площадок"))
    assert result == _empty_copy()


def test_copy_reads_bot_pending_run_artifact(monkeypatch):
    class Query(list):
        def filter(self, **kwargs):
            return Query(x for x in self if x.artifact_type == kwargs.get("artifact_type"))
        def order_by(self, *args):
            return self
        def first(self):
            return self[0] if self else None
    pending = SimpleNamespace(
        artifact_type="run_pending_input", created_at=datetime.now(timezone.utc),
        payload_json={"session_key": "social_descriptions_pending", "pending": {
            "copy": {"instagram_en": "Bot English #recipe", "vk_ru": "Бот ВК #рецепт"}}})
    monkeypatch.setattr(upload_admin.GenerationArtifact, "objects", Query([pending]))
    monkeypatch.setattr(upload_admin.ChatSession, "objects", Query())
    monkeypatch.setattr(upload_admin.PublicationJob, "objects", Query())
    result = upload_admin._freshest_publication_copy_from_run(SimpleNamespace(id=uuid.uuid4()))
    assert result["instagram_en"] == "Bot English #recipe"
    assert result["vk_ru"] == "Бот ВК #рецепт"


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


def test_description_textarea_rows_expand_for_full_copy() -> None:
    copy = _empty_copy()
    copy["youtube_en_description"] = "\n".join(
        ["A long platform description " * 8 for _ in range(18)]
    )
    form = PublicationUploadForm(
        copy=copy,
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    widget = form.fields["youtube_en_description"].widget
    assert widget.attrs["class"] == "cf-upload-textarea"
    assert int(widget.attrs["rows"]) > 18


def test_publication_jobs_are_dispatched_as_independent_tasks(monkeypatch) -> None:
    from pipeline_admin import tasks

    calls = []
    monkeypatch.setattr(
        tasks,
        "publish_publication_job_task",
        SimpleNamespace(delay=lambda job_id: calls.append(job_id) or f"task-{job_id}"),
    )

    results = tasks.dispatch_publication_jobs(["job-a", "job-b", "job-c"])

    assert calls == ["job-a", "job-b", "job-c"]
    assert results == ["task-job-a", "task-job-b", "task-job-c"]


def test_publication_monitor_groups_trial_and_main_jobs(monkeypatch) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    jobs = [
        SimpleNamespace(
            pk=uuid.uuid4(),
            project=SimpleNamespace(slug="grandma_kitchen"),
            bundle_key=f"{root}-trial",
            platform="instagram",
            language="ru",
            status="published",
            payload_json={"instagram_trial": True},
            attempt_count=1,
            external_post_id="trial-media-id",
            publish_error=None,
            published_at=datetime.now(timezone.utc),
        ),
        SimpleNamespace(
            pk=uuid.uuid4(),
            project=SimpleNamespace(slug="grandma_kitchen"),
            bundle_key=root,
            platform="instagram",
            language="ru",
            status="approved",
            payload_json={"publish_after_job_id": "previous"},
            attempt_count=0,
            external_post_id=None,
            publish_error="Waiting for previous publication.",
            published_at=None,
        ),
        SimpleNamespace(
            pk=uuid.uuid4(),
            project=SimpleNamespace(slug="grandma_kitchen"),
            bundle_key=root,
            platform="youtube",
            language="en",
            status="failed",
            payload_json={},
            attempt_count=1,
            external_post_id=None,
            publish_error="API error",
            published_at=None,
        ),
    ]

    class FakeQuerySet:
        def select_related(self, *_args):
            return self

        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter(jobs)

    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())
    request = RequestFactory().get("/admin/pipeline_admin/publicationjob/", {"bundle": root})

    monitor = admin_module._publication_monitor_context(request)

    assert monitor is not None
    assert monitor["total"] == 3
    assert monitor["published"] == 1
    assert monitor["failed"] == 1
    assert monitor["pending"] is True
    assert monitor["auto_refresh"] is True
    assert monitor["rows"][0]["label"] == "Instagram RU · Trial Reel"
    failed_row = next(row for row in monitor["rows"] if row["status"] == "failed")
    assert failed_row["retry_url"].endswith(f"/{jobs[2].pk}/retry/")
    assert failed_row["youtube_reconnect_url"] == ""

    jobs[2].publish_error = "YouTube OAuth refresh: HTTP 400: invalid_grant"
    monitor = admin_module._publication_monitor_context(request)
    failed_row = next(row for row in monitor["rows"] if row["status"] == "failed")
    assert failed_row["youtube_reconnect_url"].endswith(
        "/youtube-oauth/?project_slug=grandma_kitchen&language=en"
    )
    jobs[2].publish_error = "API error"

    jobs[2].platform = "instagram"
    jobs[2].payload_json = {"instagram_upload": {"stage": "upload_rejected"}}
    monitor = admin_module._publication_monitor_context(request)
    failed_row = next(row for row in monitor["rows"] if row["status"] == "failed")
    assert failed_row["instagram_recovery_url"].endswith(f"/{jobs[2].pk}/instagram-recovery/")
    assert not next(row for row in monitor["rows"] if row["status"] == "published")["instagram_recovery_url"]
    jobs[2].payload_json["instagram_upload"]["publish_ambiguous"] = True
    monitor = admin_module._publication_monitor_context(request)
    assert not next(row for row in monitor["rows"] if row["status"] == "failed")["instagram_recovery_url"]

    jobs[2].platform = 'vk'
    jobs[2].language = 'ru'
    jobs[2].external_post_id = '-123_42'
    jobs[2].payload_json = {
        'vk_stories': True, 'vk_wall_post_verified': True,
        'external_post_url': 'https://vk.com/wall-123_99',
        'vk_story_parts': [{'id': '-123_7', 'verified': True, 'upload_result': 'private-result'},
                           {'id': 'javascript:alert(1)', 'verified': False}],
    }
    monitor = admin_module._publication_monitor_context(request)
    row = next(r for r in monitor['rows'] if r['vk_stories_enabled'])
    assert row['vk_wall_verified']
    assert row['external_url'] == 'https://vk.com/wall-123_99'
    assert row['vk_story_results'] == [{'url': 'https://vk.com/story-123_7', 'verified': True}]
    assert row['publish_error'] == 'API error'
    assert 'private-result' not in str(row)


def test_publication_monitor_shows_schedule_and_publish_now_action(monkeypatch) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    scheduled_at = datetime(2026, 11, 24, 10, 28, tzinfo=timezone.utc)
    job = SimpleNamespace(
        pk=uuid.uuid4(),
        project=SimpleNamespace(slug="grandma_kitchen"),
        bundle_key=root,
        platform="youtube",
        language="ru",
        status="scheduled",
        scheduled_at=scheduled_at,
        payload_json={},
        attempt_count=0,
        external_post_id=None,
        publish_error=None,
        published_at=None,
    )

    class FakeQuerySet:
        def select_related(self, *_args):
            return self

        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter([job])

    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())

    monitor = admin_module._publication_monitor_context(
        RequestFactory().get(f"/admin/pipeline_admin/publicationjob/control/{root}/"),
        bundle_key=root,
    )

    assert monitor is not None
    assert monitor["next_scheduled_at"] == scheduled_at
    assert monitor["all_scheduled_at_same_time"] is True
    assert monitor["publish_now_url"].endswith(f"/control/{root}/publish-now/")
    assert monitor["rows"][0]["scheduled_at"] == scheduled_at


def test_publish_bundle_now_marks_scheduled_jobs_due_and_dispatches_them(monkeypatch) -> None:
    from pipeline_admin import tasks

    root = "11111111-2222-3333-4444-555555555555"
    original_schedule = datetime(2026, 11, 24, 10, 28, tzinfo=timezone.utc)
    job = SimpleNamespace(
        pk=uuid.uuid4(),
        status="scheduled",
        scheduled_at=original_schedule,
        payload_json={"scheduled_at": original_schedule.isoformat()},
        updated_at=original_schedule,
        save_calls=[],
    )
    job.save = lambda **kwargs: job.save_calls.append(kwargs)

    class FakeQuerySet:
        def select_for_update(self):
            return self

        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter([job])

    dispatched = []
    messages = []
    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())
    monkeypatch.setattr(admin_module.transaction, "atomic", lambda: nullcontext())
    monkeypatch.setattr(tasks, "dispatch_publication_jobs", lambda job_ids: dispatched.extend(job_ids))

    model_admin = admin_module.PublicationJobAdmin(admin_module.PublicationJob, admin_module.admin.site)
    monkeypatch.setattr(model_admin, "has_change_permission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        model_admin,
        "message_user",
        lambda _request, message, **_kwargs: messages.append(message),
    )

    response = model_admin.publish_bundle_now_view(
        RequestFactory().post(f"/admin/pipeline_admin/publicationjob/control/{root}/publish-now/"),
        root,
    )

    assert isinstance(response, HttpResponseRedirect)
    assert response.url.endswith(f"/control/{root}/")
    assert job.status == "approved"
    assert job.scheduled_at <= datetime.now(timezone.utc)
    assert job.payload_json["publish_now_requested_at"]
    assert job.save_calls
    assert dispatched == [str(job.pk)]
    assert messages == ["Немедленная заливка запущена. Площадок: 1."]


def test_publication_bundle_history_groups_runs_and_links_to_control(monkeypatch) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    created = datetime.now(timezone.utc)
    project = SimpleNamespace(title="Grandma Kitchen", __str__=lambda self: self.title)
    jobs = [
        SimpleNamespace(
            project=project,
            bundle_key=root,
            platform="youtube",
            language="ru",
            status="published",
            payload_json={},
            title="Сочная курица",
            description="",
            created_at=created,
            updated_at=created,
        ),
        SimpleNamespace(
            project=project,
            bundle_key=f"{root}-trial",
            platform="instagram",
            language="ru",
            status="failed",
            payload_json={"instagram_trial": True},
            title="",
            description="Пробное описание",
            created_at=created,
            updated_at=created,
        ),
    ]

    class FakeQuerySet:
        def select_related(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter(jobs)

    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())

    history = admin_module._publication_bundle_history_context(
        RequestFactory().get("/admin/pipeline_admin/publicationjob/")
    )

    assert history["total"] == 1
    bundle = history["bundles"][0]
    assert bundle["bundle_key"] == root
    assert bundle["title"] == "Сочная курица"
    assert bundle["published"] == 1
    assert bundle["failed"] == 1
    assert bundle["status_class"] == "error"
    assert "Instagram RU · Trial" in bundle["targets"]
    assert bundle["monitor_url"].endswith(f"/control/{root}/")


def test_publication_bundle_history_shows_time_for_scheduled_run(monkeypatch) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    scheduled_at = datetime(2026, 11, 24, 13, 3, tzinfo=timezone.utc)
    created = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    project = SimpleNamespace(title="Grandma Kitchen", __str__=lambda self: self.title)
    jobs = [
        SimpleNamespace(
            project=project,
            bundle_key=root,
            platform=platform,
            language=language,
            status="scheduled",
            scheduled_at=scheduled_at,
            payload_json={},
            title="Мандариновый десерт",
            description="",
            created_at=created,
            updated_at=created,
        )
        for platform, language in (("youtube", "ru"), ("instagram", "en"))
    ]

    class FakeQuerySet:
        def select_related(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter(jobs)

    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())

    history = admin_module._publication_bundle_history_context(
        RequestFactory().get("/admin/pipeline_admin/publicationjob/")
    )

    bundle = history["bundles"][0]
    assert bundle["status_label"] == "Запланировано"
    assert bundle["scheduled_at"] == scheduled_at
    assert bundle["all_scheduled_at_same_time"] is True


def test_publication_monitor_shows_verified_vk_processing_without_stale_retry(monkeypatch) -> None:
    root = "11111111-2222-3333-4444-555555555555"
    vk_job = SimpleNamespace(
        pk=uuid.uuid4(),
        project=SimpleNamespace(slug="grandma_kitchen"),
        bundle_key=root,
        platform="vk",
        language="ru",
        status="needs_manual_action",
        payload_json={
            "vk_video_processing": True,
            "vk_video_external_id": "-123_42",
            "external_post_url": "https://vkvideo.ru/video-123_42",
        },
        attempt_count=1,
        external_post_id=None,
        publish_error="VK upload not confirmed. Check the saved upload stage; automatic re-upload is blocked.",
        published_at=None,
    )

    class FakeQuerySet:
        def select_related(self, *_args):
            return self

        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def __iter__(self):
            return iter([vk_job])

    monkeypatch.setattr(admin_module.PublicationJob, "objects", FakeQuerySet())
    request = RequestFactory().get("/admin/pipeline_admin/publicationjob/", {"bundle": root})

    monitor = admin_module._publication_monitor_context(request)

    assert monitor is not None
    row = monitor["rows"][0]
    assert row["status"] == "vk_processing"
    assert row["status_label"] == "VK обрабатывает видео"
    assert row["status_class"] == "active"
    assert row["publish_error"] == ""
    assert row["retry_url"] == ""
    assert monitor["failed"] == 0
    assert monitor["pending"] is True


def test_publication_external_url_prefers_saved_permalink() -> None:
    job = SimpleNamespace(
        payload_json={"external_post_url": "https://www.instagram.com/reel/example/"},
        external_post_id="media-id",
        platform="instagram",
    )

    assert admin_module._publication_external_url(job) == "https://www.instagram.com/reel/example/"


def test_publication_video_previews_are_scoped_per_language(monkeypatch) -> None:
    ru = SimpleNamespace(id=uuid.uuid4(), language="ru", label="RU final", payload_json={"cover_frame_suggestion_sec": 1.8})
    en = SimpleNamespace(id=uuid.uuid4(), language="en", label="EN final")
    monkeypatch.setattr(admin_module, "_publication_all_final_artifacts", lambda _run: [ru, en])
    monkeypatch.setattr(admin_module, "_artifact_media_url", lambda artifact: f"/media/{artifact.id}/")

    previews = upload_admin._publication_video_previews(SimpleNamespace(id=uuid.uuid4()))

    assert previews["ru"] == ({
        "source": f"artifact:{ru.id}",
        "url": f"/media/{ru.id}/",
        "label": "RU final",
        "cover_time": "1.8",
    },)
    assert previews["en"] == ({
        "source": f"artifact:{en.id}",
        "url": f"/media/{en.id}/",
        "label": "EN final",
        "cover_time": "1",
    },)


def test_monitor_url_uses_root_bundle_for_trial_job() -> None:
    jobs = [SimpleNamespace(bundle_key="bundle-123-trial")]

    url = upload_admin._publication_jobs_monitor_url(jobs)

    assert url.endswith("/control/bundle-123/")


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


def test_tiktok_upload_requires_manual_privacy_and_music_confirmation() -> None:
    base = {
        "project_slug": "grandma_kitchen",
        "target_keys": ["tiktok:ru"],
        "video_variant": "clean",
        "video_source_ru": "upload",
        "tiktok_ru": "Описание TikTok #рецепт",
    }
    files = {"custom_video_ru": SimpleUploadedFile("ru.mp4", b"ru")}
    form = PublicationUploadForm(
        data=base,
        files=files,
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid() is False
    assert "tiktok_privacy_level" in form.errors
    assert "tiktok_music_usage_confirmed" in form.errors
    assert "tiktok_publish_confirmed" in form.errors

    form = PublicationUploadForm(
        data={
            **base,
            "tiktok_privacy_level": "SELF_ONLY",
            "tiktok_music_usage_confirmed": "on",
            "tiktok_publish_confirmed": "on",
            "tiktok_is_aigc": "on",
        },
        files={"custom_video_ru": SimpleUploadedFile("ru.mp4", b"ru")},
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["tiktok_privacy_level"] == "SELF_ONLY"
    assert form.cleaned_data["tiktok_music_usage_confirmed"] is True


def test_instagram_ru_trial_requires_its_own_description_not_normal_copy() -> None:
    form = PublicationUploadForm(
        data={
            "project_slug": "grandma_kitchen",
            "target_keys": ["instagram:ru"],
            "video_variant": "clean",
            "video_source_ru": "upload",
            "instagram_ru_trial": "on",
            "instagram_ru": "Обычное описание",
        },
        files={"custom_video_ru": SimpleUploadedFile("ru.mp4", b"ru")},
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid() is False
    assert "instagram_ru_trial_description" in form.errors

    form = PublicationUploadForm(
        data={
            "project_slug": "grandma_kitchen",
            "target_keys": ["instagram:ru"],
            "video_variant": "clean",
            "video_source_ru": "upload",
            "instagram_ru_trial": "on",
            "instagram_ru": "Обычное описание",
            "instagram_ru_trial_description": "Отдельный пробный текст #тест",
        },
        files={"custom_video_ru": SimpleUploadedFile("ru.mp4", b"ru")},
        copy=_empty_copy(),
        project_choices=(("grandma_kitchen", "Grandma"),),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid(), form.errors


def test_trial_description_is_generated_separately_and_strips_attribution(monkeypatch) -> None:
    calls = []

    async def refine(*, recipe, previous_copy, feedback, project_slug=None):
        calls.append((recipe, previous_copy, feedback, project_slug))
        return {
            **previous_copy,
            "instagram_ru": (
                "Новый пробный хук\n\nСделано с помощью @syntx_ai\n\n#рецепт"
            ),
        }

    monkeypatch.setattr(
        "app.handlers.handlers_state_media._refine_social_copy_from_feedback",
        refine,
    )

    result = upload_admin._generate_instagram_ru_trial_description(
        recipe="Рецепт",
        normal_copy={"instagram_ru": "Обычный текст"},
        feedback="короче",
    )

    assert result == "Новый пробный хук\n\n#рецепт"
    assert len(calls) == 1
    assert "НОВОЕ альтернативное описание" in calls[0][2]
    assert "не упоминай Syntx" in calls[0][2]


def test_admin_copy_generator_passes_project_to_hashtag_generator(monkeypatch) -> None:
    calls = []

    async def generate(recipe, *, project_slug=None, **_kwargs):
        calls.append((recipe, project_slug))
        return {**_empty_copy(), "instagram_ru": "Текст #тихаядисциплина"}

    monkeypatch.setattr(
        "app.handlers.handlers_state_media._generate_social_copy_from_recipe",
        generate,
    )
    run = SimpleNamespace(
        project_id=1,
        project=SimpleNamespace(slug="discipline"),
    )

    result = admin_module._generate_publication_copy_for_admin(
        run,
        current_copy=_empty_copy(),
        recipe="История",
    )

    assert result["instagram_ru"].endswith("#тихаядисциплина")
    assert calls == [("История", "discipline")]


def test_trial_description_retries_when_generator_copies_normal_text(monkeypatch) -> None:
    calls = []

    async def refine(**kwargs):
        calls.append(kwargs["feedback"])
        return {
            **kwargs["previous_copy"],
            "instagram_ru": (
                "Обычный текст" if len(calls) == 1 else "Новый Trial с другим хуком"
            ),
        }

    monkeypatch.setattr(
        "app.handlers.handlers_state_media._refine_social_copy_from_feedback",
        refine,
    )

    result = upload_admin._generate_instagram_ru_trial_description(
        recipe="Рецепт",
        normal_copy={"instagram_ru": "Обычный текст"},
    )

    assert result == "Новый Trial с другим хуком"
    assert len(calls) == 2
    assert "Перепиши его полностью" in calls[1]


def test_trial_description_draft_is_saved_without_syntx(monkeypatch) -> None:
    created = []
    monkeypatch.setattr(
        upload_admin.GenerationArtifact,
        "objects",
        SimpleNamespace(create=lambda **kwargs: created.append(kwargs)),
    )
    run_id = uuid.uuid4()

    upload_admin._save_instagram_ru_trial_draft(
        SimpleNamespace(id=run_id),
        "Пробное описание\n\nСделано с помощью @syntx_ai\n\n#пробное",
    )

    assert created[0]["artifact_type"] == upload_admin.PUBLICATION_TRIAL_COPY_DRAFT_ARTIFACT_TYPE
    assert created[0]["payload_json"]["description"] == "Пробное описание\n\n#пробное"


def test_freshest_copy_ignores_trial_job_and_preserves_other_platform_copy(monkeypatch) -> None:
    run_id = uuid.uuid4()

    class Query(list):
        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return self[0] if self else None

    session = SimpleNamespace(
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        session_json={
            "social_descriptions_pending": {
                "copy": {
                    **_empty_copy(),
                    "youtube_ru_title": "Сохранённый заголовок",
                    "youtube_ru_description": "Сохранённое описание",
                    "instagram_ru": "Старое обычное описание",
                }
            }
        },
    )
    normal_job = SimpleNamespace(
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        payload_json={"run_id": str(run_id), "instagram_trial": False},
        bundle_key="normal",
        platform="instagram",
        language="ru",
        title="",
        description="Новое обычное описание",
    )
    trial_job = SimpleNamespace(
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        payload_json={"run_id": str(run_id), "instagram_trial": True},
        bundle_key="trial",
        platform="instagram",
        language="ru",
        title="",
        description="Пробное описание",
    )
    monkeypatch.setattr(upload_admin.ChatSession, "objects", Query([session]))
    monkeypatch.setattr(upload_admin.GenerationArtifact, "objects", Query())
    monkeypatch.setattr(upload_admin.PublicationJob, "objects", Query([trial_job, normal_job]))

    copy = upload_admin._freshest_publication_copy_from_run(SimpleNamespace(id=run_id))

    assert copy["instagram_ru"] == "Новое обычное описание"
    assert copy["youtube_ru_title"] == "Сохранённый заголовок"
    assert copy["youtube_ru_description"] == "Сохранённое описание"


def test_freshest_copy_recovers_legacy_bot_jobs_by_exact_recipe(monkeypatch) -> None:
    run_id = uuid.uuid4()

    class Query(list):
        def filter(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return self[0] if self else None

    jobs = Query(
        [
            SimpleNamespace(
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                payload_json={},
                bundle_key="bot-bundle",
                platform="instagram",
                language="ru",
                title="",
                description="Точное ботское описание Instagram",
                recipe="Точный утверждённый рецепт",
            ),
            SimpleNamespace(
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                payload_json={},
                bundle_key="bot-bundle",
                platform="vk",
                language="ru",
                title="",
                description="Отдельное ботское описание VK",
                recipe="Точный утверждённый рецепт",
            ),
        ]
    )
    monkeypatch.setattr(upload_admin.ChatSession, "objects", Query())
    monkeypatch.setattr(upload_admin.GenerationArtifact, "objects", Query())
    monkeypatch.setattr(upload_admin.PublicationJob, "objects", jobs)
    monkeypatch.setattr(admin_module, "_publication_recipe_from_draft", lambda _run: "")
    monkeypatch.setattr(
        admin_module,
        "_publication_recipe_for_run",
        lambda _run: "Точный утверждённый рецепт",
    )

    copy = upload_admin._freshest_publication_copy_from_run(
        SimpleNamespace(id=run_id, script="Точный утверждённый рецепт")
    )

    assert copy["instagram_ru"] == "Точное ботское описание Instagram"
    assert copy["vk_ru"] == "Отдельное ботское описание VK"


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
    assert "Сгенерировать все разные описания" in html
    assert "отдельное уникальное описание Trial Reel" in html
    assert "requestAnimationFrame(()=>grow(trialCopy))" in html


def test_standalone_upload_generates_platform_and_trial_copy_separately(monkeypatch) -> None:
    monkeypatch.setattr(
        admin_module,
        "_publication_project_choices",
        lambda: (("grandma_kitchen", "Grandma Kitchen"),),
    )
    normal = {
        **_empty_copy(),
        "instagram_ru": "Основной Instagram #обычный",
        "vk_ru": "Другой текст VK",
    }
    monkeypatch.setattr(
        admin_module,
        "_generate_publication_copy_for_admin",
        lambda *_args, **_kwargs: normal,
    )
    monkeypatch.setattr(
        upload_admin,
        "_generate_instagram_ru_trial_description",
        lambda **_kwargs: "Совсем другой Trial #пробный",
    )
    messages = []
    request = RequestFactory().post(
        "/admin/pipeline_admin/publicationjob/prepare/",
        {
            "project_slug": "grandma_kitchen",
            "publication_recipe": "Исходный рецепт",
            "generate_descriptions": "1",
        },
    )
    modeladmin = SimpleNamespace(
        admin_site=None,
        message_user=lambda _request, message, **_kwargs: messages.append(message),
    )

    response = admin_module.PublicationJobAdmin.prepare_view(modeladmin, request)
    response.render()
    form = response.context_data["form"]

    assert form["instagram_ru"].value().startswith("Основной Instagram")
    assert "Сделано с помощью @syntx_ai" in form["instagram_ru"].value()
    assert form["vk_ru"].value() == "Другой текст VK"
    assert form["instagram_ru_trial_description"].value() == "Совсем другой Trial #пробный"
    assert any("Все описания созданы отдельно" in message for message in messages)


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
