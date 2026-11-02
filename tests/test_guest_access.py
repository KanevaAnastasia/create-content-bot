from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import guest_access, guest_handlers, session_store
from app.models import (
    Base,
    BotUser,
    Character,
    GenerationArtifact,
    GenerationRun,
    Project,
    ProjectCharacter,
    Scene,
    SceneImageCandidate,
    SceneVideoCandidate,
)


@pytest.fixture()
def guest_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(guest_access, "SessionLocal", factory)
    monkeypatch.setattr(guest_handlers, "SessionLocal", factory)
    monkeypatch.setattr(session_store, "SessionLocal", factory)
    monkeypatch.setattr(guest_handlers, "is_quiet_mode_enabled", lambda: False)
    return factory


def _telegram_user(user_id: int, username: str | None, name: str = "Пользователь"):
    return SimpleNamespace(id=user_id, username=username, full_name=name)


def test_username_registration_is_claimed_once_then_resolved_only_by_id(guest_db):
    with guest_db() as db:
        db.add(
            BotUser(
                telegram_id=None,
                normalized_username="anyasovet",
                display_name="@AnyaSovet",
                role="guest",
                is_active=True,
            )
        )
        db.commit()

    claimed = guest_access.resolve_bot_identity(
        _telegram_user(101, "AnyaSovet", "Аня"),
        allow_username_claim=True,
    )

    assert claimed is not None
    assert claimed.role == "guest"
    assert claimed.telegram_id == 101
    assert claimed.project_id is not None

    # Username changes do not matter after binding; the immutable ID still works.
    by_id = guest_access.resolve_bot_identity(_telegram_user(101, "new_name"))
    assert by_id is not None
    assert by_id.user_id == claimed.user_id

    # A different Telegram ID can never reclaim the original username.
    assert guest_access.resolve_bot_identity(
        _telegram_user(202, "AnyaSovet"),
        allow_username_claim=True,
    ) is None

    with guest_db() as db:
        projects = db.execute(select(Project).where(Project.owner_user_id == claimed.user_id)).scalars().all()
        assert len(projects) == 1
        assert projects[0].title == "Мой проект"


def test_unknown_and_disabled_users_are_denied(guest_db):
    assert guest_access.resolve_bot_identity(_telegram_user(303, "unknown"), allow_username_claim=True) is None
    with guest_db() as db:
        db.add(
            BotUser(
                telegram_id=404,
                normalized_username="disabled",
                display_name="Отключён",
                role="guest",
                is_active=False,
            )
        )
        db.commit()
    assert guest_access.resolve_bot_identity(_telegram_user(404, "disabled")) is None


def test_guest_main_menu_contract_is_russian_and_minimal():
    rows = [[button.text for button in row] for row in guest_handlers.guest_main_menu_markup().keyboard]
    assert rows == [
        ["Добавить персонажа"],
        ["Генерация изображения"],
        ["Генерация видео"],
        ["Генерация мультфильма"],
        ["Мои генерации"],
    ]


def test_project_workspace_menu_offers_story_and_visualization_formats():
    rows = [
        [button.text for button in row]
        for row in guest_handlers.guest_main_menu_markup({"project_workspace_mode": True}).keyboard
    ]

    assert [guest_handlers.MENU_CREATE_STORY] in rows
    assert [guest_handlers.MENU_CREATE_VISUALIZATION] in rows


def test_guest_approval_uses_new_prompt_button():
    rows = [[button.text for button in row] for row in guest_handlers._approval_markup().keyboard]
    assert rows == [["ОК"], ["ПЕРЕГЕНЕРИРОВАТЬ"], ["Другой промпт"], ["В меню"]]


def test_guest_final_review_offers_fix_and_add_scene():
    session = {
        "state": guest_handlers.STATE_CARTOON_FINAL_REVIEW,
        "guest_data": {"approved_scenes": [{"scene": 1}, {"scene": 2}]},
    }
    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]
    assert rows == [
        ["ОК"],
        ["Исправить сцены"],
        ["Добавить сцену"],
        ["Пересобрать мультфильм"],
        ["В меню"],
    ]


def test_guest_main_menu_offers_rebuild_only_for_saved_cartoon():
    empty_rows = [[button.text for button in row] for row in guest_handlers.guest_main_menu_markup().keyboard]
    session = {
        "generation_run_id": str(uuid.uuid4()),
        "guest_data": {"approved_scenes": [{"scene": 1}]},
    }
    saved_rows = [
        [button.text for button in row]
        for row in guest_handlers.guest_main_menu_markup(session).keyboard
    ]
    assert [guest_handlers.BUTTON_REBUILD_CARTOON] not in empty_rows
    assert saved_rows[-1] == [guest_handlers.BUTTON_REBUILD_CARTOON]


