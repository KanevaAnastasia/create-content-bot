import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db, final_video_delivery, session_store
from app.drafts import clear_scene_db_approvals_for_repair
from app.generation_store import _human_title_for_session
from app.models import (
    ChatSession,
    GenerationArtifact,
    GenerationDraft,
    GenerationRun,
    Scene,
    SceneImageCandidate,
    SceneTextRevision,
    SceneVideoCandidate,
)
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


def test_clear_scene_db_approvals_for_video_repair_updates_rows_and_artifacts(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(run_id=run.id, scene_order=3, text="scene", duration_sec=6)
        other_scene = Scene(run_id=run.id, scene_order=4, text="other", duration_sec=6)
        check_db.add_all([scene, other_scene])
        check_db.flush()
        check_db.add_all(
            [
                SceneImageCandidate(
                    candidate_id="img-scene-4",
                    scene_id=scene.id,
                    kind="start",
                    approved_for_video=True,
                ),
                SceneVideoCandidate(
                    candidate_id="vid-ru-scene-4",
                    scene_id=scene.id,
                    language="ru",
                    approved_for_production=True,
                    speed_coef=1.2,
                ),
                SceneVideoCandidate(
                    candidate_id="vid-en-scene-4",
                    scene_id=scene.id,
                    language="en",
                    approved_for_production=True,
                    speed_coef=1.2,
                ),
                SceneVideoCandidate(
                    candidate_id="vid-ru-scene-5",
                    scene_id=other_scene.id,
                    language="ru",
                    approved_for_production=True,
                    speed_coef=1.2,
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_video_candidate",
                    label="ru",
                    uri="db://scene_video_candidates/vid-ru-scene-4",
                    language="ru",
                    mime_type="video/mp4",
                    payload_json={"candidate_id": "vid-ru-scene-4", "approved_for_production": True},
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_video_candidate",
                    label="en",
                    uri="db://scene_video_candidates/vid-en-scene-4",
                    language="en",
                    mime_type="video/mp4",
                    payload_json={"candidate_id": "vid-en-scene-4", "approved_for_production": True},
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_start_image",
                    label="image",
                    uri="db://scene_image_candidates/img-scene-4",
                    mime_type="image/png",
                    payload_json={"candidate_id": "img-scene-4", "approved_for_video": True},
                ),
            ]
        )
        check_db.commit()
        scene_id = str(scene.id)

    clear_scene_db_approvals_for_repair(scene_id, stage="video", languages=["ru"])

    with db.SessionLocal() as check_db:
        image = check_db.query(SceneImageCandidate).filter_by(candidate_id="img-scene-4").one()
        video_ru = check_db.query(SceneVideoCandidate).filter_by(candidate_id="vid-ru-scene-4").one()
        video_en = check_db.query(SceneVideoCandidate).filter_by(candidate_id="vid-en-scene-4").one()
        other_video_ru = check_db.query(SceneVideoCandidate).filter_by(candidate_id="vid-ru-scene-5").one()
        artifacts = {row.uri: row.payload_json for row in check_db.query(GenerationArtifact).all()}

    assert image.approved_for_video is True
    assert video_ru.approved_for_production is False
    assert video_en.approved_for_production is True
    assert other_video_ru.approved_for_production is True
    assert artifacts["db://scene_video_candidates/vid-ru-scene-4"]["approved_for_production"] is False
    assert artifacts["db://scene_video_candidates/vid-en-scene-4"]["approved_for_production"] is True
    assert artifacts["db://scene_image_candidates/img-scene-4"]["approved_for_video"] is True


def test_clear_scene_db_approvals_for_image_repair_updates_image_and_video_artifacts(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(run_id=run.id, scene_order=3, text="scene", duration_sec=6)
        check_db.add(scene)
        check_db.flush()
        check_db.add_all(
            [
                SceneImageCandidate(
                    candidate_id="img-start-scene-4",
                    scene_id=scene.id,
                    kind="start",
                    approved_for_video=True,
                ),
                SceneVideoCandidate(
                    candidate_id="vid-ru-scene-4",
                    scene_id=scene.id,
                    language="ru",
                    approved_for_production=True,
                    speed_coef=1.2,
                ),
                SceneVideoCandidate(
                    candidate_id="vid-en-scene-4",
                    scene_id=scene.id,
                    language="en",
                    approved_for_production=True,
                    speed_coef=1.2,
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_start_image",
                    label="image",
                    uri="db://scene_image_candidates/img-start-scene-4",
                    mime_type="image/png",
                    payload_json={"candidate_id": "img-start-scene-4", "approved_for_video": True},
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_video_candidate",
                    label="ru",
                    uri="db://scene_video_candidates/vid-ru-scene-4",
                    language="ru",
                    mime_type="video/mp4",
                    payload_json={"candidate_id": "vid-ru-scene-4", "approved_for_production": True},
                ),
                GenerationArtifact(
                    run_id=run.id,
                    scene_id=scene.id,
                    artifact_type="scene_video_candidate",
                    label="en",
                    uri="db://scene_video_candidates/vid-en-scene-4",
                    language="en",
                    mime_type="video/mp4",
                    payload_json={"candidate_id": "vid-en-scene-4", "approved_for_production": True},
                ),
            ]
        )
        check_db.commit()
        scene_id = str(scene.id)

    clear_scene_db_approvals_for_repair(scene_id, stage="image", languages=["ru"])

    with db.SessionLocal() as check_db:
        image = check_db.query(SceneImageCandidate).filter_by(candidate_id="img-start-scene-4").one()
        video_ru = check_db.query(SceneVideoCandidate).filter_by(candidate_id="vid-ru-scene-4").one()
        video_en = check_db.query(SceneVideoCandidate).filter_by(candidate_id="vid-en-scene-4").one()
        artifacts = {row.uri: row.payload_json for row in check_db.query(GenerationArtifact).all()}

    assert image.approved_for_video is False
    assert video_ru.approved_for_production is False
    assert video_en.approved_for_production is True
    assert artifacts["db://scene_image_candidates/img-start-scene-4"]["approved_for_video"] is False
    assert artifacts["db://scene_video_candidates/vid-ru-scene-4"]["approved_for_production"] is False
    assert artifacts["db://scene_video_candidates/vid-en-scene-4"]["approved_for_production"] is True


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


def test_save_session_syncs_existing_run_scene_plan_revision(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="media_in_progress", language="ru", script="recipe", human_title="Пастила")
        check_db.add(run)
        check_db.flush()
        scene = Scene(run_id=run.id, scene_order=1, text="Старая первая фраза", duration_sec=6)
        check_db.add(scene)
        check_db.commit()
        run_id = str(run.id)

    session = {
        "state": "waiting_scene_approval",
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: str(scene.id)},
        "scenes_plan": [
            {
                "scene": 1,
                "text": "Не знаете куда девать яблоки? Присаживайтесь, будем делать пастилу!",
                "duration_sec": 6,
            }
        ],
    }

    session_store.save_session(305928305, session)

    with db.SessionLocal() as check_db:
        saved_scene = check_db.query(Scene).filter_by(run_id=uuid.UUID(run_id), scene_order=1).one()
        revisions = check_db.query(SceneTextRevision).filter_by(run_id=uuid.UUID(run_id)).all()

    assert saved_scene.text == "Не знаете куда девать яблоки? Присаживайтесь, будем делать пастилу!"
    assert revisions
    assert revisions[0].old_text == "Старая первая фраза"
    assert revisions[0].new_text == "Не знаете куда девать яблоки? Присаживайтесь, будем делать пастилу!"


def test_load_session_restores_scene_refine_context_from_existing_run(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(
            status="media_in_progress",
            language="ru",
            script="Яблоки, вода, сахар.\nСварить пастилу.",
            human_title="Яблочная пастила",
        )
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Старая первая фраза",
            duration_sec=6,
        )
        check_db.add(scene)
        check_db.add(
            ChatSession(
                chat_id=305928305,
                state="waiting_scene_approval",
                generation_run_id=run.id,
                session_json={"state": "waiting_scene_approval", "generation_run_id": str(run.id)},
            )
        )
        check_db.commit()

    loaded = session_store.load_session(305928305)

    assert loaded["recipe_full_text"] == "Яблоки, вода, сахар.\nСварить пастилу."
    assert loaded["selected_concept"] == "Яблочная пастила"
    assert loaded["selected_concept_name"] == "Яблочная пастила"
    assert loaded["scenes_plan"][0]["text"] == "Старая первая фраза"
    assert loaded["production"]["json_plan"][0]["text"] == "Старая первая фраза"


def test_load_session_restores_overlay_title_by_lang_from_run_artifact(monkeypatch):
    _install_sqlite(monkeypatch)

    title_by_lang = {"ru": "Борщ с пампушками", "en": "Borscht with Pampushky"}
    with db.SessionLocal() as check_db:
        run = GenerationRun(
            status="media_in_progress",
            language="ru",
            script="Свекла, капуста, пампушки.",
            human_title="Борщ с пампушками",
        )
        check_db.add(run)
        check_db.flush()
        check_db.add(Scene(run_id=run.id, scene_order=1, text="Готовим борщ", duration_sec=6))
        check_db.add(
            GenerationArtifact(
                run_id=run.id,
                scene_id=None,
                artifact_type="overlay_title_by_lang",
                label="Overlay title by language",
                uri=f"db://generation_runs/{run.id}/overlay_title_by_lang",
                language=None,
                mime_type="application/json",
                payload_json={"overlay_title_by_lang": title_by_lang},
            )
        )
        check_db.add(
            ChatSession(
                chat_id=305928306,
                state="waiting_scene_media",
                generation_run_id=run.id,
                session_json={"state": "waiting_scene_media", "generation_run_id": str(run.id)},
            )
        )
        check_db.commit()

    loaded = session_store.load_session(305928306)

    assert loaded["overlay_title_by_lang"] == title_by_lang
    assert loaded["production"]["overlay_title_by_lang"] == title_by_lang


def test_load_session_keeps_cached_overlay_title_over_stale_artifact(monkeypatch):
    _install_sqlite(monkeypatch)

    cached_title = {"ru": "Кабачковая икра", "en": "Zucchini Caviar"}
    stale_title = {"ru": "Кабачковая икра", "en": "Kabachkovaya ikra"}
    with db.SessionLocal() as check_db:
        run = GenerationRun(
            status="media_in_progress",
            language="ru",
            script="Кабачки, морковь, лук.",
            human_title="Кабачковая икра",
        )
        check_db.add(run)
        check_db.flush()
        check_db.add(Scene(run_id=run.id, scene_order=1, text="Готовим икру", duration_sec=6))
        check_db.add(
            GenerationArtifact(
                run_id=run.id,
                scene_id=None,
                artifact_type="overlay_title_by_lang",
                label="Overlay title by language",
                uri=f"db://generation_runs/{run.id}/overlay_title_by_lang",
                language=None,
                mime_type="application/json",
                payload_json={"overlay_title_by_lang": stale_title},
            )
        )
        check_db.add(
            ChatSession(
                chat_id=305928307,
                state="waiting_scene_media",
                generation_run_id=run.id,
                session_json={
                    "state": "waiting_scene_media",
                    "generation_run_id": str(run.id),
                    "overlay_title_by_lang": cached_title,
                },
            )
        )
        check_db.commit()

    loaded = session_store.load_session(305928307)

    assert loaded["overlay_title_by_lang"] == cached_title
    assert loaded["production"]["overlay_title_by_lang"] == cached_title


def test_recover_interrupted_guest_syntax_sessions_updates_chat_and_run_snapshot(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="guest_in_progress", language="ru", script="Гостевая сцена")
        check_db.add(run)
        check_db.flush()
        check_db.add_all(
            [
                ChatSession(
                    chat_id=606,
                    state=session_store.GUEST_SYNTAX_PENDING_STATE,
                    generation_run_id=run.id,
                    session_json={
                        "state": session_store.GUEST_SYNTAX_PENDING_STATE,
                        "guest_mode": True,
                        "guest_data": {"syntax_retry_target": "video"},
                    },
                ),
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type=session_store.GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
                    label="Состояние гостевой генерации",
                    uri=f"db://generation_runs/{run.id}/guest_session",
                    mime_type="application/json",
                    payload_json={
                        "session": {
                            "state": session_store.GUEST_SYNTAX_PENDING_STATE,
                            "guest_mode": True,
                        }
                    },
                ),
                ChatSession(
                    chat_id=607,
                    state="guest_video_approval",
                    session_json={"state": "guest_video_approval", "guest_mode": True},
                ),
            ]
        )
        check_db.commit()

    assert session_store.recover_interrupted_guest_syntax_sessions() == 1

    with db.SessionLocal() as check_db:
        recovered = check_db.execute(select(ChatSession).where(ChatSession.chat_id == 606)).scalar_one()
        unaffected = check_db.execute(select(ChatSession).where(ChatSession.chat_id == 607)).scalar_one()
        snapshot = check_db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == run.id,
                GenerationArtifact.artifact_type == session_store.GUEST_RUN_SNAPSHOT_ARTIFACT_TYPE,
            )
        ).scalar_one()

    assert recovered.state == session_store.GUEST_SYNTAX_RETRY_STATE
    assert recovered.session_json["state"] == session_store.GUEST_SYNTAX_RETRY_STATE
    assert recovered.session_json["guest_data"] == {"syntax_retry_target": "video"}
    assert snapshot.payload_json["session"]["state"] == session_store.GUEST_SYNTAX_RETRY_STATE
    assert unaffected.state == "guest_video_approval"


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
        image_rows = check_db.query(SceneImageCandidate).all()
        video_rows = check_db.query(SceneVideoCandidate).all()
    assert {item.artifact_type for item in artifacts} == {"scene_start_image", "scene_video_candidate"}
    assert image_rows[0].image_blob is None
    assert image_rows[0].media_uri.startswith("file://")
    assert image_rows[0].byte_size == len(b"img-start")
    assert all(row.video_blob is None for row in video_rows)
    assert all(str(row.media_uri or "").startswith("file://") for row in video_rows)
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


