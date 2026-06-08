import base64
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Project
from app.publications import (
    build_publication_targets,
    export_publication_video_assets,
    extract_hashtags,
    resolve_project_for_publications,
    upsert_project_publish_accounts_from_env,
)


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine, Session


def test_build_publication_targets_includes_rutube_and_dual_locale_mapping() -> None:
    copy = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU youtube #one",
        "rutube_ru_title": "RuTube title",
        "rutube_ru_description": "RuTube desc #two",
        "instagram_ru": "Instagram ru #three",
        "vk_ru": "VK ru #four",
        "tiktok_ru": "TikTok ru #five",
        "instagram_en": "Instagram en #six",
        "youtube_en_title": "EN title",
        "youtube_en_description": "EN youtube #seven",
    }

    targets = build_publication_targets(copy)

    assert [(item.platform, item.language) for item in targets] == [
        ("youtube", "ru"),
        ("rutube", "ru"),
        ("instagram", "ru"),
        ("vk", "ru"),
        ("tiktok", "ru"),
        ("instagram", "en"),
        ("youtube", "en"),
    ]
    assert targets[1].title == "RuTube title"
    assert targets[5].video_label == "EN video / final master"


def test_extract_hashtags_preserves_order_and_dedupes() -> None:
    out = extract_hashtags("text #one #two #one #три")

    assert out == ["#one", "#two", "#три"]


def test_export_publication_video_assets_writes_language_files(tmp_path: Path) -> None:
    ru_bytes = b"ru-video"
    en_bytes = b"en-video"
    post_production = {
        "renders_by_language": {
            "ru": {"subtitled_music": {"video_b64": base64.b64encode(ru_bytes).decode("ascii")}},
            "en": {"clean": {"video_b64": base64.b64encode(en_bytes).decode("ascii")}},
        }
    }

    out = export_publication_video_assets(
        post_production,
        bundle_key="bundle-1",
        base_dir=tmp_path,
    )

    assert set(out) == {"ru", "en"}
    assert Path(out["ru"]).read_bytes() == ru_bytes
    assert Path(out["en"]).read_bytes() == en_bytes


def test_resolve_project_for_publications_rejects_unknown_slug() -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            session.add(Project(slug="default-project", title="Default", is_default=True))
            session.commit()

            try:
                resolve_project_for_publications(session, "missing-project")
            except ValueError as exc:
                assert "missing-project" in str(exc)
            else:
                raise AssertionError("Expected ValueError for unknown project slug")
    finally:
        engine.dispose()


def test_upsert_project_publish_accounts_disables_mapping_when_env_is_removed(monkeypatch) -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            project = Project(slug="default-project", title="Default", is_default=True)
            session.add(project)
            session.commit()

            monkeypatch.setenv("YOUTUBE_CHANNEL_ID_RU", "channel-1")
            rows = upsert_project_publish_accounts_from_env(session, project)
            session.commit()

            assert len(rows) == 1
            assert rows[0].external_account_id == "channel-1"
            assert rows[0].is_active is True

            monkeypatch.delenv("YOUTUBE_CHANNEL_ID_RU", raising=False)
            rows = upsert_project_publish_accounts_from_env(session, project)
            session.commit()

            assert len(rows) == 1
            assert rows[0].external_account_id is None
            assert rows[0].is_active is False
    finally:
        engine.dispose()
