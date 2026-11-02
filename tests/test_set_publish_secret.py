from pathlib import Path

from scripts.set_publish_secret import env_name, set_env_value


def test_set_publish_secret_is_project_scoped_and_replaces_existing(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('KEEP="yes"\nPUBLISH_GRANDMA_KITCHEN_TOKEN="old"\n', encoding="utf-8")

    set_env_value(path, env_name("grandma_kitchen", "token"), 'new"secret')

    text = path.read_text(encoding="utf-8")
    assert 'KEEP="yes"' in text
    assert 'PUBLISH_GRANDMA_KITCHEN_TOKEN="new\\"secret"' in text
    assert "old" not in text
    assert env_name("discipline", "youtube refresh token") == "PUBLISH_DISCIPLINE_YOUTUBE_REFRESH_TOKEN"


def test_set_publish_secret_rejects_multiline_values(tmp_path: Path) -> None:
    try:
        set_env_value(tmp_path / ".env", "TOKEN", "first\nINJECTED=second")
    except ValueError as exc:
        assert "single line" in str(exc)
    else:
        raise AssertionError("Expected multiline secret to be rejected")