def test_video_candidate_persistence_drops_zero_duration_speech_cues(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Первая часть [пауза] Вторая часть",
        "speech_by_lang": {"ru": "Первая часть [пауза] Вторая часть"},
        "speech_cues_by_lang": {
            "ru": [
                {"text": "Первая часть", "start_sec": 0.0, "end_sec": 0.0},
                {"text": "Вторая часть", "start_sec": 0.0, "end_sec": 0.0},
            ]
        },
    }
    candidate = register_video_candidate(scene, language="ru", video_bytes=b"video")
    candidate["approved_for_production"] = True
    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru"],
        "production": {"json_plan": [scene]},
    }

    persist_video_candidate(session, scene_idx=0, candidate=candidate)

    assert "speech_cues_by_lang" not in candidate
    with db.SessionLocal() as check_db:
        artifact = check_db.query(GenerationArtifact).filter_by(artifact_type="scene_video_candidate").one()
        row = check_db.query(SceneVideoCandidate).filter_by(candidate_id=candidate["candidate_id"]).one()
        assert "speech_cues_by_lang" not in artifact.payload_json
        assert row.speech_by_lang == {"ru": "Первая часть [пауза] Вторая часть"}
        assert row.speech_cues_by_lang == {}

    rebuild_production_session(session, session.get("generation_run_id"))
    restored = session["production"]["json_plan"][0]["video_candidates_by_lang"]["ru"][0]

    assert "speech_cues_by_lang" not in restored


