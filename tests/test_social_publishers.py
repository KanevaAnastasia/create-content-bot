from pathlib import Path

from app.publication_config import project_env, project_env_name
from app.publication_media import publication_media_token, publication_media_token_is_valid
from app.social_publishers import platform_readiness


def test_project_scoped_publish_env_never_falls_back_to_global_tokens(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "legacy")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_YOUTUBE_CLIENT_ID", "grandma-client")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_YOUTUBE_CLIENT_ID", "discipline-client")

    assert project_env("grandma_kitchen", "YOUTUBE_CLIENT_ID") == "grandma-client"
    assert project_env("discipline", "YOUTUBE_CLIENT_ID") == "discipline-client"
    monkeypatch.delenv("PUBLISH_DISCIPLINE_YOUTUBE_CLIENT_ID")
    assert project_env("discipline", "YOUTUBE_CLIENT_ID") == ""
    assert project_env_name("grandma_kitchen", "youtube_refresh_token") == (
        "PUBLISH_GRANDMA_KITCHEN_YOUTUBE_REFRESH_TOKEN"
    )


def test_platform_readiness_reports_project_scoped_missing_keys(monkeypatch) -> None:
    for key in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID_RU",
    ):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"PUBLISH_DISCIPLINE_{key}", raising=False)

    readiness = platform_readiness("discipline", "youtube", "ru")

    assert readiness.ready is False
    assert "PUBLISH_DISCIPLINE_YOUTUBE_REFRESH_TOKEN" in readiness.missing


def test_platform_readiness_accepts_complete_project_credentials(monkeypatch) -> None:
    for key in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID_RU",
    ):
        monkeypatch.setenv(f"PUBLISH_GRANDMA_KITCHEN_{key}", "configured")

    readiness = platform_readiness("grandma_kitchen", "youtube", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_instagram_readiness_uses_shared_https_and_project_credentials(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_BUSINESS_ACCOUNT_ID_RU", "account")
    monkeypatch.setenv("CONTENT_FABRIC_PUBLIC_BASE_URL", "https://publish.example")
    monkeypatch.setenv("PUBLICATION_MEDIA_SIGNING_KEY", "signing-key")

    readiness = platform_readiness("grandma_kitchen", "instagram", "ru")

    assert readiness.ready is True
    assert readiness.missing == ()


def test_publication_media_token_is_bound_to_job_and_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBLICATION_MEDIA_SIGNING_KEY", "test-signing-key")
    path = tmp_path / "video.mp4"
    token = publication_media_token("job-1", path)

    assert publication_media_token_is_valid("job-1", path, token) is True
    assert publication_media_token_is_valid("job-2", path, token) is False
    assert publication_media_token_is_valid("job-1", tmp_path / "other.mp4", token) is False
