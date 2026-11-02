import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.generation_store import (
    _apply_prompt_payload_to_scene_row,
    _human_title_for_session,
    ensure_generation_run_for_session,
    merge_scene_prompt_payload,
)
from app.models import GenerationArtifact, GenerationRun, Scene


def _install_sqlite(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    return engine


def test_existing_scene_prompt_payload_survives_empty_scene_payload(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(
            GenerationRun(
                id=run_id,
                status="media_in_progress",
                language="ru",
                script="script",
            )
        )
        row = Scene(
            run_id=run_id,
            scene_order=1,
            text="Scene text",
            duration_sec=6,
            start_frame_prompt="old start prompt",
            video_prompt_by_lang={"ru": "old video prompt"},
            speech_by_lang={"ru": "old speech"},
        )
        dbs.add(row)
        dbs.flush()
        changed = _apply_prompt_payload_to_scene_row(row, {"scene": 1})
        dbs.commit()

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=run_id, scene_order=1).one()

    assert changed is False
    assert row.start_frame_prompt == "old start prompt"
    assert row.video_prompt_by_lang == {"ru": "old video prompt"}
    assert row.speech_by_lang == {"ru": "old speech"}


def test_scene_prompt_payload_persists_visual_and_dialogue_metadata(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(
            GenerationRun(
                id=run_id,
                status="media_in_progress",
                language="ru",
                script="script",
            )
        )
        row = Scene(run_id=run_id, scene_order=1, text="old", duration_sec=6)
        dbs.add(row)
        dbs.flush()
        changed = _apply_prompt_payload_to_scene_row(
            row,
            {
                "scene": 1,
                "characters": ["grandma", "grandson"],
                "dialogue_mode": "multi",
                "dialogue": [
                    {"speaker": "grandma", "text": "Угощайтесь"},
                    {"speaker": "grandson", "text": "Очень вкусно, бабушка!"},
                ],
                "action": "Внук пробует ломтик.",
                "location": "светлый городской двор",
                "lighting": "мягкий вечерний свет",
                "color_palette": "зелёный, золотой и голубой",
                "generation_mode": "long",
                "aspect_ratio": "16:9",
                "video_prompt_by_lang": {"ru": "video"},
            },
        )
        dbs.commit()

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=run_id, scene_order=1).one()

    assert changed is True
    assert row.video_prompt_by_lang == {"ru": "video"}
    assert row.prompt_meta_json["characters"] == ["grandma", "cat", "grandson"]
    assert row.prompt_meta_json["dialogue"][1]["speaker"] == "grandson"
    assert row.prompt_meta_json["action"] == "Внук пробует ломтик."
    assert row.prompt_meta_json["location"] == "светлый городской двор"
    assert row.prompt_meta_json["lighting"] == "мягкий вечерний свет"
    assert row.prompt_meta_json["color_palette"] == "зелёный, золотой и голубой"
    assert row.prompt_meta_json["generation_mode"] == "long"
    assert row.prompt_meta_json["aspect_ratio"] == "16:9"


def test_story_scene_metadata_survives_persistence_without_forcing_core_cast(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    scene_payload = {
        "scene": 1,
        "generation_mode": "story",
        "characters": ["antonin_young"],
        "narration_excerpt": "Ему было всего десять.",
        "start_sec": 6.0,
        "end_sec": 9.0,
        "timeline_duration_sec": 3.0,
        "semantic_beat": 2,
        "semantic_clip_index": 1,
        "semantic_clip_count": 3,
        "time_period": "past",
        "era_style": "retro watercolor sepia",
        "story_character_descriptions": {"antonin_young": "Ten-year-old Antonin"},
        "story_character_labels": {"antonin_young": "Антонен, 10 лет"},
        "style_reference_paths": ["/tmp/retro-watercolor.jpg"],
        "start_frame_ref_count": 3,
        "start_frame_image_model": "gemini-3-pro-image",
        "start_image_generation_attempts": [
            {"attempt_id": "attempt-1", "status": "completed", "ref_count": 3}
        ],
    }
    with db.SessionLocal() as dbs:
        dbs.add(GenerationRun(id=run_id, status="media_in_progress", language="ru", script="script"))
        row = Scene(run_id=run_id, scene_order=1, text="Scene text", duration_sec=3)
        dbs.add(row)
        dbs.flush()
        assert _apply_prompt_payload_to_scene_row(row, scene_payload) is True
        dbs.commit()

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=run_id, scene_order=1).one()
        persisted = dict(row.prompt_meta_json)

    assert persisted["characters"] == ["antonin_young"]
    assert persisted["semantic_beat"] == 2
    assert persisted["start_sec"] == 6.0
    assert persisted["time_period"] == "past"
    assert persisted["story_character_labels"]["antonin_young"] == "Антонен, 10 лет"
    assert persisted["style_reference_paths"] == ["/tmp/retro-watercolor.jpg"]
    assert persisted["start_frame_ref_count"] == 3
    assert persisted["start_frame_image_model"] == "gemini-3-pro-image"
    assert persisted["start_image_generation_attempts"][0]["status"] == "completed"

    restored = {"scene": 1, "text": "Scene text", "duration_sec": 3}
    merge_scene_prompt_payload(restored, {"prompt_meta_json": persisted})
    assert restored["characters"] == ["antonin_young"]
    assert "grandma" not in restored["characters"]
    assert "cat" not in restored["characters"]
    assert restored["semantic_clip_count"] == 3
    assert restored["time_period"] == "past"
    assert restored["start_frame_ref_count"] == 3
    assert restored["start_image_generation_attempts"][0]["attempt_id"] == "attempt-1"


def test_revised_story_scene_replaces_stale_structured_metadata(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(GenerationRun(id=run_id, status="media_in_progress", language="ru", script="script"))
        row = Scene(
            run_id=run_id,
            scene_order=1,
            text="Scene text",
            duration_sec=3,
            prompt_meta_json={
                "generation_mode": "story",
                "characters": ["grandma", "cat"],
                "semantic_beat": 1,
                "time_period": "present",
            },
        )
        dbs.add(row)
        dbs.flush()
        _apply_prompt_payload_to_scene_row(
            row,
            {
                "scene": 1,
                "generation_mode": "story",
                "characters": ["antonin_boy"],
                "semantic_beat": 2,
                "time_period": "past",
            },
        )
        dbs.commit()

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=run_id, scene_order=1).one()
        assert row.prompt_meta_json["characters"] == ["antonin_boy"]
        assert row.prompt_meta_json["semantic_beat"] == 2
        assert row.prompt_meta_json["time_period"] == "past"


def test_revised_story_scene_can_clear_cast_without_inferring_grandma(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(GenerationRun(id=run_id, status="media_in_progress", language="ru", script="script"))
        row = Scene(
            run_id=run_id,
            scene_order=1,
            text="Dessert macro",
            duration_sec=3,
            prompt_meta_json={
                "generation_mode": "story",
                "characters": ["grandma", "cat"],
                "time_period": "present",
            },
        )
        dbs.add(row)
        dbs.flush()
        _apply_prompt_payload_to_scene_row(
            row,
            {
                "scene": 1,
                "generation_mode": "story",
                "characters": [],
                "time_period": "past",
                "action": "Macro shot of a historical dessert.",
            },
        )
        dbs.commit()

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=run_id, scene_order=1).one()
        persisted = dict(row.prompt_meta_json)

    assert persisted["characters"] == []
    restored = {"scene": 1, "text": "Dessert macro", "duration_sec": 3}
    merge_scene_prompt_payload(restored, {"prompt_meta_json": persisted})
    assert restored["characters"] == []


def test_existing_generation_run_prompt_payload_survives_empty_session_sync(monkeypatch):
    _install_sqlite(monkeypatch)
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "text": "Scene text",
                    "duration_sec": 6,
                    "start_frame_prompt": "old start prompt",
                    "video_prompt_by_lang": {"ru": "old video prompt"},
                    "speech_by_lang": {"ru": "old speech"},
                }
            ]
        },
    }
    run_id, _scene_map = ensure_generation_run_for_session(session)

    session["production"]["json_plan"][0] = {
        "scene": 1,
        "text": "Scene text after media sync",
        "duration_sec": 6,
    }
    ensure_generation_run_for_session(session)

    with db.SessionLocal() as dbs:
        row = dbs.query(Scene).filter_by(run_id=uuid.UUID(run_id), scene_order=1).one()

    assert row.text == "Scene text after media sync"
    assert row.start_frame_prompt == "old start prompt"
    assert row.video_prompt_by_lang == {"ru": "old video prompt"}
    assert row.speech_by_lang == {"ru": "old speech"}