def test_save_session_persists_video_prompts_as_scene_artifacts(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    session = {
        "state": "waiting_scene_media",
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "duration_sec": 6,
                    "text": "Сцена 1",
                    "video_prompt_by_lang": {"ru": "ru prompt 1", "en": "en prompt 1"},
                },
                {
                    "scene": 2,
                    "duration_sec": 6,
                    "text": "Сцена 2",
                    "video_prompt_by_lang": {"ru": "ru prompt 2"},
                },
            ]
        },
    }

    session_store.save_session(313, session)

    with db.SessionLocal() as check_db:
        scenes = check_db.query(Scene).order_by(Scene.scene_order).all()
        artifacts = (
            check_db.query(GenerationArtifact)
            .filter_by(artifact_type="scene_video_prompt")
            .order_by(GenerationArtifact.language)
            .all()
        )

    assert scenes[0].video_prompt_by_lang == {"ru": "ru prompt 1", "en": "en prompt 1"}
    assert scenes[1].video_prompt_by_lang == {"ru": "ru prompt 2"}
    with db.SessionLocal() as check_db:
        saved = check_db.query(ChatSession).filter_by(chat_id=313).one()
        assert "production" not in saved.session_json
        assert "scenes_plan" not in saved.session_json
    assert {(row.scene_id, row.language, row.payload_json["prompt"]) for row in artifacts} == {
        (scenes[0].id, "en", "en prompt 1"),
        (scenes[0].id, "ru", "ru prompt 1"),
        (scenes[1].id, "ru", "ru prompt 2"),
    }


def test_rebuild_production_session_restores_video_prompt_from_scene_artifact(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Сцена",
            duration_sec=4,
            video_prompt_by_lang={},
        )
        check_db.add(scene)
        check_db.flush()
        check_db.add(
            GenerationArtifact(
                run_id=run.id,
                scene_id=scene.id,
                artifact_type="scene_video_prompt",
                label="Scene 1 video prompt (ru)",
                uri=f"db://scenes/{scene.id}/video_prompt/ru",
                language="ru",
                mime_type="text/plain",
                payload_json={"prompt": "artifact prompt", "video_prompt_by_lang": {"ru": "artifact prompt"}},
            )
        )
        check_db.commit()
        run_id = str(run.id)
        scene_id = str(scene.id)

    session = {
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: scene_id},
        "production": {"json_plan": [{"scene": 1, "duration_sec": 4, "text": "Сцена"}]},
    }

    rebuild_production_session(session, run_id, include_media_blobs=False)

    assert session["production"]["json_plan"][0]["video_prompt_by_lang"] == {"ru": "artifact prompt"}


