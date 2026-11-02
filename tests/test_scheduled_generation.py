from __future__ import annotations

import io
import os
import sys
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import httpx

from app import db
from app.drafts import hide_generation_draft, list_generation_drafts, load_generation_draft, save_generation_draft
from app.models import ChatSession, ContentSeries, GenerationDraft, GenerationRun, ScheduledGenerationItem
import app.scheduled_generation as scheduled_generation
from app.scheduled_generation import (
    QueueResult,
    build_scheduled_generation_template_bytes,
    due_scheduled_generation_items,
    import_scheduled_generation_xlsx,
    process_scheduled_generation_item,
    queue_scheduled_generation_item_now,
)


def _install_sqlite(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    return engine


def test_scheduled_generation_tables_registered_in_metadata():
    assert "scheduled_generation_items" in db.Base.metadata.tables
    assert "generation_drafts" in db.Base.metadata.tables


def test_scheduled_generation_template_imports_to_db(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr(scheduled_generation, "IMPORT_DIR", tmp_path)

    data = build_scheduled_generation_template_bytes()
    result = import_scheduled_generation_xlsx(io.BytesIO(data), original_name="queue.xlsx")

    assert result.created == 1
    assert result.updated == 0
    assert result.saved_path.endswith(".xlsx")
    with db.SessionLocal() as session:
        row = session.execute(select(ScheduledGenerationItem)).scalar_one()
        assert row.title == "Банановое мороженое"
        assert row.comment == "Сделай упор на быстрый летний хук и домашнюю подачу."
        assert row.overlay_subtitle == "КБЖУ на 100 гр: 126/10/4/24"
        assert row.status == "pending"
        assert row.series_id is None
        assert row.source_row_key == "banana-icecream-001"
        assert row.source_file_path == result.saved_path
        assert "generation_mode" not in (row.payload_json or {})


def test_scheduled_generation_import_sets_silence_mode(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr(scheduled_generation, "IMPORT_DIR", tmp_path)

    data = scheduled_generation._build_xlsx(
        [
            list(scheduled_generation.SCHEDULED_EXCEL_COLUMNS),
            [
                "Тихий пирог",
                "Рецепт: яблоки и тесто.",
                "",
                "",
                "#c72638",
                "2026-06-30",
                True,
                "",
                "silent-pie-001",
                "silence",
            ],
        ]
    )
    result = import_scheduled_generation_xlsx(io.BytesIO(data), original_name="queue.xlsx")

    assert result.created == 1
    with db.SessionLocal() as session:
        row = session.execute(select(ScheduledGenerationItem)).scalar_one()
        assert row.payload_json["generation_mode"] == "silence"


def test_due_scheduled_generation_waits_until_local_1am(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setenv("SCHEDULED_GENERATION_TIMEZONE", "Europe/Moscow")
    with db.SessionLocal() as session:
        session.add_all(
            [
                ScheduledGenerationItem(
                    title="Old",
                    recipe="old",
                    run_at=datetime(2026, 6, 29, 20, 0, tzinfo=timezone.utc),
                    status="pending",
                ),
                ScheduledGenerationItem(
                    title="Today late",
                    recipe="today",
                    run_at=datetime(2026, 6, 30, 20, 59, tzinfo=timezone.utc),
                    status="pending",
                ),
                ScheduledGenerationItem(
                    title="Tomorrow",
                    recipe="tomorrow",
                    run_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
                    status="pending",
                ),
            ]
        )
        session.commit()

    rows = due_scheduled_generation_items(now=datetime(2026, 6, 29, 21, 59, tzinfo=timezone.utc))

    assert [row.title for row in rows] == ["Old"]

    rows = due_scheduled_generation_items(now=datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc))

    assert [row.title for row in rows] == ["Old", "Today late"]


def test_save_generation_draft_marks_run_as_draft(monkeypatch):
    _install_sqlite(monkeypatch)
    session = {
        "state": "waiting_scene_media",
        "is_draft": True,
        "recipe_full_text": "Рецепт",
        "production": {
            "overlay_title_by_lang": {"ru": "Черновой ролик", "en": "Draft Video"},
            "json_plan": [
                {
                    "scene": 1,
                    "text": "Сцена",
                    "duration_sec": 6,
                    "start_frame_prompt": "Полный стартовый промпт",
                    "speech_by_lang": {"ru": "Точная реплика"},
                    "image_candidates": [{"candidate_id": "img-1", "image_b64": "large-inline-media"}],
                }
            ],
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }

    draft = save_generation_draft(123, session)

    assert draft.title == "Черновой ролик"
    assert list_generation_drafts(123, limit=5)[0].id == draft.id
    with db.SessionLocal() as dbs:
        stored = dbs.get(GenerationDraft, draft.id)
        assert stored is not None
        assert stored.scene_index == 0
        assert stored.substep == "video_prompt"
        stored_snapshot = stored.session_snapshot_json
        assert stored_snapshot["recipe_full_text"] == "Рецепт"
        assert stored_snapshot["production"]["json_plan"][0]["text"] == "Сцена"
        assert stored_snapshot["production"]["json_plan"][0]["start_frame_prompt"] == "Полный стартовый промпт"
        assert stored_snapshot["production"]["json_plan"][0]["speech_by_lang"] == {"ru": "Точная реплика"}
        assert "image_b64" not in stored_snapshot["production"]["json_plan"][0]["image_candidates"][0]
        assert stored_snapshot["production"]["overlay_title_by_lang"] == {
            "ru": "Черновой ролик",
            "en": "Draft Video",
        }

    loaded = load_generation_draft(str(draft.id))
    assert loaded is not None
    _row, snapshot = loaded
    assert snapshot["production"]["json_plan"][0]["text"] == "Сцена"
    assert snapshot["production"]["json_plan"][0]["duration_sec"] == 6
    assert snapshot["production"]["json_plan"][0]["start_frame_prompt"] == "Полный стартовый промпт"
    assert snapshot["production"]["json_plan"][0]["speech_by_lang"] == {"ru": "Точная реплика"}
    assert snapshot["production"]["overlay_title_by_lang"] == {"ru": "Черновой ролик", "en": "Draft Video"}


def test_save_generation_draft_reuses_existing_run_draft(monkeypatch):
    _install_sqlite(monkeypatch)
    session = {
        "state": "waiting_scene_media",
        "recipe_full_text": "Рецепт",
        "production": {
            "overlay_title_by_lang": {"ru": "Первый заголовок"},
            "json_plan": [{"scene": 1, "text": "Сцена", "duration_sec": 6}],
        },
        "image_scene_index": 0,
        "scene_media_substep": "image",
    }

    first = save_generation_draft(123, session)
    session["production"]["overlay_title_by_lang"]["ru"] = "Второй заголовок"
    session["image_scene_index"] = 2
    session["scene_media_substep"] = "video_prompt"

    second = save_generation_draft(123, session)

    assert second.id == first.id
    with db.SessionLocal() as dbs:
        drafts = dbs.execute(select(GenerationDraft)).scalars().all()
        assert [row.id for row in drafts] == [first.id]
        stored = drafts[0]
        assert stored.title == "Второй заголовок"
        assert stored.scene_index == 2
        assert stored.substep == "video_prompt"
        run = dbs.get(GenerationRun, stored.run_id)
        assert run is not None
        assert run.is_draft is True


def test_list_generation_drafts_collapses_duplicate_run_rows(monkeypatch):
    _install_sqlite(monkeypatch)
    with db.SessionLocal() as dbs:
        run = GenerationRun(status="media_in_progress", is_draft=True, language="ru", script="recipe")
        dbs.add(run)
        dbs.flush()
        older = GenerationDraft(
            chat_id=123,
            run_id=run.id,
            title="Старый индекс",
            session_snapshot_json={},
            payload_json={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        newer = GenerationDraft(
            chat_id=123,
            run_id=run.id,
            title="Новый индекс",
            session_snapshot_json={},
            payload_json={},
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        dbs.add_all([older, newer])
        dbs.commit()
        newer_id = newer.id

    assert [row.id for row in list_generation_drafts(123, limit=5)] == [newer_id]


def test_hide_generation_draft_clears_linked_run_from_telegram_list(monkeypatch):
    _install_sqlite(monkeypatch)
    session = {
        "state": "waiting_scene_media",
        "recipe_full_text": "Рецепт",
        "production": {"json_plan": [{"scene": 1, "text": "Сцена", "duration_sec": 6}]},
    }
    draft = save_generation_draft(123, session)

    assert [row.id for row in list_generation_drafts(123, limit=5)] == [draft.id]

    assert hide_generation_draft(str(draft.id)) is True
    assert list_generation_drafts(123, limit=5) == []
    with db.SessionLocal() as dbs:
        stored = dbs.get(GenerationDraft, draft.id)
        assert stored is not None
        run = dbs.get(GenerationRun, stored.run_id)
        assert run is not None
        assert run.is_draft is False


def test_scheduled_media_pipeline_persists_chat_session_for_final_delivery(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "909")
    item_id = uuid.uuid4()
    with db.SessionLocal() as session_db:
        session_db.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Бешбармак",
                recipe="Мясо, тесто, лук.",
                run_at=datetime.now(timezone.utc),
                status="pending",
            )
        )
        session_db.commit()

    async def fake_record(*_args, **_kwargs):
        return None

    async def fake_ai_approve(_update, *, context, chat_id, session):
        assert chat_id == 909
        _ = context
        session["state"] = "ready_for_production"

    monkeypatch.setattr(scheduled_generation, "record_scheduled_generation_event_async", fake_record)
    monkeypatch.setattr("app.handlers.handlers_state_media._run_scene_media_ai_approve", fake_ai_approve)

    generation_session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru", "en"],
        "selected_concept": "Бешбармак",
        "recipe_full_text": "Мясо, тесто, лук.",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "duration_sec": 6,
                    "text": "Готовим бешбармак.",
                    "start_frame_image_b64": "start-image",
                    "video_prompt_by_lang": {"ru": "prompt ru", "en": "prompt en"},
                    "video_results_by_lang": {"ru": {"video_b64": "ru"}, "en": {"video_b64": "en"}},
                    "video_approvals_by_lang": {"ru": True, "en": True},
                }
            ]
        },
    }

    summary = asyncio.run(scheduled_generation._run_scheduled_media_pipeline(item_id, generation_session))

    assert summary["video_count"] == 2
    with db.SessionLocal() as session_db:
        row = session_db.query(ChatSession).filter(ChatSession.chat_id == 909).one()
        assert str(row.generation_run_id) == generation_session["generation_run_id"]


def test_scheduled_media_pipeline_stops_for_manual_review_without_cursor_reset(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "909")
    item_id = uuid.uuid4()
    with db.SessionLocal() as session_db:
        session_db.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Курица",
                recipe="Филе, сливки.",
                run_at=datetime.now(timezone.utc),
                status="pending",
            )
        )
        session_db.commit()

    async def fake_record(*_args, **_kwargs):
        return None

    async def fake_ai_approve(_update, *, context, chat_id, session):
        assert chat_id == 909
        _ = context
        session["scene_media_ai_human_review"] = {"stage": "video", "reason": "needs manual check"}

    monkeypatch.setattr(scheduled_generation, "record_scheduled_generation_event_async", fake_record)
    monkeypatch.setattr("app.handlers.handlers_state_media._run_scene_media_ai_approve", fake_ai_approve)

    generation_session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru"],
        "selected_concept": "Курица",
        "recipe_full_text": "Филе, сливки.",
        "image_scene_index": 2,
        "scene_media_substep": "video_prompt",
        "scene_media_language_index": 0,
        "production": {
            "json_plan": [
                {"scene": 1, "duration_sec": 6, "text": "Первая", "video_prompt_by_lang": {"ru": "prompt 1"}},
                {"scene": 2, "duration_sec": 6, "text": "Вторая", "video_prompt_by_lang": {"ru": "prompt 2"}},
                {"scene": 3, "duration_sec": 6, "text": "Третья", "video_prompt_by_lang": {"ru": "prompt 3"}},
            ]
        },
    }

    with pytest.raises(scheduled_generation.ScheduledGenerationNeedsReview):
        asyncio.run(scheduled_generation._run_scheduled_media_pipeline(item_id, generation_session))

    assert generation_session["scene_media_approve_mode"] == "manual"
    assert "scene_media_ai_human_review" not in generation_session
    assert generation_session["image_scene_index"] == 2
    assert generation_session["scene_media_substep"] == "video_prompt"
    with db.SessionLocal() as session_db:
        item = session_db.get(ScheduledGenerationItem, item_id)
        media_step = (item.payload_json or {})["step_state"]["scene_media"]
        assert media_step["status"] == "failed_needs_review"
        assert media_step["cursor"]["scene_index"] == 2
        assert media_step["cursor"]["substep"] == "video_prompt"
        assert media_step["data"]["human_review"] == {"stage": "video", "reason": "needs manual check"}


