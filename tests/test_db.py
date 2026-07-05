import pytest

from app import db


def test_get_database_url_requires_env(monkeypatch):
    monkeypatch.setattr(db, "load_env_file", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db.get_database_url()


def test_get_database_url_reads_and_trims_value(monkeypatch):
    monkeypatch.setattr(db, "load_env_file", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "  postgresql://user:pass@localhost:5432/test_db  ")
    assert db.get_database_url() == "postgresql://user:pass@localhost:5432/test_db"


def test_get_database_url_loads_env_file(monkeypatch):
    called = {"value": False}

    def fake_load_env_file():
        called["value"] = True

    monkeypatch.setattr(db, "load_env_file", fake_load_env_file)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/test_db")
    assert db.get_database_url() == "postgresql://user:pass@localhost:5432/test_db"
    assert called["value"] is True


def test_create_db_engine_uses_provided_url():
    engine = db.create_db_engine("postgresql://user:pass@localhost:5432/test_db")
    assert str(engine.url) == "postgresql://user:***@localhost:5432/test_db"


def test_create_db_engine_blocks_real_database_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_db.py::guard (call)")

    with pytest.raises(RuntimeError, match="Refusing to use non-test database under pytest"):
        db.create_db_engine("postgresql+psycopg2://postgres:postgres@localhost:5432/content_fabric")


def test_create_db_engine_allows_sqlite_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_db.py::guard (call)")

    engine = db.create_db_engine("sqlite+pysqlite:///:memory:")

    assert engine.url.drivername == "sqlite+pysqlite"


def test_create_db_engine_allows_named_test_database_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_db.py::guard (call)")

    engine = db.create_db_engine("postgresql://user:pass@localhost:5432/content_fabric_test")

    assert engine.url.database == "content_fabric_test"


def test_create_tables_uses_passed_engine(monkeypatch):
    class DummyEngine:
        pass

    calls = []

    def fake_create_all(*, bind):
        calls.append(bind)

    monkeypatch.setattr(db.Base.metadata, "create_all", fake_create_all)
    test_engine = DummyEngine()
    db.create_tables(engine=test_engine)

    assert calls == [test_engine]


def test_session_local_calls_cached_factory(monkeypatch):
    calls = []

    class DummySession:
        pass

    def fake_factory(**kwargs):
        calls.append(kwargs)
        return DummySession()

    monkeypatch.setattr(db, "get_session_factory", lambda: fake_factory)
    session = db.SessionLocal(bind="custom")
    assert isinstance(session, DummySession)
    assert calls == [{"bind": "custom"}]


def test_health_check_executes_select():
    calls = []

    class DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement):
            calls.append(str(statement))

    class DummyEngine:
        def connect(self):
            return DummyConnection()

    assert db.health_check(engine=DummyEngine()) is True
    assert calls == ["SELECT 1"]


def test_multiproject_tables_registered_in_metadata():
    expected = {
        "chat_sessions",
        "projects",
        "characters",
        "locations",
        "project_characters",
        "project_locations",
        "project_publish_accounts",
        "publication_jobs",
        "scene_image_candidates",
        "scene_media_message_links",
        "scene_video_candidates",
        "text_steps",
        "text_step_revisions",
    }

    assert expected.issubset(set(db.Base.metadata.tables))