def test_generation_title_ignores_direct_recipe_placeholder():
    title = _human_title_for_session(
        {
            "selected_concept": "Рецепт пользователя",
            "recipe_full_text": "Огурцы - 4 шт.\nНарезать и посолить.",
        },
        [],
    )

    assert title == "Огурцы - 4 шт."


def test_existing_generation_run_placeholder_updates_from_overlay_title(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(
            GenerationRun(
                id=run_id,
                status="media_in_progress",
                language="ru",
                script="script",
                human_title="Рецепт пользователя",
            )
        )
        dbs.add(Scene(run_id=run_id, scene_order=1, text="Scene text", duration_sec=6))
        dbs.commit()

    session = {
        "generation_run_id": str(run_id),
        "selected_concept": "Рецепт пользователя",
        "overlay_title_by_lang": {"ru": "Малосольные огурцы", "en": "Quick Pickles"},
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "text": "Scene text",
                    "duration_sec": 6,
                }
            ],
        },
    }

    ensure_generation_run_for_session(session)

    with db.SessionLocal() as dbs:
        run = dbs.get(GenerationRun, run_id)

    assert run.human_title == "Малосольные огурцы"


def test_existing_generation_run_title_syncs_from_overlay_title(monkeypatch):
    _install_sqlite(monkeypatch)
    run_id = uuid.uuid4()
    with db.SessionLocal() as dbs:
        dbs.add(
            GenerationRun(
                id=run_id,
                status="media_in_progress",
                language="ru",
                script="script",
                human_title="Старый служебный концепт",
            )
        )
        dbs.add(Scene(run_id=run_id, scene_order=1, text="Scene text", duration_sec=6))
        dbs.commit()

    session = {
        "generation_run_id": str(run_id),
        "selected_concept": "Старый служебный концепт",
        "production": {
            "overlay_title_by_lang": {"ru": "Мой нормальный заголовок", "en": "My Normal Title"},
            "json_plan": [{"scene": 1, "text": "Scene text", "duration_sec": 6}],
        },
    }

    ensure_generation_run_for_session(session)

    with db.SessionLocal() as dbs:
        run = dbs.get(GenerationRun, run_id)
        artifact = dbs.query(GenerationArtifact).filter_by(run_id=run_id, artifact_type="overlay_title_by_lang").one()

    assert run.human_title == "Мой нормальный заголовок"
    assert artifact.payload_json["overlay_title_by_lang"] == {
        "ru": "Мой нормальный заголовок",
        "en": "My Normal Title",
    }
