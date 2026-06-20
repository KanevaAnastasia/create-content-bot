import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db, session_store
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


def test_save_and_load_session_restores_bytes_and_scene_candidates(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Сцена 1",
        "video_results_by_lang": {},
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
    session_store.save_session(101, session)

    loaded = session_store.load_session(101)

    assert loaded is not None
    assert loaded["first_step_custom_reference_images"] == [b"ref-image"]
    restored_scene = loaded["production"]["json_plan"][0]
    assert restored_scene["image_candidates"][0]["liked"] is True
    selected = resolve_scene_video_for_production(restored_scene, "ru")
    assert selected is not None
    assert selected["candidate_id"] == latest_video["candidate_id"]
    assert selected["speed_coef"] == 1.2


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
