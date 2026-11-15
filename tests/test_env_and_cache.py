import importlib

from app import cache
from app import env_utils


def test_load_env_file_sets_missing_and_keeps_existing(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "A=1\n"
        "B='two'\n"
        '# comment\n'
        "BAD_LINE\n"
        "C = three\n",
        encoding="utf-8",
    )
    fake_module_file = tmp_path / "pkg" / "env_utils.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(env_utils, "__file__", str(fake_module_file))
    monkeypatch.setenv("B", "already")
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("C", raising=False)
    env_utils._FILE_MANAGED_KEYS.difference_update({"A", "B", "C"})

    env_utils.load_env_file()

    assert "A" in importlib.import_module("os").environ
    assert importlib.import_module("os").environ["A"] == "1"
    assert importlib.import_module("os").environ["B"] == "already"
    assert importlib.import_module("os").environ["C"] == "three"


def test_load_env_file_refreshes_values_previously_loaded_from_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ROTATING_TOKEN=old\n", encoding="utf-8")
    fake_module_file = tmp_path / "pkg" / "env_utils.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(env_utils, "__file__", str(fake_module_file))
    monkeypatch.delenv("ROTATING_TOKEN", raising=False)
    env_utils._FILE_MANAGED_KEYS.discard("ROTATING_TOKEN")

    env_utils.load_env_file()
    assert importlib.import_module("os").environ["ROTATING_TOKEN"] == "old"

    env_file.write_text("ROTATING_TOKEN=new\n", encoding="utf-8")
    env_utils.load_env_file()

    assert importlib.import_module("os").environ["ROTATING_TOKEN"] == "new"


def test_load_env_file_never_overwrites_external_value(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEPLOYMENT_VALUE=from-file\n", encoding="utf-8")
    fake_module_file = tmp_path / "pkg" / "env_utils.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(env_utils, "__file__", str(fake_module_file))
    monkeypatch.setenv("DEPLOYMENT_VALUE", "from-service-manager")
    env_utils._FILE_MANAGED_KEYS.discard("DEPLOYMENT_VALUE")

    env_utils.load_env_file()
    env_file.write_text("DEPLOYMENT_VALUE=changed-in-file\n", encoding="utf-8")
    env_utils.load_env_file()

    assert importlib.import_module("os").environ["DEPLOYMENT_VALUE"] == "from-service-manager"


def test_load_env_file_ignores_missing_file(monkeypatch, tmp_path):
    fake_module_file = tmp_path / "pkg" / "env_utils.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(env_utils, "__file__", str(fake_module_file))

    env_utils.load_env_file()


def test_load_env_file_logs_read_errors(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1", encoding="utf-8")
    fake_module_file = tmp_path / "pkg" / "env_utils.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")
    calls: list[str] = []

    def boom(*_args, **_kwargs):
        raise OSError("cannot read")

    monkeypatch.setattr(env_utils, "__file__", str(fake_module_file))
    monkeypatch.setattr(env_utils.Path, "read_text", boom)
    monkeypatch.setattr(env_utils.logging, "exception", lambda message: calls.append(message))

    env_utils.load_env_file()

    assert calls == ["Failed to read .env"]


def test_cache_roundtrip():
    cache.set_concepts(" idea ", None, None, {"ok": 1})
    assert cache.get_concepts("idea", "", []) == {"ok": 1}

    cache.set_recipe_steps(" concept ", "fb", ["a"], ["s1"])
    assert cache.get_recipe_steps("concept", "fb", ["a"]) == ["s1"]

    cache.set_scenes_plan(" c ", "полный рецепт текст", " x ", {"p": 1})
    assert cache.get_scenes_plan("c", "полный рецепт текст", "x") == {"p": 1}

    cache.set_recipe_full("concept", "fb", "prev", "текст рецепта")
    assert cache.get_recipe_full("concept", "fb", "prev") == "текст рецепта"

    cache.set_derived_recipe_steps("полный рецепт", "концепт", ["a", "b"])
    assert cache.get_derived_recipe_steps("полный рецепт", "концепт") == ["a", "b"]