def _scheduled_media_scene(scene_no: int, *, start: bool = True, videos: tuple[str, ...] = ("ru", "en")) -> dict:
    scene = {
        "scene": scene_no,
        "duration_sec": 6,
        "text": f"Scene {scene_no}",
        "video_prompt_by_lang": {"ru": "prompt ru", "en": "prompt en"},
        "video_results_by_lang": {},
        "video_approvals_by_lang": {},
    }
    if start:
        scene["start_frame_image_b64"] = f"start-{scene_no}"
    for language in videos:
        scene["video_results_by_lang"][language] = {"video_b64": f"video-{scene_no}-{language}"}
        scene["video_approvals_by_lang"][language] = True
    return scene


def test_scheduled_media_resume_reconciles_stale_cursor_to_first_missing_video():
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 2,
        "scene_media_substep": "video_prompt",
        "scene_media_language_index": 1,
        "production": {
            "json_plan": [
                _scheduled_media_scene(1),
                _scheduled_media_scene(2),
                _scheduled_media_scene(3),
                _scheduled_media_scene(4),
                _scheduled_media_scene(5, videos=()),
            ]
        },
    }

    changed = scheduled_generation._reconcile_scheduled_media_resume_cursor(session)

    assert changed is True
    assert session["image_scene_index"] == 4
    assert session["scene_media_substep"] == "video_prompt"
    assert session["scene_media_language_index"] == 0
    assert session["scheduled_media_resume_reconciled_cursor"]["from_scene_index"] == 2
    assert session["scheduled_media_resume_reconciled_cursor"]["to_scene_index"] == 4


