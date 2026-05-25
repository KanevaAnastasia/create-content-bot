import pytest

import main


def test_main_requires_token(monkeypatch):
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda *_: "")
    with pytest.raises(RuntimeError):
        main.main()


def test_main_runs_polling(monkeypatch):
    class App:
        def __init__(self):
            self.called = False

        def run_polling(self):
            self.called = True

    app = App()
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    monkeypatch.setattr(main.os, "getenv", lambda key, default=None: "token" if key == main.TOKEN_ENV_NAME else "")
    monkeypatch.setattr(main, "build_application", lambda token: app)
    main.main()
    assert app.called is True


def test_main_checks_database_when_configured(monkeypatch):
    class App:
        def run_polling(self):
            pass

    calls = []
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

        def run_polling(self):
            self.called = True

    app = App()
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