def test_rebuild_production_session_restores_scene_metadata_from_prompt_meta(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Сцена",
            duration_sec=6,
            video_prompt_by_lang={"ru": "video"},
            prompt_meta_json={
                "characters": ["grandma", "grandson"],
                "dialogue_mode": "multi",
                "dialogue": [
                    {"speaker": "grandma", "text": "Угощайтесь"},
                    {"speaker": "grandson", "text": "Очень вкусно, бабушка!"},
                ],
                "action": "Внук пробует ломтик.",
            },
        )
        check_db.add(scene)
        check_db.commit()
        run_id = str(run.id)
        scene_id = str(scene.id)

    session = {
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: scene_id},
        "production": {"json_plan": [{"scene": 1, "duration_sec": 6, "text": "Сцена"}]},
    }

    rebuild_production_session(session, run_id, include_media_blobs=False)

    scene = session["production"]["json_plan"][0]
    assert scene["characters"] == ["grandma", "cat", "grandson"]
    assert scene["dialogue"][1]["speaker"] == "grandson"
    assert scene["action"] == "Внук пробует ломтик."


def test_rebuild_production_session_infers_grandson_from_video_prompt_when_meta_missing(monkeypatch):
    _install_sqlite(monkeypatch)
    prompt = (
        "6-second clip. Her внук, a cartoon boy, reaches for a slice. "
        "CHARACTERS SPEAKS: Угощайтесь [пауза] Очень вкусно, бабушка!\n"
        "speaker_dialogue_order: GRANDMA, GRANDSON\n"
        "character_dialogue_script:\n"
        "GRANDMA: Угощайтесь\n"
        "GRANDSON: Очень вкусно, бабушка!\n"
        "voice_lock_full_grandson: male child voice Russian language."
    )

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Угощайтесь [пауза] Очень вкусно, бабушка!",
            duration_sec=6,
            video_prompt_by_lang={"ru": prompt},
        )
        check_db.add(scene)
        check_db.commit()
        run_id = str(run.id)
        scene_id = str(scene.id)

    session = {
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: scene_id},
        "production": {"json_plan": [{"scene": 1, "duration_sec": 6, "text": "Угощайтесь"}]},
    }

    rebuild_production_session(session, run_id, include_media_blobs=False)

    scene = session["production"]["json_plan"][0]
    assert scene["characters"] == ["grandma", "cat", "grandson"]
    assert scene["dialogue_mode"] == "multi"
    assert scene["dialogue"] == [
        {"speaker": "grandma", "text": "Угощайтесь"},
        {"speaker": "grandson", "text": "Очень вкусно, бабушка!"},
    ]


def test_load_session_restores_pending_input_from_artifact_when_snapshot_lost(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    pending = {"scene_idx": 0, "target": "start", "mode": "prompt_feedback"}
    session = {
        "state": "waiting_scene_media",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "duration_sec": 6,
                    "text": "Сцена",
                    "video_prompt_by_lang": {"ru": "video prompt"},
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "start_image_approval",
        "scene_image_edit_pending": pending,
    }

    session_store.save_session(414, session)

    with db.SessionLocal() as check_db:
        artifact = check_db.query(GenerationArtifact).filter_by(artifact_type="scene_pending_input").one()
        saved = check_db.query(ChatSession).filter_by(chat_id=414).one()
        assert "scene_image_edit_pending" not in saved.session_json
        check_db.commit()

    loaded = session_store.load_session(414)

    assert artifact.payload_json["session_key"] == "scene_image_edit_pending"
    assert loaded["scene_image_edit_pending"] == pending


def test_save_session_uses_chat_session_only_as_minimal_cache(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    session = {
        "state": "waiting_scene_media",
        "recipe_full_text": "Не сохранять это в chat session",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "duration_sec": 6,
                    "text": "Сцена",
                    "start_frame_prompt": "start prompt",
                    "video_prompt_by_lang": {"ru": "video prompt"},
                }
            ]
        },
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "scene_image_edit_pending": {"scene_idx": 0, "target": "start", "mode": "prompt_feedback"},
    }

    session_store.save_session(515, session)

    with db.SessionLocal() as check_db:
        saved = check_db.query(ChatSession).filter_by(chat_id=515).one()
        scene = check_db.query(Scene).one()
        pending = check_db.query(GenerationArtifact).filter_by(artifact_type="scene_pending_input").one()

    assert saved.session_json == {
        "state": "waiting_scene_media",
        "generation_run_id": session["generation_run_id"],
        "scene_db_ids_by_index": {"0": str(scene.id)},
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
    }
    assert "production" not in saved.session_json
    assert "recipe_full_text" not in saved.session_json
    assert "scene_image_edit_pending" not in saved.session_json
    assert scene.video_prompt_by_lang == {"ru": "video prompt"}
    assert pending.payload_json["session_key"] == "scene_image_edit_pending"


def test_final_build_queue_persists_only_minimal_chat_session_cache(monkeypatch):
    _install_sqlite(monkeypatch)

    from app.final_build_queue import _persist_queue_session_snapshot

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.commit()
        run_id = run.id

    session = {
        "state": "waiting_scene_media",
        "generation_run_id": str(run_id),
        "recipe_full_text": "Не класть в chat session",
        "production": {"json_plan": [{"scene": 1, "video_prompt_by_lang": {"ru": "prompt"}}]},
        "image_scene_index": 2,
        "scene_media_substep": "final_build",
        "scene_video_feedback_pending": {"scene_idx": 0, "language": "ru"},
    }

    _persist_queue_session_snapshot(616, session, run_id)

    with db.SessionLocal() as check_db:
        saved = check_db.query(ChatSession).filter_by(chat_id=616).one()

    assert saved.session_json == {
        "state": "waiting_scene_media",
        "generation_run_id": str(run_id),
        "image_scene_index": 2,
        "scene_media_substep": "final_build",
    }
    assert "production" not in saved.session_json
    assert "recipe_full_text" not in saved.session_json
    assert "scene_video_feedback_pending" not in saved.session_json


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


