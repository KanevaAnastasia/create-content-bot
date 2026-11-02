from pathlib import Path
from unittest.mock import Mock

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
    monkeypatch.setattr(refresh_meta_token, "ENV_PATH", tmp_path / ".env")
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "long-token", "expires_in": 5184000}
    http_get = Mock(return_value=response)

    expires_in = refresh_meta_token.exchange_meta_token("grandma_kitchen", http_get=http_get)

    assert expires_in == 5184000
    assert 'PUBLISH_GRANDMA_KITCHEN_INSTAGRAM_ACCESS_TOKEN="long-token"' in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")
    params = http_get.call_args.kwargs["params"]
    assert params["client_secret"] == "app-secret"
    assert params["fb_exchange_token"] == "short-token"


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
