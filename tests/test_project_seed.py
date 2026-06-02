from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Character, Location, Project, ProjectCharacter, ProjectLocation, TextStep, VisualProp
from app.project_seed import DEFAULT_PROJECT_SLUG, seed_default_project


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine, Session


def test_seed_default_project_creates_project_assets_and_text_steps():
    engine, Session = _session_factory()
    assets = {
        "characters": {
            "grandma": {"base": "assets/characters/grandma/base.png"},
            "cat": {"base": "assets/characters/cat/base.png"},
        },
        "locations": {
            "kitchen": {"base": "assets/locations/kitchen/base.png"},
            "stove": {
                "base": "assets/locations/stove/base.png",
                "top_view": "assets/locations/stove/top_view.png",
            },
        },
    }

    with Session() as session:
        stats = seed_default_project(session, assets=assets)

        project = session.scalar(select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG))
        assert project is not None
        assert project.title == "Grandma Kitchen"
        assert project.is_default is True
        assert project.default_video_provider == "vertex"
        assert project.hashtags_fixed_ru == ["#убабушкивкуснее", "#бабушкинырецепты", "#рецепты"]
        assert project.hashtags_fixed_en == ["#grandmatastesbetter", "#grandmasrecipes", "#recipes"]
        assert stats.characters == 2
        assert stats.locations == 2
        assert stats.visual_props >= 10
        assert stats.text_steps == 3
        assert session.query(Character).count() == 2
        assert session.query(Location).count() == 2
        assert session.query(ProjectCharacter).count() == 2
        assert session.query(ProjectLocation).count() == 2
        assert session.query(VisualProp).count() == stats.visual_props
        assert session.query(TextStep).count() == 3
        grandma = session.scalar(select(Character).where(Character.canonical_key == "grandma"))
        assert grandma is not None
        assert grandma.name == "Бабушка"
        assert grandma.description and "платком с красными розами" in grandma.description
        cat = session.scalar(select(Character).where(Character.canonical_key == "cat"))
        assert cat is not None
        assert cat.name == "Тимоша"
        assert cat.meta_json["short_description"] == "рыжий кот"
        cat_link = session.scalar(
            select(ProjectCharacter)
            .join(Character, Character.id == ProjectCharacter.character_id)
            .where(Character.canonical_key == "cat")
        )
        assert cat_link is not None
        assert cat_link.meta_json["short_description"] == "рыжий кот"
    engine.dispose()


def test_seed_default_project_is_idempotent():
    engine, Session = _session_factory()
    assets = {
        "characters": {"grandma": {"base": "assets/characters/grandma/base.png"}},
        "locations": {"kitchen": {"base": "assets/locations/kitchen/base.png"}},
    }

    with Session() as session:
        seed_default_project(session, assets=assets)
        seed_default_project(session, assets=assets)

        assert session.query(Project).count() == 1
        assert session.query(Character).count() == 1
        assert session.query(Location).count() == 1
        assert session.query(ProjectCharacter).count() == 1
        assert session.query(ProjectLocation).count() == 1
        assert session.query(VisualProp).count() >= 10
        assert session.query(TextStep).count() == 3
    engine.dispose()