def test_save_session_updates_existing_generation_run_cursor(monkeypatch):
    _install_sqlite(monkeypatch)
    SESSIONS.clear()

    session = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "start_prompt",
        "scene_media_language_index": 0,
        "target_languages": ["ru", "en"],
        "production": {
            "json_plan": [
                {"scene": 1, "duration_sec": 6, "text": "Сцена 1"},
                {"scene": 2, "duration_sec": 6, "text": "Сцена 2"},
                {"scene": 3, "duration_sec": 6, "text": "Сцена 3"},
                {"scene": 4, "duration_sec": 6, "text": "Сцена 4"},
            ]
        },
    }
    session_store.save_session(222, session)
    run_id = session["generation_run_id"]

    session["image_scene_index"] = 3
    session["scene_media_substep"] = "video_prompt"
    session["scene_media_language_index"] = 1
    session_store.save_session(222, session)

    with db.SessionLocal() as check_db:
        run = check_db.get(GenerationRun, uuid.UUID(str(run_id)))
        assert run.current_scene_index == 3
        assert run.current_media_substep == "video_prompt"
        assert run.current_media_language_index == 1

    restored = session_store.load_session(222)

    assert restored["image_scene_index"] == 3
    assert restored["scene_media_substep"] == "video_prompt"
    assert restored["scene_media_language_index"] == 1


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


def test_final_video_delivery_uses_generation_draft_chat_when_session_moved(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"mp4")

    with db.SessionLocal() as check_db:
        old_run = GenerationRun(status="final_video_delivery_failed", language="ru", script="old recipe")
        active_run = GenerationRun(status="media_in_progress", language="ru", script="new recipe")
        check_db.add_all([old_run, active_run])
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=303,
                state="waiting_scene_media",
                session_json={"generation_run_id": str(active_run.id)},
                generation_run_id=active_run.id,
            )
        )
        check_db.add(
            GenerationDraft(
                chat_id=303,
                run_id=old_run.id,
                title="Old draft",
                session_snapshot_json={"recipe_full_text": "old recipe"},
                payload_json={},
            )
        )
        artifact = GenerationArtifact(
            run_id=old_run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final video",
            uri=f"file://{video_path}",
            language="ru",
            mime_type="video/mp4",
            payload_json={},
        )
        check_db.add(artifact)
        check_db.commit()
        run_id = str(old_run.id)
        artifact_id = str(artifact.id)

    async def _fake_send(token, chat_id, path, caption):
        assert token == "token"
        assert chat_id == 303
        assert path == video_path
        assert "Final video" in caption
        return 778

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.final_video_delivery._send_video_file", _fake_send)

    delivery = deliver_final_artifact_to_telegram(run_id, artifact_id)

    assert delivery["status"] == "sent"
    assert delivery["telegram_message_id"] == 778


def test_final_video_delivery_sends_cloud_copy_for_large_file(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"large")
    delivery_path = tmp_path / "final.telegram.mp4"

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_video_delivery_failed", language="ru", script="recipe")
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

    def _fake_prepare(path, *, run_uuid, artifact_uuid):
        assert path == video_path
        _ = (run_uuid, artifact_uuid)
        delivery_path.write_bytes(b"small")
        return delivery_path, {
            "original_uri": f"file://{video_path}",
            "sent_uri": f"file://{delivery_path}",
            "compressed_for_telegram": True,
        }

    async def _fake_send(token, chat_id, path, caption):
        assert token == "token"
        assert chat_id == 303
        assert path == delivery_path
        assert "Telegram delivery copy" in caption
        return 779

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.final_video_delivery._prepare_telegram_delivery_file", _fake_prepare)
    monkeypatch.setattr("app.final_video_delivery._send_video_file", _fake_send)

    delivery = deliver_final_artifact_to_telegram(run_id, artifact_id)

    assert delivery["status"] == "sent"
    assert delivery["compressed_for_telegram"] is True
    assert delivery["original_uri"] == f"file://{video_path}"
    assert delivery["sent_uri"] == f"file://{delivery_path}"


def test_final_video_delivery_triggers_social_descriptions_once(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"mp4")

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_running", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=303,
                state="ready_for_production",
                session_json={
                    "state": "ready_for_production",
                    "recipe_full_text": "Ингредиенты: картофель.",
                    "generation_run_id": str(run.id),
                },
                generation_run_id=run.id,
            )
        )
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
        _ = (token, chat_id, path, caption)
        return 777

    calls: list[int] = []

    async def _fake_social_descriptions(update, session):
        from app.handlers import handlers_state_media

        calls.append(update.effective_chat.id)
        session["state"] = handlers_state_media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
        session[handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY] = {
            "recipe": session["recipe_full_text"],
            "copy": {"instagram_ru": "Описание"},
            "origin": "production_final",
        }
        return True

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.final_video_delivery._send_video_file", _fake_send)
    monkeypatch.setattr("app.handlers.handlers_state_media._send_final_social_descriptions", _fake_social_descriptions)

    delivery = deliver_final_artifact_to_telegram(run_id, artifact_id)
    duplicate = deliver_final_artifact_to_telegram(run_id, artifact_id)

    assert delivery["status"] == "sent"
    assert duplicate["status"] == "sent"
    assert calls == [303]
    restored = session_store.load_session(303)
    assert restored is not None
    assert restored["state"] == "waiting_social_descriptions_approval"
    assert "social_descriptions_pending" in restored


def test_failed_final_build_does_not_block_social_descriptions(monkeypatch):
    _install_sqlite(monkeypatch)
    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_failed", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=303,
                state="ready_for_production",
                session_json={
                    "state": "ready_for_production",
                    "recipe_full_text": "Ингредиенты: картофель.",
                    "generation_run_id": str(run.id),
                },
                generation_run_id=run.id,
            )
        )
        for variant, status in (("clean", "success"), ("titled_subtitled_music", "failed")):
            check_db.add(
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label=f"Final build {variant}",
                    uri=f"admin://final-build/{run.id}/ru/{variant}",
                    language="ru",
                    mime_type="application/json",
                    payload_json={"language": "ru", "variant": variant, "status": status},
                )
            )
        check_db.commit()
        run_id = str(run.id)

    calls: list[int] = []

    async def _fake_social_descriptions(update, session):
        calls.append(update.effective_chat.id)
        session["social_descriptions_pending"] = {"recipe": session["recipe_full_text"]}
        return True

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        "app.handlers.handlers_state_media._send_final_social_descriptions",
        _fake_social_descriptions,
    )

    final_video_delivery.trigger_final_completion_followup(run_id)

    assert calls == [303]
    restored = session_store.load_session(303)
    assert restored is not None
    assert restored["social_descriptions_pending"]["recipe"] == "Ингредиенты: картофель."


