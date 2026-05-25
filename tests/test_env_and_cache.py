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

    env_utils.load_env_file()

    assert "A" in importlib.import_module("os").environ
    assert importlib.import_module("os").environ["A"] == "1"
    assert importlib.import_module("os").environ["B"] == "already"
    assert importlib.import_module("os").environ["C"] == "three"


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
