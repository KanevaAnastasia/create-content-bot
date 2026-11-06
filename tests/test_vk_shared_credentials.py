import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from app import social_publishers as p
from app import publish_secret_store as store
from app.publication_config import project_env_name
from app.vk_credentials import locked_vk_credentials


@pytest.fixture
def configured(monkeypatch, tmp_path):
    values = {
        ("discipline", "VK_USER_CREDENTIALS_PROJECT"): "grandma",
        ("discipline", "VK_USER_ID"): "7",
        ("discipline", "VK_GROUP_ID_RU"): "222",
        ("discipline", "VK_COMMUNITY_TOKEN_RU"): "discipline-wall",
        ("grandma", "VK_USER_ID"): "7",
        ("grandma", "VK_ACCESS_TOKEN"): "user-token",
        ("grandma", "VK_GROUP_ID_RU"): "111",
        ("grandma", "VK_COMMUNITY_TOKEN_RU"): "grandma-wall",
    }
    monkeypatch.setattr(p, "project_env", lambda project, key, default="": values.get((project, key), default))
    monkeypatch.setattr(store, "ENV_PATH", tmp_path / ".env")
    return values


def test_only_user_credentials_are_shared(configured):
    assert p._vk_access_token("discipline") == "user-token"
    assert p.platform_readiness("discipline", "vk", "ru").ready
    assert p.project_env("discipline", "VK_GROUP_ID_RU") == "222"
    del configured["discipline", "VK_COMMUNITY_TOKEN_RU"]
    state = p.platform_readiness("discipline", "vk", "ru")
    assert not state.ready
    assert state.missing == ("PUBLISH_DISCIPLINE_VK_COMMUNITY_TOKEN_RU",)


@pytest.mark.parametrize("user_id", ["", "8", "name"])
def test_shared_oauth_rejects_identity_mismatch(configured, monkeypatch, user_id):
    configured["discipline", "VK_USER_ID"] = user_id
    post = Mock()
    monkeypatch.setattr(p.requests, "post", post)
    with pytest.raises(p.PublicationDeliveryError, match="same explicit VK_USER_ID"):
        p._vk_access_token("discipline")
    assert not p.platform_readiness("discipline", "vk", "ru").ready
    post.assert_not_called()


def test_no_implicit_cross_project_fallback(configured):
    del configured["discipline", "VK_USER_CREDENTIALS_PROJECT"]
    assert not p.platform_readiness("discipline", "vk", "ru").ready
    with pytest.raises(p.PublicationDeliveryError, match="not configured"):
        p._vk_access_token("discipline")


def test_shared_oauth_rejects_chains(configured):
    configured["grandma", "VK_USER_CREDENTIALS_PROJECT"] = "discipline"
    with pytest.raises(p.PublicationDeliveryError, match="Chained"):
        p._vk_access_token("discipline")


def test_refresh_persists_only_to_owner(configured, monkeypatch):
    for key, value in {"VK_CLIENT_ID": "app", "VK_REFRESH_TOKEN": "refresh",
                       "VK_DEVICE_ID": "device", "VK_REDIRECT_URI": "http://localhost/callback"}.items():
        configured["grandma", key] = value
    monkeypatch.setattr(p, "locked_vk_credentials", lambda source: nullcontext())
    monkeypatch.setattr(p.secrets, "token_urlsafe", lambda n: "state")
    monkeypatch.setattr(p.requests, "post", Mock(return_value=Mock(ok=True, status_code=200,
        json=lambda: {"state": "state", "access_token": "new", "refresh_token": "rotated"})))
    save = Mock()
    monkeypatch.setattr(p, "set_project_secrets", save)
    assert p._vk_access_token("discipline") == "new"
    save.assert_called_once_with("grandma", {"VK_ACCESS_TOKEN": "new", "VK_REFRESH_TOKEN": "rotated"})


def test_locked_read_replaces_stale_credentials_only_for_owner(configured, monkeypatch):
    monkeypatch.setenv("PUBLISH_GRANDMA_VK_ACCESS_TOKEN", "stale-access")
    monkeypatch.setenv("PUBLISH_GRANDMA_VK_REFRESH_TOKEN", "stale")
    store.set_project_secrets("grandma", {"VK_REFRESH_TOKEN": "latest", "VK_ACCESS_TOKEN": "current"})
    monkeypatch.setenv("PUBLISH_GRANDMA_VK_REFRESH_TOKEN", "stale")
    monkeypatch.setenv("PUBLISH_DISCIPLINE_VK_REFRESH_TOKEN", "unrelated")
    with locked_vk_credentials("grandma"):
        assert os.environ["PUBLISH_GRANDMA_VK_REFRESH_TOKEN"] == "latest"
        assert os.environ["PUBLISH_DISCIPLINE_VK_REFRESH_TOKEN"] == "unrelated"
    assert store.ENV_PATH.stat().st_mode & 0o777 == 0o600


def test_concurrent_rotations_read_latest_saved_value(configured, monkeypatch):
    key = project_env_name("grandma", "VK_REFRESH_TOKEN")
    monkeypatch.setenv(key, "0")
    store.set_project_secrets("grandma", {"VK_REFRESH_TOKEN": "0"})

    def rotate(_):
        with locked_vk_credentials("grandma"):
            value = int(os.environ[key]) + 1
            store.set_project_secrets("grandma", {"VK_REFRESH_TOKEN": str(value)})
            return value

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(rotate, range(8))) == list(range(1, 9))


def test_concurrent_secret_writes_preserve_other_projects(configured, monkeypatch):
    for n in range(12):
        monkeypatch.setenv(f"VK_TEST_{n}", "")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.set_env_values(store.ENV_PATH, {f"VK_TEST_{n}": str(n)}), range(12)))
    contents = store.ENV_PATH.read_text()
    for n in range(12):
        assert f'VK_TEST_{n}="{n}"' in contents