def test_quiet_mode_delays_and_rejects_guest_image_and_video_generation(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(guest_handlers, "is_quiet_mode_enabled", lambda: True)
    monkeypatch.setattr(guest_handlers.random, "randint", lambda start, end: 73)
    monkeypatch.setattr(guest_handlers.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        guest_handlers,
        "generate_image_vertex",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Vertex must not be called")),
    )
    monkeypatch.setattr(
        guest_handlers,
        "try_generate_via_syntax",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Syntax must not be called")),
    )
    session = {
        "state": guest_handlers.STATE_IMAGE_DESCRIPTION,
        "guest_mode": True,
        "guest_data": {"image_description": "Лес"},
    }
    message = _Message()
    update = SimpleNamespace(message=message)

    asyncio.run(guest_handlers._generate_image(update, session, target="image"))
    assert session["state"] == guest_handlers.STATE_IMAGE_DESCRIPTION

    session["state"] = guest_handlers.STATE_VIDEO_DESCRIPTION
    session["guest_data"] = {"video_description": "Герой идёт"}
    asyncio.run(guest_handlers._generate_video(update, session, target="video"))
    assert session["state"] == guest_handlers.STATE_VIDEO_DESCRIPTION

    assert delays == [73, 73]
    assert [item[1] for item in message.sent] == [
        "Не удалось сгенерировать, попробуйте позже.",
        "Не удалось сгенерировать, попробуйте позже.",
    ]


def test_quiet_mode_never_blocks_owner_workspace(monkeypatch):
    monkeypatch.setattr(guest_handlers, "is_quiet_mode_enabled", lambda: True)
    session = {"user_role": "admin", "state": guest_handlers.STATE_VIDEO_DESCRIPTION}
    assert asyncio.run(
        guest_handlers._reject_generation_in_quiet_mode(SimpleNamespace(message=None), session)
    ) is False


def test_quiet_mode_allows_guest_final_build(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["state"] = guest_handlers.STATE_CARTOON_FINISH_CONFIRM
    session["guest_data"]["approved_scenes"] = [{"scene": 1}]
    chat_id = 922
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    monkeypatch.setattr(guest_handlers, "is_quiet_mode_enabled", lambda: True)
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)
    queued = []

    def fake_queue(active_chat_id, active_session):
        queued.append((active_chat_id, active_session["generation_run_id"]))
        return {"run_id": active_session["generation_run_id"]}

    monkeypatch.setattr(guest_handlers, "_queue_cartoon_final_build", fake_queue)
    message = _Message()
    message.text = "Да, склеить"
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert queued == [(chat_id, session["generation_run_id"])]
    assert session["state"] == guest_handlers.STATE_MENU
    assert "Мультфильм поставлен в очередь" in message.sent[-1][1]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_cartoon_video_keyboard_can_return_to_image():
    session = {
        "state": guest_handlers.STATE_CARTOON_VIDEO_APPROVAL,
        "guest_data": {},
    }

    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]

    assert rows == [
        ["ОК"],
        [guest_handlers.BUTTON_RETRY_DELIVERY],
        ["ПЕРЕГЕНЕРИРОВАТЬ"],
        ["Другой промпт"],
        ["Изменить персонажей"],
        ["Вернуться к генерации изображения"],
        ["В меню"],
    ]


def test_video_delivery_timeout_offers_resend_without_generation(monkeypatch):
    session = {
        "state": guest_handlers.STATE_CARTOON_VIDEO_APPROVAL,
        "guest_data": {
            "last_video_target": "cartoon",
            "current_video_uri": "file:///saved-video.mp4",
        },
    }
    message = _Message()
    update = SimpleNamespace(message=message)

    async def fail_delivery(*_args, **_kwargs):
        raise guest_handlers.TelegramError("telegram upload timeout")

    monkeypatch.setattr(guest_handlers, "_send_video", fail_delivery)

    delivered = asyncio.run(
        guest_handlers._deliver_current_video(update, session, video_bytes=b"already-generated-video")
    )

    assert delivered is False
    assert session["state"] == guest_handlers.STATE_VIDEO_DELIVERY_ERROR
    assert session["guest_data"]["video_delivery_target"] == "cartoon"
    assert "новая генерация не запустится" in message.sent[-1][1]
    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]
    assert rows == [[guest_handlers.BUTTON_RETRY_DELIVERY], [guest_handlers.BUTTON_MENU]]


def test_post_selection_steps_offer_character_change():
    for state in (
        guest_handlers.STATE_IMAGE_DESCRIPTION,
        guest_handlers.STATE_IMAGE_APPROVAL,
        guest_handlers.STATE_VIDEO_DESCRIPTION,
        guest_handlers.STATE_CARTOON_FRAME_APPROVAL,
        guest_handlers.STATE_CARTOON_VIDEO_APPROVAL,
    ):
        session = {"state": state, "guest_data": {}}
        rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]
        assert [guest_handlers.BUTTON_CHANGE_CHARACTERS] in rows


def test_change_characters_after_image_approval_regenerates_same_run(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    with guest_db() as db:
        character = Character(name="Новый герой", voice_description="спокойный голос", is_active=True)
        db.add(character)
        db.flush()
        character_id = character.id
        db.add(
            ProjectCharacter(
                project_id=project_id,
                character_id=character_id,
                asset_uri="file:///tmp/new-character.png",
            )
        )
        db.commit()

    run_id = str(uuid.uuid4())
    session["generation_run_id"] = run_id
    session["state"] = guest_handlers.STATE_IMAGE_APPROVAL
    session["guest_data"] = {
        "selected_character_ids": [],
        "image_description": "Герой стоит в парке",
    }
    chat_id = 914
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    generated = []
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)

    async def fake_generate_image(_update, active_session, *, target):
        generated.append(target)
        active_session["state"] = guest_handlers.STATE_IMAGE_APPROVAL

    monkeypatch.setattr(guest_handlers, "_generate_image", fake_generate_image)
    message = _Message()
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    message.text = guest_handlers.BUTTON_CHANGE_CHARACTERS
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_IMAGE_CHARACTERS
    assert session["generation_run_id"] == run_id

    character_button = next(iter(session["guest_data"]["character_buttons"]))
    message.text = character_button
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["guest_data"]["selected_character_ids"] == [str(character_id)]

    message.text = guest_handlers.BUTTON_DONE
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert generated == ["image"]
    assert session["state"] == guest_handlers.STATE_IMAGE_APPROVAL
    assert session["generation_run_id"] == run_id
    assert "character_selection_return_state" not in session["guest_data"]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_character_selection_allows_empty_choice(guest_db):
    with guest_db() as db:
        user = BotUser(
            telegram_id=505,
            normalized_username="guest505",
            display_name="Гость",
            role="guest",
            is_active=True,
        )
        db.add(user)
        db.flush()
        project = Project(owner_user_id=user.id, slug="guest-505", title="Мой проект")
        db.add(project)
        db.commit()
    session = {
        "state": guest_handlers.STATE_IMAGE_CHARACTERS,
        "guest_mode": True,
        "bot_user_id": str(user.id),
        "project_id": str(project.id),
        "guest_data": {"selected_character_ids": []},
    }

    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]

    assert rows == [["Готово"], ["В меню"]]


