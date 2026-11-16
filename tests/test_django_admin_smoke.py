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


def test_django_settings_uses_sqlite_for_unsafe_database_under_pytest(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_django_admin_smoke.py::guard (call)")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/content_fabric")

    from cf_admin import settings

    database = settings._database_from_url()

    assert database["ENGINE"] == "django.db.backends.sqlite3"
    assert "pytest" in database["NAME"]


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
            "overlay_subtitle_by_lang_json": '{"ru": "КБЖУ на 100 гр: 126/10/4/24"}',
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
    assert saved.session_json["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}
    assert saved.session_json["production"]["overlay_title_style"] == {"title_color": "#c72638"}


def test_django_admin_session_form_stages_title_color_until_build():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    instance = ChatSession(
        chat_id=987654330,
        state="waiting_scene_media",
        generation_run_id=None,
        session_json={
            "state": "waiting_scene_media",
            "production": {
                "json_plan": [],
                "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
            },
            "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        },
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654330",
            "state": "waiting_scene_media",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": "{}",
            "recipe_full_text": "",
            "scenes_plan_json": "[]",
            "production_plan_json": "[]",
            "overlay_title_by_lang_json": "{}",
            "overlay_title_color": "#1a2b3c",
            "overlay_title_style_json": '{"title_color": "#c72638", "top_margin_ratio": 0.05}',
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
    saved = form.save(commit=False)

    assert saved.session_json["production"]["overlay_title_style"] == {
        "title_color": "#c72638",
        "top_margin_ratio": 0.05,
    }
    assert saved.session_json["production"]["pending_overlay_title_style"] == {"title_color": "#1a2b3c"}
    assert saved.session_json["pending_overlay_title_style"] == {"title_color": "#1a2b3c"}


def test_django_generation_admin_title_color_field_is_after_created_at():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    fields = list(admin_module.GenerationRunAdmin.fields)

    assert "overlay_title_ru" in fields
    assert "overlay_title_en" in fields
    assert "overlay_subtitle_ru" in fields
    assert "overlay_subtitle_en" in fields
    assert "human_title" not in fields
    assert fields[fields.index("created_at") + 1] == "overlay_title_color"
    assert "overlay_title_color" in admin_module.GenerationRunAdmin.readonly_fields


def test_django_generation_admin_ajax_stages_title_color_until_build(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    saved_sessions = []

    class ChatSessionStub:
        session_json = {
            "production": {
                "json_plan": [],
                "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
            },
            "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        }

        def save(self, **kwargs):
            saved_sessions.append({"session_json": self.session_json, "kwargs": kwargs})

    chat_session = ChatSessionStub()
    run = SimpleNamespace(id=run_id, title_color="#c72638", saved=[])

    def save_run(**kwargs):
        run.saved.append(kwargs)

    run.save = save_run

    class QuerySetStub:
        def order_by(self, *_args):
            return self

        def first(self):
            return chat_session

    class ManagerStub:
        def filter(self, **kwargs):
            assert kwargs == {"generation_run_id": run_id}
            return QuerySetStub()

    monkeypatch.setattr(admin_module.ChatSession, "objects", ManagerStub())

    pending = admin_module._stage_pending_title_color_for_run(run, "#1a2b3c")

    assert saved_sessions
    assert pending == {"title_color": "#1a2b3c"}
    assert run.title_color == "#1a2b3c"
    assert run.saved == [{"update_fields": ["title_color"]}]
    assert chat_session.session_json["production"]["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert "pending_overlay_title_style" not in chat_session.session_json["production"]


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


def test_django_generation_admin_syncs_changed_name_to_overlay_title():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run = SimpleNamespace(human_title="Манты")
    session = {
        "overlay_title_by_lang": {"ru": "Beshbarmak", "en": "Beshbarmak"},
        "production": {
            "overlay_title_by_lang": {"ru": "Beshbarmak", "en": "Beshbarmak"},
            "post_production": {
                "title_overlay_by_language": {
                    "ru": {"title": "Beshbarmak", "start_sec": 0.0, "end_sec": 4.0},
                    "en": {"title": "Beshbarmak", "start_sec": 0.0, "end_sec": 4.0},
                }
            },
        },
    }
    production = session["production"]

    admin_module._sync_overlay_title_from_run(session, run)
    rendered = admin_module._overlay_title_for_admin_render(run, session, production)

    assert session["overlay_title_by_lang"]["ru"] == "Манты"
    assert production["overlay_title_by_lang"]["ru"] == "Манты"
    assert rendered["ru"] == "Манты"


def test_django_generation_admin_draft_flag_creates_draft_row(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.models import GenerationRun

    created = []

    class QuerySetStub:
        def order_by(self, *_args):
            return self

        def first(self):
            return None

    class DraftManagerStub:
        def filter(self, **kwargs):
            assert kwargs["run_id"] == run.id
            return QuerySetStub()

        def create(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(**kwargs)

    run = GenerationRun(
        id=uuid.uuid4(),
        human_title="Котлеты",
        status="final_videos_delivered",
        is_draft=True,
        language="ru",
        script="recipe",
    )
    monkeypatch.setattr(admin_module.GenerationDraft, "objects", DraftManagerStub())
    monkeypatch.setattr(
        admin_module,
        "_run_session_for_admin_render",
        lambda _run: (
            None,
            {
                "state": "waiting_scene_media",
                "image_scene_index": 2,
                "video_b64": "large-video",
                "production": {
                    "json_plan": [
                        {
                            "scene": 1,
                            "image_b64": "large-image",
                            "video_candidates_by_lang": {
                                "ru": [
                                    {
                                        "candidate_id": "approved",
                                        "approved_for_production": True,
                                        "video_prompt_by_lang": {"ru": "candidate prompt"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
            {},
        ),
    )

    draft = admin_module._ensure_generation_draft_for_admin_run(run)

    assert draft is not None
    assert created[0]["run"] is run
    assert created[0]["title"] == "Котлеты"
    assert created[0]["stage"] == "waiting_scene_media"
    assert created[0]["scene_index"] == 2
    assert created[0]["language"] == "ru"
    assert created[0]["session_snapshot_json"] == {}


def test_django_generation_admin_form_saves_ru_and_en_overlay_titles(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.models import GenerationRun

    class QuerySetStub:
        def order_by(self, *_args):
            return self

        def first(self):
            return None

    class ManagerStub:
        def filter(self, **_kwargs):
            return QuerySetStub()

    monkeypatch.setattr(admin_module.ChatSession, "objects", ManagerStub())

    run = GenerationRun(
        id=uuid.uuid4(),
        human_title="Beshbarmak",
        status="final_videos_delivered",
        language="ru",
        script="recipe",
    )
    form = admin_module.GenerationRunAdminForm(
        data={
            "overlay_title_ru": " Бешбармак ",
            "overlay_title_en": " Beshbarmak Feast ",
            "overlay_subtitle_ru": " КБЖУ на 100 гр: 126/10/4/24 ",
            "overlay_subtitle_en": " per 100g: 126/10/4/24 ",
            "status": run.status,
            "is_active": "on",
            "id": str(run.id),
            "language": run.language,
            "script": run.script,
            "title_color": "#e2b512",
            "created_at": "2026-07-01 12:00:00",
            "current_scene_index": "",
            "current_media_substep": "",
            "current_media_language_index": "",
            "series": "",
            "series_video_index": "",
            "series_total_videos": "",
            "scheduled_item": "",
        },
        instance=run,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)

    assert saved.human_title == "Бешбармак"
    assert form.overlay_title_by_lang() == {"ru": "Бешбармак", "en": "Beshbarmak Feast"}
    assert form.overlay_subtitle_by_lang() == {
        "ru": "КБЖУ на 100 гр: 126/10/4/24",
        "en": "per 100g: 126/10/4/24",
    }


def test_django_generation_admin_title_subtitle_override_survives_without_chat_session(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    saved_subtitle = {"ru": "КБЖУ на 100 гр: 126/10/4/24", "en": "per 100g: 126/10/4/24"}
    artifact = SimpleNamespace(payload_json={"status": "active", "overlay_subtitle_by_lang": saved_subtitle})

    class EmptyChatQuerySet:
        def order_by(self, *_args):
            return self

        def first(self):
            return None

    class ChatManagerStub:
        def filter(self, **kwargs):
            assert kwargs == {"generation_run_id": run_id}
            return EmptyChatQuerySet()

    class ArtifactQuerySet(list):
        def order_by(self, *_args):
            return self

    class ArtifactManagerStub:
        def filter(self, **kwargs):
            if kwargs.get("artifact_type") == admin_module.TITLE_SUBTITLE_OVERRIDE_ARTIFACT_TYPE:
                assert kwargs == {
                    "run_id": run_id,
                    "artifact_type": admin_module.TITLE_SUBTITLE_OVERRIDE_ARTIFACT_TYPE,
                    "uri": admin_module._title_subtitle_override_uri(run_id),
                }
                return ArtifactQuerySet([artifact])
            raise AssertionError(kwargs)

    monkeypatch.setattr(admin_module.ChatSession, "objects", ChatManagerStub())
    monkeypatch.setattr(admin_module.GenerationArtifact, "objects", ArtifactManagerStub())

    run = SimpleNamespace(id=run_id, human_title="Бешбармак")

    assert admin_module._overlay_subtitle_for_admin_edit(run) == saved_subtitle
    assert admin_module._overlay_subtitle_for_admin_render(run, {}, {}) == saved_subtitle


def test_django_generation_admin_recovers_story_alignment_from_narration_artifact(monkeypatch, tmp_path):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    alignment = {
        "characters": list("Тест"),
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4],
    }
    artifact = SimpleNamespace(
        language="ru",
        uri=audio_path.as_uri(),
        payload_json={
            "voiceover_text": "Тест",
            "duration_sec": 0.4,
            "alignment": alignment,
        },
    )

    class ArtifactQuerySet(list):
        def order_by(self, *_args):
            return self

    class ArtifactManagerStub:
        def filter(self, **kwargs):
            assert kwargs == {
                "run_id": run_id,
                "artifact_type": "story_narration_audio",
            }
            return ArtifactQuerySet([artifact])

    run_id = uuid.uuid4()
    monkeypatch.setattr(admin_module.GenerationArtifact, "objects", ArtifactManagerStub())

    paths, voiceovers, durations, alignments = admin_module._story_narration_for_admin_render(
        SimpleNamespace(id=run_id)
    )

    assert paths == {"ru": str(audio_path)}
    assert voiceovers == {"ru": "Тест"}
    assert durations == {"ru": 0.4}
    assert alignments == {"ru": alignment}


def test_django_generation_admin_title_override_survives_without_chat_session(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    saved_title = {"ru": "Бешбармак", "en": "Beshbarmak Feast"}
    artifact = SimpleNamespace(payload_json={"status": "active", "overlay_title_by_lang": saved_title})

    class EmptyChatQuerySet:
        def order_by(self, *_args):
            return self

        def first(self):
            return None

    class ChatManagerStub:
        def filter(self, **kwargs):
            assert kwargs == {"generation_run_id": run_id}
            return EmptyChatQuerySet()

    class ArtifactQuerySet(list):
        def order_by(self, *_args):
            return self

    class ArtifactManagerStub:
        def filter(self, **kwargs):
            if kwargs.get("artifact_type") == admin_module.TITLE_OVERRIDE_ARTIFACT_TYPE:
                assert kwargs == {
                    "run_id": run_id,
                    "artifact_type": admin_module.TITLE_OVERRIDE_ARTIFACT_TYPE,
                    "uri": admin_module._title_override_uri(run_id),
                }
                return ArtifactQuerySet([artifact])
            if kwargs.get("artifact_type") == admin_module.OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE:
                return ArtifactQuerySet([])
            raise AssertionError(kwargs)

    monkeypatch.setattr(admin_module.ChatSession, "objects", ChatManagerStub())
    monkeypatch.setattr(admin_module.GenerationArtifact, "objects", ArtifactManagerStub())

    run = SimpleNamespace(id=run_id, human_title="Бешбармак")

    assert admin_module._overlay_title_for_admin_edit(run) == saved_title
    assert admin_module._overlay_title_for_admin_render(run, {}, {}) == saved_title


def test_django_generation_admin_uses_stored_overlay_title_artifact(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    saved_title = {"ru": "Кабачковая икра", "en": "Zucchini Caviar"}
    artifact = SimpleNamespace(payload_json={"overlay_title_by_lang": saved_title})

    class EmptyChatQuerySet:
        def order_by(self, *_args):
            return self

        def first(self):
            return None

    class ChatManagerStub:
        def filter(self, **kwargs):
            assert kwargs == {"generation_run_id": run_id}
            return EmptyChatQuerySet()

    class ArtifactQuerySet(list):
        def order_by(self, *_args):
            return self

    class ArtifactManagerStub:
        def filter(self, **kwargs):
            if kwargs.get("artifact_type") == admin_module.OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE:
                assert kwargs == {
                    "run_id": run_id,
                    "artifact_type": admin_module.OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE,
                    "uri": admin_module._overlay_title_by_lang_artifact_uri(run_id),
                    "language__isnull": True,
                }
                return ArtifactQuerySet([artifact])
            if kwargs.get("artifact_type") == admin_module.TITLE_OVERRIDE_ARTIFACT_TYPE:
                return ArtifactQuerySet([])
            raise AssertionError(kwargs)

    monkeypatch.setattr(admin_module.ChatSession, "objects", ChatManagerStub())
    monkeypatch.setattr(admin_module.GenerationArtifact, "objects", ArtifactManagerStub())

    run = SimpleNamespace(id=run_id, human_title="Кабачковая икра")

    assert admin_module._overlay_title_for_admin_edit(run) == saved_title
    assert admin_module._overlay_title_for_admin_render(run, {}, {}) == saved_title


def test_django_generation_admin_inlines_render_hidden_primary_keys():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    scene_form = admin_module.SceneInlineForm()
    artifact_form = admin_module.GenerationArtifactInlineForm()

    assert scene_form.fields["id"].widget.is_hidden
    assert artifact_form.fields["id"].widget.is_hidden
    assert "id" in admin_module.SceneInline.fields
    assert "id" in admin_module.GenerationArtifactInline.fields


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


def test_django_admin_series_overlay_counter_uses_current_series_day():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    series = SimpleNamespace(current_video_index=6, total_videos=10)
    run = SimpleNamespace(series=series, series_video_index=7, series_total_videos=10)

    counter = admin_module._overlay_series_counter_for_admin_render(
        run,
        session={"series_counter_by_lang": {"ru": "день 7 из 10", "en": "day 7 of 10"}},
        production={"series_counter_by_lang": {"ru": "день 7 из 10", "en": "day 7 of 10"}},
    )

    assert counter == {"ru": "день 6 из 10", "en": "day 6 of 10"}


def test_django_admin_series_overlay_counter_supports_story_label():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    series = SimpleNamespace(
        current_video_index=8,
        total_videos=10,
        meta_json={"counter_label_ru": "история", "counter_label_en": "story"},
    )

    assert admin_module._series_counter_by_lang_from_series(series) == {
        "ru": "история 8 из 10",
        "en": "story 8 of 10",
    }


def test_django_admin_series_context_sync_uses_current_series_day():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    series = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000006"),
        title_ru="Каюта вкусов",
        title_en="Taste Cabin",
        current_video_index=6,
        total_videos=10,
    )
    run = SimpleNamespace(series=series, series_video_index=7, series_total_videos=10)
    session = {"series_counter_by_lang": {"ru": "день 7 из 10", "en": "day 7 of 10"}}

    admin_module._sync_series_context_from_run(session, run)

    assert session["series_video_index"] == 6
    assert session["series_counter_by_lang"] == {"ru": "день 6 из 10", "en": "day 6 of 10"}
    assert session["series"]["counter_by_lang"] == {"ru": "день 6 из 10", "en": "day 6 of 10"}


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
        SceneInline,
        VideoOverviewAdmin,
    )
    from pipeline_admin.models import PublicationJob
    from django.contrib.admin.sites import AdminSite

    assert "series" in GenerationRunAdmin.list_editable
    assert "series_video_index" in GenerationRunAdmin.list_editable
    assert "series_total_videos" in GenerationRunAdmin.list_editable
    assert GenerationRunAdmin.autocomplete_fields == ("series",)
    assert GenerationRunAdmin.list_select_related == ("project", "series")
    assert GenerationRunAdmin.list_per_page == 50
    assert GenerationRunAdmin.show_full_result_count is False
    assert GenerationRunAdmin.inlines == (SceneInline,)
    assert "artifacts_link" in GenerationRunAdmin.readonly_fields
    assert "current_video_index" in ContentSeriesAdmin.list_editable
    assert "total_videos" in ContentSeriesAdmin.list_editable
    assert "is_active" in ContentSeriesAdmin.list_editable
    assert "build_final_title_and_videos" in [action.__name__ for action in GenerationRunAdmin.actions]
    assert "build_final_title_and_videos" in [action.__name__ for action in VideoOverviewAdmin.actions]
    assert "build_final_title_and_videos" in [action.__name__ for action in ChatSessionAdmin.actions]
    assert "publication_prep" in GenerationRunAdmin.readonly_fields
    assert "publication_prep" in GenerationRunAdmin.fields
    assert "status" in PublicationJobAdmin.list_editable
    assert "monitor_link" in PublicationJobAdmin.list_display
    assert "is_active" in ProjectPublishAccountAdmin.list_editable
    publication_admin = PublicationJobAdmin(PublicationJob, AdminSite())
    custom_url_names = {url.name for url in publication_admin.get_urls() if url.name}
    assert "pipeline_admin_publicationjob_youtube_oauth" in custom_url_names
    assert "pipeline_admin_publicationjob_youtube_oauth_callback" in custom_url_names
    assert "pipeline_admin_publicationjob_tiktok_oauth" in custom_url_names
    assert "pipeline_admin_publicationjob_tiktok_creator_info" in custom_url_names
    assert "pipeline_admin_publicationjob_tiktok_oauth_callback" in custom_url_names
    assert "pipeline_admin_publicationjob_monitor" in custom_url_names


def test_tiktok_oauth_callback_uses_reachable_loopback_admin_host():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.test import RequestFactory
    from pipeline_admin.admin import _local_oauth_callback_url

    callback_path = "/admin/pipeline_admin/publicationjob/tiktok-oauth/callback/"
    request = RequestFactory().get("/", HTTP_HOST="127.0.0.1:8000")
    assert _local_oauth_callback_url(request, callback_path) == f"http://127.0.0.1:8000{callback_path}"

    request = RequestFactory().get("/", HTTP_HOST="localhost:8000")
    assert _local_oauth_callback_url(request, callback_path) == f"http://localhost:8000{callback_path}"

    request = RequestFactory().get("/", HTTP_HOST="localhost:9000")
    assert _local_oauth_callback_url(request, callback_path) == f"http://127.0.0.1:8000{callback_path}"


def test_tiktok_oauth_setup_allows_reusing_saved_client_secret():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.admin import TikTokOAuthSetupAdminForm

    form = TikTokOAuthSetupAdminForm(
        data={"project_slug": "grandma_kitchen", "client_key": "client-key", "client_secret": ""},
        project_choices=(("grandma_kitchen", "Grandma Kitchen"),),
    )
    assert form.is_valid()
    assert form.cleaned_data["client_secret"] == ""


def test_youtube_oauth_setup_allows_reusing_saved_client_secret():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.admin import YouTubeOAuthSetupAdminForm

    form = YouTubeOAuthSetupAdminForm(
        data={
            "project_slug": "grandma_kitchen",
            "language": "en",
            "client_id": "client-id",
            "client_secret": "",
        },
        project_choices=(("grandma_kitchen", "Grandma Kitchen"),),
    )
    assert form.is_valid()
    assert form.cleaned_data["client_secret"] == ""


def test_youtube_oauth_reuses_secret_only_for_matching_client_id(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    values = {
        "YOUTUBE_CLIENT_ID_RU": "shared-client",
        "YOUTUBE_CLIENT_SECRET_RU": "shared-secret",
    }
    monkeypatch.setattr(admin_module, "project_env", lambda _project, key: values.get(key, ""))

    assert (
        admin_module._youtube_reusable_client_secret("grandma_kitchen", "shared-client", "en")
        == "shared-secret"
    )
    assert admin_module._youtube_reusable_client_secret("grandma_kitchen", "different-client", "en") == ""


def test_youtube_oauth_callback_works_across_browser_profiles(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.contrib.admin.sites import AdminSite
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    import pipeline_admin.admin as admin_module
    from pipeline_admin.models import PublicationJob

    class OAuthSession(dict):
        rows = {}
        sequence = 0

        def __init__(self, session_key=None):
            super().__init__(self.rows.get(session_key, {}))
            self.session_key = session_key

        def set_expiry(self, _seconds):
            pass

        def save(self):
            if not self.session_key:
                type(self).sequence += 1
                self.session_key = f"oauth-state-{self.sequence}"
            self.rows[self.session_key] = dict(self)

        def delete(self):
            self.rows.pop(self.session_key, None)

    initiating_request = SimpleNamespace(session=OAuthSession())
    state = admin_module._save_youtube_oauth_flow(
        initiating_request,
        {
            "project_slug": "grandma_kitchen",
            "language": "en",
            "client_id": "client-id",
            "redirect_uri": "http://127.0.0.1:8000/youtube/callback/",
            "nonce": "nonce",
        }
    )
    saved = {}
    monkeypatch.setattr(
        admin_module,
        "project_env",
        lambda _project, key: "client-secret" if key == "YOUTUBE_CLIENT_SECRET_EN" else "",
    )
    monkeypatch.setattr(
        admin_module,
        "exchange_youtube_code",
        lambda **_kwargs: SimpleNamespace(access_token="access-token", refresh_token="refresh-token"),
    )
    monkeypatch.setattr(
        admin_module,
        "youtube_channels",
        lambda _token: [{"id": "channel-en", "title": "Grandma Kitchen"}],
    )
    monkeypatch.setattr(
        admin_module,
        "set_project_secrets",
        lambda project, values: saved.update(project=project, values=values),
    )
    request = RequestFactory().get(
        "/admin/pipeline_admin/publicationjob/youtube-oauth/callback/",
        {"state": state, "code": "authorization-code"},
    )
    request.user = AnonymousUser()
    request.session = OAuthSession()

    response = admin_module.PublicationJobAdmin(PublicationJob, AdminSite()).youtube_oauth_callback_view(request)

    assert response.status_code == 200
    assert b"YouTube EN connected for grandma_kitchen" in response.content
    assert saved == {
        "project": "grandma_kitchen",
        "values": {
            "YOUTUBE_REFRESH_TOKEN_EN": "refresh-token",
            "YOUTUBE_CHANNEL_ID_EN": "channel-en",
        },
    }


def test_generation_run_admin_artifact_count_uses_queryset_annotation():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.contrib.admin.sites import AdminSite
    from pipeline_admin.admin import GenerationRunAdmin
    from pipeline_admin.models import GenerationRun

    model_admin = GenerationRunAdmin(GenerationRun, AdminSite())
    run = SimpleNamespace(_artifact_count=7)
    queryset = model_admin.get_queryset(SimpleNamespace(GET={}))

    assert "_artifact_count" in queryset.query.annotations
    assert model_admin.artifact_count(run) == 7


def test_generation_run_admin_display_session_skips_blobs_and_is_cached(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    calls = []
    expected = (None, {"production": {}}, {})

    def fake_render(run, *, include_media_blobs=True):
        calls.append((run.id, include_media_blobs))
        return expected

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", fake_render)
    run = SimpleNamespace(id=uuid.uuid4())

    assert admin_module._run_session_for_admin_display(run) is expected
    assert admin_module._run_session_for_admin_display(run) is expected
    assert calls == [(run.id, False)]


def test_django_content_series_admin_accepts_empty_meta_json():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory
    from pipeline_admin.admin import ContentSeriesAdmin
    from pipeline_admin.models import ContentSeries

    series = ContentSeries(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        slug="karta-vkusov",
        title_ru="Карта вкусов",
        title_en="Flavor map",
        current_video_index=2,
        total_videos=10,
        is_active=True,
        meta_json={},
    )
    model_admin = ContentSeriesAdmin(ContentSeries, AdminSite())
    request = RequestFactory().post("/")
    form_class = model_admin.get_form(request, series)
    form = form_class(
        data={
            "project_id": str(series.project_id),
            "slug": series.slug,
            "title_ru": series.title_ru,
            "title_en": series.title_en,
            "current_video_index": str(series.current_video_index),
            "total_videos": str(series.total_videos),
            "is_active": "on",
            "notes": "",
            "meta_json": "{}",
        },
        instance=series,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["meta_json"] == {}


def test_django_scene_admin_speech_accepts_plain_text():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django import forms
    from pipeline_admin.forms import _speech_by_lang_from_admin_text

    assert _speech_by_lang_from_admin_text(
        " Новая речь сцены ",
        existing={"ru": "Старая речь", "en": "Old speech"},
    ) == {"ru": "Новая речь сцены", "en": "Old speech"}
    assert _speech_by_lang_from_admin_text('{"ru": "Новая речь", "en": "New speech"}') == {
        "ru": "Новая речь",
        "en": "New speech",
    }
    with pytest.raises(forms.ValidationError):
        _speech_by_lang_from_admin_text('{"ru": ')


def test_django_generation_run_scene_inline_speech_preserves_en(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.models import GenerationRun, Scene

    run = GenerationRun(id=uuid.uuid4(), human_title="Лагман", status="ok", language="ru", script="Лагман")
    scene = Scene(
        id=uuid.uuid4(),
        run=run,
        scene_order=6,
        duration_sec=6.0,
        text="old text",
        speech_by_lang={},
    )
    scene._state.adding = False
    monkeypatch.setattr(
        admin_module,
        "_latest_scene_payload_for_admin",
        lambda _scene: {"speech_by_lang": {"ru": "Старая речь", "en": "Old speech"}},
    )
    form = admin_module.SceneInlineForm(
        data={
            "id": str(scene.id),
            "scene_order": "6",
            "duration_sec": "6.0",
            "text": "",
            "speech_ru": "Новая речь",
            "speech_en": "Old speech",
        },
        instance=scene,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.text == "old text"
    assert saved.speech_by_lang == {"ru": "Новая речь", "en": "Old speech"}
    assert saved._scene_text_revision_changes == [
        {
            "field_name": "speech",
            "language": "ru",
            "old_text": "Старая речь",
            "new_text": "Новая речь",
        }
    ]


def test_django_generation_run_scene_inline_revision_sets_created_at(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    captured = {}

    monkeypatch.setattr(
        admin_module.SceneTextRevision.objects,
        "create",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(**kwargs),
    )

    scene = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        scene_order=1,
    )

    revision = admin_module._record_scene_text_revision(
        scene,
        field_name="speech",
        language="ru",
        old_text="Старая речь",
        new_text="Новая речь",
        source="generation_run_admin_inline",
    )

    assert revision is not None
    assert captured["created_at"].tzinfo is not None
    assert captured["field_name"] == "speech"
    assert captured["language"] == "ru"


def test_django_admin_final_build_scenes_prefer_page_speech_over_candidate_snapshot(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    scene_row = SimpleNamespace(
        scene_order=1,
        text="Текст на странице",
        duration_sec=6.0,
        start_frame_prompt=None,
        start_frame_prompt_effective=None,
        end_frame_prompt=None,
        video_prompt_by_lang={},
        speech_by_lang={"ru": "Новая речь со страницы"},
        syntax_prompt_by_lang={},
        prompt_meta_json={},
    )

    class _Rows:
        def order_by(self, *_args):
            return [scene_row]

    monkeypatch.setattr(admin_module.Scene.objects, "filter", lambda **_kwargs: _Rows())

    scenes = [
        {
            "scene": 1,
            "text": "Старый текст плана",
            "duration_sec": 5.0,
            "speech_by_lang": {"ru": "Старая речь плана"},
            "speech_cues_by_lang": {"ru": [{"text": "ОК", "start_sec": 0.0, "end_sec": 6.0}]},
            "video_candidates_by_lang": {
                "ru": [
                    {
                        "candidate_id": "old",
                        "approved_for_production": True,
                        "speech_by_lang": {"ru": "ОК"},
                        "speech_cues_by_lang": {
                            "ru": [{"text": "ОК", "start_sec": 0.0, "end_sec": 6.0}]
                        },
                        "video_prompt_by_lang": {"ru": "CHARACTERS SPEAKS: ОК"},
                    }
                ]
            },
        }
    ]

    out = admin_module._admin_final_build_scenes_from_page(SimpleNamespace(id=uuid.uuid4()), scenes)

    assert out[0]["text"] == "Текст на странице"
    assert out[0]["speech_by_lang"] == {"ru": "Новая речь со страницы"}
    assert "speech_cues_by_lang" not in out[0]
    candidate = out[0]["video_candidates_by_lang"]["ru"][0]
    assert candidate["speech_by_lang"] == {"ru": "ОК"}
    assert candidate["speech_cues_by_lang"] == {
        "ru": [{"text": "ОК", "start_sec": 0.0, "end_sec": 6.0}]
    }
    assert candidate["video_prompt_by_lang"] == {"ru": "CHARACTERS SPEAKS: ОК"}


def test_django_admin_final_build_scenes_restore_missing_language_subtitles_from_candidate(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    scene_row = SimpleNamespace(
        scene_order=1,
        text="Русский текст",
        duration_sec=6.0,
        start_frame_prompt=None,
        start_frame_prompt_effective=None,
        end_frame_prompt=None,
        video_prompt_by_lang={},
        speech_by_lang={},
        syntax_prompt_by_lang={},
        prompt_meta_json={},
    )

    class _Rows:
        def order_by(self, *_args):
            return [scene_row]

    monkeypatch.setattr(admin_module.Scene.objects, "filter", lambda **_kwargs: _Rows())

    scenes = [
        {
            "scene": 1,
            "text": "Старый текст плана",
            "duration_sec": 5.0,
            "video_candidates_by_lang": {
                "en": [
                    {
                        "candidate_id": "old",
                        "approved_for_production": False,
                        "speech_by_lang": {"en": "Old English speech"},
                    },
                    {
                        "candidate_id": "approved",
                        "approved_for_production": True,
                        "speech_by_lang": {"en": "Current English speech"},
                        "speech_cues_by_lang": {
                            "en": [{"text": "Current English speech", "start_sec": 0.0, "end_sec": 6.0}]
                        },
                        "video_prompt_by_lang": {"en": "CHARACTERS SPEAKS: Current English speech"},
                    },
                ]
            },
        }
    ]

    out = admin_module._admin_final_build_scenes_from_page(SimpleNamespace(id=uuid.uuid4()), scenes)

    assert out[0]["speech_by_lang"] == {"en": "Current English speech"}
    assert out[0]["speech_cues_by_lang"] == {
        "en": [{"text": "Current English speech", "start_sec": 0.0, "end_sec": 6.0}]
    }
    assert out[0]["video_prompt_by_lang"] == {"en": "CHARACTERS SPEAKS: Current English speech"}
    approved = out[0]["video_candidates_by_lang"]["en"][1]
    assert approved["speech_by_lang"] == {"en": "Current English speech"}
    assert approved["speech_cues_by_lang"] == {
        "en": [{"text": "Current English speech", "start_sec": 0.0, "end_sec": 6.0}]
    }
    assert approved["video_prompt_by_lang"] == {"en": "CHARACTERS SPEAKS: Current English speech"}


def test_django_admin_backfills_actual_spoken_words_for_approved_candidate(monkeypatch, tmp_path):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    video_path = tmp_path / "approved.mp4"
    video_path.write_bytes(b"video")
    candidate = {
        "candidate_id": "approved",
        "approved_for_production": True,
        "media_uri": video_path.as_uri(),
        "speech_by_lang": {"ru": "Старый плановый текст"},
    }
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6.0,
            "speech_by_lang": {"ru": "Текст страницы"},
            "video_candidates_by_lang": {"ru": [candidate]},
        }
    ]
    captured = {}

    class _Rows:
        def update(self, **kwargs):
            captured.update(kwargs)
            return 1

    monkeypatch.setattr(admin_module.SceneVideoCandidate.objects, "filter", lambda **_kwargs: _Rows())
    alignment_calls = []

    def fake_align_video_speech(**kwargs):
        alignment_calls.append(kwargs)
        return SimpleNamespace(
            recognized_text="Фактически произнесено здесь.",
            word_timings=[
                {"word": "Фактически", "start_sec": 0.7, "end_sec": 1.2},
                {"word": "произнесено", "start_sec": 1.2, "end_sec": 1.8},
                {"word": "здесь.", "start_sec": 1.8, "end_sec": 2.2},
            ],
            confidence=0.91,
            billed_duration_sec=3.0,
            provider="google_cloud_speech_v1",
        )

    monkeypatch.setattr(
        admin_module,
        "align_video_speech",
        fake_align_video_speech,
    )

    usage_rows = []
    create_attempts = []

    def create_usage(**kwargs):
        assert kwargs.get("created_at") is not None
        create_attempts.append(kwargs)
        if len(create_attempts) == 1:
            raise RuntimeError("usage storage unavailable")
        usage_rows.append(kwargs)

    monkeypatch.setattr(
        admin_module.GenerationArtifact.objects,
        "filter",
        lambda **_kwargs: SimpleNamespace(exists=lambda: bool(usage_rows)),
    )
    monkeypatch.setattr(admin_module.GenerationArtifact.objects, "create", create_usage)
    run = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(RuntimeError, match="usage storage unavailable"):
        admin_module._ensure_approved_candidate_word_timings(scenes, "ru", run=run)

    # Retry after a bookkeeping failure must use the saved recognition result,
    # restore its usage entry, and never bill the same audio again.
    aligned_at = candidate["video_speech_qa_by_lang"]["ru"]["aligned_at"]
    admin_module._ensure_approved_candidate_word_timings(scenes, "ru", run=run)
    admin_module._ensure_approved_candidate_word_timings(scenes, "ru", run=run)
    assert len(usage_rows) == 1
    assert usage_rows[0]["created_at"].isoformat() == aligned_at
    assert usage_rows[0]["payload_json"]["billed_duration_sec"] == 3.0

    assert candidate["speech_by_lang"]["ru"] == "Фактически произнесено здесь."
    cues = candidate["speech_cues_by_lang"]["ru"]
    assert cues[-1]["text"] == "Фактически произнесено здесь."
    assert all(cue["word_synced"] is True for cue in cues)
    assert all(cue["story_timing_source"] == "google_cloud_speech_word_offsets" for cue in cues)
    assert captured["speech_by_lang"]["ru"] == "Фактически произнесено здесь."
    assert captured["video_speech_qa_by_lang"]["ru"]["confidence"] == 0.91
    assert captured["video_speech_qa_by_lang"]["ru"]["billed_duration_sec"] == 3.0
    assert captured["video_speech_qa_by_lang"]["ru"]["timing_provider"] == "google_cloud_speech_v1"
    assert alignment_calls == [{"video_path": video_path, "language": "ru"}]


def test_django_admin_does_not_transcribe_guest_candidates(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    monkeypatch.setattr(
        admin_module,
        "align_video_speech",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("guest audio must not be transcribed")),
    )

    admin_module._ensure_approved_candidate_word_timings(
        [
            {
                "scene": 1,
                "guest_mode": True,
                "video_candidates_by_lang": {
                    "ru": [
                        {
                            "candidate_id": "guest",
                            "approved_for_production": True,
                            "speech_by_lang": {"ru": "Не должно попасть в субтитры"},
                        }
                    ]
                },
            }
        ],
        "ru",
    )


def test_django_generation_run_scene_inline_shows_ru_text_fallback_and_saves_en(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.models import GenerationRun, Scene

    run = GenerationRun(id=uuid.uuid4(), human_title="Лагман", status="ok", language="ru", script="Лагман")
    scene = Scene(
        id=uuid.uuid4(),
        run=run,
        scene_order=1,
        duration_sec=6.0,
        text="Русский текст из Text",
        speech_by_lang={},
    )
    scene._state.adding = False
    monkeypatch.setattr(
        admin_module,
        "_latest_scene_payload_for_admin",
        lambda _scene: {"speech_by_lang": {"en": "Existing English"}},
    )
    initial_form = admin_module.SceneInlineForm(instance=scene)

    assert initial_form.fields["speech_ru"].initial == "Русский текст из Text"
    assert initial_form.fields["speech_en"].initial == "Existing English"

    form = admin_module.SceneInlineForm(
        data={
            "id": str(scene.id),
            "scene_order": "1",
            "duration_sec": "6.0",
            "text": "Русский текст из Text",
            "speech_ru": "Новая русская речь",
            "speech_en": "New English speech",
        },
        instance=scene,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.speech_by_lang == {"ru": "Новая русская речь", "en": "New English speech"}
    assert saved._scene_text_revision_changes == [
        {
            "field_name": "speech",
            "language": "ru",
            "old_text": "Русский текст из Text",
            "new_text": "Новая русская речь",
        },
        {
            "field_name": "speech",
            "language": "en",
            "old_text": "Existing English",
            "new_text": "New English speech",
        },
    ]


def test_django_admin_scene_sync_updates_chat_session_payload(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    scene_id = uuid.uuid4()
    scene = SimpleNamespace(
        id=scene_id,
        run_id=run_id,
        scene_order=1,
        text="Новый текст",
        duration_sec=7.0,
        start_frame_prompt="new start",
        start_frame_prompt_effective="new effective",
        end_frame_prompt="new end",
        video_prompt_by_lang={"ru": "new video prompt"},
        speech_by_lang={"ru": "new speech"},
        syntax_prompt_by_lang={"ru": "syntax"},
        prompt_meta_json={"source": "admin"},
    )
    chat_session = SimpleNamespace(
        session_json={
            "production": {
                "json_plan": [
                    {
                        "scene": 1,
                        "text": "Старый текст",
                        "video_candidates_by_lang": {
                            "ru": [{"candidate_id": "vid", "approved_for_production": True}]
                        },
                    }
                ]
            },
            "scene_db_ids_by_index": {0: str(scene_id)},
            "image_scene_index": 0,
        },
        generation_run_id=run_id,
        updated_at=None,
    )
    saved = []

    def save(**kwargs):
        saved.append(kwargs)

    chat_session.save = save

    class SceneRows:
        def order_by(self, *_args):
            return [scene]

    class ChatRows:
        def order_by(self, *_args):
            return [chat_session]

    monkeypatch.setattr(admin_module.Scene.objects, "filter", lambda **_kwargs: SceneRows())
    monkeypatch.setattr(admin_module.ChatSession.objects, "filter", lambda **_kwargs: ChatRows())

    updated = admin_module._sync_chat_sessions_for_run_scenes(run_id)

    assert updated == 1
    synced_scene = chat_session.session_json["production"]["json_plan"][0]
    assert synced_scene["text"] == "Новый текст"
    assert synced_scene["duration_sec"] == 7.0
    assert synced_scene["start_frame_prompt"] == "new start"
    assert synced_scene["video_prompt_by_lang"] == {"ru": "new video prompt"}
    assert synced_scene["speech_by_lang"] == {"ru": "new speech"}
    assert synced_scene["video_candidates_by_lang"]["ru"][0]["candidate_id"] == "vid"
    assert chat_session.session_json["scene_db_ids_by_index"] == {"0": str(scene_id)}
    assert saved and saved[0]["update_fields"] == ["session_json", "generation_run_id", "updated_at"]


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
    assert "Синхронизировать и пересобрать субтитры" in html
    assert "Первая сборка всегда остаётся бесплатной" in html
    assert html.index("Build/rebuild this video") < html.index("Синхронизировать и пересобрать субтитры")
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
    assert calls[-1]["extra_payload"] == {"synchronize_subtitles": False}
    assert kicks == [run_id]


def test_django_generation_admin_sync_subtitles_queues_only_selected_languages(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module
    from pipeline_admin.admin import GenerationRunAdmin

    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, pk=run_id, workflow_type="")
    queue_calls = []
    kicks = []

    class PostData:
        @staticmethod
        def getlist(_key):
            return ["ru", "de"]

    request = SimpleNamespace(method="POST", POST=PostData())
    modeladmin = SimpleNamespace(get_object=lambda _request, _object_id: run)
    monkeypatch.setattr(admin_module, "_final_artifacts_for_run", lambda _run_id: [])
    monkeypatch.setattr(admin_module, "_latest_final_build_statuses", lambda _run_id: {})
    monkeypatch.setattr(admin_module, "_final_target_languages_for_run", lambda *_args: ["ru", "en"])
    monkeypatch.setattr(admin_module, "_story_alignment_counts_for_run", lambda _run: (0, 0))

    def fake_queue(_run, **kwargs):
        queue_calls.append(kwargs)
        return "pending"

    monkeypatch.setattr(admin_module, "_queue_final_build", fake_queue)
    monkeypatch.setattr(admin_module, "_kick_final_build_worker", lambda value: kicks.append(value) or True)

    response = GenerationRunAdmin.sync_subtitles_view(modeladmin, request, str(run_id))
    payload = json.loads(response.content)

    assert response.status_code == 200
    assert payload["languages"] == ["ru"]
    assert [call["variant"] for call in queue_calls] == ["subtitled_music", "titled_subtitled_music"]
    assert all(call["language"] == "ru" for call in queue_calls)
    assert all(call["synchronize_subtitles"] is True for call in queue_calls)
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
        "overlay_subtitle_by_lang": {"ru": "КБЖУ на 100 гр: 126/10/4/24"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c"},
    }
    session = {
        "production": production,
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c"},
    }
    calls = []
    monkeypatch.setattr(admin_module, "apply_story_word_synced_subtitles",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("title build must preserve subtitle timings")))

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    def fake_fast_path(scenes, **kwargs):
        calls.append({"scenes": scenes, **kwargs})
        return {"renders_by_language": {"ru": {"titled_subtitled_music": {"video_path": "/tmp/titled.mp4"}}}}

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "_subtitled_artifact_matches_current_subtitles", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(admin_module, "build_titled_post_production_from_subtitled_master", fake_fast_path)
    monkeypatch.setattr(
        admin_module,
        "build_post_production",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full build should not run")),
    )
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda _session, _post: None)
    monkeypatch.setattr(
        admin_module,
        "_ensure_approved_candidate_word_timings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("title-only build must never call Speech-to-Text")
        ),
    )

    out = admin_module._build_final_videos_for_run(
        RunStub(id=run_id, human_title=""),
        target_language="ru",
        target_variants=["titled_subtitled_music"],
        source_subtitled_video_path=Path("/tmp/source_subtitled.mp4"),
        synchronize_subtitles=True,
    )

    assert out["renders_by_language"]["ru"]["titled_subtitled_music"]["video_path"] == "/tmp/titled.mp4"
    assert calls[0]["subtitled_video_path"] == Path("/tmp/source_subtitled.mp4")
    assert calls[0]["language"] == "ru"
    assert calls[0]["use_candidate_subtitle_snapshot"] is True
    assert calls[0]["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}
    assert calls[0]["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert session["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert production["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert "pending_overlay_title_style" not in session
    assert "pending_overlay_title_style" not in production


def test_django_admin_spoken_title_preserves_existing_captions(monkeypatch):
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
        "overlay_subtitle_by_lang": {"ru": "КБЖУ на 100 гр: 126/10/4/24"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c", "replace_subtitle_phrases": True},
    }
    session = {
        "production": production,
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c", "replace_subtitle_phrases": True},
    }
    calls = []

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    def fake_fast_path(scenes, **kwargs):
        calls.append({"scenes": scenes, **kwargs})
        return {"renders_by_language": {"ru": {"titled_subtitled_music": {"video_path": "/tmp/titled.mp4"}}}}

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "_subtitled_artifact_matches_current_subtitles", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(admin_module, "build_titled_post_production_from_subtitled_master", fake_fast_path)
    monkeypatch.setattr(
        admin_module,
        "build_post_production",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("title-only build must not change burned captions")),
    )
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda _session, _post: None)
    monkeypatch.setattr(
        admin_module,
        "_ensure_approved_candidate_word_timings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("title-only build must never call Speech-to-Text")
        ),
    )

    out = admin_module._build_final_videos_for_run(
        RunStub(id=run_id, human_title=""),
        target_language="ru",
        target_variants=["titled_subtitled_music"],
        source_subtitled_video_path=Path("/tmp/source_subtitled.mp4"),
        synchronize_subtitles=True,
    )

    assert out["renders_by_language"]["ru"]["titled_subtitled_music"]["video_path"] == "/tmp/titled.mp4"
    assert calls[0]["subtitled_video_path"] == Path("/tmp/source_subtitled.mp4")
    assert calls[0]["overlay_title_style"]["replace_subtitle_phrases"] is True
    assert calls[0]["use_candidate_subtitle_snapshot"] is True
    assert calls[0]["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}
    assert calls[0]["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert session["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert production["overlay_title_style"]["title_color"] == "#1a2b3c"
    assert "pending_overlay_title_style" not in session
    assert "pending_overlay_title_style" not in production


def test_django_admin_first_subtitle_build_does_not_call_speech_to_text(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    production = {
        "json_plan": [{"scene": 1, "duration_sec": 8, "text": "scene"}],
        "overlay_title_by_lang": {"ru": "Борщ"},
        "overlay_title_style": {"series_theme": "grandma_christmas"},
    }
    session = {"production": production}

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "_admin_final_build_scenes_from_page", lambda _run, scenes: scenes)
    monkeypatch.setattr(
        admin_module,
        "_ensure_approved_candidate_word_timings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default subtitle build must remain free")
        ),
    )
    def build_subtitled(*_args, **kwargs):
        assert kwargs["overlay_title_style"]["series_theme"] == "grandma_christmas"
        return {"renders_by_language": {"ru": {"subtitled_music": {}}}}

    monkeypatch.setattr(admin_module, "build_subtitled_post_production_from_upscaled_master", build_subtitled)
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda *_args, **_kwargs: None)

    admin_module._build_final_videos_for_run(
        RunStub(id=uuid.uuid4(), human_title="", workflow_type=""),
        target_language="ru",
        target_variants=["subtitled_music"],
        source_upscaled_video_path=Path("/tmp/upscaled.mp4"),
    )


def test_django_admin_opt_in_subtitle_rebuild_aligns_only_requested_language(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    production = {
        "json_plan": [{"scene": 1, "duration_sec": 8, "text": "scene"}],
        "overlay_title_by_lang": {"ru": "Борщ"},
    }
    session = {"production": production}
    calls = []

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    run = RunStub(id=uuid.uuid4(), human_title="", workflow_type="")
    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "_admin_final_build_scenes_from_page", lambda _run, scenes: scenes)
    monkeypatch.setattr(
        admin_module,
        "_ensure_approved_candidate_word_timings",
        lambda scenes, language, *, run=None: calls.append((scenes, language, run)),
    )
    monkeypatch.setattr(
        admin_module,
        "build_subtitled_post_production_from_upscaled_master",
        lambda *_args, **_kwargs: {"renders_by_language": {"ru": {"subtitled_music": {}}}},
    )
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda *_args, **_kwargs: None)

    admin_module._build_final_videos_for_run(
        run,
        target_language="ru",
        target_variants=["subtitled_music"],
        source_upscaled_video_path=Path("/tmp/upscaled.mp4"),
        synchronize_subtitles=True,
    )

    assert calls == [(production["json_plan"], "ru", run)]


def test_django_admin_subtitled_reuse_rejects_stale_scene_video_fingerprint(monkeypatch, tmp_path):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    source = tmp_path / "subtitled.mp4"
    source.write_bytes(b"video")
    run = SimpleNamespace(id=uuid.uuid4())
    scenes = [
        {
            "scene": 1,
            "duration_sec": 4,
            "speech_by_lang": {"ru": "текст"},
            "video_candidates_by_lang": {
                "ru": [{"candidate_id": "new", "approved_for_production": True, "speed_coef": 1.0}]
            },
        }
    ]
    artifact = SimpleNamespace(
        payload_json={
            "subtitle_fingerprint": admin_module.subtitle_fingerprint_for_language(
                scenes,
                "ru",
                use_candidate_subtitle_snapshot=True,
            ),
            "scene_video_fingerprint": "old-fingerprint",
        }
    )

    monkeypatch.setattr(admin_module, "_latest_final_artifact", lambda *_args: artifact)
    monkeypatch.setattr(admin_module, "_artifact_local_path", lambda _artifact: source)

    assert admin_module._subtitled_artifact_matches_current_subtitles(run, scenes, "ru", source) is False


def test_django_admin_default_overlay_title_requires_explicit_english():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    run = SimpleNamespace(human_title="Лагман")

    assert admin_module._default_overlay_title(run) == {"ru": "Лагман"}


def test_django_admin_english_titled_build_requires_explicit_english_title(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    production = {
        "json_plan": [{"duration_sec": 8, "text": "scene"}],
        "overlay_title_by_lang": {"ru": "Лагман"},
    }
    session = {"production": production}

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))

    run = RunStub(id=uuid.uuid4(), human_title="Лагман", series=None)
    with pytest.raises(RuntimeError, match="explicit Title EN"):
        admin_module._build_final_videos_for_run(
            run,
            target_language="en",
            target_variants=["titled_subtitled_music"],
            source_subtitled_video_path=Path("/tmp/source_subtitled.mp4"),
        )


def test_django_admin_titled_build_prefers_current_series_titles_over_snapshot(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    production = {
        "json_plan": [{"duration_sec": 8, "text": "scene"}],
        "overlay_title_by_lang": {"ru": "Лагман", "en": "Lagman"},
        "series_title_by_lang": {"ru": "Карта вкусов", "en": "Карта вкусов"},
    }
    session = {"production": production, "series_title_by_lang": {"ru": "Карта вкусов", "en": "Карта вкусов"}}
    calls = []

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    def fake_fast_path(scenes, **kwargs):
        calls.append({"scenes": scenes, **kwargs})
        return {"renders_by_language": {"en": {"titled_subtitled_music": {"video_path": "/tmp/titled-en.mp4"}}}}

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "_subtitled_artifact_matches_current_subtitles", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(admin_module, "build_titled_post_production_from_subtitled_master", fake_fast_path)
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda _session, _post: None)

    run = RunStub(
        id=uuid.uuid4(),
        human_title="Лагман",
        series=SimpleNamespace(title_ru="Карта вкусов", title_en="Flavor map"),
    )
    admin_module._build_final_videos_for_run(
        run,
        target_language="en",
        target_variants=["titled_subtitled_music"],
        source_subtitled_video_path=Path("/tmp/source_subtitled.mp4"),
    )

    assert calls[0]["overlay_series_title_by_lang"] == {"ru": "Карта вкусов", "en": "Flavor map"}
    assert calls[0]["use_candidate_subtitle_snapshot"] is True
    assert production["series_title_by_lang"] == {"ru": "Карта вкусов", "en": "Flavor map"}


def test_django_admin_non_title_build_keeps_pending_title_color(monkeypatch):
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
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c"},
    }
    session = {
        "production": production,
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.05},
        "pending_overlay_title_style": {"title_color": "#1a2b3c"},
    }
    calls = []

    class RunStub(SimpleNamespace):
        def save(self, **_kwargs):
            pass

    def fake_build_post_production(scenes, **kwargs):
        calls.append({"scenes": scenes, **kwargs})
        return {"renders_by_language": {"ru": {"clean": {"video_path": "/tmp/clean.mp4"}}}}

    monkeypatch.setattr(admin_module, "_run_session_for_admin_render", lambda _run: (None, session, production))
    monkeypatch.setattr(admin_module, "build_post_production", fake_build_post_production)
    monkeypatch.setattr(admin_module, "persist_post_production_artifacts", lambda _session, _post: None)

    admin_module._build_final_videos_for_run(
        RunStub(id=run_id, human_title=""),
        target_language="ru",
        target_variants=["clean"],
    )

    assert calls[0]["overlay_title_style"]["title_color"] == "#c72638"
    assert calls[0]["use_candidate_subtitle_snapshot"] is True
    assert session["pending_overlay_title_style"] == {"title_color": "#1a2b3c"}
    assert production["pending_overlay_title_style"] == {"title_color": "#1a2b3c"}


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


def test_default_publication_targets_skip_unconfigured_destinations(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    copy_payload = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU desc",
        "tiktok_ru": "TT desc",
    }
    monkeypatch.setattr(
        admin_module,
        "platform_readiness",
        lambda _project, platform, _language: SimpleNamespace(ready=platform != "tiktok"),
    )

    assert admin_module._default_publication_target_keys(copy_payload, "discipline") == ["youtube:ru"]


def test_django_publication_form_requires_project_and_accepts_custom_mp4():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.core.files.uploadedfile import SimpleUploadedFile
    import pipeline_admin.admin as admin_module

    copy_payload = {key: "" for key, _label in admin_module.SOCIAL_COPY_FIELD_LABELS}
    copy_payload["youtube_ru_title"] = "Title"
    copy_payload["youtube_ru_description"] = "Description"
    form = admin_module.PublicationQueueAdminForm(
        data={
            "project_slug": "grandma_kitchen",
            "target_keys": ["youtube:ru"],
            "video_variant": "clean",
            "video_source_ru": "upload",
            "youtube_ru_title": "Title",
            "youtube_ru_description": "Description",
        },
        files={"custom_video_ru": SimpleUploadedFile("own.mp4", b"video", content_type="video/mp4")},
        copy=copy_payload,
        project_choices=(("grandma_kitchen", "Grandma Kitchen"), ("discipline", "Discipline")),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["project_slug"] == "grandma_kitchen"
    assert form.cleaned_data["custom_video_ru"].name == "own.mp4"


def test_django_publication_form_rejects_custom_video_without_project():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    copy_payload = {key: "" for key, _label in admin_module.SOCIAL_COPY_FIELD_LABELS}
    form = admin_module.PublicationQueueAdminForm(
        data={
            "project_slug": "",
            "target_keys": ["youtube:ru"],
            "video_variant": "clean",
            "video_source_ru": "upload",
        },
        copy=copy_payload,
        project_choices=(("grandma_kitchen", "Grandma Kitchen"), ("discipline", "Discipline")),
        video_source_choices={"ru": (), "en": ()},
    )

    assert form.is_valid() is False
    assert "project_slug" in form.errors
    assert "custom_video_ru" in form.errors


def test_django_publication_queue_view_allows_undelivered_final_artifacts(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.test import RequestFactory
    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    copy_payload = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU desc",
        "instagram_ru": "IG ru",
        "vk_ru": "VK ru",
        "tiktok_ru": "TT ru",
        "instagram_en": "IG en",
        "youtube_en_title": "EN title",
        "youtube_en_description": "EN desc",
    }
    messages_seen = []
    run = SimpleNamespace(id=run_id, pk=run_id, human_title="Лагман", script="recipe")

    monkeypatch.setattr(admin_module, "_publication_copy_from_run", lambda _run: copy_payload)
    monkeypatch.setattr(
        admin_module,
        "_publication_post_production_from_final_artifacts",
        lambda _run: {"renders_by_language": {"ru": {"titled_subtitled_music": {"video_path": "/tmp/ru.mp4"}}}},
    )
    monkeypatch.setattr(admin_module, "_publication_account_index", lambda _run: {})
    monkeypatch.setattr(admin_module, "all_final_artifacts_delivered", lambda _run_id: False)

    modeladmin = SimpleNamespace(
        get_object=lambda _request, _object_id: run,
        message_user=lambda _request, message, **kwargs: messages_seen.append((message, kwargs)),
    )
    request = RequestFactory().get("/")

    response = admin_module.GenerationRunAdmin.queue_publications_view(modeladmin, request, str(run_id))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Queue publications for" in html
    assert "Queue publication jobs" in html
    assert messages_seen
    assert messages_seen[0][1]["level"] == admin_module.messages.WARNING


def test_django_publication_generate_descriptions_saves_draft(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.test import RequestFactory
    import pipeline_admin.admin as admin_module
    import pipeline_admin.publication_upload_admin as upload_admin

    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, pk=run_id, human_title="Баурсаки", script="recipe")
    generated = {
        key: ""
        for key, _label in admin_module.SOCIAL_COPY_FIELD_LABELS
    }
    generated["instagram_ru"] = "Новые описания"
    generated["youtube_en_description"] = "Fresh descriptions"
    saved = []

    monkeypatch.setattr(admin_module, "_publication_copy_from_run", lambda _run: {})
    monkeypatch.setattr(admin_module, "_publication_recipe_for_run", lambda _run: "base recipe")
    monkeypatch.setattr(admin_module, "_publication_recipe_from_draft", lambda _run: "")
    monkeypatch.setattr(admin_module, "_generate_publication_copy_for_admin", lambda *_args, **_kwargs: generated)
    monkeypatch.setattr(admin_module, "_save_publication_copy_draft", lambda *args, **kwargs: saved.append(kwargs))
    monkeypatch.setattr(admin_module, "_publication_post_production_from_final_artifacts", lambda _run: {})
    monkeypatch.setattr(admin_module, "_publication_account_index", lambda _run: {})
    monkeypatch.setattr(
        upload_admin,
        "_generate_instagram_ru_trial_description",
        lambda **_kwargs: "Отдельный Trial #пробный",
    )
    monkeypatch.setattr(upload_admin, "_save_instagram_ru_trial_draft", lambda *_args, **_kwargs: None)

    modeladmin = SimpleNamespace(
        get_object=lambda _request, _object_id: run,
        message_user=lambda *_args, **_kwargs: None,
    )
    request = RequestFactory().post(
        "/",
        {
            "generate_descriptions": "1",
            "publication_recipe": "edited recipe",
            "generation_feedback": "make it brighter",
        },
    )

    response = admin_module.GenerationRunAdmin.queue_publications_view(modeladmin, request, str(run_id))

    assert response.status_code == 200
    assert saved == [
        {
            "copy_payload": generated,
            "recipe": "edited recipe",
            "feedback": "make it brighter",
        }
    ]
    assert "Новые описания" in response.content.decode()


def test_django_publication_queue_view_reloads_saved_draft(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.test import RequestFactory
    import pipeline_admin.admin as admin_module

    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, pk=run_id, human_title="Баурсаки", script="old recipe")
    copy_payload = {
        key: ""
        for key, _label in admin_module.SOCIAL_COPY_FIELD_LABELS
    }
    copy_payload["instagram_ru"] = "Сохраненный черновик"
    copy_payload["youtube_ru_title"] = "Черновик"

    monkeypatch.setattr(admin_module, "_publication_copy_from_run", lambda _run: copy_payload)
    monkeypatch.setattr(admin_module, "_publication_recipe_from_draft", lambda _run: "saved recipe")
    monkeypatch.setattr(admin_module, "_publication_recipe_for_run", lambda _run: "base recipe")
    monkeypatch.setattr(admin_module, "_publication_post_production_from_final_artifacts", lambda _run: {})
    monkeypatch.setattr(admin_module, "_publication_account_index", lambda _run: {})

    modeladmin = SimpleNamespace(
        get_object=lambda _request, _object_id: run,
        message_user=lambda *_args, **_kwargs: None,
    )
    request = RequestFactory().get("/")

    response = admin_module.GenerationRunAdmin.queue_publications_view(modeladmin, request, str(run_id))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Сохраненный черновик" in html
    assert "saved recipe" in html
    assert "base recipe" not in html


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


def test_title_worker_reuses_saved_subtitles_and_local_build_sends_nothing(monkeypatch, tmp_path):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    import django
    django.setup()
    import pipeline_admin.admin as module

    source = tmp_path / "subtitled.mp4"
    source.write_bytes(b"approved captions")
    run = SimpleNamespace(id=uuid.uuid4())
    artifact = SimpleNamespace(id=uuid.uuid4())
    updates, builds, statuses = [], [], []
    manager = SimpleNamespace(get=lambda **_: run,
                              filter=lambda **_: SimpleNamespace(update=lambda **kw: updates.append(kw)))
    monkeypatch.setattr(module.GenerationRun, "objects", manager)
    monkeypatch.setattr(module, "close_old_connections", lambda: None)
    monkeypatch.setattr(module, "_run_session_for_admin_render", lambda _: (None, {}, {"json_plan": [{"text": "edited"}]}))
    monkeypatch.setattr(module, "_admin_final_build_scenes_from_page", lambda _run, scenes: scenes)
    monkeypatch.setattr(module, "_latest_final_artifact", lambda *_: artifact)
    monkeypatch.setattr(module, "_artifact_local_path", lambda _: source)
    monkeypatch.setattr(module, "_final_artifact_matches_current_scene_videos", lambda *_: False)
    monkeypatch.setattr(module, "_latest_final_build_statuses", lambda _: {})
    monkeypatch.setattr(module, "_set_final_build_status", lambda *_, **kw: statuses.append(kw))
    monkeypatch.setattr(module.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None, join=lambda **_: None))

    def build(_run, **kwargs):
        builds.append(kwargs)
        return {}
    monkeypatch.setattr(module, "_build_final_videos_for_run", build)
    monkeypatch.setattr(module, "_render_video_path_from_post", lambda *_: str(source))
    def forbidden(*_a, **_kw):
        raise AssertionError("Local rebuild must not send or advance the series")
    monkeypatch.setattr(module, "deliver_final_artifact_to_telegram", forbidden)
    monkeypatch.setattr(module, "trigger_final_completion_followup", forbidden)
    monkeypatch.setattr(module, "_advance_series_after_successful_queued_final_build", forbidden)
    module._run_final_build_batch(str(run.id), "ru", ["titled_subtitled_music"], deliver=False)
    assert len(builds) == 1
    assert builds[0]["target_variants"] == ["titled_subtitled_music"]
    assert builds[0]["source_subtitled_video_path"] == source
    assert statuses[-1]["status"] == "success"
    assert updates[-1]["status"] == "final_videos_built"
