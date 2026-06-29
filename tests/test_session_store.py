import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, session_store
from app.generation_store import _human_title_for_session
from app.models import ChatSession, GenerationArtifact, GenerationRun, Scene
from app.final_video_delivery import all_final_artifacts_delivered, deliver_final_artifact_to_telegram
from app.handlers.handlers_media_core import SyntaxFallbackPendingConfirmation, VideoProviderChoiceRequired
from app.handlers.handlers_state_commands import generate_video_command
from app.handlers.handlers_state_scene import on_message_reaction, on_text
from app.scene_media_candidates import (
    register_image_candidate,
    register_media_message_candidate,
    register_video_candidate,
    resolve_scene_video_for_production,
)
from app.scene_media_store import (
    persist_image_candidate,
    persist_message_link,
    persist_video_candidate,
    rebuild_production_session,
)
from app.state import SESSIONS


def _install_sqlite(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    return engine


def test_generation_title_extracts_structured_viral_brief():
    title = _human_title_for_session(
        {"viral_concept_brief": {"dish_name": "Вишнёвый пирог"}},
        [],
    )

    assert title == "Вишнёвый пирог"


def test_generation_title_prefers_russian_overlay_title():
    title = _human_title_for_session(
        {
            "selected_concept": "Старое название",
            "production": {
                "overlay_title_by_lang": {
                    "ru": "  Новый русский заголовок  ",
                    "en": "New English title",
                }
            },
        },
        [],
    )

    assert title == "Новый русский заголовок"


def test_save_and_load_session_restores_bytes_and_scene_candidates(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Сцена 1",
        "start_frame_image_b64": "inline-start-image",
        "video_results_by_lang": {"ru": {"video_b64": "inline-video", "estimated_cost_usd": 0.01}},
        "video_prompt_by_lang": {"ru": "lang:ru prompt"},
    }
    image_candidate = register_image_candidate(scene, kind="start", image_bytes=b"img-start")
    image_candidate["liked"] = True
    image_candidate["approved_for_video"] = True
    liked_video = register_video_candidate(scene, language="ru", video_bytes=b"liked-video")
    latest_video = register_video_candidate(scene, language="ru", video_bytes=b"latest-video")
    liked_video["liked"] = True
    liked_video["speed_coef"] = 1.7
    latest_video["approved_for_production"] = True
    latest_video["speed_coef"] = 1.2

    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru"],
        "first_step_custom_reference_images": [b"ref-image"],
        "production": {"json_plan": [scene]},
    }
    persist_image_candidate(session, scene_idx=0, candidate=image_candidate)
    persist_video_candidate(session, scene_idx=0, candidate=liked_video)
    persist_video_candidate(session, scene_idx=0, candidate=latest_video)
    with db.SessionLocal() as check_db:
        artifacts = check_db.query(GenerationArtifact).all()
    assert {item.artifact_type for item in artifacts} == {"scene_start_image", "scene_video_candidate"}
    session_store.save_session(101, session)
    with db.SessionLocal() as check_db:
        saved = check_db.query(ChatSession).filter(ChatSession.chat_id == 101).one()
        saved_json_text = str(saved.session_json)
    assert "inline-start-image" not in saved_json_text
    assert "inline-video" not in saved_json_text
    assert "image_b64" not in saved_json_text
    assert "video_b64" not in saved_json_text

    loaded = session_store.load_session(101)

    assert loaded is not None
    assert loaded["first_step_custom_reference_images"] == [b"ref-image"]
    restored_scene = loaded["production"]["json_plan"][0]
    assert restored_scene["image_candidates"][0]["liked"] is True
    selected = resolve_scene_video_for_production(restored_scene, "ru")
    assert selected is not None
    assert selected["candidate_id"] == latest_video["candidate_id"]
    assert selected["speed_coef"] == 1.2


def test_session_persists_run_cursor_and_restores_scene_prompts_from_db(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 1,
        "scene_media_substep": "video_prompt",
        "scene_media_language_index": 1,
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {"scene": 1, "duration_sec": 6, "text": "Сцена 1"},
                {
                    "scene": 2,
                    "duration_sec": 6,
                    "text": "Сцена 2",
                    "start_frame_prompt": "start db",
                    "start_frame_prompt_effective": "effective db",
                    "end_frame_prompt": "end db",
                    "video_prompt_by_lang": {"ru": "video ru"},
                    "speech_by_lang": {"ru": "speech ru"},
                    "syntax_prompt_by_lang": {"ru": {"prompt": "compact"}},
                },
            ]
        },
    }

    session_store.save_session(202, session)

    with db.SessionLocal() as check_db:
        run = check_db.query(GenerationRun).one()
        scene_two = check_db.query(Scene).filter(Scene.scene_order == 2).one()
        assert run.is_active is True
        assert run.current_scene_index == 1
        assert run.current_media_substep == "video_prompt"
        assert run.current_media_language_index == 1
        assert scene_two.start_frame_prompt == "start db"
        assert scene_two.video_prompt_by_lang == {"ru": "video ru"}

        scene_two.start_frame_prompt = "edited start"
        scene_two.video_prompt_by_lang = {"ru": "edited video"}
        check_db.commit()

    restored = session_store.load_session(202)

    assert restored is not None
    assert restored["image_scene_index"] == 1
    assert restored["scene_media_substep"] == "video_prompt"
    restored_scene = restored["production"]["json_plan"][1]
    assert restored_scene["start_frame_prompt"] == "edited start"
    assert restored_scene["video_prompt_by_lang"] == {"ru": "edited video"}


