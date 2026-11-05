from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from scripts import refresh_meta_token


def test_exchange_meta_token_is_project_scoped_and_hides_value(
    monkeypatch, tmp_path: Path
) -> None:
    values = {
        "INSTAGRAM_APP_ID": "app-id",
        "INSTAGRAM_APP_SECRET": "app-secret",
        "INSTAGRAM_ACCESS_TOKEN": "short-token",
    }
    monkeypatch.setattr(refresh_meta_token, "project_env", lambda project, key: values[key])
    store = Mock()
    monkeypatch.setattr(refresh_meta_token, "set_project_secrets", store)
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "long-token", "expires_in": 5184000}
    http_get = Mock(return_value=response)

    expires_in = refresh_meta_token.exchange_meta_token("grandma_kitchen", http_get=http_get)

    assert expires_in == 5184000
    stored = store.call_args.args
    assert stored[0] == "grandma_kitchen"
    assert stored[1]["INSTAGRAM_ACCESS_TOKEN"] == "long-token"
    assert int(stored[1]["INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT"]) > 0
    params = http_get.call_args.kwargs["params"]
    assert params["client_secret"] == "app-secret"
    assert params["fb_exchange_token"] == "short-token"


def test_exchange_meta_token_supports_language_scoped_credentials(monkeypatch) -> None:
    values = {
        "INSTAGRAM_APP_ID_EN": "app-id-en",
        "INSTAGRAM_APP_SECRET_EN": "app-secret-en",
        "INSTAGRAM_ACCESS_TOKEN_EN": "short-token-en",
    }
    monkeypatch.setattr(refresh_meta_token, "project_env", lambda project, key: values[key])
    store = Mock()
    monkeypatch.setattr(refresh_meta_token, "set_project_secrets", store)
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "long-token-en", "expires_in": 5_184_000}
    http_get = Mock(return_value=response)

    refresh_meta_token.exchange_meta_token(
        "grandma_kitchen", language="en", http_get=http_get
    )

    params = http_get.call_args.kwargs["params"]
    assert params["client_id"] == "app-id-en"
    assert params["client_secret"] == "app-secret-en"
    assert params["fb_exchange_token"] == "short-token-en"
    stored = store.call_args.args[1]
    assert stored["INSTAGRAM_ACCESS_TOKEN_EN"] == "long-token-en"
    assert int(stored["INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT_EN"]) > 0


def test_exchange_meta_token_failure_does_not_include_secret(monkeypatch) -> None:
    secret = "never-print-this"
    monkeypatch.setattr(
        refresh_meta_token,
        "project_env",
        lambda project, key: secret,
    )
    response = Mock(status_code=400)
    response.json.return_value = {"error": {"type": "OAuthException", "code": 190}}

    try:
        refresh_meta_token.exchange_meta_token("grandma_kitchen", http_get=Mock(return_value=response))
    except RuntimeError as exc:
        assert secret not in str(exc)
        assert "OAuthException" in str(exc)
        assert "190" in str(exc)
    else:
        raise AssertionError("Expected Meta token exchange to fail")


def test_exchange_meta_token_network_failure_does_not_include_secret(monkeypatch) -> None:
    secret = "never-print-this"
    monkeypatch.setattr(refresh_meta_token, "project_env", lambda project, key: secret)
    http_get = Mock(side_effect=requests.ConnectionError(f"failed URL with {secret}"))

    with pytest.raises(RuntimeError) as error:
        refresh_meta_token.exchange_meta_token("grandma_kitchen", http_get=http_get)

    assert secret not in str(error.value)
