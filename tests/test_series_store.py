from types import SimpleNamespace

from app import series_store


def test_create_series_starts_on_first_day_without_reserving(monkeypatch):
    rows: list[SimpleNamespace] = []
    commits: list[bool] = []

    class _ScalarResult:
        def scalar_one_or_none(self):
            return None

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _stmt):
            return _ScalarResult()

        def add(self, row):
            rows.append(row)

        def flush(self):
            for row in rows:
                if not getattr(row, "id", None):
                    row.id = "series-id"

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(series_store, "SessionLocal", lambda: _Db())

    payload = series_store.create_series(
        title_ru="Летние напитки",
        title_en="Summer Drinks",
        total_videos=10,
    )

    assert payload.video_index == 1
    assert payload.total_videos == 10
    assert rows[0].current_video_index == 1
    assert rows[0].is_active is True
    assert rows[0].meta_json["title_by_lang"] == {
        "ru": "Летние напитки",
        "en": "Summer Drinks",
    }
    assert commits == [True]


def test_payload_for_series_includes_extra_language_titles_from_meta():
    row = SimpleNamespace(
        id="series-id",
        title_ru="летние напитки",
        title_en="summer drinks",
        current_video_index=2,
        total_videos=5,
        meta_json={
            "title_by_lang": {
                "ru": "летние напитки",
                "en": "summer drinks",
                "es": "bebidas de verano",
            }
        },
    )

    payload = series_store.payload_for_series(row)

    assert payload.title_by_lang == {
        "ru": "летние напитки",
        "en": "summer drinks",
        "es": "bebidas de verano",
    }
    assert payload.counter_by_lang["es"] == "day 2 of 5"


def test_advance_series_after_successful_render_moves_counter_once(monkeypatch):
    row = SimpleNamespace(
        id="series-id",
        title_ru="летние напитки",
        title_en="summer drinks",
        current_video_index=1,
        total_videos=3,
        is_active=True,
    )
    commits: list[bool] = []

    class _Result:
        def scalar_one_or_none(self):
            return row

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _stmt):
            return _Result()

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(series_store, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(series_store.uuid, "UUID", lambda value: value)

    session = {
        "series": {"id": "series-id", "video_index": 1, "total_videos": 3},
        "series_video_index": 1,
    }

    series_store.advance_series_after_successful_render(session)
    series_store.advance_series_after_successful_render(session)

    assert row.current_video_index == 2
    assert row.is_active is True
    assert commits == [True]
    assert session["series_render_counter_advanced"] is True


def test_advance_series_after_successful_render_closes_last_day(monkeypatch):
    row = SimpleNamespace(
        id="series-id",
        title_ru="летние напитки",
        title_en="summer drinks",
        current_video_index=3,
        total_videos=3,
        is_active=True,
    )

    class _Result:
        def scalar_one_or_none(self):
            return row

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _stmt):
            return _Result()

        def commit(self):
            pass

    monkeypatch.setattr(series_store, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(series_store.uuid, "UUID", lambda value: value)

    session = {
        "series": {"id": "series-id", "video_index": 3, "total_videos": 3},
        "series_video_index": 3,
    }

    series_store.advance_series_after_successful_render(session)

    assert row.current_video_index == 3
    assert row.is_active is False


def test_series_voice_follows_selection_and_is_cleared():
    from app.project_profiles import resolve_story_voice_id
    from app.discipline_modes import LEARNING, MODE_KEY, MALE_VOICE_ID
    row = SimpleNamespace(id='learning', title_ru='Учимся учиться', title_en='Learning to Learn',
                          current_video_index=1, total_videos=10, meta_json={}, narrator_voice_id='anastasia')
    payload = series_store.payload_for_series(row)
    session = {'selected_project_key': 'discipline', MODE_KEY: LEARNING}
    series_store.attach_series_to_session(session, payload)
    assert resolve_story_voice_id(session) == 'anastasia'
    assert session['series']['narrator_voice_id'] == 'anastasia'
    session['project_voice_id'] = 'anastasia'  # TTS caches the chosen voice.
    row.narrator_voice_id = None
    series_store.attach_series_to_session(session, series_store.payload_for_series(row))
    assert 'series_voice_id' not in session
    assert 'project_voice_id' not in session
    assert resolve_story_voice_id(session) == MALE_VOICE_ID
    series_store.attach_series_to_session(session, payload)
    series_store.attach_series_to_session(session, None)
    assert 'series_voice_id' not in session
    assert 'series' not in session