def test_scheduled_media_resume_reconciles_stale_cursor_to_first_missing_start_image():
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "scene_media_language_index": 1,
        "production": {
            "json_plan": [
                _scheduled_media_scene(1),
                _scheduled_media_scene(2),
                _scheduled_media_scene(3, start=False, videos=()),
            ]
        },
    }

    changed = scheduled_generation._reconcile_scheduled_media_resume_cursor(session)

    assert changed is True
    assert session["image_scene_index"] == 2
    assert session["scene_media_substep"] == "start_prompt"
    assert session["scene_media_language_index"] == 0


def test_scheduled_headless_telegram_send_failure_is_nonfatal(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Гуляш",
                recipe="Рецепт",
                run_at=datetime.now(timezone.utc),
                status="pending",
            )
        )
        session.commit()

    class _Bot:
        async def send_message(self, **_kwargs):
            raise httpx.ConnectError("temporary DNS failure")

    message = scheduled_generation._ScheduledTelegramMessage(item_id=item_id, chat_id=123, bot=_Bot())

    out = asyncio.run(message.reply_text("Промпт перед генерацией видео"))

    assert out.message_id is None
    with db.SessionLocal() as session:
        statuses = [
            row.status
            for row in session.query(scheduled_generation.ScheduledGenerationEvent)
            .filter(scheduled_generation.ScheduledGenerationEvent.scheduled_item_id == item_id)
            .order_by(scheduled_generation.ScheduledGenerationEvent.created_at)
        ]
    assert statuses == ["message", "telegram_send_failed"]


