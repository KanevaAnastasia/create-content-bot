from unittest.mock import MagicMock
import uuid

from app import publication_copy_store as store


def test_bot_copy_is_saved_independently_of_chat_cache(monkeypatch):
    db = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = db
    monkeypatch.setattr(store, "SessionLocal", lambda: manager)
    rid = str(uuid.uuid4())
    copy = {"instagram_en": "English #recipe", "instagram_ru": "Русский #рецепт"}
    store.save_bot_publication_copy({"generation_run_id": rid}, {
        "origin": "production_final", "copy": copy, "recipe": "source",
    })
    artifact = db.add.call_args.args[0]
    assert str(artifact.run_id) == rid
    assert artifact.artifact_type == "publication_copy_draft"
    assert artifact.payload_json["copy"] == copy
    db.commit.assert_called_once()


def test_standalone_recipe_does_not_overwrite_current_run(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(store, "SessionLocal", factory)
    store.save_bot_publication_copy({"generation_run_id": str(uuid.uuid4())}, {
        "origin": "recipe", "copy": {"instagram_en": "Another recipe"},
    })
    factory.assert_not_called()
