from unittest.mock import MagicMock
import uuid
import json

from app import publication_copy_store as store


def test_bot_copy_is_saved_independently_of_chat_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "COPY_HISTORY_DIR", tmp_path)
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


def test_standalone_recipe_does_not_overwrite_current_run(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "COPY_HISTORY_DIR", tmp_path)
    factory = MagicMock()
    monkeypatch.setattr(store, "SessionLocal", factory)
    store.save_bot_publication_copy({"generation_run_id": str(uuid.uuid4())}, {
        "origin": "recipe", "copy": {"instagram_en": "Another recipe"},
    })
    factory.assert_not_called()
    history = list(tmp_path.glob("*.json"))
    assert len(history) == 1
    payload = json.loads(history[0].read_text())
    assert payload["run_id"] is None
    assert payload["copy"]["instagram_en"] == "Another recipe"


def test_feedback_and_approval_append_history_without_losing_original(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "COPY_HISTORY_DIR", tmp_path)
    session = {"selected_project_key": "grandma_kitchen"}
    pending = {"origin": "description_mode", "copy": {"instagram_en": "Original #tag"}}
    first = store.save_bot_publication_copy(session, pending)
    revised = {**pending, "copy": {"instagram_en": "Revised #tag"}, "last_feedback": "Shorter"}
    second = store.save_bot_publication_copy(session, revised)
    third = store.save_bot_publication_copy(session, revised, approved=True)
    def read(key):
        path = tmp_path / f"{key}.json"
        assert path.stat().st_mode & 0o777 == 0o600
        return json.loads(path.read_text())
    assert read(first)["copy"]["instagram_en"] == "Original #tag"
    assert read(second)["parent_revision_id"] == first
    assert read(second)["feedback"] == "Shorter"
    assert read(third)["status"] == "approved"
    assert read(third)["parent_revision_id"] == second
    assert len(list(tmp_path.glob("*.json"))) == 3