def test_scheduled_runner_classifies_httpx_connect_error_as_retryable():
    assert scheduled_generation._is_retryable_scheduled_network_error(httpx.ConnectError("temporary DNS failure"))


def test_scheduled_generation_admin_action_runs_selected_now(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    import pipeline_admin.admin as admin_module

    item_id = uuid.uuid4()
    calls = []
    messages = []

    class Queryset(list):
        def order_by(self, *_args):
            return self

    monkeypatch.setattr(
        admin_module,
        "queue_scheduled_generation_item_now",
        lambda value: calls.append(value) or QueueResult(status="queued", backend="celery", detail="task-1"),
    )
    modeladmin = SimpleNamespace(message_user=lambda _request, message, level=None: messages.append((message, level)))
    item = SimpleNamespace(id=item_id, title="Test item")

    admin_module.run_scheduled_items_now(modeladmin, None, Queryset([item]))

    assert calls == [item_id]
    assert "Test item: queued (celery: task-1)" in messages[0][0]
    assert admin_module.ScheduledGenerationItemAdminForm.base_fields["title_color"].widget.input_type == "color"
    assert admin_module.ScheduledGenerationItem._meta.get_field("created_at").auto_now_add is True
    assert admin_module.ScheduledGenerationItem._meta.get_field("updated_at").auto_now is True


def test_scheduled_generation_admin_form_normalizes_empty_source_key(monkeypatch):
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from django.db import connection
    import pipeline_admin.admin as admin_module

    created_table = False
    if admin_module.ScheduledGenerationItem._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as schema:
            schema.create_model(admin_module.ScheduledGenerationItem)
        created_table = True
    try:
        form = admin_module.ScheduledGenerationItemAdminForm(
            data={
                "id": str(uuid.uuid4()),
                "title": "Баурсаки",
                "recipe": "https://example.com/recipe",
                "comment": "Пусть бабушка будет в казахском настроении.",
                "generation_mode": "silence",
                "title_color": "#ead257",
                "run_at": "2026-06-30 13:02:05",
                "is_draft": "on",
                "status": "pending",
                "generation_run": "",
                "source_row_key": "",
                "error_text": "",
            }
        )

        assert form.is_valid(), form.errors
        obj = form.save(commit=False)
        assert obj.source_row_key is None
        assert obj.payload_json["generation_mode"] == "silence"
    finally:
        if created_table:
            with connection.schema_editor() as schema:
                schema.delete_model(admin_module.ScheduledGenerationItem)


def test_scheduled_generation_comment_is_used_for_viral_plan(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Летний пирог",
                recipe="Рецепт: яблоки и тесто.",
                comment="После рецепта: больше семейной интриги в первом кадре.",
                overlay_subtitle="КБЖУ на 100 гр: 126/10/4/24",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
            )
        )
        session.commit()

    calls: dict[str, object] = {}
    captured_session: dict[str, object] = {}

    async def fake_viral(concept, *, full_recipe_text, **_kwargs):
        calls["viral"] = (concept, full_recipe_text)
        return {"hook": "family intrigue"}

    async def fake_scenes(concept, *, full_recipe_text, viral_concept_brief=None, generation_comment=None, **_kwargs):
        calls["scenes"] = (concept, full_recipe_text, viral_concept_brief, generation_comment)
        return {"json_plan": [{"scene": 1, "text": "Сцена"}]}

    async def fake_production(scenes_plan, *, viral_concept_brief=None, generation_comment=None, dish_visual_brief=None, **_kwargs):
        calls["production"] = (scenes_plan, viral_concept_brief, generation_comment, dish_visual_brief)
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "Prompt"}]}

    async def fake_notify(*_args, **_kwargs):
        return None

    async def fake_media(item_id_arg, session):
        captured_session.update(session)
        session["state"] = "ready_for_production"
        return {"scene_count": 1, "video_count": 1, "generation_run_id": session.get("generation_run_id")}

    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fake_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fake_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fake_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", lambda session: (str(uuid.uuid4()), {}))
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    assert calls["viral"][0] == "Летний пирог"
    assert "Рецепт: яблоки и тесто." in calls["viral"][1]
    assert "КОММЕНТАРИЙ К ГЕНЕРАЦИИ ВИРАЛЬНОГО ПЛАНА" in calls["viral"][1]
    assert "больше семейной интриги" in calls["viral"][1]
    assert calls["scenes"] == (
        "Летний пирог",
        "Рецепт: яблоки и тесто.",
        {"hook": "family intrigue"},
        "После рецепта: больше семейной интриги в первом кадре.",
    )
    assert calls["production"][1] == {"hook": "family intrigue"}
    assert calls["production"][2] == "После рецепта: больше семейной интриги в первом кадре."
    assert "Летний пирог" in calls["production"][3]
    assert "canonical real-world appearance" in calls["production"][3]
    assert captured_session["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}
    assert captured_session["production"]["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}


def test_scheduled_generation_every_comment_is_persistent_start_prompt_instruction(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Зимний пирог",
                recipe="Рецепт: яблоки и тесто.",
                comment="every Сделай стартовый кадр в зимнем настроении.",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
            )
        )
        session.commit()

    calls: dict[str, object] = {}

    async def fake_viral(concept, *, full_recipe_text, **_kwargs):
        calls["viral"] = (concept, full_recipe_text)
        return {"hook": "winter"}

    async def fake_scenes(concept, *, full_recipe_text, viral_concept_brief=None, generation_comment=None, **_kwargs):
        calls["scenes_comment"] = generation_comment
        return {"json_plan": [{"scene": 1, "text": "Сцена"}]}

    async def fake_production(scenes_plan, *, generation_comment=None, **_kwargs):
        calls["production_comment"] = generation_comment
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "Prompt"}]}

    async def fake_notify(*_args, **_kwargs):
        return None

    async def fake_media(item_id_arg, session):
        calls["session"] = dict(session)
        session["state"] = "ready_for_production"
        return {"scene_count": 1, "video_count": 1, "generation_run_id": session.get("generation_run_id")}

    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fake_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fake_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fake_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", lambda session: (str(uuid.uuid4()), {}))
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    assert "Сделай стартовый кадр в зимнем настроении." in calls["viral"][1]
    assert "every Сделай" not in calls["viral"][1]
    assert calls["scenes_comment"] == "Сделай стартовый кадр в зимнем настроении."
    assert calls["production_comment"] == "Сделай стартовый кадр в зимнем настроении."
    captured_session = calls["session"]
    assert captured_session["scheduled_generation_comment"] == "Сделай стартовый кадр в зимнем настроении."
    assert (
        captured_session["scheduled_generation_every_start_prompt_comment"]
        == "Сделай стартовый кадр в зимнем настроении."
    )


