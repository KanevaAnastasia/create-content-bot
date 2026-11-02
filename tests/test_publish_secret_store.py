import os

from app.publish_secret_store import set_env_values


def test_set_env_values_updates_atomically_and_process_environment(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text('UNCHANGED="yes"\nTOKEN="old"\n', encoding="utf-8")

    set_env_values(path, {"TOKEN": "new-secret", "SECOND_TOKEN": "two"})

    text = path.read_text(encoding="utf-8")
    assert 'UNCHANGED="yes"' in text
    assert 'TOKEN="new-secret"' in text
    assert 'SECOND_TOKEN="two"' in text
    assert os.environ["TOKEN"] == "new-secret"
    assert os.environ["SECOND_TOKEN"] == "two"
    assert path.stat().st_mode & 0o777 == 0o600