def test_final_video_delivery_marks_artifact_sent(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"mp4")

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_running", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        check_db.add(ChatSession(chat_id=303, state="ready_for_production", session_json={}, generation_run_id=run.id))
        artifact = GenerationArtifact(
            run_id=run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final video",
            uri=f"file://{video_path}",
            language="ru",
            mime_type="video/mp4",
            payload_json={},
        )
        check_db.add(artifact)
        check_db.commit()
        run_id = str(run.id)
        artifact_id = str(artifact.id)

    async def _fake_send(token, chat_id, path, caption):
        assert token == "token"
        assert chat_id == 303
        assert path == video_path
        assert "Final video" in caption
        return 777

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.final_video_delivery._send_video_file", _fake_send)

    delivery = deliver_final_artifact_to_telegram(run_id, artifact_id)

    assert delivery["status"] == "sent"
    assert delivery["telegram_message_id"] == 777
    with db.SessionLocal() as check_db:
        saved = check_db.get(GenerationArtifact, artifact.id)
        assert saved.payload_json["telegram_delivery"]["status"] == "sent"


def test_final_video_delivery_rejects_artifact_from_other_run(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"mp4")

    with db.SessionLocal() as check_db:
        owner_run = GenerationRun(status="final_render_running", language="ru", script="owner")
        other_run = GenerationRun(status="final_render_running", language="ru", script="other")
        check_db.add_all([owner_run, other_run])
        check_db.flush()
        check_db.add(ChatSession(chat_id=404, state="ready_for_production", session_json={}, generation_run_id=owner_run.id))
        artifact = GenerationArtifact(
            run_id=other_run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final video",
            uri=f"file://{video_path}",
            language="ru",
            mime_type="video/mp4",
            payload_json={},
        )
        check_db.add(artifact)
        check_db.commit()
        owner_run_id = str(owner_run.id)
        artifact_id = str(artifact.id)

    async def _unexpected_send(*_args, **_kwargs):
        raise AssertionError("must not send artifacts from another run")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.final_video_delivery._send_video_file", _unexpected_send)

    delivery = deliver_final_artifact_to_telegram(owner_run_id, artifact_id)

    assert delivery["status"] == "failed"
    assert delivery["last_error"] == "artifact does not belong to run"


def test_all_final_artifacts_delivered_uses_latest_video_per_track(monkeypatch):
    _install_sqlite(monkeypatch)
    now = datetime.now(timezone.utc)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        check_db.add_all(
            [
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_titled_subtitled_music",
                    label="Old final video",
                    uri="file:///tmp/old.mp4",
                    language="ru",
                    mime_type="video/mp4",
                    payload_json={},
                    created_at=now - timedelta(minutes=5),
                ),
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_titled_subtitled_music",
                    label="Latest final video",
                    uri="file:///tmp/latest.mp4",
                    language="ru",
                    mime_type="video/mp4",
                    payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 7}},
                    created_at=now,
                ),
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label="Build status",
                    uri="admin://final-build/status",
                    language="ru",
                    mime_type="application/json",
                    payload_json={},
                    created_at=now + timedelta(minutes=1),
                ),
            ]
        )
        check_db.commit()
        run_id = str(run.id)

    assert all_final_artifacts_delivered(run_id) is True


def test_reaction_like_works_after_memory_clear(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Сцена 1",
        "video_results_by_lang": {},
        "video_prompt_by_lang": {"ru": "lang:ru prompt"},
    }
    image_candidate = register_image_candidate(scene, kind="start", image_bytes=b"img")
    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
    }
    persist_image_candidate(session, scene_idx=0, candidate=image_candidate)
    register_media_message_candidate(
        session,
        message_id=77,
        scene_idx=0,
        media_type="image",
        candidate_id=image_candidate["candidate_id"],
        kind="start",
    )
    persist_message_link(
        session,
        chat_id=555,
        message_id=77,
        scene_idx=0,
        media_type="image",
        candidate_id=image_candidate["candidate_id"],
        kind="start",
    )
    session_store.save_session(555, session)

    SESSIONS.clear()

    update = SimpleNamespace(
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=555),
            message_id=77,
            new_reaction=[SimpleNamespace(type="emoji")],
        )
    )
    asyncio.run(on_message_reaction(update, SimpleNamespace()))

    SESSIONS.clear()
    restored = SESSIONS.get(555)

    assert restored is not None
    restored_scene = restored["production"]["json_plan"][0]
    assert restored_scene["image_candidates"][0]["liked"] is True