def test_scheduled_generation_url_recipe_is_resolved_before_pipeline(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Эчпочмак",
                recipe="https://www.russianfood.com/recipes/recipe.php?rid=132701",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
            )
        )
        session.commit()

    calls: dict[str, object] = {}

    def fake_fetch(url):
        calls["fetch"] = url
        return "raw page with recipe"

    async def fake_extract(source_text, *, source_url=None):
        calls["extract"] = (source_text, source_url)
        return "Ингредиенты: мясо 1 кг, картофель 1 кг. Шаги: замесить тесто, нарезать начинку, испечь."

    async def fake_viral(title, *, full_recipe_text):
        calls["viral"] = (title, full_recipe_text)
        return {"hook": "family"}

    async def fake_scenes(title, *, full_recipe_text, viral_concept_brief=None, **_kwargs):
        calls["scenes"] = (title, full_recipe_text, viral_concept_brief)
        return {"json_plan": [{"scene": 1, "text": "Сцена"}]}

    async def fake_production(scenes_plan, *, viral_concept_brief=None, **_kwargs):
        calls["production"] = (scenes_plan, viral_concept_brief)
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "Prompt"}]}

    async def fake_media(_item_id, session):
        calls["session_recipe"] = session.get("recipe_full_text")
        session["state"] = "ready_for_production"
        return {"scene_count": 1}

    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduled_generation, "fetch_recipe_text_from_url", fake_fetch)
    monkeypatch.setattr(scheduled_generation, "generate_recipe_from_source_text", fake_extract)
    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fake_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fake_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fake_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", lambda session: (str(uuid.uuid4()), {}))
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    expected = "Ингредиенты: мясо 1 кг, картофель 1 кг. Шаги: замесить тесто, нарезать начинку, испечь."
    assert calls["fetch"] == "https://www.russianfood.com/recipes/recipe.php?rid=132701"
    assert calls["extract"] == ("raw page with recipe", "https://www.russianfood.com/recipes/recipe.php?rid=132701")
    assert calls["viral"] == ("Эчпочмак", expected)
    assert calls["scenes"] == ("Эчпочмак", expected, {"hook": "family"})
    assert calls["session_recipe"] == expected


