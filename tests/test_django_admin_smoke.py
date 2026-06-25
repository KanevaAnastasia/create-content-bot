import importlib.util
from io import StringIO
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(importlib.util.find_spec("django") is None, reason="django is not installed")


def test_django_admin_session_form_updates_scene_json():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    instance = ChatSession(
        chat_id=987654321,
        state="waiting_scene_media",
        generation_run_id=None,
        session_json={
            "state": "waiting_scene_media",
            "image_scene_index": 0,
            "production": {
                "json_plan": [
                    {
                        "scene": 1,
                        "start_frame_prompt": "old start",
                        "end_frame_prompt": "old end",
                        "video_prompt_by_lang": {"ru": "old"},
                    }
                ]
            },
        },
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654321",
            "state": "waiting_scene_media",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": '{"dish_name": "Пирог"}',
            "recipe_full_text": "",
            "scenes_plan_json": "[]",
            "production_plan_json": '[{"scene": 1, "start_frame_prompt": "old start", "end_frame_prompt": "old end"}]',
            "overlay_title_by_lang_json": '{"ru": "Новый заголовок", "en": "New title"}',
            "overlay_title_style_json": '{"title_color": "#c72638"}',
            "scene_index": "0",
            "start_frame_prompt": "new start",
            "end_frame_requested": "on",
            "end_frame_prompt": "new end",
            "video_prompt_by_lang_json": '{"ru": "new"}',
            "speech_by_lang_json": "{}",
            "syntax_retry_context_json": "{}",
            "video_approvals_by_lang_json": '{"ru": false}',
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    scene = saved.session_json["production"]["json_plan"][0]
    assert scene["start_frame_prompt"] == "new start"
    assert scene["end_frame_requested"] is True
    assert scene["video_prompt_by_lang"]["ru"] == "new"
    assert saved.session_json["viral_concept_brief"] == {"dish_name": "Пирог"}
    assert saved.session_json["overlay_title_by_lang"] == {"ru": "Новый заголовок", "en": "New title"}
    assert saved.session_json["production"]["overlay_title_style"] == {"title_color": "#c72638"}


def test_django_admin_session_form_syncs_ru_overlay_title_to_generation_name(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.forms as forms_module
    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    run_id = uuid.uuid4()
    updates: list[dict[str, object]] = []

    class _QuerySet:
        def update(self, **kwargs):
            updates.append({"update": kwargs})
            return 1

    class _Manager:
        def filter(self, **kwargs):
            updates.append({"filter": kwargs})
            return _QuerySet()

    class _GenerationRun:
        objects = _Manager()

    monkeypatch.setattr(forms_module, "GenerationRun", _GenerationRun)
    monkeypatch.setattr(ChatSession, "save", lambda self, *args, **kwargs: None)

    instance = ChatSession(
        chat_id=987654329,
        state="waiting_scene_media",
        generation_run_id=run_id,
        session_json={"state": "waiting_scene_media", "production": {"json_plan": []}},
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654329",
            "state": "waiting_scene_media",
            "generation_run_id": str(run_id),
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": "{}",
            "recipe_full_text": "",
            "scenes_plan_json": "[]",
            "production_plan_json": "[]",
            "overlay_title_by_lang_json": '{"ru": " Русский заголовок ", "en": "English title"}',
            "overlay_title_style_json": "{}",
            "scene_index": "0",
            "start_frame_prompt": "",
            "end_frame_requested": "",
            "end_frame_prompt": "",
            "video_prompt_by_lang_json": "{}",
            "speech_by_lang_json": "{}",
            "syntax_retry_context_json": "{}",
            "video_approvals_by_lang_json": "{}",
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    form.save(commit=True)

    assert updates == [
        {"filter": {"id": run_id}},
        {"update": {"human_title": "Русский заголовок"}},
    ]


def test_django_content_series_model_string():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.models import ContentSeries

    series = ContentSeries(
        title_ru="летние напитки",
        title_en="summer drinks",
        current_video_index=7,
        total_videos=10,
    )

    assert str(series) == "летние напитки / summer drinks (7 из 10)"


def test_django_admin_allows_series_binding_and_day_editing():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.admin import (
        ChatSessionAdmin,
        ContentSeriesAdmin,
        GenerationRunAdmin,
        ProjectPublishAccountAdmin,
        PublicationJobAdmin,
        VideoOverviewAdmin,
    )

    assert "series" in GenerationRunAdmin.list_editable
    assert "series_video_index" in GenerationRunAdmin.list_editable
    assert "series_total_videos" in GenerationRunAdmin.list_editable
    assert GenerationRunAdmin.autocomplete_fields == ("series",)
    assert "current_video_index" in ContentSeriesAdmin.list_editable
    assert "total_videos" in ContentSeriesAdmin.list_editable
    assert "is_active" in ContentSeriesAdmin.list_editable
    assert "build_final_title_and_videos" in [action.__name__ for action in GenerationRunAdmin.actions]
    assert "build_final_title_and_videos" in [action.__name__ for action in VideoOverviewAdmin.actions]
    assert "build_final_title_and_videos" in [action.__name__ for action in ChatSessionAdmin.actions]
    assert "publication_prep" in GenerationRunAdmin.readonly_fields
    assert "publication_prep" in GenerationRunAdmin.fields
    assert "status" in PublicationJobAdmin.list_editable
    assert "is_active" in ProjectPublishAccountAdmin.list_editable


def test_django_generation_admin_shows_final_videos_panel(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.admin import GenerationArtifactAdmin, GenerationRunAdmin

    run_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    old_artifact_id = uuid.uuid4()
    artifact = SimpleNamespace(
        pk=artifact_id,
        uri="file:///tmp/final_titled_subtitled_music_master_ru.mp4",
        mime_type="video/mp4",
        artifact_type="final_titled_subtitled_music",
        language="ru",
        payload_json={"width": 1080, "height": 1920},
        created_at=None,
    )
    old_artifact = SimpleNamespace(
        pk=old_artifact_id,
        uri="file:///tmp/old_final_titled_subtitled_music_master_ru.mp4",
        mime_type="video/mp4",
        artifact_type="final_titled_subtitled_music",
        language="ru",
        payload_json={"width": 1080, "height": 1920},
        created_at=None,
    )

    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [artifact])
    monkeypatch.setattr(
        admin_module,
        "_latest_final_build_statuses",
        lambda _run_id: {
            ("ru", "titled_subtitled_music"): {
                "status": "failed",
                "message": "render exploded",
                "updated_at": "2026-06-24T12:00:00",
            }
        },
    )
    monkeypatch.setattr(admin_module, "_final_target_languages_for_run", lambda _run, _artifacts, _statuses: ["ru"])

    html = str(admin_module._final_videos_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert "Rebuild all final videos" in html
    assert f"/{run_id}/build-final/" in html
    assert "Build/rebuild this video" in html
    assert f"/{run_id}/queue-final/ru/titled_subtitled_music/" in html
    assert "Latest final builds only" not in html
    assert "Latest video" in html
    assert "Build status" in html
    assert "Links" in html
    assert "open all final builds" in html
    assert f"q={run_id}" in html
    assert "final_builds=1" in html
    assert "artifact_type__exact=final_titled_subtitled_music" in html
    assert "title/subtitled/music" in html
    assert "render exploded" in html
    assert "final_titled_subtitled_music" in html
    assert "open latest video" in html
    assert "open folder in Finder" in html
    assert "file:///tmp" in html
    assert f"/{artifact_id}/media/" in html
    assert f"/{old_artifact_id}/media/" not in html
    assert "file:///tmp/final_titled_subtitled_music_master_ru.mp4" not in html
    assert "final_videos" in GenerationRunAdmin.readonly_fields
    assert "final_videos" in GenerationRunAdmin.fields
    assert "run__id" in GenerationArtifactAdmin.search_fields
    assert GenerationArtifactAdmin.list_filter[0].parameter_name == "final_builds"


def test_django_admin_overlay_title_style_normalizes_legacy_top_margin():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    assert admin_module._overlay_title_style_for_admin_render({}, {})["top_margin_ratio"] == 0.05
    assert admin_module._overlay_title_style_for_admin_render(
        {},
        {"overlay_title_style": {"top_margin_ratio": 0.15, "title_color": "#fff"}},
    ) == {"top_margin_ratio": 0.05, "title_color": "#fff"}
    assert admin_module._overlay_title_style_for_admin_render(
        {},
        {"overlay_title_style": {"top_margin_ratio": 0.08}},
    )["top_margin_ratio"] == 0.08


def test_django_admin_title_overlay_recovers_saved_language_and_style():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run = SimpleNamespace(human_title="Малиновый лимонад")
    production = {
        "post_production": {
            "title_overlay_by_language": {
                "ru": {"title": "Малиновый лимонад", "start_sec": 0.0, "end_sec": 4.0},
                "en": {"title": "Raspberry Lemonade", "start_sec": 0.0, "end_sec": 4.0},
            },
            "title_overlay_style": {
                "title_color": "#e30b5d",
                "top_margin_ratio": 0.05,
                "side_margin_ratio": 0.1,
            },
        },
    }

    assert admin_module._overlay_title_for_admin_render(run, {}, production) == {
        "ru": "Малиновый лимонад",
        "en": "Raspberry Lemonade",
    }
    assert admin_module._overlay_title_style_for_admin_render({}, production) == {
        "title_color": "#e30b5d",
        "top_margin_ratio": 0.05,
        "side_margin_ratio": 0.1,
    }


def test_django_generation_admin_keeps_only_latest_final_per_language_variant():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    latest = SimpleNamespace(language="ru", artifact_type="final_clean", pk="latest")
    older = SimpleNamespace(language="ru", artifact_type="final_clean", pk="older")
    other = SimpleNamespace(language="en", artifact_type="final_clean", pk="other")

    assert admin_module._latest_final_artifacts([latest, older, other]) == [latest, other]


def test_django_generation_admin_running_status_renders_without_format_html_args(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [])
    monkeypatch.setattr(
        admin_module,
        "_latest_final_build_statuses",
        lambda _run_id: {
            ("ru", "clean"): {
                "status": "running",
                "updated_at": "2999-01-01T00:00:00+00:00",
                "heartbeat_at": "2999-01-01T00:00:00+00:00",
                "worker_id": "test-worker",
            }
        },
    )
    monkeypatch.setattr(admin_module, "_final_target_languages_for_run", lambda _run, _artifacts, _statuses: ["ru"])

    html = str(admin_module._final_videos_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert "Build is running in final_build_worker; refresh for status." in html
    assert "Build/rebuild this video" in html


def test_django_generation_admin_stale_running_status_allows_rebuild(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    stale_status = {
        "status": "running",
        "updated_at": "2000-01-01T00:00:00+00:00",
        "language": "ru",
        "variant": "subtitled_music",
    }
    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [])
    monkeypatch.setattr(
        admin_module,
        "_latest_final_build_statuses",
        lambda _run_id: {("ru", "subtitled_music"): stale_status},
    )
    monkeypatch.setattr(admin_module, "_final_target_languages_for_run", lambda _run, _artifacts, _statuses: ["ru"])

    html = str(admin_module._final_videos_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert "stale" in html
    assert "No recent heartbeat from final_build_worker" in html
    assert "Build/rebuild this video" in html
    assert "Build is running in final_build_worker; refresh for status." not in html


def test_django_generation_admin_queue_requeues_stale_running_status(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    calls = []
    kicks = []

    monkeypatch.setattr(
        admin_module,
        "_latest_final_build_statuses",
        lambda _run_id: {
            ("ru", "subtitled_music"): {
                "status": "running",
                "updated_at": "2000-01-01T00:00:00+00:00",
            }
        },
    )
    monkeypatch.setattr(admin_module, "_set_final_build_status", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(admin_module, "_kick_final_build_worker", lambda run_id: kicks.append(run_id) or True)

    status = admin_module._queue_final_build(
        SimpleNamespace(id=run_id),
        language="ru",
        variant="subtitled_music",
    )

    assert status == "pending"
    assert calls[-1]["status"] == "pending"
    assert kicks == [run_id]


def test_django_generation_admin_bulk_final_build_kicks_after_queue(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    queue_calls = []
    kicks = []
    messages = []

    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [])
    monkeypatch.setattr(admin_module, "_latest_final_build_statuses", lambda _run_id: {})
    monkeypatch.setattr(admin_module, "_final_target_languages_for_run", lambda _run, _artifacts, _statuses: ["ru"])

    def fake_queue(run, *, language, variant, kick_worker=True):
        queue_calls.append((run.id, language, variant, kick_worker))
        return "pending"

    monkeypatch.setattr(admin_module, "_queue_final_build", fake_queue)
    monkeypatch.setattr(admin_module, "_kick_final_build_worker", lambda run_id: kicks.append(run_id) or True)

    modeladmin = SimpleNamespace(message_user=lambda _request, message, **_kwargs: messages.append(message))
    admin_module.build_final_title_and_videos(modeladmin, None, [SimpleNamespace(id=run_id)])

    assert [item[3] for item in queue_calls] == [False] * len(admin_module.FINAL_VIDEO_VARIANT_LABELS)
    assert kicks == [run_id]
    assert messages and "Queued 4 final render job(s)" in messages[-1]


def test_django_final_build_progress_differs_by_variant_stage():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    clean_progress = admin_module._final_build_stage_progress("upscale_building", "clean")
    upscale_progress = admin_module._final_build_stage_progress("upscale_building", "upscaled_4k")
    title_progress = admin_module._final_build_stage_progress("upscale_building", "titled_subtitled_music")

    assert clean_progress["percent"] > upscale_progress["percent"] > title_progress["percent"]
    assert clean_progress["label"] != title_progress["label"]


def test_django_final_build_required_variants_follow_dependency_chain():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    assert admin_module._final_build_required_variants(["clean"]) == ["clean"]
    assert admin_module._final_build_required_variants(["upscaled_4k"]) == ["clean", "upscaled_4k"]
    assert admin_module._final_build_required_variants(["subtitled_music"]) == [
        "clean",
        "upscaled_4k",
        "subtitled_music",
    ]
    assert admin_module._final_build_required_variants(["titled_subtitled_music"]) == [
        "clean",
        "upscaled_4k",
        "subtitled_music",
        "titled_subtitled_music",
    ]


def test_django_final_build_worker_command_processes_claimed_jobs(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.core.management import call_command
    import pipeline_admin.management.commands.final_build_worker as worker_module

    run_id = uuid.uuid4()
    claimed = [[
        {"run_id": str(run_id), "language": "ru", "variant": "clean"},
        {"run_id": str(run_id), "language": "ru", "variant": "upscaled_4k"},
    ]]
    processed = []

    def fake_claim(worker_id, *, run_id=None):
        return claimed.pop(0) if claimed else []

    def fake_run(run_id, language, variants, *, worker_id):
        processed.append((run_id, language, list(variants), worker_id))

    monkeypatch.setattr(worker_module, "_claim_next_final_build_batch", fake_claim)
    monkeypatch.setattr(worker_module, "_run_final_build_batch", fake_run)

    out = StringIO()
    call_command("final_build_worker", "--once", "--worker-id=test-worker", stdout=out)

    assert processed == [(str(run_id), "ru", ["clean", "upscaled_4k"], "test-worker")]
    assert "processed=2" in out.getvalue()


def test_django_admin_titled_only_build_uses_subtitled_source_fast_path(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    production = {
        "json_plan": [{"duration_sec": 8, "text": "scene"}],
        "overlay_title_by_lang": {"ru": "Борщ"},
    }
    session = {"production": production}
    calls = []

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    def fake_fast_path(scenes, **kwargs):
        calls.append({"scenes": scenes, **kwargs})
        return {"renders_by_language": {"ru": {"titled_subtitled_music": {"video_path": "/tmp/titled.mp4"}}}}

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "build_titled_post_production_from_subtitled_master", fake_fast_path)
    monkeypatch.setattr(
        admin_module,
        "build_post_production",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full build should not run")),
    )
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda _session, _post: None)

    out = admin_module._build_final_videos_for_run(
        RunStub(id=run_id, human_title=""),
        target_language="ru",
        target_variants=["titled_subtitled_music"],
        source_subtitled_video_path=Path("/tmp/source_subtitled.mp4"),
    )

    assert out["renders_by_language"]["ru"]["titled_subtitled_music"]["video_path"] == "/tmp/titled.mp4"
    assert calls[0]["subtitled_video_path"] == Path("/tmp/source_subtitled.mp4")
    assert calls[0]["language"] == "ru"


def test_django_generation_admin_shows_publication_prep_panel(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    monkeypatch.setattr(
        admin_module,
        "_publication_copy_from_run",
        lambda _run: {
            "youtube_ru_title": "RU title",
            "youtube_ru_description": "RU desc",
            "rutube_ru_title": "RT title",
            "rutube_ru_description": "RT desc",
            "instagram_ru": "IG ru",
            "vk_ru": "VK ru",
            "tiktok_ru": "TT ru",
            "instagram_en": "IG en",
            "youtube_en_title": "EN title",
            "youtube_en_description": "EN desc",
        },
    )
    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [SimpleNamespace()])

    class _EmptyJobs:
        def order_by(self, *_args):
            return []

    monkeypatch.setattr(admin_module.PublicationJob, "objects", _EmptyJobs())

    html = str(admin_module._publication_prep_panel(SimpleNamespace(id=run_id, pk=run_id)))

    assert "Queue publications" in html
    assert f"/{run_id}/queue-publications/" in html
    assert "YouTube Shorts RU" in html
    assert "Instagram EN" in html


def test_django_publication_queue_form_renders_rich_preflight(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    copy_payload = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU desc",
        "rutube_ru_title": "RT title",
        "rutube_ru_description": "RT desc",
        "instagram_ru": "IG ru",
        "vk_ru": "VK ru",
        "tiktok_ru": "TT ru",
        "instagram_en": "IG en",
        "youtube_en_title": "EN title",
        "youtube_en_description": "EN desc",
    }
    form = admin_module.PublicationQueueAdminForm(copy=copy_payload, initial={"target_keys": ["youtube:ru"]})
    monkeypatch.setattr(
        admin_module,
        "_publication_post_production_from_final_artifacts",
        lambda _run: {"renders_by_language": {"ru": {"titled_subtitled_music": {"video_path": "/tmp/ru.mp4"}}}},
    )
    monkeypatch.setattr(admin_module, "_publication_account_index", lambda _run: {})

    html = str(admin_module._publication_queue_form_html(form, SimpleNamespace(id=run_id), copy_payload))

    assert 'type="checkbox"' in html
    assert "cf-pub-badge" in html
    assert "Publication time" in html
    assert "manual account" in html
    assert "no video" in html


def test_django_generation_artifact_media_supports_byte_ranges():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.admin import _parse_http_range

    assert _parse_http_range("bytes=0-1023", 5000) == (0, 1023)
    assert _parse_http_range("bytes=1024-", 5000) == (1024, 4999)
    assert _parse_http_range("bytes=-500", 5000) == (4500, 4999)
    assert _parse_http_range("bytes=9999-", 5000) is None


def test_django_generation_artifact_media_url_falls_back_when_reverse_is_stale(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from django.urls import NoReverseMatch

    artifact_id = uuid.uuid4()
    artifact = SimpleNamespace(pk=artifact_id)
    monkeypatch.setattr(admin_module, "reverse", lambda *_args, **_kwargs: (_ for _ in ()).throw(NoReverseMatch("stale")))

    assert admin_module._artifact_media_url(artifact) == f"/admin/pipeline_admin/generationartifact/{artifact_id}/media/"


def test_django_admin_final_render_strips_inline_media_before_session_save():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    cleaned = admin_module._strip_inline_media_payloads(
        {
            "production": {
                "json_plan": [
                    {
                        "start_frame_image_b64": "huge-start-frame",
                        "image_candidates": [{"candidate_id": "img", "image_b64": "huge-image"}],
                        "video_candidates_by_lang": {
                            "ru": [{"candidate_id": "vid", "video_b64": "huge-video"}]
                        },
                    }
                ],
                "post_production": {
                    "renders_by_language": {"ru": {"clean": {"video_b64": "huge-final"}}}
                },
            }
        }
    )

    assert "image_b64" not in str(cleaned)
    assert "video_b64" not in str(cleaned)
    assert "start_frame_image_b64" not in str(cleaned)
    assert cleaned["production"]["json_plan"][0]["image_candidates"][0]["candidate_id"] == "img"
    assert cleaned["production"]["json_plan"][0]["video_candidates_by_lang"]["ru"][0]["candidate_id"] == "vid"


def test_django_admin_session_form_rejects_wrong_json_shapes():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    instance = ChatSession(
        chat_id=987654322,
        state="waiting_scene_media",
        generation_run_id=None,
        session_json={"state": "waiting_scene_media", "image_scene_index": "broken"},
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654322",
            "state": "waiting_scene_media",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": "[]",
            "recipe_full_text": "",
            "scenes_plan_json": "{}",
            "production_plan_json": "{}",
            "overlay_title_by_lang_json": "[]",
            "overlay_title_style_json": "[]",
            "scene_index": "0",
            "start_frame_prompt": "",
            "end_frame_requested": "",
            "end_frame_prompt": "",
            "video_prompt_by_lang_json": "[]",
            "speech_by_lang_json": "[]",
            "syntax_retry_context_json": "[]",
            "video_approvals_by_lang_json": "[]",
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid() is False
    assert "viral_concept_brief" in form.errors
    assert "production_plan_json" in form.errors
    assert "overlay_title_by_lang_json" in form.errors


def test_django_admin_session_form_can_move_current_media_step():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    production_plan = [
        {"scene": 1, "start_frame_prompt": "s1", "video_prompt": "v1"},
        {"scene": 2, "start_frame_prompt": "s2", "video_prompt": "v2"},
        {"scene": 3, "start_frame_prompt": "s3", "video_prompt": "v3"},
        {"scene": 4, "start_frame_prompt": "s4", "video_prompt": "v4"},
    ]
    instance = ChatSession(
        chat_id=987654323,
        state="waiting_production_bundle_approval",
        generation_run_id=None,
        session_json={
            "state": "waiting_production_bundle_approval",
            "image_scene_index": 0,
            "scene_media_substep": "start_prompt",
            "production": {"json_plan": production_plan},
            "scene_video_feedback_pending": {"scene_idx": 0},
        },
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654323",
            "state": "waiting_production_bundle_approval",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": "{}",
            "recipe_full_text": "",
            "scenes_plan_json": "[]",
            "production_plan_json": json.dumps(production_plan),
            "overlay_title_by_lang_json": "{}",
            "overlay_title_style_json": "{}",
            "scene_index": "0",
            "start_frame_prompt": "s1",
            "end_frame_requested": "",
            "end_frame_prompt": "",
            "video_prompt_by_lang_json": "{}",
            "speech_by_lang_json": "{}",
            "syntax_retry_context_json": "{}",
            "video_approvals_by_lang_json": "{}",
            "apply_media_step_override": "on",
            "media_scene_number": "4",
            "media_substep": "start_prompt",
            "media_language_index": "0",
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)

    assert saved.state == "waiting_scene_media"
    assert saved.session_json["state"] == "waiting_scene_media"
    assert saved.session_json["image_scene_index"] == 3
    assert saved.session_json["scene_media_substep"] == "start_prompt"
    assert saved.session_json["scene_media_language_index"] == 0
    assert "scene_video_feedback_pending" not in saved.session_json


def test_django_admin_json_fields_accept_decoded_values():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.models import PipelineStepCheckpoint

    field = PipelineStepCheckpoint._meta.get_field("payload_json")

    assert field.from_db_value({"ok": True}, None, None) == {"ok": True}
    assert field.from_db_value([{"ok": True}], None, None) == [{"ok": True}]


def test_django_admin_image_candidate_form_replaces_blob():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django
    from django.core.files.uploadedfile import SimpleUploadedFile

    django.setup()

    from pipeline_admin.forms import SceneImageCandidateAdminForm
    from pipeline_admin.models import SceneImageCandidate

    instance = SceneImageCandidate(candidate_id="img-1", kind="start", image_blob=b"old")
    form = SceneImageCandidateAdminForm(
        data={"kind": "start", "liked": "on", "approved_for_video": ""},
        files={"replacement_image": SimpleUploadedFile("frame.png", b"new-image", content_type="image/png")},
        instance=instance,
    )

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.image_blob == b"new-image"
    assert saved.liked is True


def test_django_admin_video_candidate_form_replaces_blob():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django
    from django.core.files.uploadedfile import SimpleUploadedFile

    django.setup()

    from pipeline_admin.forms import SceneVideoCandidateAdminForm
    from pipeline_admin.models import SceneVideoCandidate

    instance = SceneVideoCandidate(candidate_id="vid-1", language="ru", video_blob=b"old", speed_coef=1.2)
    form = SceneVideoCandidateAdminForm(
        data={"liked": "", "approved_for_production": "on", "speed_coef": "1.1"},
        files={"replacement_video": SimpleUploadedFile("scene.mp4", b"new-video", content_type="video/mp4")},
        instance=instance,
    )

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.video_blob == b"new-video"
    assert saved.approved_for_production is True
    assert saved.speed_coef == 1.1