def test_final_followup_does_not_relabel_unrelated_admin_session(monkeypatch):
    _install_sqlite(monkeypatch)
    with db.SessionLocal() as check_db:
        completed_run = GenerationRun(status="final_videos_delivered", language="ru", script="old story")
        active_run = GenerationRun(status="media_in_progress", language="ru", script="active story")
        check_db.add_all([completed_run, active_run])
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=303,
                state="waiting_scene_media",
                session_json={
                    "state": "waiting_scene_media",
                    "generation_run_id": str(active_run.id),
                    "recipe_full_text": "active story",
                },
                generation_run_id=active_run.id,
            )
        )
        check_db.add(
            GenerationArtifact(
                run_id=completed_run.id,
                artifact_type="final_build_status",
                label="Final build titled",
                uri=f"admin://final-build/{completed_run.id}/ru/titled_subtitled_music",
                language="ru",
                mime_type="application/json",
                payload_json={
                    "language": "ru",
                    "variant": "titled_subtitled_music",
                    "status": "success",
                },
            )
        )
        check_db.commit()
        completed_run_id = str(completed_run.id)
        active_run_id = str(active_run.id)

    calls: list[int] = []

    async def _unexpected_social_descriptions(update, session):
        calls.append(update.effective_chat.id)
        return True

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "303")
    monkeypatch.setattr(
        "app.handlers.handlers_state_media._send_final_social_descriptions",
        _unexpected_social_descriptions,
    )

    final_video_delivery.trigger_final_completion_followup(completed_run_id)

    assert calls == []
    restored = session_store.load_session(303)
    assert restored is not None
    assert restored["generation_run_id"] == active_run_id
    assert restored["recipe_full_text"] == "active story"


def test_guest_final_delivery_opens_cartoon_review_once(monkeypatch):
    _install_sqlite(monkeypatch)
    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_running", language="ru", script="cartoon", workflow_type="cartoon")
        check_db.add(run)
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=404,
                state="guest_menu",
                session_json={
                    "state": "guest_menu",
                    "guest_mode": True,
                    "guest_data": {"approved_scenes": [{"scene": 1}]},
                    "generation_run_id": str(run.id),
                },
                generation_run_id=run.id,
            )
        )
        artifacts = []
        for variant in ("clean", "upscaled_4k"):
            artifact = GenerationArtifact(
                run_id=run.id,
                artifact_type=f"final_{variant}",
                label=variant,
                uri=f"file:///tmp/{variant}.mp4",
                language="ru",
                mime_type="video/mp4",
                payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 10}},
            )
            check_db.add(artifact)
            check_db.flush()
            artifacts.append(artifact)
            check_db.add(
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label=f"status {variant}",
                    uri=f"admin://final-build/{run.id}/ru/{variant}",
                    language="ru",
                    mime_type="application/json",
                    payload_json={
                        "status": "success",
                        "language": "ru",
                        "variant": variant,
                        "artifact_id": str(artifact.id),
                    },
                )
            )
        check_db.commit()
        run_id = str(run.id)

    sent = []

    async def fake_reply(self, text, **kwargs):
        sent.append((text, kwargs))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(final_video_delivery._FinalDeliveryMessage, "reply_text", fake_reply)

    assert final_video_delivery._trigger_guest_final_review_if_ready(run_id, 404) is True
    assert final_video_delivery._trigger_guest_final_review_if_ready(run_id, 404) is True
    assert len(sent) == 1
    restored = session_store.load_session(404)
    assert restored is not None
    assert restored["state"] == "guest_cartoon_final_review"
    assert restored["guest_data"]["final_review_prompted_run_id"] == run_id


def test_final_artifacts_delivered_waits_for_all_queued_final_statuses(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    ru_path = tmp_path / "final-ru.mp4"
    ru_path.write_bytes(b"ru")

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_running", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        ru_artifact = GenerationArtifact(
            run_id=run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final RU",
            uri=f"file://{ru_path}",
            language="ru",
            mime_type="video/mp4",
            payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 1}},
        )
        check_db.add(ru_artifact)
        check_db.flush()
        check_db.add_all(
            [
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label="Final build status title/subtitled/music (ru)",
                    uri=f"admin://final-build/{run.id}/ru/titled_subtitled_music",
                    language="ru",
                    mime_type="application/json",
                    payload_json={
                        "status": "success",
                        "language": "ru",
                        "variant": "titled_subtitled_music",
                        "artifact_id": str(ru_artifact.id),
                    },
                ),
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label="Final build status title/subtitled/music (en)",
                    uri=f"admin://final-build/{run.id}/en/titled_subtitled_music",
                    language="en",
                    mime_type="application/json",
                    payload_json={
                        "status": "pending",
                        "language": "en",
                        "variant": "titled_subtitled_music",
                    },
                ),
            ]
        )
        check_db.commit()
        run_id = str(run.id)

    assert all_final_artifacts_delivered(run_id) is False


def test_final_artifacts_delivered_accepts_sent_artifact_with_stale_failed_status(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    clean_path = tmp_path / "clean-en.mp4"
    titled_path = tmp_path / "titled-en.mp4"
    clean_path.write_bytes(b"clean")
    titled_path.write_bytes(b"titled")

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="final_render_running", language="en", script="recipe")
        check_db.add(run)
        check_db.flush()
        clean_artifact = GenerationArtifact(
            run_id=run.id,
            artifact_type="final_clean",
            label="Final EN clean",
            uri=f"file://{clean_path}",
            language="en",
            mime_type="video/mp4",
            payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 10}},
        )
        titled_artifact = GenerationArtifact(
            run_id=run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final EN titled",
            uri=f"file://{titled_path}",
            language="en",
            mime_type="video/mp4",
            payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 11}},
        )
        check_db.add_all([clean_artifact, titled_artifact])
        check_db.flush()
        check_db.add_all(
            [
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label="Final build status clean (en)",
                    uri=f"admin://final-build/{run.id}/en/clean",
                    language="en",
                    mime_type="application/json",
                    payload_json={
                        "status": "failed",
                        "language": "en",
                        "variant": "clean",
                        "message": "stale failure after a later delivery",
                    },
                ),
                GenerationArtifact(
                    run_id=run.id,
                    artifact_type="final_build_status",
                    label="Final build status title/subtitled/music (en)",
                    uri=f"admin://final-build/{run.id}/en/titled_subtitled_music",
                    language="en",
                    mime_type="application/json",
                    payload_json={
                        "status": "success",
                        "language": "en",
                        "variant": "titled_subtitled_music",
                        "artifact_id": str(titled_artifact.id),
                    },
                ),
            ]
        )
        check_db.commit()
        run_id = str(run.id)

    assert all_final_artifacts_delivered(run_id) is True