def test_scheduled_generation_notifications_include_generated_content(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Баурсаки",
                recipe="Рецепт",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
            )
        )
        session.commit()

    notifications: list[str] = []

    async def fake_viral(*_args, **_kwargs):
        return {
            "main_hook": "Бабушка доказывает, что баурсаки спасают семейный праздник.",
            "viewer_promise": "Покажем самый воздушный способ без лишней суеты.",
            "strongest_first_frame": "Кот спрятал дрожжи за минуту до гостей.",
        }

    async def fake_scenes(*_args, **_kwargs):
        return {
            "json_plan": [
                {
                    "scene": 1,
                    "action": "Бабушка ищет дрожжи, кот виновато смотрит на миску.",
                    "text": "Без паники, сейчас будут баурсаки.",
                }
            ]
        }

    async def fake_production(*_args, **_kwargs):
        return {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_prompt": "Warm kitchen, grandma kneads baursaki dough, orange cat nearby.",
                    "end_frame_prompt": "Golden baursaki pile on a plate.",
                    "video_prompt": "Grandma rolls dough, cat taps flour, cozy comic timing.",
                }
            ]
        }

    async def fake_notify(text):
        notifications.append(text)

    async def fake_media(item_id_arg, session):
        session["state"] = "ready_for_production"
        return {"scene_count": 1, "video_count": 1, "generation_run_id": session.get("generation_run_id")}

    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fake_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fake_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fake_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", lambda session: (str(uuid.uuid4()), {}))
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    all_text = "\n\n".join(notifications)
    assert "баурсаки спасают семейный праздник" in all_text
    assert "Бабушка ищет дрожжи" in all_text
    assert "Warm kitchen, grandma kneads baursaki dough" in all_text
    assert "Golden baursaki pile" in all_text
    assert "Grandma rolls dough" in all_text
    assert "в Telegram он отправит мини-копии" in all_text


