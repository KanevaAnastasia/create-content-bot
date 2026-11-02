from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.models import PipelineStepCheckpoint
from app.pipeline_history import record_checkpoint, restore_previous_checkpoint, restore_scene_prompt_fields_from_history
from app.handlers.handlers_state_scene import on_text
from app.state import SESSIONS


def _install_sqlite(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    return engine


def test_checkpoint_restore_skips_current_step_and_cleans_pending(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8101
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "production": {"json_plan": [{"scene": 1, "video_prompt_by_lang": {"ru": "old"}}]},
    }
    SESSIONS[chat_id] = session
    record_checkpoint(chat_id, session)

    session["scene_media_substep"] = "video_prompt"
    session["production"]["json_plan"][0]["video_provider_choice_pending"] = {"source": "scene"}
    session["scene_video_feedback_pending"] = {"scene_idx": 0}
    record_checkpoint(chat_id, session)

    restored = restore_previous_checkpoint(chat_id, session)

    assert restored is not None
    assert session["scene_media_substep"] == "start_prompt"
    assert "scene_video_feedback_pending" not in session
    assert "video_provider_choice_pending" not in session["production"]["json_plan"][0]


def test_checkpoint_restore_moves_backward_repeatedly_without_bouncing(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8105
    session = {"state": "waiting_concept", "concepts": [{"name": "one"}]}
    record_checkpoint(chat_id, session)
    session.update({"state": "waiting_recipe_approval", "recipe_full_text": "recipe"})
    record_checkpoint(chat_id, session)
    session.update({"state": "waiting_scene_approval", "scenes_plan": [{"scene": 1}]})
    record_checkpoint(chat_id, session)

    first = restore_previous_checkpoint(chat_id, session)
    second = restore_previous_checkpoint(chat_id, session)

    assert first is not None
    assert second is not None
    assert first["step_key"] == "recipe"
    assert second["step_key"] == "concept"
    assert session["state"] == "waiting_concept"


def test_checkpoint_record_updates_duplicate_visible_step(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8102
    session = {"state": "waiting_concept", "selected_concept": "a"}

    record_checkpoint(chat_id, session)
    session["selected_concept"] = "b"
    record_checkpoint(chat_id, session)

    with db.SessionLocal() as dbs:
        rows = dbs.query(PipelineStepCheckpoint).filter_by(chat_id=chat_id).all()

    assert len(rows) == 1
    assert rows[0].session_snapshot_json["selected_concept"] == "b"


def test_checkpoint_record_omits_large_media_payloads(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8106
    large_b64 = "a" * 250_000
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_prompt": "prompt",
                    "start_frame_image_b64": large_b64,
                    "video_candidates_by_lang": {"ru": [{"video_b64": large_b64}]},
                }
            ]
        },
    }

    record_checkpoint(chat_id, session)

    with db.SessionLocal() as dbs:
        row = dbs.query(PipelineStepCheckpoint).filter_by(chat_id=chat_id).one()

    scene = row.session_snapshot_json["production"]["json_plan"][0]
    assert scene["start_frame_prompt"] == "prompt"
    assert scene["start_frame_image_b64"] is None
    assert scene["video_candidates_by_lang"] == {}
    assert row.payload_json["checkpoint_omitted_media"]["omitted_values"] >= 2


def test_restore_scene_prompt_fields_uses_previous_non_empty_checkpoint(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8107
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_prompt": "old start prompt",
                    "video_prompt": "old legacy video prompt",
                    "video_prompt_by_lang": {"ru": "old ru video prompt", "en": "old en video prompt"},
                    "speech_by_lang": {"ru": "old speech"},
                }
            ]
        },
    }
    record_checkpoint(chat_id, session)

    current = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1, "text": "same scene"}]},
    }
    record_checkpoint(chat_id, current)

    restored = restore_scene_prompt_fields_from_history(chat_id, current, 0)

    scene = current["production"]["json_plan"][0]
    assert restored is True
    assert scene["start_frame_prompt"] == "old start prompt"
    assert scene["video_prompt"] == "old legacy video prompt"
    assert scene["video_prompt_by_lang"] == {"ru": "old ru video prompt", "en": "old en video prompt"}
    assert scene["speech_by_lang"] == {"ru": "old speech"}


def test_restore_scene_prompt_fields_does_not_cross_generation_run(monkeypatch):
    _install_sqlite(monkeypatch)
    chat_id = 8108
    other = {
        "generation_run_id": "11111111-1111-4111-8111-111111111111",
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1, "video_prompt_by_lang": {"ru": "wrong run prompt"}}]},
    }
    current = {
        "generation_run_id": "22222222-2222-4222-8222-222222222222",
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1, "text": "current scene"}]},
    }
    record_checkpoint(chat_id, other)
    record_checkpoint(chat_id, current)

    restored = restore_scene_prompt_fields_from_history(chat_id, current, 0)

    assert restored is False
    assert "video_prompt_by_lang" not in current["production"]["json_plan"][0]


def test_previous_button_uses_deterministic_media_substep_navigation(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr("app.handlers.handlers_state_scene.DEBOUNCE_DELAY_SEC", 0)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.restore_previous_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("media navigation must not restore an arbitrary checkpoint")
        ),
    )
    sent_steps: list[str] = []

    async def _fake_send_next(_update, session):
        sent_steps.append(session["scene_media_substep"])

    monkeypatch.setattr("app.handlers.handlers_state_scene._send_next_scene_media_step", _fake_send_next)

    chat_id = 8103
    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "production": {"json_plan": [{"scene": 1, "video_prompt_by_lang": {"ru": "prompt"}}]},
    }
    SESSIONS[chat_id] = session
    record_checkpoint(chat_id, session)
    session["scene_media_substep"] = "video_prompt"
    record_checkpoint(chat_id, session)

    class _Msg:
        text = "Предыдущий шаг"

        async def reply_text(self, _text, **_kwargs):
            return None

    import asyncio
    from types import SimpleNamespace

    asyncio.run(on_text(SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=chat_id)), context=None))

    assert SESSIONS[chat_id]["scene_media_substep"] == "start_prompt"
    assert sent_steps == ["start_prompt"]


def test_on_text_does_not_record_checkpoint_when_visible_step_is_unchanged(monkeypatch):
    _install_sqlite(monkeypatch)
    monkeypatch.setattr("app.handlers.handlers_state_scene.DEBOUNCE_DELAY_SEC", 0)
    calls: list[int] = []
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.record_checkpoint",
        lambda chat_id, _session: calls.append(chat_id),
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.save_loaded_session_if_possible",
        lambda _chat_id: None,
    )
    chat_id = 8104
    SESSIONS[chat_id] = {"state": "refining_scenes"}

    class _Msg:
        text = "ещё сообщение"

        async def reply_text(self, _text, **_kwargs):
            return None

    import asyncio
    from types import SimpleNamespace

    asyncio.run(on_text(SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=chat_id)), context=None))

    assert calls == []