def test_final_video_delivery_triggers_social_descriptions_from_draft_when_session_moved(monkeypatch, tmp_path):
    _install_sqlite(monkeypatch)
    video_path = tmp_path / "final.mp4"
    video_path.write_bytes(b"mp4")

    with db.SessionLocal() as check_db:
        old_run = GenerationRun(status="final_video_delivery_failed", language="ru", script="old recipe")
        active_run = GenerationRun(status="media_in_progress", language="ru", script="new recipe")
        check_db.add_all([old_run, active_run])
        check_db.flush()
        check_db.add(
            ChatSession(
                chat_id=303,
                state="waiting_scene_media",
                session_json={"generation_run_id": str(active_run.id), "recipe_full_text": "new recipe"},
                generation_run_id=active_run.id,
            )
        )
        check_db.add(
            GenerationDraft(
                chat_id=303,
                run_id=old_run.id,
                title="Old draft",
                session_snapshot_json={
                    "state": "ready_for_production",
                    "recipe_full_text": "old recipe",
                    "generation_run_id": str(old_run.id),
                },
                payload_json={},
            )
        )
        artifact = GenerationArtifact(
            run_id=old_run.id,
            artifact_type="final_titled_subtitled_music",
            label="Final video",
            uri=f"file://{video_path}",
            language="ru",
            mime_type="video/mp4",
            payload_json={"telegram_delivery": {"status": "sent", "telegram_message_id": 7}},
        )
        check_db.add(artifact)
        check_db.commit()
        run_id = str(old_run.id)
        artifact_id = str(artifact.id)

    calls: list[str] = []

    async def _fake_social_descriptions(update, session):
        from app.handlers import handlers_state_media

        calls.append(session["recipe_full_text"])
        session["state"] = handlers_state_media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL
        session[handlers_state_media.SOCIAL_DESCRIPTIONS_PENDING_KEY] = {
            "recipe": session["recipe_full_text"],
            "copy": {"instagram_ru": "Описание"},
            "origin": "production_final",
        }
        return True

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("app.handlers.handlers_state_media._send_final_social_descriptions", _fake_social_descriptions)

    delivery = deliver_final_artifact_to_telegram(run_id, artifact_id)

    assert delivery["status"] == "sent"
    assert calls == ["old recipe"]
    restored = session_store.load_session(303)
    assert restored is not None
    assert restored["generation_run_id"] == run_id
    assert restored["social_descriptions_pending"]["recipe"] == "old recipe"


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


def test_rebuild_production_session_hydrates_missing_scene_prompt_from_approved_candidate(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Сцена",
            duration_sec=4,
            video_prompt_by_lang={},
            speech_by_lang={},
        )
        check_db.add(scene)
        check_db.flush()
        check_db.add(
            SceneVideoCandidate(
                candidate_id="vid-approved",
                scene_id=scene.id,
                language="ru",
                approved_for_production=True,
                speed_coef=1.0,
                video_prompt_by_lang={"ru": "candidate prompt from row"},
                speech_by_lang={"ru": "candidate speech from row"},
            )
        )
        check_db.add(
            GenerationArtifact(
                run_id=run.id,
                scene_id=scene.id,
                artifact_type="scene_video_candidate",
                label="video",
                uri="db://scene_video_candidates/vid-approved",
                language="ru",
                mime_type="video/mp4",
                payload_json={
                    "candidate_id": "vid-approved",
                    "video_prompt_by_lang": {"ru": "stale artifact prompt"},
                    "speech_by_lang": {"ru": "stale artifact speech"},
                },
            )
        )
        check_db.commit()
        run_id = str(run.id)
        scene_id = str(scene.id)

    session = {
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: scene_id},
        "production": {"json_plan": [{"scene": 1, "duration_sec": 4, "text": "Сцена"}]},
    }

    rebuild_production_session(session, run_id, include_media_blobs=False)
    restored_scene = session["production"]["json_plan"][0]

    assert restored_scene["video_prompt_by_lang"] == {"ru": "candidate prompt from row"}
    assert restored_scene["speech_by_lang"] == {"ru": "candidate speech from row"}


def test_rebuild_production_session_falls_back_to_legacy_candidate_artifact_snapshot(monkeypatch):
    _install_sqlite(monkeypatch)

    with db.SessionLocal() as check_db:
        run = GenerationRun(status="ready", language="ru", script="recipe")
        check_db.add(run)
        check_db.flush()
        scene = Scene(
            run_id=run.id,
            scene_order=1,
            text="Сцена",
            duration_sec=4,
            video_prompt_by_lang={},
            speech_by_lang={},
        )
        check_db.add(scene)
        check_db.flush()
        check_db.add(
            SceneVideoCandidate(
                candidate_id="vid-legacy",
                scene_id=scene.id,
                language="ru",
                approved_for_production=True,
                speed_coef=1.0,
            )
        )
        check_db.add(
            GenerationArtifact(
                run_id=run.id,
                scene_id=scene.id,
                artifact_type="scene_video_candidate",
                label="video",
                uri="db://scene_video_candidates/vid-legacy",
                language="ru",
                mime_type="video/mp4",
                payload_json={
                    "candidate_id": "vid-legacy",
                    "video_prompt_by_lang": {"ru": "legacy prompt"},
                    "speech_by_lang": {"ru": "legacy speech"},
                },
            )
        )
        check_db.commit()
        run_id = str(run.id)
        scene_id = str(scene.id)

    session = {
        "generation_run_id": run_id,
        "scene_db_ids_by_index": {0: scene_id},
        "production": {"json_plan": [{"scene": 1, "duration_sec": 4, "text": "Сцена"}]},
    }

    rebuild_production_session(session, run_id, include_media_blobs=False)
    restored_scene = session["production"]["json_plan"][0]

    assert restored_scene["video_prompt_by_lang"] == {"ru": "legacy prompt"}
    assert restored_scene["speech_by_lang"] == {"ru": "legacy speech"}


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


