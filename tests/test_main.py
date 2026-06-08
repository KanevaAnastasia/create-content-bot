import pytest

import main
from telegram.error import NetworkError


def _patch_process_lock(monkeypatch):
    monkeypatch.setattr(main, "_acquire_process_lock", lambda: None)


def test_main_requires_token(monkeypatch):
    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda *_: "")
    with pytest.raises(RuntimeError):
        main.main()


def test_main_runs_polling(monkeypatch):
    class App:
        def __init__(self):
            self.called = False

        def run_polling(self, **kwargs):
            self.called = True

    app = App()
    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda key, default=None: "token" if key == main.TOKEN_ENV_NAME else "")
    monkeypatch.setattr(main, "build_application", lambda token: app)
    main.main()
    assert app.called is True


def test_main_checks_database_when_configured(monkeypatch):
    class App:
        def run_polling(self, **kwargs):
            pass

    calls = []
    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(
        main.os,
        "getenv",
        lambda key, default=None: {
            main.TOKEN_ENV_NAME: "token",
            main.DATABASE_URL_ENV_NAME: "postgresql://user:pass@localhost/db",
        }.get(key, ""),
    )
    monkeypatch.setattr(main, "health_check", lambda: calls.append("checked") or True)
    monkeypatch.setattr(main, "build_application", lambda token: App())

    main.main()

    assert calls == ["checked"]


def test_main_continues_when_database_health_check_fails(monkeypatch):
    class App:
        def __init__(self):
            self.called = False

        def run_polling(self, **kwargs):
            self.called = True

    app = App()
    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(
        main.os,
        "getenv",
        lambda key, default=None: {
            main.TOKEN_ENV_NAME: "token",
            main.DATABASE_URL_ENV_NAME: "postgresql://user:pass@localhost/db",
        }.get(key, ""),
    )
    monkeypatch.setattr(main, "health_check", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(main, "build_application", lambda token: app)

    main.main()

    assert app.called is True


def test_main_retries_polling_after_network_error(monkeypatch):
    class App:
        def __init__(self, outcomes):
            self.outcomes = outcomes

        def run_polling(self, **kwargs):
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome

    build_calls = []
    outcomes = [NetworkError("connect failed"), None]

    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda key, default=None: "token" if key == main.TOKEN_ENV_NAME else "")
    monkeypatch.setattr(main.time, "sleep", lambda seconds: build_calls.append(("sleep", seconds)))

    def fake_build_application(token):
        build_calls.append(("build", token))
        return App(outcomes)

    monkeypatch.setattr(main, "build_application", fake_build_application)

    main.main()

    assert build_calls == [("build", "token"), ("sleep", main._POLLING_RETRY_BASE_SECONDS), ("build", "token")]


def test_main_raises_non_network_polling_errors(monkeypatch):
    class App:
        def run_polling(self, **kwargs):
            raise RuntimeError("boom")

    _patch_process_lock(monkeypatch)
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda key, default=None: "token" if key == main.TOKEN_ENV_NAME else "")
    monkeypatch.setattr(main, "build_application", lambda token: App())

    with pytest.raises(RuntimeError, match="boom"):
        main.main()
