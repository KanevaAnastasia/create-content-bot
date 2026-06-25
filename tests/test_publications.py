import base64
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Project
from app.publications import (
    DEFAULT_PUBLICATION_VIDEO_VARIANT,
    PUBLICATION_STATUS_SCHEDULED,
    build_publication_targets,
    export_publication_video_assets,
    extract_hashtags,
    normalize_publication_target_keys,
    normalize_publication_scheduled_at,
    normalize_publication_video_variant,
    publication_jobs_summary,
    queue_publication_bundle,
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


def test_build_publication_targets_can_filter_selected_destinations() -> None:
    copy = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU youtube",
        "rutube_ru_title": "RuTube title",
        "rutube_ru_description": "RuTube desc",
        "instagram_ru": "Instagram ru",
        "vk_ru": "VK ru",
        "tiktok_ru": "TikTok ru",
        "instagram_en": "Instagram en",
        "youtube_en_title": "EN title",
        "youtube_en_description": "EN youtube",
    }

    targets = build_publication_targets(copy, selected_target_keys=["instagram:ru", "youtube/en"])

    assert [(item.platform, item.language) for item in targets] == [
        ("instagram", "ru"),
        ("youtube", "en"),
    ]
    assert normalize_publication_target_keys("instagram:ru youtube/en") == {"instagram:ru", "youtube:en"}
    assert normalize_publication_video_variant("unknown") == DEFAULT_PUBLICATION_VIDEO_VARIANT


def test_extract_hashtags_preserves_order_and_dedupes() -> None:
    out = extract_hashtags("text #one #two #one #три")

    assert out == ["#one", "#two", "#три"]


def test_export_publication_video_assets_writes_language_files(tmp_path: Path) -> None:
    ru_bytes = b"ru-video"
    en_bytes = b"en-video"
    title_bytes = b"title-video"
    post_production = {
        "renders_by_language": {
            "ru": {
                "subtitled_music": {"video_b64": base64.b64encode(ru_bytes).decode("ascii")},
                "titled_subtitled_music": {"video_b64": base64.b64encode(title_bytes).decode("ascii")},
            },
            "en": {"clean": {"video_b64": base64.b64encode(en_bytes).decode("ascii")}},
        }
    }

    out = export_publication_video_assets(
        post_production,
        bundle_key="bundle-1",
        base_dir=tmp_path,
    )

    assert set(out) == {"ru", "en"}
    assert Path(out["ru"]).read_bytes() == title_bytes
    assert Path(out["en"]).read_bytes() == en_bytes


def test_export_publication_video_assets_uses_requested_variant(tmp_path: Path) -> None:
    post_production = {
        "renders_by_language": {
            "ru": {
                "clean": {"video_b64": base64.b64encode(b"clean").decode("ascii")},
                "titled_subtitled_music": {"video_b64": base64.b64encode(b"titled").decode("ascii")},
            },
        }
    }

    out = export_publication_video_assets(
        post_production,
        bundle_key="bundle-clean",
        base_dir=tmp_path,
        video_variant="clean",
    )

    assert Path(out["ru"]).read_bytes() == b"clean"


def test_export_publication_video_assets_falls_back_to_upscaled_4k(tmp_path: Path) -> None:
    upscaled_bytes = b"upscaled-video"
    post_production = {
        "renders_by_language": {
            "ru": {"upscaled_4k": {"video_b64": base64.b64encode(upscaled_bytes).decode("ascii")}},
        }
    }

    out = export_publication_video_assets(
        post_production,
        bundle_key="bundle-4k",
        base_dir=tmp_path,
    )

    assert set(out) == {"ru"}
    assert Path(out["ru"]).read_bytes() == upscaled_bytes


def test_export_publication_video_assets_accepts_render_paths(tmp_path: Path) -> None:
    source_path = tmp_path / "rendered_ru.mp4"
    source_path.write_bytes(b"rendered-ru")
    post_production = {
        "renders_by_language": {
            "ru": {"subtitled_music": {"video_path": str(source_path)}},
        }
    }

    out = export_publication_video_assets(
        post_production,
        bundle_key="bundle-path",
        base_dir=tmp_path / "exports",
    )

    assert set(out) == {"ru"}
    assert Path(out["ru"]).read_bytes() == b"rendered-ru"


def test_queue_publication_bundle_sets_scheduled_status(monkeypatch, tmp_path: Path) -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            project = Project(slug="default-project", title="Default", is_default=True)
            session.add(project)
            session.commit()
            monkeypatch.setenv("YOUTUBE_CHANNEL_ID_RU", "channel-ru")
            post_production = {
                "renders_by_language": {
                    "ru": {"clean": {"video_b64": base64.b64encode(b"clean").decode("ascii")}},
                }
            }
            copy = {
                "youtube_ru_title": "RU title",
                "youtube_ru_description": "RU youtube #tag",
            }
            monkeypatch.setenv("PUBLICATION_ARTIFACTS_DIR", str(tmp_path))

            jobs = queue_publication_bundle(
                session,
                project_slug="default-project",
                recipe="recipe",
                copy=copy,
                post_production=post_production,
                selected_target_keys=["youtube:ru"],
                video_variant="clean",
                scheduled_at="2026-06-24 18:30",
            )

            assert len(jobs) == 1
            assert jobs[0].status == PUBLICATION_STATUS_SCHEDULED
            assert jobs[0].scheduled_at == normalize_publication_scheduled_at("2026-06-24 18:30")
            assert jobs[0].payload_json["scheduled_at"]
    finally:
        engine.dispose()


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


def test_publication_jobs_summary_counts_scheduled_as_n8n_ready() -> None:
    jobs = [
        type("Job", (), {"status": PUBLICATION_STATUS_SCHEDULED, "platform": "youtube", "language": "ru", "video_path": "/tmp/ru.mp4"})(),
    ]

    summary = publication_jobs_summary(jobs)

    assert "Автоготовы для n8n: 1." in summary
    assert "Запланированы: 1." in summary