def test_cartoon_second_scene_offers_continue_scene(guest_db, tmp_path):
    session, _project_id = _guest_session_with_project(guest_db)
    previous_video = tmp_path / "previous.mp4"
    previous_video.write_bytes(b"previous-video")
    session["state"] = guest_handlers.STATE_CARTOON_CHARACTERS
    session["guest_data"] = {
        "scene_index": 1,
        "previous_video_uri": previous_video.as_uri(),
        "selected_character_ids": [],
    }

    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]

    assert ["Продолжи сцену"] in rows
    assert rows[-2:] == [["Готово"], ["В меню"]]


def test_continue_scene_uses_last_video_frame_without_vertex(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    previous_video = tmp_path / "previous.mp4"
    previous_video.write_bytes(b"previous-video")
    reference = tmp_path / "character.png"
    reference.write_bytes(b"character-reference")
    with guest_db() as db:
        character = Character(name="Персонаж", voice_description="голос", is_active=True)
        db.add(character)
        db.flush()
        character_id = character.id
        db.add(ProjectCharacter(project_id=project_id, character_id=character_id, asset_uri=reference.as_uri()))
        db.commit()
    session["state"] = guest_handlers.STATE_CARTOON_CHARACTERS
    session["guest_data"] = {
        "scene_index": 1,
        "previous_video_uri": previous_video.as_uri(),
        "selected_character_ids": [],
        "approved_scenes": [{"selected_character_ids": [str(character_id)]}],
    }
    captured = {}

    def fake_extract(*, video_bytes=None, video_path=None):
        captured["video_bytes"] = video_bytes
        captured["video_path"] = video_path
        return b"\x89PNG\r\n\x1a\nlast-frame"

    def fake_persist(active_session, image, *, kind):
        captured["image"] = image
        captured["kind"] = kind
        active_session["guest_data"]["current_image_candidate_id"] = "continued-frame"
        active_session["guest_data"]["current_image_uri"] = "file:///continued-frame.png"
        return "continued-frame", "file:///continued-frame.png"

    monkeypatch.setattr(guest_handlers, "extract_last_video_frame", fake_extract)
    monkeypatch.setattr(guest_handlers, "_persist_image_candidate", fake_persist)
    monkeypatch.setattr(guest_handlers, "_update_scene_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        guest_handlers,
        "generate_image_vertex",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Vertex must not be called")),
    )

    message = _Message()
    update = SimpleNamespace(message=message)
    asyncio.run(guest_handlers._continue_cartoon_from_previous_video(update, session))

    assert captured["video_bytes"] == b"previous-video"
    assert captured["image"].startswith(b"\x89PNG")
    assert captured["kind"] == "start"
    assert session["state"] == guest_handlers.STATE_CARTOON_CONTINUATION_FRAME_APPROVAL
    assert session["guest_data"]["continued_from_previous_scene"] is True
    assert session["guest_data"]["selected_character_ids"] == [str(character_id)]
    assert any(item[0] == "photo" for item in message.sent)
    rows = [[button.text for button in row] for row in guest_handlers.guest_keyboard_for_session(session).keyboard]
    assert rows == [["ОК"], ["Сгенерировать изображение"], ["Изменить персонажей"], ["В меню"]]


def test_unsuitable_continuation_frame_returns_to_vertex_prompt(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    session["state"] = guest_handlers.STATE_CARTOON_CONTINUATION_FRAME_APPROVAL
    session["guest_data"] = {
        "scene_index": 1,
        "continued_from_previous_scene": True,
        "scene_description": "Продолжение предыдущей сцены.",
        "selected_character_ids": [],
    }
    chat_id = 912
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)

    message = _Message()
    message.text = guest_handlers.BUTTON_GENERATE_IMAGE
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["state"] == guest_handlers.STATE_CARTOON_DESCRIPTION
    assert session["guest_data"]["continued_from_previous_scene"] is False
    assert "scene_description" not in session["guest_data"]
    assert "через Vertex" in message.sent[-1][1]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_cartoon_video_back_button_restores_image_approval(guest_db, monkeypatch, tmp_path):
    session, _project_id = _guest_session_with_project(guest_db)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    candidate_id, _uri = guest_handlers._persist_image_candidate(
        session,
        b"\x89PNG\r\n\x1a\nstart-frame",
        kind="start",
    )
    guest_handlers._approve_image_candidate(session)
    session["state"] = guest_handlers.STATE_CARTOON_VIDEO_APPROVAL
    message = _Message()
    update = SimpleNamespace(message=message)

    asyncio.run(guest_handlers._return_cartoon_to_image_step(update, session))

    assert session["state"] == guest_handlers.STATE_CARTOON_FRAME_APPROVAL
    assert any(item[0] == "photo" for item in message.sent)
    with guest_db() as db:
        row = db.execute(
            select(SceneImageCandidate).where(SceneImageCandidate.candidate_id == candidate_id)
        ).scalar_one()
        assert row.liked is False
        assert row.approved_for_video is False


def test_normalize_username_strips_at_and_case():
    assert guest_access.normalize_telegram_username(" @AnyaSovet ") == "anyasovet"
    assert guest_access.normalize_telegram_username("not valid!") is None


class _Message:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(("text", text, kwargs))

    async def reply_photo(self, photo, caption=None, **kwargs):
        self.sent.append(("photo", photo.read(), {"caption": caption, **kwargs}))

    async def reply_video(self, video, caption=None, **kwargs):
        self.sent.append(("video", video.read(), {"caption": caption, **kwargs}))


def test_guest_video_delivery_uses_extended_upload_timeout():
    message = _Message()
    update = SimpleNamespace(message=message)

    asyncio.run(guest_handlers._send_video(update, b"video", "Готово"))

    payload = message.sent[-1][2]
    assert payload["read_timeout"] == guest_handlers.GUEST_VIDEO_SEND_TIMEOUT_SEC
    assert payload["write_timeout"] == guest_handlers.GUEST_VIDEO_SEND_TIMEOUT_SEC
    assert payload["connect_timeout"] == 30.0
    assert payload["pool_timeout"] == 60.0


def _guest_session_with_project(guest_db):
    with guest_db() as db:
        user = BotUser(
            telegram_id=606,
            normalized_username="guest606",
            display_name="Гость",
            role="guest",
            is_active=True,
        )
        db.add(user)
        db.flush()
        project = Project(owner_user_id=user.id, slug="guest-606", title="Мой проект")
        db.add(project)
        db.commit()
    return {
        "state": guest_handlers.STATE_MENU,
        "guest_mode": True,
        "bot_user_id": str(user.id),
        "project_id": str(project.id),
        "guest_data": {},
    }, project.id


def test_guest_generation_history_uses_daily_numbers_and_restores_selected_run(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    first_run_id, _ = guest_handlers._new_run(session, "image", title="Изображение")
    session["guest_data"]["image_description"] = "Первое изображение"
    first_candidate_id, _ = guest_handlers._persist_image_candidate(session, b"first-image", kind="result")
    guest_handlers._approve_image_candidate(session)

    second_run_id, _ = guest_handlers._new_run(session, "video", title="Видео")
    session["guest_data"]["video_description"] = "Второе видео"
    guest_handlers._persist_image_candidate(session, b"start-image", kind="start")
    guest_handlers._approve_image_candidate(session)
    guest_handlers._persist_video_candidate(
        session,
        b"video",
        prompt="Второе видео",
        user_prompt="Второе видео",
    )
    with guest_db() as db:
        first_run = db.get(GenerationRun, first_run_id)
        second_run = db.get(GenerationRun, second_run_id)
        first_run.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        second_run.created_at = datetime.now(timezone.utc)
        db.commit()

    items = guest_handlers._generation_history_items(session)

    assert [item.run_id for item in items[:2]] == [second_run_id, first_run_id]
    assert [item.day_number for item in items[:2]] == [2, 1]
    assert "№2 — Видео" in items[0].label
    assert "№1 — Изображение" in items[1].label

    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=606,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    restored, preview_type, _preview_uri = guest_handlers._rebuild_guest_session_from_run(
        identity,
        str(first_run_id),
    )
    assert restored["state"] == guest_handlers.STATE_IMAGE_APPROVAL
    assert restored["guest_data"]["current_image_candidate_id"] == first_candidate_id
    assert preview_type == "image"


def test_guest_cannot_open_generation_from_another_project(guest_db):
    session, project_id = _guest_session_with_project(guest_db)
    with guest_db() as db:
        other_user = BotUser(
            telegram_id=707,
            normalized_username="otherguest",
            display_name="Другой гость",
            role="guest",
            is_active=True,
        )
        db.add(other_user)
        db.flush()
        other_project = Project(owner_user_id=other_user.id, slug="guest-707", title="Мой проект")
        db.add(other_project)
        db.flush()
        other_run = GenerationRun(
            project_id=other_project.id,
            workflow_type="image",
            status="completed",
            is_active=False,
            human_title="Изображение",
            language="ru",
            script="Изображение",
        )
        db.add(other_run)
        db.commit()
        other_run_id = other_run.id

    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=606,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    with pytest.raises(PermissionError):
        guest_handlers._rebuild_guest_session_from_run(identity, str(other_run_id))


def test_guest_run_snapshot_is_saved_per_generation(guest_db):
    session, _project_id = _guest_session_with_project(guest_db)
    run_id, _ = guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["state"] = guest_handlers.STATE_CARTOON_SCENE_DONE
    session["guest_data"]["approved_scenes"] = [{"scene": 1, "video_uri": "file:///scene.mp4"}]

    session_store.save_session(606, session)
    restored = session_store.load_guest_run_snapshot(run_id)

    assert restored is not None
    assert restored["state"] == guest_handlers.STATE_CARTOON_SCENE_DONE
    assert restored["generation_run_id"] == str(run_id)
    assert restored["guest_data"]["approved_scenes"][0]["scene"] == 1


def test_character_creation_replaces_stale_run_without_scene(guest_db):
    session, _project_id = _guest_session_with_project(guest_db)
    stale_run_id = uuid.uuid4()
    session["generation_run_id"] = str(stale_run_id)
    session["guest_data"] = {"character_name": "Новый персонаж"}

    run_id, scene_id = guest_handlers._ensure_character_run(session)

    assert run_id != stale_run_id
    assert session["generation_run_id"] == str(run_id)
    assert session["guest_data"]["scene_id"] == str(scene_id)
    with guest_db() as db:
        scene = db.get(Scene, scene_id)
        assert scene is not None
        assert scene.run_id == run_id


def test_vertex_receives_only_selected_character_references(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"first-reference")
    second_path.write_bytes(b"second-reference")
    with guest_db() as db:
        first = Character(name="Первый", voice_description="низкий голос", is_active=True)
        second = Character(name="Второй", voice_description="высокий голос", is_active=True)
        db.add_all([first, second])
        db.flush()
        db.add_all(
            [
                ProjectCharacter(project_id=project_id, character_id=first.id, asset_uri=first_path.as_uri()),
                ProjectCharacter(project_id=project_id, character_id=second.id, asset_uri=second_path.as_uri()),
            ]
        )
        db.commit()
    session["guest_data"] = {
        "selected_character_ids": [str(first.id)],
        "image_description": "Портрет в саду",
    }
    guest_handlers._new_run(session, "image", title="Изображение")
    captured = {}

    def fake_vertex(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return b"generated-image"

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", fake_vertex)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    message = _Message()
    update = SimpleNamespace(message=message)

    asyncio.run(guest_handlers._generate_image(update, session, target="image"))

    assert captured["reference_images"] == [b"first-reference"]
    assert captured["aspect_ratio"] == "9:16"
    assert "Референс №1: персонаж «Первый»" in captured["prompt"]
    assert "Второй" not in captured["prompt"]
    assert session["state"] == guest_handlers.STATE_IMAGE_APPROVAL


def test_guest_image_generation_is_limited_to_seven_successes_per_moscow_day(guest_db, monkeypatch, tmp_path):
    session, _project_id = _guest_session_with_project(guest_db)
    session["guest_data"] = {"image_description": "Портрет в саду"}
    guest_handlers._new_run(session, "image", title="Изображение")
    calls = 0

    def fake_vertex(_prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return b"generated-image"

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", fake_vertex)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    message = _Message()
    update = SimpleNamespace(message=message)

    for _ in range(guest_handlers.GUEST_IMAGE_DAILY_LIMIT):
        asyncio.run(guest_handlers._generate_image(update, session, target="image"))

    assert calls == 7
    assert guest_handlers._guest_generated_images_today(session) == 7

    asyncio.run(guest_handlers._generate_image(update, session, target="image"))

    assert calls == 7
    assert message.sent[-1][0] == "text"
    assert message.sent[-1][1] == guest_handlers.GUEST_IMAGE_QUOTA_EXHAUSTED_MESSAGE
    assert "только из собственных изображений" in message.sent[-1][1]


def test_failed_and_user_uploaded_images_do_not_consume_guest_daily_limit(guest_db, monkeypatch, tmp_path):
    session, _project_id = _guest_session_with_project(guest_db)
    session["guest_data"] = {"image_description": "Портрет в саду"}
    guest_handlers._new_run(session, "image", title="Изображение")
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    guest_handlers._persist_image_candidate(session, b"uploaded-image", kind="start")

    def failed_vertex(_prompt, **_kwargs):
        raise RuntimeError("generation failed")

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", failed_vertex)
    asyncio.run(guest_handlers._generate_image(SimpleNamespace(message=_Message()), session, target="image"))

    assert guest_handlers._guest_generated_images_today(session) == 0


def test_cartoon_accepts_uploaded_start_frame_without_reaching_image_limit(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["state"] = guest_handlers.STATE_CARTOON_DESCRIPTION
    session["guest_data"]["scene_description"] = "Учительница и мальчик в классе"
    chat_id = 913
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    async def fake_uploaded_image(*_args):
        return b"uploaded-frame"

    monkeypatch.setattr(guest_handlers, "_guest_image_limit_reached", lambda _session: False)
    monkeypatch.setattr(guest_handlers, "_extract_uploaded_image", fake_uploaded_image)
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)

    message = _Message()
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(guest_handlers.guest_on_photo(update, SimpleNamespace(), identity))

    assert session["state"] == guest_handlers.STATE_CARTOON_VIDEO_DESCRIPTION
    assert session["guest_data"]["last_image_target"] == "cartoon_frame"
    assert "Стартовый кадр сцены принят" in message.sent[-1][1]
    with guest_db() as db:
        candidate = db.execute(
            select(SceneImageCandidate).where(
                SceneImageCandidate.candidate_id == session["guest_data"]["current_image_candidate_id"]
            )
        ).scalar_one()
        assert candidate.liked is True
        assert candidate.approved_for_video is True
    assert guest_handlers._guest_generated_images_today(session) == 0
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_cartoon_frame_recovers_previous_character_references_before_vertex(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    reference = tmp_path / "previous-character.jpg"
    reference.write_bytes(b"\xff\xd8\xffprevious-character")
    with guest_db() as db:
        character = Character(name="Миша", voice_description="голос", is_active=True)
        db.add(character)
        db.flush()
        character_id = character.id
        db.add(ProjectCharacter(project_id=project_id, character_id=character_id, asset_uri=reference.as_uri()))
        db.commit()
    session["guest_data"] = {
        "selected_character_ids": [],
        "approved_scenes": [{"selected_character_ids": [str(character_id)]}],
        "scene_description": "Продолжение сцены с Мишей",
    }
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["guest_data"]["selected_character_ids"] = []
    session["guest_data"]["approved_scenes"] = [{"selected_character_ids": [str(character_id)]}]
    session["guest_data"]["scene_description"] = "Продолжение сцены с Мишей"
    captured = {}

    def fake_vertex(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return b"generated-image"

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", fake_vertex)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    update = SimpleNamespace(message=_Message())

    asyncio.run(guest_handlers._generate_image(update, session, target="cartoon_frame"))

    assert session["guest_data"]["selected_character_ids"] == [str(character_id)]
    assert captured["reference_images"] == [b"\xff\xd8\xffprevious-character"]
    assert "Референс №1: персонаж «Миша»" in captured["prompt"]


def test_new_character_generation_uses_first_primary_character_as_style_only_reference(
    guest_db, monkeypatch, tmp_path
):
    session, project_id = _guest_session_with_project(guest_db)
    reference = tmp_path / "knopka.jpg"
    reference.write_bytes(b"knopka-style")
    with guest_db() as db:
        knopka = Character(
            name="Кнопка",
            description="сиренево-серый ушастый зверёк",
            voice_description="невербальный персонаж",
            is_active=True,
        )
        db.add(knopka)
        db.flush()
        db.add(
            ProjectCharacter(
                project_id=project_id,
                character_id=knopka.id,
                display_name="Кнопка",
                asset_uri=reference.as_uri(),
                sort_order=0,
                is_primary=True,
            )
        )
        db.commit()
    session["guest_data"] = {
        "character_name": "Новый герой",
        "appearance": "Высокий человек в синем костюме",
        "character_feedback": [],
    }
    guest_handlers._ensure_character_run(session)
    captured = {}

    def fake_vertex(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return b"generated-character"

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", fake_vertex)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    asyncio.run(guest_handlers._generate_image(SimpleNamespace(message=_Message()), session, target="character"))

    assert captured["reference_images"] == [b"knopka-style"]
    assert "основного персонажа проекта «Кнопка»" in captured["prompt"]
    assert "ТОЛЬКО как референс художественной стилистики" in captured["prompt"]
    assert "Не копируй его вид животного или человека" in captured["prompt"]


def test_cartoon_frame_preserves_explicit_empty_character_choice(guest_db, monkeypatch, tmp_path):
    session, _project_id = _guest_session_with_project(guest_db)
    session["guest_data"] = {
        "selected_character_ids": [],
        "allow_empty_character_selection": True,
        "approved_scenes": [{"selected_character_ids": ["not-used"]}],
        "scene_description": "Пустой класс",
    }
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["guest_data"].update(
        selected_character_ids=[],
        allow_empty_character_selection=True,
        approved_scenes=[{"selected_character_ids": ["not-used"]}],
        scene_description="Пустой класс",
    )
    captured = {}

    def fake_vertex(_prompt, **kwargs):
        captured.update(kwargs)
        return b"generated-image"

    monkeypatch.setattr(guest_handlers, "generate_image_vertex", fake_vertex)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    update = SimpleNamespace(message=_Message())

    asyncio.run(guest_handlers._generate_image(update, session, target="cartoon_frame"))

    assert captured["reference_images"] == []


def test_syntax_gets_start_frame_and_only_selected_voice_profiles(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    start_path = tmp_path / "start.png"
    start_path.write_bytes(b"start-frame")
    ref_path = tmp_path / "character.png"
    ref_path.write_bytes(b"character-reference-must-not-be-sent")
    with guest_db() as db:
        selected = Character(
            name="Аня",
            description="Полное визуальное описание школьницы",
            voice_description="Аня говорит мягким спокойным голосом",
            meta_json={"short_description": "Девочка с двумя косичками"},
            is_active=True,
        )
        unselected = Character(
            name="Борис",
            description="Мальчик-хулиган в кепке",
            voice_description="громкий бас",
            is_active=True,
        )
        db.add_all([selected, unselected])
        db.flush()
        db.add_all(
            [
                ProjectCharacter(project_id=project_id, character_id=selected.id, asset_uri=ref_path.as_uri()),
                ProjectCharacter(project_id=project_id, character_id=unselected.id, asset_uri=ref_path.as_uri()),
            ]
        )
        db.commit()
    session["guest_data"] = {
        "selected_character_ids": [str(selected.id)],
        "video_description": "Персонаж машет рукой и говорит привет",
        "current_image_uri": start_path.as_uri(),
    }
    captured = {}

    def fake_syntax(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(success=False, video_bytes=None, provider_message="failed")

    monkeypatch.setattr(guest_handlers, "try_generate_via_syntax", fake_syntax)
    message = _Message()
    update = SimpleNamespace(message=message)

    asyncio.run(guest_handlers._generate_video(update, session, target="video"))

    assert captured["start_image"] == b"start-frame"
    assert captured["duration_sec"] == 8
    assert captured["aspect_ratio"] == "9:16"
    assert captured["syntax_provider"] == "veo"
    assert captured["prompt"].startswith("Персонаж машет рукой и говорит привет\n\n")
    assert "Вертикальное видео 9:16" not in captured["prompt"]
    assert "девочка с двумя косичками: говорит мягким спокойным голосом" in captured["prompt"]
    assert "Аня" not in captured["prompt"]
    assert "Борис" not in captured["prompt"]
    assert b"character-reference-must-not-be-sent" != captured["start_image"]
    assert session["state"] == guest_handlers.STATE_SYNTAX_ERROR


def test_voice_profile_short_description_falls_back_to_role_in_voice_text():
    character = guest_handlers.GuestCharacter(
        id=uuid.uuid4(),
        name="Миша",
        short_description="",
        description="",
        voice_description="Голос мальчика хулигана, не очень низкий",
        asset_uri="file:///character.png",
    )

    assert guest_handlers._short_voice_character_description(character, 1) == "мальчик-хулиган"
    assert guest_handlers._without_character_name("Миша говорит немного хрипло", "Миша") == "говорит немного хрипло"


def test_stored_short_descriptions_distinguish_two_girls():
    first = guest_handlers.GuestCharacter(
        id=uuid.uuid4(),
        name="Соня",
        short_description="Светловолосая девочка с синим бантом",
        description="",
        voice_description="Соня говорит тихо",
        asset_uri="file:///sonya.png",
    )
    second = guest_handlers.GuestCharacter(
        id=uuid.uuid4(),
        name="Олеся",
        short_description="Рыжеволосая девочка с двумя косичками и очками",
        description="",
        voice_description="Олеся говорит уверенно",
        asset_uri="file:///olesya.png",
    )

    assert guest_handlers._short_voice_character_description(first, 1) == "светловолосая девочка с синим бантом"
    assert guest_handlers._short_voice_character_description(second, 2) == "рыжеволосая девочка с двумя косичками и очками"


def test_legacy_stored_video_prompt_is_reduced_to_user_text():
    stored = (
        "Вертикальное видео 9:16, длительность 8 секунд.\n\n"
        "Учительница объясняет правило. "
        "Вертикальный формат, композиция для смартфона. Динамичная, понятная подача урока, "
        "естественная мимика, синхронная русская речь, без субтитров, без надписей. "
        "Без новых персонажей.\n\n"
        "Краткие описания выбранных персонажей и их голоса:\n"
        "- учительница: спокойный голос"
    )

    assert guest_handlers._user_prompt_from_stored_video_prompt(stored) == "Учительница объясняет правило."


def test_cartoon_frame_approval_requests_video_prompt_before_syntax(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    session["state"] = guest_handlers.STATE_CARTOON_FRAME_APPROVAL
    session["guest_data"] = {"current_image_candidate_id": "frame-1"}
    chat_id = 909
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    generated = []

    monkeypatch.setattr(guest_handlers, "_approve_image_candidate", lambda _session: None)
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)

    async def fake_generate_video(_update, _session, *, target):
        generated.append(target)

    monkeypatch.setattr(guest_handlers, "_generate_video", fake_generate_video)

    message = _Message()
    message.text = guest_handlers.BUTTON_OK
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["state"] == guest_handlers.STATE_CARTOON_VIDEO_DESCRIPTION
    assert generated == []
    assert "движение, действие и речь" in message.sent[-1][1]

    message.text = "Учитель говорит, а ученики внимательно слушают."
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["guest_data"]["video_description"] == message.text
    assert generated == ["cartoon"]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_current_cartoon_video_approval_accepts_replacement_prompt(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    session["state"] = guest_handlers.STATE_CARTOON_VIDEO_APPROVAL
    session["guest_data"] = {
        "video_description": "Старый промпт",
        "cartoon_video_feedback": ["Старая корректировка"],
    }
    chat_id = 910
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    generated = []

    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)

    async def fake_generate_video(_update, _session, *, target):
        generated.append(target)

    monkeypatch.setattr(guest_handlers, "_generate_video", fake_generate_video)

    message = _Message()
    message.text = "Учитель поднимает апельсин и произносит новую реплику."
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["guest_data"]["video_description"] == message.text
    assert session["guest_data"]["cartoon_video_feedback"] == []
    assert generated == ["cartoon"]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_cartoon_frame_new_prompt_replaces_old_prompt_and_feedback(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    session["state"] = guest_handlers.STATE_CARTOON_FRAME_FEEDBACK
    session["guest_data"] = {
        "scene_description": "Старый промпт",
        "frame_feedback": ["Старая корректировка"],
    }
    chat_id = 911
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    updated = []
    generated = []

    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)
    monkeypatch.setattr(
        guest_handlers,
        "_update_scene_text",
        lambda _session, text, *, prompt=None: updated.append((text, prompt)),
    )

    async def fake_generate_image(_update, _session, *, target):
        generated.append(target)

    monkeypatch.setattr(guest_handlers, "_generate_image", fake_generate_image)

    message = _Message()
    message.text = "Новый класс без телевизора, доски и ножа."
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["guest_data"]["scene_description"] == message.text
    assert session["guest_data"]["frame_feedback"] == []
    assert updated == [(message.text, message.text)]
    assert generated == ["cartoon_frame"]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_cartoon_final_confirmation_queues_only_clean_and_4k(guest_db, monkeypatch):
    session, _project_id = _guest_session_with_project(guest_db)
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["guest_data"]["approved_scenes"] = [
        {
            "scene": 1,
            "text": "Сцена",
            "duration_sec": 8,
            "video_candidates_by_lang": {
                "ru": [{"candidate_id": "approved", "approved_for_production": True}]
            },
        }
    ]
    captured = {}

    def fake_queue(chat_id, queued_session, scenes, **kwargs):
        captured.update(
            {
                "chat_id": chat_id,
                "session": queued_session,
                "scenes": scenes,
                **kwargs,
            }
        )
        return {"run_id": queued_session["generation_run_id"]}

    monkeypatch.setattr(guest_handlers, "queue_final_builds_for_session", fake_queue)

    result = guest_handlers._queue_cartoon_final_build(777, session)

    assert result["run_id"] == session["generation_run_id"]
    assert captured["languages"] == ["ru"]
    assert captured["variants"] == ["clean", "upscaled_4k"]
    assert captured["kick_worker"] is True


def test_rebuild_cartoon_button_forces_clean_and_4k_requeue(guest_db, monkeypatch):
    session, project_id = _guest_session_with_project(guest_db)
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    session["state"] = guest_handlers.STATE_MENU
    session["guest_data"]["approved_scenes"] = [{"scene": 1, "video_uri": "file:///scene.mp4"}]
    chat_id = 923
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    captured = {}
    monkeypatch.setattr(guest_handlers, "is_quiet_mode_enabled", lambda: True)

    async def unexpected_sleep(_seconds):
        raise AssertionError("Quiet mode must not delay final rebuilds")

    monkeypatch.setattr(guest_handlers.asyncio, "sleep", unexpected_sleep)

    def fake_queue(active_chat_id, active_session, *, force_rebuild=False):
        captured.update(
            chat_id=active_chat_id,
            run_id=active_session["generation_run_id"],
            force_rebuild=force_rebuild,
        )
        return {"run_id": active_session["generation_run_id"]}

    monkeypatch.setattr(guest_handlers, "_queue_cartoon_final_build", fake_queue)
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)
    message = _Message()
    message.text = guest_handlers.BUTTON_REBUILD_CARTOON
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert captured == {
        "chat_id": chat_id,
        "run_id": session["generation_run_id"],
        "force_rebuild": True,
    }
    assert session["state"] == guest_handlers.STATE_MENU
    assert "Пересборка запущена" in message.sent[-1][1]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_force_rebuild_removes_stuck_guest_final_statuses(guest_db):
    session, _project_id = _guest_session_with_project(guest_db)
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    run_id = uuid.UUID(session["generation_run_id"])
    with guest_db() as db:
        db.add_all(
            [
                GenerationArtifact(
                    run_id=run_id,
                    artifact_type="final_build_status",
                    label="clean",
                    uri=f"admin://final-build/{run_id}/ru/clean",
                    language="ru",
                    mime_type="application/json",
                    payload_json={"status": "success", "variant": "clean"},
                ),
                GenerationArtifact(
                    run_id=run_id,
                    artifact_type="final_build_status",
                    label="4K",
                    uri=f"admin://final-build/{run_id}/ru/upscaled_4k",
                    language="ru",
                    mime_type="application/json",
                    payload_json={"status": "running", "variant": "upscaled_4k"},
                ),
            ]
        )
        db.commit()

    guest_handlers._reset_cartoon_final_build_statuses(session)

    with guest_db() as db:
        statuses = db.execute(
            select(GenerationArtifact).where(
                GenerationArtifact.run_id == run_id,
                GenerationArtifact.artifact_type == "final_build_status",
            )
        ).scalars().all()
    assert statuses == []


def test_final_cartoon_review_can_reopen_scene_from_video(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    guest_handlers._persist_image_candidate(session, b"\x89PNG\r\n\x1a\nframe", kind="start")
    guest_handlers._approve_image_candidate(session)
    guest_handlers._persist_video_candidate(session, b"video", prompt="Старый промпт видео")
    guest_handlers._approve_video_candidate(session)
    session["guest_data"]["scene_description"] = "Первая сцена"
    session["guest_data"]["approved_scenes"] = [guest_handlers._finish_scene_payload(session)]
    session["state"] = guest_handlers.STATE_CARTOON_FINAL_REVIEW
    chat_id = 920
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)
    message = _Message()
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    message.text = guest_handlers.BUTTON_FIX_SCENES
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_CARTOON_EDIT_SCENE_SELECT

    message.text = "Сцена 1"
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_CARTOON_EDIT_STAGE_SELECT

    message.text = guest_handlers.BUTTON_FROM_VIDEO
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_CARTOON_VIDEO_FEEDBACK
    assert session["guest_data"]["editing_existing_scene"] is True
    assert session["guest_data"]["current_image_uri"]
    assert len(session["guest_data"]["approved_scenes"]) == 1
    dict.pop(guest_handlers.SESSIONS, chat_id, None)


def test_final_cartoon_review_inserts_new_video_scene_after_selected_scene(guest_db, monkeypatch, tmp_path):
    session, project_id = _guest_session_with_project(guest_db)
    monkeypatch.setenv("SCENE_MEDIA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    guest_handlers._new_run(session, "cartoon", title="Мультфильм")
    first_scene_id = session["scene_db_ids_by_index"][0]
    second_scene_id = str(guest_handlers._append_cartoon_scene(session))
    session["guest_data"] = {
        "approved_scenes": [
            {"scene": 1, "video_uri": "file:///first.mp4", "selected_character_ids": []},
            {"scene": 2, "video_uri": "file:///second.mp4", "selected_character_ids": []},
        ]
    }
    session["state"] = guest_handlers.STATE_CARTOON_FINAL_REVIEW
    chat_id = 921
    dict.__setitem__(guest_handlers.SESSIONS, chat_id, session)
    identity = guest_access.BotIdentity(
        user_id=uuid.UUID(session["bot_user_id"]),
        telegram_id=chat_id,
        normalized_username="guest606",
        display_name="Гость",
        role="guest",
        is_active=True,
        project_id=project_id,
    )
    monkeypatch.setattr(guest_handlers, "save_loaded_session_if_possible", lambda _chat_id: None)
    message = _Message()
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    message.text = guest_handlers.BUTTON_ADD_SCENE
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_CARTOON_INSERT_POSITION

    message.text = "После сцены 1"
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))
    assert session["state"] == guest_handlers.STATE_CARTOON_CHARACTERS
    inserted_scene_id = session["guest_data"]["scene_id"]

    guest_handlers._persist_image_candidate(session, b"\x89PNG\r\n\x1a\nnew-frame", kind="start")
    guest_handlers._approve_image_candidate(session)
    _candidate_id, new_video_uri = guest_handlers._persist_video_candidate(session, b"new-video", prompt="Новая сцена")
    session["guest_data"]["scene_description"] = "Новая сцена"
    session["guest_data"]["current_video_uri"] = new_video_uri
    session["state"] = guest_handlers.STATE_CARTOON_VIDEO_APPROVAL
    message.text = guest_handlers.BUTTON_OK
    asyncio.run(guest_handlers.guest_on_text(update, SimpleNamespace(), identity))

    assert session["state"] == guest_handlers.STATE_CARTOON_EDIT_SCENE_DONE
    assert [item["scene"] for item in session["guest_data"]["approved_scenes"]] == [1, 2, 3]
    assert session["guest_data"]["approved_scenes"][1]["text"] == "Новая сцена"
    assert session["scene_db_ids_by_index"] == {
        0: first_scene_id,
        1: inserted_scene_id,
        2: second_scene_id,
    }
    with guest_db() as check_db:
        ordered_ids = [
            str(row.id)
            for row in check_db.execute(
                select(Scene)
                .where(Scene.run_id == uuid.UUID(session["generation_run_id"]))
                .order_by(Scene.scene_order)
            ).scalars()
        ]
    assert ordered_ids == [first_scene_id, inserted_scene_id, second_scene_id]
    dict.pop(guest_handlers.SESSIONS, chat_id, None)