def test_scheduled_generation_resumes_from_stored_step_state(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    viral = {"hook": "stored hook"}
    scenes_plan = [{"scene": 1, "text": "Сохраненная сцена"}]
    production = {
        "json_plan": [
            {
                "scene": 1,
                "text": "Сохраненная сцена",
                "start_frame_prompt": "Stored prompt",
                "video_prompt_by_lang": {"ru": "Stored video prompt"},
            }
        ]
    }
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Баурсаки",
                recipe="Рецепт",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
                payload_json={
                    scheduled_generation.SCHEDULED_STEP_STATE_KEY: {
                        scheduled_generation.SCHEDULED_STEP_VIRAL_PLAN: {
                            "status": "done",
                            "data": {"viral_concept_brief": viral},
                        },
                        scheduled_generation.SCHEDULED_STEP_SCENES_PLAN: {
                            "status": "done",
                            "data": {"scenes_plan": scenes_plan},
                        },
                        scheduled_generation.SCHEDULED_STEP_PRODUCTION_PROMPTS: {
                            "status": "done",
                            "data": {"production": production},
                        },
                    }
                },
            )
        )
        session.commit()

    captured: dict[str, object] = {}

    async def fail_viral(*_args, **_kwargs):
        raise AssertionError("viral should be resumed, not regenerated")

    async def fail_scenes(*_args, **_kwargs):
        raise AssertionError("scenes should be resumed, not regenerated")

    async def fail_production(*_args, **_kwargs):
        raise AssertionError("production should be resumed, not regenerated")

    async def fake_media(item_id_arg, session):
        captured["item_id"] = item_id_arg
        captured["session"] = dict(session)
        session["state"] = "ready_for_production"
        return {"scene_count": 1, "video_count": 1, "generation_run_id": session.get("generation_run_id")}

    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fail_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fail_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fail_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", lambda session: (str(uuid.uuid4()), {}))
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    resumed_session = captured["session"]
    assert resumed_session["viral_concept_brief"] == viral
    assert resumed_session["scenes_plan"] == scenes_plan
    resumed_production = resumed_session["production"]
    assert resumed_production["json_plan"][0]["text"] == production["json_plan"][0]["text"]
    assert resumed_production["json_plan"][0]["video_prompt_by_lang"] == {"ru": "Stored video prompt"}
    assert resumed_production["json_plan"][0]["image_candidates"] == []


