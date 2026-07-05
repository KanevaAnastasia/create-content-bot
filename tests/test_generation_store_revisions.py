from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.generation_store import _record_scene_text_payload_revisions


class FakeDb:
    def __init__(self) -> None:
        self.rows = []

    def add(self, row) -> None:
        self.rows.append(row)


def test_scene_text_revisions_do_not_clear_speech_when_session_has_no_speech_map():
    db = FakeDb()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        scene_order=1,
        text="Русская речь",
        speech_by_lang={"ru": "Русская речь", "en": "English speech"},
    )

    _record_scene_text_payload_revisions(db, row, {"text": "Русская речь"})

    assert db.rows == []


def test_scene_text_revisions_record_changed_speech_languages():
    db = FakeDb()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        scene_order=2,
        text="Старый текст",
        speech_by_lang={"ru": "Старая речь", "en": "Old speech"},
    )

    _record_scene_text_payload_revisions(
        db,
        row,
        {
            "text": "Новый текст",
            "speech_by_lang": {"ru": "Новая речь", "en": "New speech"},
        },
    )

    assert [(item.field_name, item.language, item.old_text, item.new_text) for item in db.rows] == [
        ("text", None, "Старый текст", "Новый текст"),
        ("speech", "en", "Old speech", "New speech"),
        ("speech", "ru", "Старая речь", "Новая речь"),
    ]
