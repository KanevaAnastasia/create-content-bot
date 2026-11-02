from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import project_profiles
from app.guest_handlers import guest_main_menu_markup
from app.models import Base, BotUser, Character, Project, ProjectCharacter


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_discipline_workspace_uses_owner_project_voice_and_story_mode(monkeypatch):
    engine, factory = _factory()
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    with factory() as db:
        db.add(BotUser(id=owner_id, telegram_id=1, display_name="Owner", role="admin", is_active=True))
        db.add(
            Project(
                id=project_id,
                owner_user_id=owner_id,
                slug=project_profiles.DISCIPLINE_PROJECT_SLUG,
                title=project_profiles.DISCIPLINE_PROJECT_TITLE,
                is_default=False,
                is_active=True,
                elevenlabs_voice_id=project_profiles.DISCIPLINE_VOICE_ID,
                default_generation_mode="story",
                style_source_project_slug="grandma_kitchen",
            )
        )
        db.commit()
    monkeypatch.setattr(project_profiles, "SessionLocal", factory)

    session = project_profiles.discipline_workspace_session()

    assert session is not None
    assert session["selected_project_key"] == "discipline"
    assert session["project_id"] == str(project_id)
    assert session["project_voice_id"] == "EvnFqTSGPvDU3JBwJbCC"
    assert session["force_story_mode"] is True
    rows = [[button.text for button in row] for row in guest_main_menu_markup(session).keyboard]
    assert rows == [
        ["Добавить персонажа"],
        ["Основной персонаж"],
        ["Создать ролик-историю"],
        ["Создать визуализацию"],
        ["Сохранить черновик"],
        ["В начало"],
    ]
    engine.dispose()


def test_resolve_project_voice_uses_current_database_value_over_stale_session(monkeypatch):
    engine, factory = _factory()
    with factory() as db:
        db.add(
            Project(
                slug=project_profiles.DISCIPLINE_PROJECT_SLUG,
                title=project_profiles.DISCIPLINE_PROJECT_TITLE,
                is_default=False,
                is_active=True,
                elevenlabs_voice_id=project_profiles.DISCIPLINE_VOICE_ID,
            )
        )
        db.commit()
    monkeypatch.setattr(project_profiles, "SessionLocal", factory)

    voice_id = project_profiles.resolve_project_voice_id(
        "discipline",
        "nkvv6waLzFah2jTNUZGT",
    )

    assert voice_id == "EvnFqTSGPvDU3JBwJbCC"
    engine.dispose()


def test_project_story_characters_returns_primary_and_reference(monkeypatch, tmp_path):
    engine, factory = _factory()
    reference = tmp_path / "hero.png"
    reference.write_bytes(b"image")
    project = Project(slug="discipline", title="Дисциплина", is_default=False, is_active=True)
    hero = Character(
        canonical_key=None,
        name="Алексей",
        description="Собранный молодой мужчина в спортивной форме",
        default_asset_uri=str(reference),
        is_active=True,
    )
    with factory() as db:
        db.add_all([project, hero])
        db.flush()
        db.add(
            ProjectCharacter(
                project_id=project.id,
                character_id=hero.id,
                display_name="Алексей",
                asset_uri=str(reference),
                sort_order=0,
                is_primary=True,
            )
        )
        db.commit()
        expected_id = f"character_{hero.id.hex}"
    monkeypatch.setattr(project_profiles, "SessionLocal", factory)

    characters, primary_id = project_profiles.project_story_characters("discipline")

    assert primary_id == expected_id
    assert characters == [
        {
            "id": expected_id,
            "display_name": "Алексей",
            "role": "персонаж проекта",
            "age_stage": "adult",
            "same_person_group": expected_id,
            "description": "Собранный молодой мужчина в спортивной форме",
            "reference_prompt": "Собранный молодой мужчина в спортивной форме",
            "requires_reference": False,
            "asset_uri": str(reference),
            "is_primary": True,
        }
    ]
    engine.dispose()