def test_scene_syntax_recovery_survives_restart_and_clears_after_completion(monkeypatch):
    _install_sqlite(monkeypatch)
    from app.handlers import handlers_state_media as media

    entry = media._scene_video_provider_choice_entry(
        prompt="English scene", model="veo3_fast", duration_sec=6,
        start_image=b"reference", end_image=None, quality="1080",
        aspect_ratio="9:16", audio_enabled=True, syntax_error_message="failed",
    )
    pending = {"source": "scene_batch", "languages": ["en"],
               "entries_by_lang": {"en": entry}}
    scene = {"scene": 1, "duration_sec": 6, "text": "Scene",
             "video_prompt_by_lang": {"en": "English scene"},
             "video_provider_choice_pending": pending}
    session = {"state": "waiting_scene_media", "target_languages": ["en"],
               "production": {"json_plan": [scene]}, "image_scene_index": 0,
               "scene_media_substep": media.SCENE_MEDIA_SUBSTEP_VIDEO}
    session_store.save_session(991, session)
    restored = session_store.load_session(991)
    restored_scene = restored["production"]["json_plan"][0]
    assert restored_scene["video_provider_choice_pending"] == pending
    calls = []

    async def batch(**kwargs):
        calls.append([request.key for request in kwargs["requests"]])
        assert kwargs["requests"][0].start_image == b"reference"
        return {}

    class Message:
        async def reply_text(self, text, **kwargs):
            assert "Ожидаю апрув" not in text

    monkeypatch.setattr(media, "_attempt_syntax_video_fallback_batch", batch)
    assert asyncio.run(media._handle_waiting_scene_media_state(
        SimpleNamespace(message=Message()), None, chat_id=991,
        session=restored, text="Syntax",
    ))
    assert calls == [["en"]]
    session_store.save_session(991, restored)
    restored = session_store.load_session(991)
    assert restored["production"]["json_plan"][0]["video_provider_choice_pending"]["languages"] == ["en"]
    for key in session_store._SCENE_RECOVERY_KEYS:
        restored["production"]["json_plan"][0].pop(key, None)
    session_store.save_session(991, restored)
    reloaded = session_store.load_session(991)
    assert "video_provider_choice_pending" not in reloaded["production"]["json_plan"][0]


def test_series_and_title_review_survive_session_cache():
    from app.session_store import _chat_session_cache_payload

    session = {
        "series": {"id": "holiday", "series_theme": "grandma_christmas"},
        "series_title_by_lang": {"ru": "Новогодние рецепты"},
        "series_counter_by_lang": {"ru": "день 1 из 10"},
        "series_video_index": 1,
        "series_total_videos": 10,
        "series_render_counter_advanced": True,
        "overlay_title_pending_next": "first_frame",
        "overlay_title_return_state": "waiting_scene_media",
        "overlay_title_review_frame": "new-frame",
        "overlay_title_approved_frame": "old-frame",
    }
    assert _chat_session_cache_payload(session) == session


def test_approved_phrase_and_pending_decisions_survive_restart_without_rewriting(monkeypatch):
    from app.handlers import handlers_state_media as media
    _install_sqlite(monkeypatch)
    SESSIONS.clear()
    phrase = 'Это утверждённая пользователем длинная фраза. ' * 12
    scene = dict(scene=1, duration_sec=6, text=phrase,
                 speech_by_lang={'ru': phrase, 'en': phrase},
                 speech_budget_override_by_lang={'ru': True, 'en': True},
                 speech_origin_by_lang={'ru': 'user', 'en': 'user'},
                 video_prompt_by_lang={'ru': 'approved ru prompt', 'en': 'approved en prompt'},
                 custom_review_decision={'keep': True})
    session = dict(state='waiting_scene_media', image_scene_index=0,
                   scene_media_substep='video_prompt', scene_media_language_index=1,
                   production={'json_plan': [scene]},
                   scene_speech_shorten_pending={'scene_idx': 0, 'stage': 'approval', 'duration_sec': 6},
                   scene_phrase_translation_pending={'scene_idx': 0, 'stage': 'approval'},
                   _syntax_durable_update_id=123, custom_dialogue_option='keep me')
    session_store.save_session(994, session)
    SESSIONS.clear()
    restored = session_store.load_session(994)
    current = restored['production']['json_plan'][0]
    assert restored['scene_speech_shorten_pending'] == session['scene_speech_shorten_pending']
    assert restored['scene_phrase_translation_pending'] == session['scene_phrase_translation_pending']
    assert restored['_syntax_durable_update_id'] == 123
    assert restored['custom_dialogue_option'] == 'keep me'
    assert current['custom_review_decision'] == {'keep': True}
    assert current['speech_budget_override_by_lang'] == {'ru': True, 'en': True}
    assert current['speech_origin_by_lang'] == {'ru': 'user', 'en': 'user'}
    async def forbidden(*args, **kwargs):
        raise AssertionError('Approved phrase must not trigger LLM or Telegram dialogue')
    monkeypatch.setattr(media, '_call_with_503_retries', forbidden)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=forbidden))
    assert asyncio.run(media._maybe_shorten_scene_speech_for_video_budget(
        update, context=None, session=restored, chat_id=994, scene_idx=0, scene=current, duration_sec=6)) is False
    assert asyncio.run(media._maybe_adjust_english_speech_for_video_budget(
        update, context=None, session=restored, chat_id=994, scene=current, duration_sec=6)) is False
    assert current['text'] == phrase
    assert current['duration_sec'] == 6
    assert current['video_prompt_by_lang'] == {'ru': 'approved ru prompt', 'en': 'approved en prompt'}


def test_new_session_snapshot_does_not_resurrect_cleared_approval(monkeypatch):
    _install_sqlite(monkeypatch)
    session = dict(state='waiting_scene_media', production={'json_plan': [
        dict(scene=1, text='phrase', duration_sec=8, speech_budget_override_by_lang={'ru': True})]})
    session_store.save_session(995, session)
    session['production']['json_plan'][0]['speech_budget_override_by_lang'] = {}
    session_store.save_session(995, session)
    restored = session_store.load_session(995)
    assert restored['production']['json_plan'][0]['speech_budget_override_by_lang'] == {}