def test_rebuild_production_session_restores_message_index(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Сцена 1",
        "video_results_by_lang": {},
        "video_prompt_by_lang": {"ru": "lang:ru prompt"},
    }
    candidate = register_image_candidate(scene, kind="start", image_bytes=b"img")
    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
    }
    persist_image_candidate(session, scene_idx=0, candidate=candidate)
    persist_message_link(
        session,
        chat_id=808,
        message_id=909,
        scene_idx=0,
        media_type="image",
        candidate_id=candidate["candidate_id"],
        kind="start",
    )
    session_store.save_session(808, session)

    restored = session_store.load_session(808)
    rebuilt = rebuild_production_session(restored, restored["generation_run_id"])

    assert rebuilt is not None
    assert restored["scene_media_message_index"]["909"]["candidate_id"] == candidate["candidate_id"]


def test_on_text_persists_in_place_session_transition(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr("app.handlers.handlers_state_scene.DEBOUNCE_DELAY_SEC", 0)
    SESSIONS.clear()

    chat_id = 9091
    SESSIONS[chat_id] = {"state": "state_waiting_first_step_video_text"}

    class _Message:
        text = "6 Крупный план блюда на столе"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[chat_id]["state"] == "state_waiting_first_step_video_image"

    SESSIONS.clear()
    restored = SESSIONS.get(chat_id)

    assert restored is not None
    assert restored["state"] == "state_waiting_first_step_video_image"
    assert restored["first_step_video_duration_sec"] == 6
    assert restored["first_step_video_prompt"] == "Крупный план блюда на столе"


def test_on_text_start_over_delete_is_not_resaved_by_final_hook(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr("app.handlers.handlers_state_scene.DEBOUNCE_DELAY_SEC", 0)
    SESSIONS.clear()

    chat_id = 9092
    SESSIONS[chat_id] = {"state": "state_waiting_first_step_video_text"}

    class _Message:
        text = "В начало"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(on_text(update, context=None))

    SESSIONS.clear()

    assert SESSIONS.get(chat_id) is None


def test_video_command_pending_provider_choice_is_persisted(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    async def _fake_extract(_update, _context):
        return b"start-image"

    async def _fake_generate(*_args, **_kwargs):
        raise VideoProviderChoiceRequired(
            prompt="original prompt",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"start-image",
            end_image=None,
            quality="720",
            aspect_ratio="9:16",
            audio_enabled=True,
            syntax_error_message="no video",
        )

    monkeypatch.setattr("app.handlers.handlers_state_commands._extract_video_source_image", _fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_commands._generate_video_bytes_with_high_load_retry", _fake_generate)

    captured: list[str] = []

    class _Message:
        text = "/video 6 original prompt"
        caption = ""

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    chat_id = 9093
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(generate_video_command(update, context=None))

    assert any("Syntax" in item for item in captured)

    SESSIONS.clear()
    restored = SESSIONS.get(chat_id)

    assert restored is not None
    pending = restored["video_provider_choice_pending"]
    assert pending["source"] == "video_command"
    assert pending["original_prompt"] == "original prompt"
    assert pending["duration_for_caption"] == 6


def test_video_command_pending_syntax_confirmation_is_persisted(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    async def _fake_extract(_update, _context):
        return b"start-image"

    async def _fake_generate(*_args, **_kwargs):
        raise SyntaxFallbackPendingConfirmation(
            prompt="policy prompt",
            model="veo3_fast",
            duration_sec=6,
            start_image=b"start-image",
            end_image=None,
            quality="720",
            aspect_ratio="9:16",
            audio_enabled=True,
            vertex_error_message="policy",
        )

    monkeypatch.setattr("app.handlers.handlers_state_commands._extract_video_source_image", _fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_commands._generate_video_bytes_with_high_load_retry", _fake_generate)

    class _Message:
        text = "/video 6 policy prompt"
        caption = ""

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    chat_id = 9094
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(generate_video_command(update, context=None))

    SESSIONS.clear()
    restored = SESSIONS.get(chat_id)

    assert restored is not None
    pending = restored["syntax_fallback_pending"]
    assert pending["source"] == "video_command"
    assert pending["original_prompt"] == "policy prompt"
    assert pending["duration_for_caption"] == 6
