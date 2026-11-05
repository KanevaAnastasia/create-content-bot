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
    apply_project_publication_copy_rules,
    export_publication_cover_assets,
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


def test_build_publication_targets_includes_supported_dual_locale_mapping() -> None:
    copy = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU youtube #one",
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
        ("instagram", "ru"),
        ("vk", "ru"),
        ("tiktok", "ru"),
        ("instagram", "en"),
        ("youtube", "en"),
    ]
    assert targets[4].video_label == "EN video / final master"


def test_build_publication_targets_can_filter_selected_destinations() -> None:
    copy = {
        "youtube_ru_title": "RU title",
        "youtube_ru_description": "RU youtube",
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


def test_grandma_instagram_ru_attribution_is_inserted_once_before_hashtags() -> None:
    copy = apply_project_publication_copy_rules(
        "grandma_kitchen",
        {"instagram_ru": "Тёплое описание\n\n#рецепт #ужин"},
    )

    assert copy["instagram_ru"] == (
        "Тёплое описание\n\nСделано с помощью @syntx_ai\n\n#рецепт #ужин"
    )
    repeated = apply_project_publication_copy_rules("grandma_kitchen", copy)
    assert repeated == copy


def test_non_grandma_copy_does_not_keep_grandma_attribution() -> None:
    copy = apply_project_publication_copy_rules(
        "discipline",
        {"instagram_ru": "Текст\n\nСделано с помощью @syntx_ai\n\n#тег"},
    )

    assert copy["instagram_ru"] == "Текст\n\n#тег"


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


def test_export_publication_cover_assets_copies_supported_images(tmp_path: Path) -> None:
    ru_cover = tmp_path / "ru.png"
    en_cover = tmp_path / "en.jpg"
    ru_cover.write_bytes(b"ru-cover")
    en_cover.write_bytes(b"en-cover")

    out = export_publication_cover_assets(
        {"ru": ru_cover, "en": en_cover},
        bundle_key="bundle-covers",
        base_dir=tmp_path / "exports",
    )

    assert Path(out["ru"]).read_bytes() == b"ru-cover"
    assert Path(out["en"]).read_bytes() == b"en-cover"


def test_queue_publication_bundle_sets_scheduled_status(monkeypatch, tmp_path: Path) -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            project = Project(slug="default-project", title="Default", is_default=True)
            session.add(project)
            session.commit()
            for key in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
                monkeypatch.setenv(f"PUBLISH_DEFAULT_PROJECT_{key}", "configured")
            monkeypatch.setenv("PUBLISH_DEFAULT_PROJECT_YOUTUBE_CHANNEL_ID_RU", "channel-ru")
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


def test_queue_publication_bundle_keeps_explicit_source_label(monkeypatch, tmp_path: Path) -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            project = Project(slug="grandma_kitchen", title="Grandma", is_default=True)
            session.add(project)
            session.commit()
            monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CHANNEL_ID_RU", "grandma-channel")
            monkeypatch.setenv("PUBLICATION_ARTIFACTS_DIR", str(tmp_path))

            jobs = queue_publication_bundle(
                session,
                project_slug="grandma_kitchen",
                recipe="recipe",
                copy={"youtube_ru_title": "Title", "youtube_ru_description": "Description"},
                post_production={
                    "renders_by_language": {
                        "ru": {"clean": {"video_b64": base64.b64encode(b"custom").decode("ascii")}},
                    }
                },
                selected_target_keys=["youtube:ru"],
                video_variant="clean",
                video_source_labels={"ru": "RU · custom upload · my-video.mp4"},
            )

            assert jobs[0].video_label == "RU · custom upload · my-video.mp4"
            assert jobs[0].payload_json["video_source_label"] == "RU · custom upload · my-video.mp4"
    finally:
        engine.dispose()


def test_new_vk_jobs_include_stories_without_changing_other_platforms(monkeypatch, tmp_path):
    engine, Session = _session_factory()
    try:
        with Session() as session:
            session.add(Project(slug='story-test', title='Stories', is_default=True))
            session.commit()
            monkeypatch.setenv('PUBLICATION_ARTIFACTS_DIR', str(tmp_path))
            jobs = queue_publication_bundle(
                session, project_slug='story-test', recipe='recipe',
                copy={'vk_ru': 'VK recipe', 'youtube_ru_title': 'Title', 'youtube_ru_description': 'Recipe'},
                post_production={'renders_by_language': {'ru': {'clean': {'video_b64': base64.b64encode(b'video').decode()}}}},
                selected_target_keys=['vk:ru', 'youtube:ru'], video_variant='clean',
            )
            assert {j.platform: j.payload_json['vk_stories'] for j in jobs} == {'vk': True, 'youtube': False}
    finally:
        engine.dispose()


def test_queue_publication_bundle_attaches_language_cover(monkeypatch, tmp_path: Path) -> None:
    engine, Session = _session_factory()
    try:
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        with Session() as session:
            project = Project(slug="grandma_kitchen", title="Grandma", is_default=True)
            session.add(project)
            session.commit()
            monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CHANNEL_ID_RU", "channel")
            monkeypatch.setenv("PUBLICATION_ARTIFACTS_DIR", str(tmp_path / "published"))

            jobs = queue_publication_bundle(
                session,
                project_slug="grandma_kitchen",
                recipe="recipe",
                copy={"youtube_ru_title": "Title", "youtube_ru_description": "Description"},
                post_production={
                    "renders_by_language": {
                        "ru": {"clean": {"video_b64": base64.b64encode(b"video").decode("ascii")}},
                    }
                },
                selected_target_keys=["youtube:ru"],
                video_variant="clean",
                cover_sources={"ru": cover},
            )

            stored_cover = Path(jobs[0].payload_json["cover_path"])
            assert stored_cover.read_bytes() == b"cover"
            assert stored_cover.name == "publish_cover_ru.png"
    finally:
        engine.dispose()


def test_queue_trial_instagram_ru_uses_separate_copy_without_attribution(
    monkeypatch, tmp_path: Path
) -> None:
    engine, Session = _session_factory()
    try:
        with Session() as session:
            project = Project(slug="grandma_kitchen", title="Grandma", is_default=True)
            session.add(project)
            session.commit()
            monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN_RU", "token")
            monkeypatch.setenv(
                "PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU",
                "instagram-ru",
            )
            monkeypatch.setenv("PUBLICATION_ARTIFACTS_DIR", str(tmp_path / "published"))

            jobs = queue_publication_bundle(
                session,
                project_slug="grandma_kitchen",
                recipe="recipe",
                copy={"instagram_ru": "Пробный текст\n\n#рецепт"},
                trial_copy={"instagram_ru": "Отдельный пробный текст\n\n#новаяаудитория"},
                post_production={
                    "renders_by_language": {
                        "ru": {"clean": {"video_b64": base64.b64encode(b"video").decode("ascii")}},
                    }
                },
                selected_target_keys=["instagram:ru"],
                trial_target_keys=["instagram:ru"],
            )

            assert len(jobs) == 2
            assert jobs[0].description == "Отдельный пробный текст\n\n#новаяаудитория"
            assert "syntx" not in jobs[0].description.lower()
            assert jobs[0].payload_json["instagram_trial"] is True
            assert jobs[1].description == (
                "Пробный текст\n\nСделано с помощью @syntx_ai\n\n#рецепт"
            )
            assert jobs[1].payload_json["instagram_trial"] is False
            assert jobs[1].payload_json["publish_after_job_id"] == str(jobs[0].id)
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

            monkeypatch.setenv("PUBLISH_DEFAULT_PROJECT_YOUTUBE_CHANNEL_ID_RU", "channel-1")
            rows = upsert_project_publish_accounts_from_env(session, project)
            session.commit()

            assert len(rows) == 1
            assert rows[0].external_account_id == "channel-1"
            assert rows[0].is_active is True

            monkeypatch.delenv("PUBLISH_DEFAULT_PROJECT_YOUTUBE_CHANNEL_ID_RU", raising=False)
            rows = upsert_project_publish_accounts_from_env(session, project)
            session.commit()

            assert len(rows) == 1
            assert rows[0].external_account_id is None
            assert rows[0].is_active is False
    finally:
        engine.dispose()


def test_publication_jobs_summary_counts_scheduled_as_direct_api_ready() -> None:
    jobs = [
        type("Job", (), {"status": PUBLICATION_STATUS_SCHEDULED, "platform": "youtube", "language": "ru", "video_path": "/tmp/ru.mp4"})(),
    ]

    summary = publication_jobs_summary(jobs)

    assert "Готовы к отправке через официальные API: 1." in summary
    assert "Запланированы: 1." in summary