def test_telegram_chunks_split_long_messages_without_dropping_text():
    text = "alpha\n\n" + ("beta " * 900) + "\n\nomega"

    chunks = scheduled_generation._telegram_chunks(text, limit=120)

    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert "".join(chunks).replace("\n\n", "").replace(" ", "").startswith("alphabeta")
    assert "omega" in chunks[-1]


def test_scheduled_generation_selected_series_is_attached_to_session(monkeypatch):
    _install_sqlite(monkeypatch)
    series_id = uuid.uuid4()
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ContentSeries(
                id=series_id,
                slug="summer-pies",
                title_ru="Летние пироги",
                title_en="Summer pies",
                current_video_index=3,
                total_videos=10,
                is_active=True,
                meta_json={},
            )
        )
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Яблочный пирог",
                recipe="Рецепт",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="pending",
                series_id=series_id,
            )
        )
        session.commit()

    captured = {}

    async def fake_viral(*_args, **_kwargs):
        return {"hook": "series"}

    async def fake_scenes(*_args, **_kwargs):
        return {"json_plan": [{"scene": 1, "text": "Сцена"}]}

    async def fake_production(*_args, **_kwargs):
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "Prompt"}]}

    def fake_ensure(session):
        captured.update(session)
        return (str(uuid.uuid4()), {})

    async def fake_notify(*_args, **_kwargs):
        return None

    async def fake_media(item_id_arg, session):
        session["state"] = "ready_for_production"
        return {"scene_count": 1, "video_count": 1, "generation_run_id": session.get("generation_run_id")}

    monkeypatch.setattr(scheduled_generation, "generate_viral_concept_brief", fake_viral)
    monkeypatch.setattr(scheduled_generation, "generate_scenes_plan", fake_scenes)
    monkeypatch.setattr(scheduled_generation, "generate_full_production", fake_production)
    monkeypatch.setattr(scheduled_generation, "_run_scheduled_media_pipeline", fake_media)
    monkeypatch.setattr(scheduled_generation, "ensure_generation_run_for_session", fake_ensure)
    monkeypatch.setattr(scheduled_generation, "save_generation_draft", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduled_generation, "_notify_admin_chat", fake_notify)

    status = asyncio.run(process_scheduled_generation_item(item_id))

    assert status == "draft_ready"
    assert captured["series"]["id"] == str(series_id)
    assert captured["series_title_by_lang"] == {"ru": "Летние пироги", "en": "Summer pies"}
    assert captured["series_video_index"] == 3
    assert captured["series_total_videos"] == 10


def test_queue_scheduled_generation_falls_back_when_celery_has_no_worker(monkeypatch):
    _install_sqlite(monkeypatch)
    item_id = uuid.uuid4()
    with db.SessionLocal() as session:
        session.add(
            ScheduledGenerationItem(
                id=item_id,
                title="Очередь",
                recipe="Рецепт",
                title_color="#c72638",
                run_at=datetime.now(timezone.utc),
                is_draft=True,
                status="running",
            )
        )
        session.commit()

    calls = []
    monkeypatch.setattr(
        scheduled_generation,
        "_queue_with_celery",
        lambda value: QueueResult(status="fallback", backend="celery_unavailable", detail="No live worker"),
    )
    monkeypatch.setattr(
        scheduled_generation,
        "_queue_with_subprocess",
        lambda value: calls.append(value) or QueueResult(status="queued", backend="subprocess", detail="pid=123"),
    )

    queued = queue_scheduled_generation_item_now(item_id)

    assert queued == QueueResult(status="queued", backend="subprocess", detail="pid=123; celery=No live worker")
    assert calls == [item_id]
    with db.SessionLocal() as session:
        row = session.get(ScheduledGenerationItem, item_id)
        assert row.status == "pending"
        assert row.started_at is None
        assert row.finished_at is None
