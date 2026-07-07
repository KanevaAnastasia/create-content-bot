"""Тесты вспомогательных функций сценарных хендлеров."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.handlers.handlers_state_scene import (
    _apply_overlay_title_style_feedback,
    _apply_overlay_title_to_production,
    _clean_translated_title,
    _default_overlay_title_style,
    _ensure_end_frame_scene_requests_from_session_text,
    _handle_scene_approval_refine,
    _is_explicit_veo3_speech_format_request,
    _frame_prompts_slice_from_production,
    _scene_numbers_from_end_frame_feedback,
    _update_end_frame_scene_requests,
    _generate_project_image,
    _handle_draft_buttons,
    keyboard_for_session_state,
    on_photo,
    on_text,
)
from app.handlers.handlers_media_core import ServiceUnavailableRetriesExhaustedError
from app.handlers.handlers_state_start import (
    SERIES_CHOICE_CONFIRMED_SESSION_KEY,
    SERIES_NEXT_OVERLAY_TITLE,
    SERIES_PENDING_NEXT_SESSION_KEY,
    STATE_WAITING_SERIES_CHOICE,
    STATE_WAITING_SERIES_CREATE,
    STATE_WAITING_SERIES_TRANSLATION_APPROVAL,
    generate,
    _parse_series_create_text,
)
from app.handlers.handlers_state_commands import NAV_BUTTON_PREVIOUS_STEP, NAV_BUTTON_START_OVER
from app.series_store import (
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_PENDING_PREFER_VERTEX_SESSION_KEY,
    SERIES_PENDING_RECIPE_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SeriesPayload,
)
from app.state import SESSIONS


@pytest.fixture(autouse=True)
def _isolate_persistent_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    yield
    engine.dispose()


def test_frame_prompts_slice_from_production_maps_start_end():
    plan = [
        {
            "scene": 1,
            "location": "kitchen",
            "shot_type": "closeup",
            "model": "veo3_fast",
            "duration_sec": 4,
            "start_frame_prompt": "старт описание",
            "end_frame_prompt": "конец описание",
        }
    ]
    out = _frame_prompts_slice_from_production(plan)
    assert len(out) == 1
    assert out[0]["start_frame_prompt_ru"] == "старт описание"
    assert out[0]["end_frame_prompt_ru"] == "конец описание"
    assert "location" not in out[0]


def test_frame_prompts_slice_empty_strings_become_pusto():
    plan = [{"scene": 1, "start_frame_prompt": "", "end_frame_prompt": "   "}]
    out = _frame_prompts_slice_from_production(plan)
    assert out[0]["start_frame_prompt_ru"] == "пусто"
    assert out[0]["end_frame_prompt_ru"] == "пусто"


def test_end_frame_feedback_extracts_only_named_scenes():
    add, remove = _scene_numbers_from_end_frame_feedback(
        "В сценах 2 и 4 нужны конечные кадры; в сцене 3 конечный кадр убрать.",
        total_scenes=5,
    )
    assert add == {2, 4}
    assert remove == {3}


def test_end_frame_feedback_extracts_spelled_first_scene():
    add, remove = _scene_numbers_from_end_frame_feedback(
        "В первой сцене нужен конечный кадр: клубника и банан уже в блендере.",
        total_scenes=4,
    )
    assert add == {1}
    assert remove == set()


def test_end_frame_feedback_updates_persisted_scene_requests():
    session = {"scenes_plan": [{"scene": i} for i in range(1, 5)], "end_frame_scene_requests": [2, 3]}
    assert _update_end_frame_scene_requests(
        session,
        "В сцене 4 нужен финальный кадр. В сцене 2 конечный кадр убрать.",
    ) == [3, 4]


def test_end_frame_requests_can_be_inferred_from_initial_description():
    session = {
        "scenes_plan": [{"scene": 1}, {"scene": 2}, {"scene": 3}],
        "end_frame_scene_requests": [],
        "user_input": (
            "В первой сцене нужен конечный кадр: начинается крупно как клубника разговаривает "
            "с бананом, потом видно что они в блендере."
        ),
    }

    assert _ensure_end_frame_scene_requests_from_session_text(session) == [1]
    assert session["end_frame_scene_requests"] == [1]


def test_restored_concept_state_uses_numbered_concept_keyboard():
    markup = keyboard_for_session_state(
        {
            "state": "waiting_concept",
            "concepts": [{"name": "one"}, {"name": "two"}, {"name": "three"}],
        }
    )

    assert [[button.text for button in row] for row in markup.keyboard][:2] == [["1", "2"], ["3"]]


def test_drafts_button_lists_without_name_error(monkeypatch):
    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "123")
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(message=Message())

    handled = asyncio.run(
        _handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session={}, text="Черновики")
    )

    assert handled is True
    assert replies == ["Черновиков пока нет."]


def test_drafts_open_number_after_list_keeps_page_ids(monkeypatch):
    import app.handlers.handlers_state_scene as scene_handlers

    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "123")
    draft = SimpleNamespace(id="draft-1", title="Черновик", scene_index=None, created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    monkeypatch.setattr(scene_handlers, "list_generation_drafts", lambda *_args, **_kwargs: [draft])
    monkeypatch.setattr(
        scene_handlers,
        "load_generation_draft",
        lambda draft_id: (
            draft,
            {"production": {"json_plan": [{"scene": 1}]}, "scene_media_approve_mode": "ai"},
        ) if draft_id == "draft-1" else None,
    )
    saved_sessions: list[dict] = []
    monkeypatch.setattr(scene_handlers, "save_session_if_possible", lambda _chat_id, session: saved_sessions.append(dict(session)))
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(message=Message())
    SESSIONS.pop(123, None)

    listed = asyncio.run(
        scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session={}, text="Черновики")
    )
    opened = asyncio.run(
        scene_handlers._handle_draft_buttons(
            update,
            SimpleNamespace(),
            chat_id=123,
            session=SESSIONS.get(123) or {},
            text="Открыть 1",
        )
    )

    assert listed is True
    assert opened is True
    assert SESSIONS[123]["state"] == "state_waiting_draft_scenes"
    assert SESSIONS[123]["scene_media_approve_mode"] == "manual"
    assert saved_sessions
    assert any("Открыла черновик" in reply for reply in replies)


def test_open_draft_disables_ai_auto_and_uses_scene_keyboard(monkeypatch):
    import app.handlers.handlers_state_scene as scene_handlers

    monkeypatch.setenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID", "123")
    draft = SimpleNamespace(id="draft-1", title="Черновик")
    snapshot = {
        "state": "waiting_scene_media",
        "scene_media_auto_approve": True,
        "scene_media_approve_mode": "ai",
        "scene_media_ai_human_review": {"stage": "video"},
        "production": {"json_plan": [{"scene": 1}, {"scene": 2}, {"scene": 3}, {"scene": 4}]},
    }
    monkeypatch.setattr(scene_handlers, "load_generation_draft", lambda _draft_id: (draft, dict(snapshot)))
    replies: list[tuple[str, object | None]] = []

    class Message:
        async def reply_text(self, text, **kwargs):
            replies.append((text, kwargs.get("reply_markup")))

    update = SimpleNamespace(message=Message())

    handled = asyncio.run(
        scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session={}, text="Черновик draft-1")
    )

    assert handled is True
    opened = SESSIONS[123]
    assert opened["state"] == "state_waiting_draft_scenes"
    assert opened["scene_media_approve_mode"] == "manual"
    assert "scene_media_auto_approve" not in opened
    assert "scene_media_ai_human_review" not in opened
    buttons = [button.text for row in replies[-1][1].keyboard for button in row]
    assert "Сцена 4" in buttons
    assert "СТОП" not in buttons


def test_draft_repair_plan_can_select_scene_four_only(monkeypatch):
    import app.handlers.handlers_state_scene as scene_handlers

    sent_steps: list[tuple[int, str]] = []

    async def _fake_send_next(_update, session):
        sent_steps.append((session["image_scene_index"], session["scene_media_substep"]))

    db_clears: list[tuple[str | None, str, list[str]]] = []

    def _fake_clear_db(scene_id, *, stage, languages):
        db_clears.append((scene_id, stage, list(languages)))

    monkeypatch.setattr(scene_handlers, "_send_next_scene_media_step", _fake_send_next)
    monkeypatch.setattr(scene_handlers, "clear_scene_db_approvals_for_repair", _fake_clear_db)
    session = {
        "state": "state_waiting_draft_scenes",
        "draft_repair": {"draft_id": "draft-1", "plan": []},
        "scene_db_ids_by_index": {"3": "scene-db-4"},
        "production": {
            "json_plan": [
                {
                    "scene": idx + 1,
                    "video_results_by_lang": {"ru": {"video_b64": "old"}, "en": {}},
                    "video_approvals_by_lang": {"ru": True, "en": True},
                    "video_candidates_by_lang": {
                        "ru": [{"candidate_id": f"old-ru-{idx}", "approved_for_production": True}],
                    },
                }
                for idx in range(5)
            ]
        },
    }
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(message=Message())

    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Сцена 4"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Далее"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="RU"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Видео"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Начать правку"))

    assert session["draft_repair_active_plan"] == [{"scene_index": 3, "languages": ["ru"], "stage": "video"}]
    assert sent_steps == [(3, "video_prompt")]
    assert "video_results_by_lang" in session["production"]["json_plan"][1]
    scene_four = session["production"]["json_plan"][3]
    assert scene_four["video_approvals_by_lang"]["ru"] is False
    assert "ru" not in scene_four["video_results_by_lang"]
    assert "ru" not in scene_four["video_candidates_by_lang"]
    assert scene_four["draft_repair_ignore_restored_video_candidates_by_lang"] == {"ru": True}
    assert db_clears == [("scene-db-4", "video", ["ru"])]


def test_clear_scene_for_image_repair_unapproves_images_and_videos():
    from app.drafts import clear_scene_for_repair

    scene = {
        "start_frame_image_b64": "old-start",
        "image_candidates": [
            {"candidate_id": "img-1", "approved_for_video": True},
            {"candidate_id": "img-2", "approved_for_video": True},
        ],
        "video_results_by_lang": {
            "ru": {"video_b64": "old-ru"},
            "en": {"video_b64": "old-en"},
        },
        "video_approvals_by_lang": {"ru": True, "en": True},
        "video_candidates_by_lang": {
            "ru": [{"candidate_id": "vid-ru", "approved_for_production": True}],
            "en": [{"candidate_id": "vid-en", "approved_for_production": True}],
        },
        "video_provider_choice_pending": {"source": "scene", "language": "ru"},
        "syntax_fallback_pending": {"source": "scene"},
        "video_prompt_policy_candidate": "stale prompt",
        "video_prompt_policy_candidate_by_lang": {"ru": "stale ru", "en": "keep en"},
        "video_prompt_policy_failure_count": 2,
    }

    clear_scene_for_repair(scene, stage="image", languages=["ru"])

    assert "start_frame_image_b64" not in scene
    assert "image_candidates" not in scene
    assert "ru" not in scene["video_results_by_lang"]
    assert "en" in scene["video_results_by_lang"]
    assert "ru" not in scene["video_candidates_by_lang"]
    assert scene["video_candidates_by_lang"]["en"][0]["approved_for_production"] is True
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert scene["video_approvals_by_lang"]["en"] is True
    assert scene["draft_repair_ignore_restored_video_candidates_by_lang"] == {"ru": True}
    assert "video_provider_choice_pending" not in scene
    assert "syntax_fallback_pending" not in scene
    assert "video_prompt_policy_candidate" not in scene
    assert scene["video_prompt_policy_candidate_by_lang"] == {"en": "keep en"}
    assert "video_prompt_policy_failure_count" not in scene


def test_draft_repair_keyboard_multiple_scenes_configures_each_scene_separately(monkeypatch):
    import app.handlers.handlers_state_scene as scene_handlers

    sent_steps: list[tuple[int, str]] = []

    async def _fake_send_next(_update, session):
        sent_steps.append((session["image_scene_index"], session["scene_media_substep"]))

    monkeypatch.setattr(scene_handlers, "_send_next_scene_media_step", _fake_send_next)
    session = {
        "state": "state_waiting_draft_scenes",
        "draft_repair": {"draft_id": "draft-1", "plan": []},
        "production": {"json_plan": [{"scene": idx + 1, "video_results_by_lang": {"ru": {}, "en": {}}} for idx in range(4)]},
    }
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(message=Message())

    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Сцена 1"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Сцена 3"))
    assert session["draft_repair"]["selected_scene_indexes"] == [0, 2]

    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Далее"))
    assert "Сцена 1" in replies[-1]
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="RU"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Видео"))
    assert "Сцена 3" in replies[-1]
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="EN"))
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Промпт"))

    assert session["draft_repair"]["plan"] == [
        {"scene_index": 0, "languages": ["ru"], "stage": "video"},
        {"scene_index": 2, "languages": ["en"], "stage": "prompt"},
    ]
    assert asyncio.run(scene_handlers._handle_draft_buttons(update, SimpleNamespace(), chat_id=123, session=session, text="Начать правку"))
    assert session["draft_repair_active_plan"] == [
        {"scene_index": 0, "languages": ["ru"], "stage": "video"},
        {"scene_index": 2, "languages": ["en"], "stage": "prompt"},
    ]
    assert sent_steps == [(0, "video_prompt")]


def test_restored_overlay_title_ru_state_removes_keyboard():
    markup = keyboard_for_session_state({"state": "state_waiting_overlay_title_ru"})

    assert markup.__class__.__name__ == "ReplyKeyboardRemove"


def test_scene_feedback_with_pause_and_scene_numbers_is_not_stress_fix():
    text = (
        "Чуть подправим фразы, в сцене 1 скажем что мы сделаем а не делаем, "
        "и пауза не нужна между словами ингредиентов и сегодня"
    )
    assert _is_explicit_veo3_speech_format_request(text) is False


def test_short_explicit_stress_fix_still_supported():
    assert _is_explicit_veo3_speech_format_request("убери ударения") is True
    assert _is_explicit_veo3_speech_format_request("нормализуй паузы veo3") is True


def test_scene_approval_feedback_mentions_pause_but_regenerates_scenes(monkeypatch):
    captured: list[str] = []
    session = {
        "state": "waiting_scene_approval",
        "selected_concept": "банановое мороженое",
        "recipe_full_text": "Заморозить бананы. Взбить со сливками.",
        "scenes_plan": [
            {"scene": 1, "text": "сегодня делаем мороженое", "model": "veo3_fast"},
        ],
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1030))

    def _raise_if_called(_scenes):
        raise AssertionError("stress formatter should not handle content scene feedback")

    async def _fake_call(_update, _context, _chat_id, _name, _fn):
        return {
            "json_plan": [
                {"scene": 1, "text": "сегодня мы сделаем мороженое", "model": "veo3_fast"},
                {"scene": 2, "text": "потом отправляем на ночь в морозилку", "model": "veo3_fast"},
            ],
            "text_plan": "СЦЕНА 1\nСЦЕНА 2",
            "regeneration_events": [],
        }

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.apply_veo3_lowercase_format",
        _raise_if_called,
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    asyncio.run(
        _handle_scene_approval_refine(
            update,
            context=None,
            chat_id=1030,
            session=session,
            text="в сцене 1 пауза не нужна между ингредиентов и сегодня",
        )
    )

    assert session["state"] == "waiting_scene_approval"
    assert len(session["scenes_plan"]) == 2
    assert any("Обновила план по правкам" in item for item in captured)


def test_on_text_first_step_generate_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1001, None)

    class _Message:
        text = "Генерировать"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1001))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите идею или рецепт обычным текстом" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1001]["state"] == "state_waiting_first_step_generate_text"


def test_generate_command_launches_pipeline(monkeypatch):
    captured_calls: list[tuple[str, bool]] = []
    SESSIONS.pop(1006, None)

    class _Message:
        text = "/generate Сырники с клубникой"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_generation_with_recipe(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        captured_calls.append((recipe, prefer_vertex_api))

    monkeypatch.setattr(
        "app.handlers.handlers_state_start._start_generation_with_recipe",
        _fake_start_generation_with_recipe,
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1006))
    asyncio.run(generate(update, context=None))

    assert captured_calls == [("Сырники с клубникой", False)]


def test_start_generation_with_recipe_asks_for_series(monkeypatch):
    from app.handlers.handlers_state_start import _start_generation_with_recipe

    monkeypatch.setattr("app.handlers.handlers_state_start.list_active_series", lambda _project_slug: [])
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1060, None)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1060))
    asyncio.run(
        _start_generation_with_recipe(
            update,
            context=None,
            recipe="Клубника 350 г. Взбить с молоком.",
            prefer_vertex_api=False,
        )
    )

    assert SESSIONS[1060]["state"] == STATE_WAITING_SERIES_CHOICE
    assert SESSIONS[1060][SERIES_PENDING_RECIPE_SESSION_KEY].startswith("Клубника")
    assert any("Можно привязать" in text for text, _markup in captured)
    assert any(markup is not None for _text, markup in captured)


def test_series_skip_continues_generation(monkeypatch):
    continued: list[tuple[str, bool]] = []
    SESSIONS[1061] = {
        "state": STATE_WAITING_SERIES_CHOICE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: True,
    }

    async def _fake_continue(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        continued.append((recipe, prefer_vertex_api))

    monkeypatch.setattr("app.handlers.handlers_state_start._continue_generation_with_recipe", _fake_continue)

    class _Message:
        text = "Без серии"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1061))
    asyncio.run(on_text(update, context=None))

    assert continued == [("Идея", True)]
    assert "series" not in SESSIONS[1061]
    assert SESSIONS[1061][SERIES_CHOICE_CONFIRMED_SESSION_KEY] is True


def test_series_skip_before_overlay_title_goes_to_title_input():
    replies: list[str] = []
    SESSIONS[1064] = {
        "state": STATE_WAITING_SERIES_CHOICE,
        SERIES_PENDING_NEXT_SESSION_KEY: SERIES_NEXT_OVERLAY_TITLE,
    }

    class _Message:
        text = "Без серии"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1064))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1064]["state"] == "state_waiting_overlay_title_ru"
    assert SESSIONS[1064][SERIES_CHOICE_CONFIRMED_SESSION_KEY] is True
    assert "series" not in SESSIONS[1064]
    assert any("русский заголовок" in text for text in replies)


def test_existing_series_choice_selects_current_day_without_advancing(monkeypatch):
    payload = SeriesPayload(
        id="00000000-0000-0000-0000-000000000007",
        title_by_lang={"ru": "летние напитки", "en": "summer drinks"},
        video_index=7,
        total_videos=10,
        counter_by_lang={"ru": "день 7 из 10", "en": "day 7 of 10"},
    )
    continued: list[str] = []
    selected: list[str] = []
    SESSIONS[1063] = {
        "state": STATE_WAITING_SERIES_CHOICE,
        "series_choice_ids": [payload.id],
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }
    monkeypatch.setattr(
        "app.handlers.handlers_state_start.select_series_payload",
        lambda series_id: selected.append(series_id) or payload,
    )

    async def _fake_continue(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context, prefer_vertex_api)
        continued.append(recipe)

    monkeypatch.setattr("app.handlers.handlers_state_start._continue_generation_with_recipe", _fake_continue)

    class _Message:
        text = "1"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1063))
    asyncio.run(on_text(update, context=None))

    assert selected == [payload.id]
    assert continued == ["Идея"]
    assert SESSIONS[1063]["series_video_index"] == 7


def test_series_create_parser_and_flow(monkeypatch):
    assert _parse_series_create_text("летние напитки | 10") == (
        "летние напитки",
        None,
        10,
    )
    assert _parse_series_create_text("летние напитки | summer drinks | 10") == (
        "летние напитки",
        "summer drinks",
        10,
    )
    payload = SeriesPayload(
        id="00000000-0000-0000-0000-000000000001",
        title_by_lang={"ru": "летние напитки", "en": "summer drinks"},
        video_index=1,
        total_videos=10,
        counter_by_lang={"ru": "день 1 из 10", "en": "day 1 of 10"},
    )
    continued: list[str] = []
    SESSIONS[1062] = {
        "state": STATE_WAITING_SERIES_CREATE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
        "selected_project_key": "grandma_kitchen",
    }
    monkeypatch.setattr("app.handlers.handlers_state_start.create_series", lambda **_kwargs: payload)

    async def _fake_continue(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context, prefer_vertex_api)
        continued.append(recipe)

    monkeypatch.setattr("app.handlers.handlers_state_start._continue_generation_with_recipe", _fake_continue)

    class _Message:
        text = "летние напитки | summer drinks | 10"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1062))
    asyncio.run(on_text(update, context=None))

    assert continued == ["Идея"]
    assert SESSIONS[1062]["series"]["id"] == payload.id
    assert SESSIONS[1062]["series_video_index"] == 1


def test_series_create_with_ru_only_suggests_ai_translations_before_create(monkeypatch):
    payload = SeriesPayload(
        id="00000000-0000-0000-0000-000000000002",
        title_by_lang={"ru": "летние напитки", "en": "summer drinks", "es": "bebidas de verano"},
        video_index=1,
        total_videos=10,
        counter_by_lang={"ru": "день 1 из 10", "en": "day 1 of 10", "es": "day 1 of 10"},
    )
    created: list[dict] = []
    continued: list[str] = []
    replies: list[str] = []
    SESSIONS[1065] = {
        "state": STATE_WAITING_SERIES_CREATE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
        "selected_project_key": "grandma_kitchen",
        "target_languages": ["ru", "en", "es"],
    }

    async def _fake_translate(update, context, *, chat_id, ru_title, languages):
        _ = (update, context, chat_id)
        assert ru_title == "летние напитки"
        assert languages == ["ru", "en", "es"]
        return {"ru": ru_title, "en": "summer drinks", "es": "bebidas de verano"}

    def _fake_create_series(**kwargs):
        created.append(kwargs)
        return payload

    async def _fake_continue(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context, prefer_vertex_api)
        continued.append(recipe)

    monkeypatch.setattr("app.handlers.handlers_state_start._translate_series_titles", _fake_translate)
    monkeypatch.setattr("app.handlers.handlers_state_start.create_series", _fake_create_series)
    monkeypatch.setattr("app.handlers.handlers_state_start._continue_generation_with_recipe", _fake_continue)

    class _Message:
        text = "летние напитки | 10"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1065))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1065]["state"] == STATE_WAITING_SERIES_TRANSLATION_APPROVAL
    assert not created
    assert any("ES: bebidas de verano" in text for text in replies)

    update.message.text = "ОК"
    asyncio.run(on_text(update, context=None))

    assert created == [
        {
            "title_ru": "летние напитки",
            "title_en": "summer drinks",
            "title_by_lang": {
                "ru": "летние напитки",
                "en": "summer drinks",
                "es": "bebidas de verano",
            },
            "total_videos": 10,
            "project_slug": "grandma_kitchen",
        }
    ]
    assert continued == ["Идея"]
    assert SESSIONS[1065]["series"]["title_by_lang"]["es"] == "bebidas de verano"


def test_on_text_first_step_button_after_completed_production_starts_new_mode():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1024] = {
        "state": "ready_for_production",
        "production": {"json_plan": [{"scene": 1}]},
        "selected_project_key": "grandma_kitchen",
        "target_languages": ["ru", "en"],
    }

    class _Message:
        text = "Генерировать"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1024))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Production уже готов" not in captured[0][0]
    assert "Пришлите идею или рецепт обычным текстом" in captured[0][0]
    assert SESSIONS[1024]["state"] == "state_waiting_first_step_generate_text"
    assert "production" not in SESSIONS[1024]


def test_on_text_first_step_description_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1021, None)

    class _Message:
        text = "Описание"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1021))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите рецепт или ссылку" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1021]["state"] == "state_waiting_first_step_description_text"


def test_on_text_first_step_image_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1002, None)

    class _Message:
        text = "Изображение"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1002))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите prompt обычным текстом" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1002]["state"] == "state_waiting_first_step_image_text"


def test_on_text_first_step_project_image_button_opens_character_choice():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1012, None)

    class _Message:
        text = "Изображение проект"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1012))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Выберите персонажей" in captured[0][0]
    assert "бабушка, рыжий кот" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1012]["state"] == "state_waiting_first_step_project_image_characters"
    assert SESSIONS[1012]["first_step_project_image_characters"] == ["grandma", "cat"]


def test_on_text_first_step_sound_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1014, None)

    class _Message:
        text = "Звук"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1014))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Пришлите текст для озвучки" in captured[0][0]
    assert "grandma" in captured[0][0]
    assert SESSIONS[1014]["state"] == "state_waiting_first_step_sound_text"
    assert SESSIONS[1014]["first_step_sound_character"] == "grandma"


def test_on_text_first_step_video_button_without_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1003, None)

    class _Message:
        text = "Видео"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1003))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Шаг 1/2" in captured[0][0]
    assert captured[0][1] is not None
    assert SESSIONS[1003]["state"] == "state_waiting_first_step_video_text"


def test_on_text_first_step_generate_text_launches_pipeline(monkeypatch):
    captured_calls: list[tuple[str, bool]] = []
    SESSIONS[1004] = {"state": "state_waiting_first_step_generate_text"}

    class _Message:
        text = "Сырники с клубникой"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_generation_with_recipe(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        captured_calls.append((recipe, prefer_vertex_api))

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_generation_with_recipe",
        _fake_start_generation_with_recipe,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1004))
    asyncio.run(on_text(update, context=None))
    assert captured_calls == [("Сырники с клубникой", False)]


def test_on_text_without_first_step_selection_defaults_to_generate(monkeypatch):
    captured_calls: list[tuple[str, bool]] = []
    SESSIONS.pop(1031, None)

    class _Message:
        text = "Пирог с вишней"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_generation_with_recipe(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        captured_calls.append((recipe, prefer_vertex_api))

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_generation_with_recipe",
        _fake_start_generation_with_recipe,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1031))
    asyncio.run(on_text(update, context=None))
    assert captured_calls == [("Пирог с вишней", False)]


def test_on_photo_first_step_generate_extracts_source_and_launches_pipeline(monkeypatch):
    captured_calls: list[tuple[str, bool]] = []
    replies: list[str] = []
    SESSIONS.pop(1032, None)

    class _Message:
        caption = "Сделай ролик по этому блюду"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"photo"

    async def _fake_source(image_bytes, caption=""):
        assert image_bytes == b"photo"
        assert caption == "Сделай ролик по этому блюду"
        return "Рецепт вишнёвого пирога"

    async def _fake_call(_update, _context, _chat_id, _name, fn):
        return await fn()

    async def _fake_start_generation_with_recipe(update, context, *, recipe, prefer_vertex_api):
        _ = (update, context)
        captured_calls.append((recipe, prefer_vertex_api))

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generation_source_from_image", _fake_source)
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_generation_with_recipe",
        _fake_start_generation_with_recipe,
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1032))
    asyncio.run(on_photo(update, context=None))

    assert captured_calls == [("Рецепт вишнёвого пирога", False)]
    assert any("Распознаю идею или рецепт по фото" in text for text in replies)


def test_scene_approval_ok_asks_for_russian_overlay_title():
    replies: list[str] = []
    markups: list[object] = []
    SESSIONS[1040] = {
        "state": "waiting_scene_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "viral_concept_brief": {},
        SERIES_CHOICE_CONFIRMED_SESSION_KEY: True,
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            markups.append(reply_markup)
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1040))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1040]["state"] == "state_waiting_overlay_title_ru"
    assert any("введите русский заголовок" in text.lower() for text in replies)
    assert markups[-1].__class__.__name__ == "ReplyKeyboardRemove"


def test_scene_approval_ok_asks_for_series_before_title_if_not_chosen(monkeypatch):
    replies: list[str] = []
    markups: list[object] = []
    SESSIONS[1046] = {
        "state": "waiting_scene_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "viral_concept_brief": {},
        "selected_project_key": "grandma_kitchen",
    }
    monkeypatch.setattr("app.handlers.handlers_state_start.list_active_series", lambda _project_slug: [])

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            markups.append(reply_markup)
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1046))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1046]["state"] == STATE_WAITING_SERIES_CHOICE
    assert SESSIONS[1046][SERIES_PENDING_NEXT_SESSION_KEY] == SERIES_NEXT_OVERLAY_TITLE
    assert any("Можно привязать это видео к серии" in text for text in replies)
    assert markups[-1] is not None


def test_overlay_title_ru_generates_english_candidate(monkeypatch):
    replies: list[str] = []
    SESSIONS[1041] = {"state": "state_waiting_overlay_title_ru"}

    class _Message:
        text = "Борщ как у бабушки"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "translate_overlay_title_en"
        return "Grandma's Borscht"

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1041))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1041]["state"] == "state_waiting_overlay_title_en_approval"
    assert SESSIONS[1041]["overlay_title_translation_pending"] == {
        "ru": "Борщ как у бабушки",
        "en": "Grandma's Borscht",
    }
    assert any("EN: Grandma's Borscht" in text for text in replies)


def test_overlay_title_ru_translation_error_keeps_ru_input_state(monkeypatch):
    replies: list[str] = []
    SESSIONS[1046] = {"state": "state_waiting_overlay_title_ru"}

    class _Message:
        text = "Борщ как у бабушки"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, _name, _fn):
        raise ServiceUnavailableRetriesExhaustedError("Gemini временно недоступен")

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1046))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1046]["state"] == "state_waiting_overlay_title_ru"
    assert "overlay_title_translation_pending" not in SESSIONS[1046]
    assert any("Gemini временно недоступен" in text for text in replies)


def test_overlay_title_ru_empty_translation_falls_back_to_russian(monkeypatch):
    replies: list[str] = []
    SESSIONS[1047] = {"state": "state_waiting_overlay_title_ru"}

    class _Message:
        text = "Борщ как у бабушки"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "translate_overlay_title_en"
        return "EN:   "

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1047))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1047]["state"] == "state_waiting_overlay_title_en_approval"
    assert SESSIONS[1047]["overlay_title_translation_pending"] == {
        "ru": "Борщ как у бабушки",
        "en": "Борщ как у бабушки",
    }
    assert any("EN: Борщ как у бабушки" in text for text in replies)


def test_overlay_title_en_ok_asks_for_style_approval():
    replies: list[str] = []
    photos: list[tuple[str, str]] = []
    SESSIONS[1042] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            photos.append((getattr(photo, "name", ""), caption))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1042))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1042]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1042]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}
    assert SESSIONS[1042]["overlay_title_style"]["title_color"] == "#c72638"
    assert [name for name, _caption in photos] == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]
    assert [caption for _name, caption in photos] == ["Превью верхнего заголовка RU.", "Превью верхнего заголовка EN."]
    assert any("Цвет заголовка: #c72638" in text for text in replies)


def test_overlay_title_preview_receives_series_context(monkeypatch):
    calls: list[tuple[str, str, str, str]] = []
    SESSIONS[1051] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
        SERIES_TITLE_BY_LANG_SESSION_KEY: {"ru": "Супы у бабушки", "en": "Grandma's Soups"},
        SERIES_COUNTER_BY_LANG_SESSION_KEY: {"ru": "день 2 из 7", "en": "day 2 of 7"},
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

        async def reply_photo(self, photo, caption):
            _ = (photo, caption)

    def _fake_preview(language, title, _style, series_title="", series_counter=""):
        calls.append((language, title, series_title, series_counter))
        return b"png"

    monkeypatch.setattr("app.handlers.handlers_state_scene._render_overlay_title_preview", _fake_preview)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1051))
    asyncio.run(on_text(update, context=None))

    assert calls == [
        ("ru", "Борщ", "Супы у бабушки", "день 2 из 7"),
        ("en", "Borscht", "Grandma's Soups", "day 2 of 7"),
    ]


def test_overlay_title_en_missing_pending_returns_to_ru_title_state():
    replies: list[str] = []
    SESSIONS[1048] = {"state": "state_waiting_overlay_title_en_approval"}

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1048))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1048]["state"] == "state_waiting_overlay_title_ru"
    assert any("Введите русский заголовок ещё раз" in text for text in replies)


def test_overlay_title_en_correction_asks_for_style_approval():
    replies: list[str] = []
    photos: list[str] = []
    SESSIONS[1043] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        text = "Real Borscht"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1043))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1043]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1043]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Real Borscht"}
    assert photos == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]
    assert any("Английский заголовок исправила" in text for text in replies)


def test_overlay_title_style_preview_failure_does_not_block_approval(monkeypatch):
    replies: list[str] = []
    photos: list[str] = []
    SESSIONS[1049] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    def _raise_preview(_language, _title, _style, _series_title="", _series_counter=""):
        raise RuntimeError("font missing")

    monkeypatch.setattr("app.handlers.handlers_state_scene._render_overlay_title_preview", _raise_preview)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1049))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1049]["state"] == "state_waiting_overlay_title_style_approval"
    assert photos == []
    assert any("Предлагаю настройки верхнего заголовка" in text for text in replies)


def test_overlay_title_style_ok_generates_production(monkeypatch):
    replies: list[str] = []
    SESSIONS[1044] = {
        "state": "state_waiting_overlay_title_style_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.1},
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "generate_full_production"
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "s", "video_prompt": "v"}]}

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1044))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1044]["state"] == "waiting_production_bundle_approval"
    assert SESSIONS[1044]["production"]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}
    assert SESSIONS[1044]["production"]["overlay_title_style"] == {
        "title_color": "#c72638",
        "top_margin_ratio": 0.15,
        "side_margin_ratio": 0.1,
    }
    assert any("Заголовок утверждён" in text for text in replies)


def test_overlay_title_style_generation_empty_plan_keeps_style_state(monkeypatch):
    replies: list[str] = []
    SESSIONS[1050] = {
        "state": "state_waiting_overlay_title_style_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.1},
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "generate_full_production"
        return {"json_plan": []}

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1050))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1050]["state"] == "state_waiting_overlay_title_style_approval"
    assert "production" not in SESSIONS[1050]
    assert any("Модель не вернула production-план" in text for text in replies)


def test_overlay_title_style_feedback_updates_settings_without_generating(monkeypatch):
    replies: list[str] = []
    photos: list[str] = []
    calls: list[str] = []
    SESSIONS[1045] = {
        "state": "state_waiting_overlay_title_style_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.12, "side_margin_ratio": 0.1},
    }

    class _Message:
        text = "цвет #ff4f7b, сверху 15%, боковые 11%"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        calls.append(name)
        return {"json_plan": []}

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1045))
    asyncio.run(on_text(update, context=None))

    assert calls == []
    assert SESSIONS[1045]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1045]["overlay_title_style"] == {
        "title_color": "#ff4f7b",
        "top_margin_ratio": 0.15,
        "side_margin_ratio": 0.11,
    }
    assert photos == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]
    assert any("Цвет заголовка: #ff4f7b" in text for text in replies)


def test_overlay_title_helpers_clean_sanitize_and_clamp_values():
    assert _clean_translated_title('"EN: Grandma Borscht"') == "Grandma Borscht"

    session = {
        "overlay_title": "  Борщ  ",
        "overlay_title_by_lang": {"ru": " Борщ ", "": "skip", "en": "", "es": " Sopa "},
        "overlay_title_style": {"title_color": "#C72638", "": "skip", "top_margin_ratio": 15},
    }
    production: dict[str, object] = {}

    _apply_overlay_title_to_production(session, production)

    assert production["overlay_title"] == "Борщ"
    assert production["overlay_title_by_lang"] == {"ru": "Борщ", "es": "Sopa"}
    assert production["overlay_title_style"] == {"title_color": "#C72638", "top_margin_ratio": 15}

    updated = _apply_overlay_title_style_feedback(
        {"title_color": "#fff7e8", "top_margin_ratio": 0.15, "side_margin_ratio": 0.10},
        "цвет aa00ff, top 99%, side 1%",
    )

    assert updated["title_color"] == "#aa00ff"
    assert updated["top_margin_ratio"] == 0.22
    assert updated["side_margin_ratio"] == 0.04


def test_default_overlay_title_style_uses_five_percent_top_margin():
    style = _default_overlay_title_style({"overlay_title": "Борщ"})

    assert style["top_margin_ratio"] == 0.05


def test_on_text_without_choice_on_recipe_step_is_feedback(monkeypatch):
    captured_feedback: list[str] = []
    SESSIONS[1033] = {
        "state": "waiting_recipe_approval",
        "recipe_full_text": "Старый рецепт",
    }

    class _Message:
        text = "Добавь в начинку немного корицы"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_recipe_handler(update, context, *, chat_id, session, text):
        _ = (update, context, chat_id, session)
        captured_feedback.append(text)
        return True

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._handle_waiting_recipe_approval_state",
        _fake_recipe_handler,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1033))
    asyncio.run(on_text(update, context=None))

    assert captured_feedback == ["Добавь в начинку немного корицы"]


def test_on_text_first_step_description_text_generates_social_copy(monkeypatch):
    captured_calls: list[str] = []
    SESSIONS[1022] = {"state": "state_waiting_first_step_description_text"}

    class _Message:
        text = "Сырники с клубникой"

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_start_description_with_recipe(update, context, *, recipe):
        _ = (update, context)
        captured_calls.append(recipe)

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_description_with_recipe",
        _fake_start_description_with_recipe,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1022))
    asyncio.run(on_text(update, context=None))
    assert captured_calls == ["Сырники с клубникой"]


def test_on_text_first_step_image_text_generates_image(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    SESSIONS[1005] = {"state": "state_waiting_first_step_image_text"}

    class _Message:
        text = "Бабушка с котом и пирогом"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"img"
            assert "Изображение готово" in caption
            sent["ok"] = True

    async def _fake_generate(prompt: str):
        assert "пирогом" in prompt
        return b"img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1005))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "Генерируем изображение" in captured[0][0]
    assert SESSIONS[1005]["state"] == ""
    assert SESSIONS[1005]["selected_project_key"] == "grandma_kitchen"


def test_on_text_first_step_image_accepts_custom_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1016] = {"state": "state_waiting_first_step_image_text"}

    class _Message:
        text = "Добавить свое"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"img"
            assert "Изображение готово" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1016]["state"] == "state_waiting_first_step_image_references"
    assert "Пришлите одно или несколько" in captured[-1][0]

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"custom-ref"

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_photo(photo_update, context=None))
    assert SESSIONS[1016]["first_step_custom_reference_images"] == [b"custom-ref"]
    assert "Добавила файл #1" in captured[-1][0]

    class _DoneMessage(_Message):
        text = "больше файлов не нужно"

    update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1016]["state"] == "state_waiting_first_step_image_text"
    assert "Теперь пришлите prompt" in captured[-1][0]

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Красивая тарелка супа в мультяшном стиле"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1016))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert seen["prompt"] == "Красивая тарелка супа в мультяшном стиле"
    assert seen["reference_images"] == [b"custom-ref"]
    assert SESSIONS[1016]["state"] == ""


def test_frame_prompts_approval_can_collect_reference_images(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    seen: dict[str, object] = {}
    SESSIONS[1045] = {
        "state": "waiting_frame_prompts_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена", "model": "veo3_fast"}],
        "frame_prompts": [{"start_frame_prompt_ru": "Бабушка у стола", "end_frame_prompt_ru": "пусто"}],
    }

    class _Message:
        text = "Добавить"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1045))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1045]["state"] == "state_waiting_frame_prompt_references"
    assert "Пришлите одно или несколько изображений" in captured[-1][0]

    images = [b"frame-ref-1", b"frame-ref-2"]

    async def _fake_extract(update, context):
        _ = (update, context)
        return images.pop(0)

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1045))
    asyncio.run(on_photo(photo_update, context=None))
    asyncio.run(on_photo(photo_update, context=None))

    assert SESSIONS[1045]["frame_prompt_reference_images"] == [b"frame-ref-1", b"frame-ref-2"]

    class _DoneMessage(_Message):
        text = "больше файлов не нужно"

    done_update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1045))
    asyncio.run(on_text(done_update, context=None))

    assert SESSIONS[1045]["state"] == "waiting_frame_prompts_approval"
    done_buttons = [btn.text for row in captured[-1][1].keyboard for btn in row]
    assert "ОК" in done_buttons
    assert "Добавить" in done_buttons
    assert "Добавь" in done_buttons
    assert "Исправь" in done_buttons

    async def _fake_call(_update, _context, _chat_id, _name, fn):
        return await fn()

    async def _fake_generate_frame_prompts_for_approval(
        scenes_plan,
        user_feedback=None,
        previous_frame_prompts=None,
        requested_end_frame_scenes=None,
        reference_images=None,
    ):
        seen["scenes_plan"] = scenes_plan
        seen["user_feedback"] = user_feedback
        seen["previous_frame_prompts"] = previous_frame_prompts
        seen["requested_end_frame_scenes"] = requested_end_frame_scenes
        seen["reference_images"] = reference_images
        return {
            "json_plan": [
                {"start_frame_prompt_ru": "Бабушка держит тарелку как на референсе", "end_frame_prompt_ru": "пусто"}
            ],
            "text_plan": "СЦЕНА 1",
        }

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.generate_frame_prompts_for_approval",
        _fake_generate_frame_prompts_for_approval,
    )

    class _FeedbackMessage(_Message):
        text = "учти прикрепленные изображения"

    feedback_update = SimpleNamespace(message=_FeedbackMessage(), effective_chat=SimpleNamespace(id=1045))
    asyncio.run(on_text(feedback_update, context=None))

    assert seen["reference_images"] == [b"frame-ref-1", b"frame-ref-2"]
    assert SESSIONS[1045]["state"] == "waiting_frame_prompts_approval"
    feedback_buttons = [btn.text for row in captured[-1][1].keyboard for btn in row]
    assert "Добавить" in feedback_buttons


def test_frame_prompts_ok_passes_scene1_end_frame_request_from_initial_description(monkeypatch):
    captured: dict[str, object] = {}
    replies: list[str] = []
    SESSIONS[1046] = {
        "state": "waiting_frame_prompts_approval",
        "user_input": "В первой сцене нужен конечный кадр, где клубника и банан уже в блендере.",
        "end_frame_scene_requests": [],
        "scenes_plan": [
            {"scene": 1, "text": "сцена 1", "model": "veo3_fast"},
            {"scene": 2, "text": "сцена 2", "model": "veo3_fast"},
        ],
        "frame_prompts": [
            {"start_frame_prompt_ru": "Старт 1", "end_frame_prompt_ru": "Конец 1"},
            {"start_frame_prompt_ru": "Старт 2", "end_frame_prompt_ru": "Конец 2"},
        ],
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, fn):
        assert name == "generate_full_production"
        return await fn()

    async def _fake_generate_full_production(
        scenes_plan,
        approved_frame_prompts=None,
        *,
        requested_end_frame_scenes=None,
        **_kwargs,
    ):
        captured["scenes_plan"] = scenes_plan
        captured["approved_frame_prompts"] = approved_frame_prompts
        captured["requested_end_frame_scenes"] = requested_end_frame_scenes
        return {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_prompt": "Старт 1",
                    "end_frame_prompt": "Конец 1",
                    "end_frame_requested": True,
                    "video_prompt": "v1",
                },
                {
                    "scene": 2,
                    "start_frame_prompt": "Старт 2",
                    "end_frame_prompt": "пусто",
                    "end_frame_requested": False,
                    "video_prompt": "v2",
                },
            ]
        }

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.generate_full_production",
        _fake_generate_full_production,
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1046))
    asyncio.run(on_text(update, context=None))

    assert captured["requested_end_frame_scenes"] == [1]
    assert SESSIONS[1046]["end_frame_scene_requests"] == [1]
    assert SESSIONS[1046]["production"]["json_plan"][0]["end_frame_requested"] is True
    assert SESSIONS[1046]["production"]["json_plan"][1]["end_frame_requested"] is False
    assert any("Ниже — все промпты" in text for text in replies)


def test_on_text_first_step_sound_text_generates_audio(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    SESSIONS[1015] = {
        "state": "state_waiting_first_step_sound_text",
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "Варим до мягкости."

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_audio(self, audio, caption):
            data = audio.getvalue() if hasattr(audio, "getvalue") else b""
            assert data == b"mp3"
            assert "Звук готов" in caption
            sent["ok"] = True

    class _Result:
        audio_bytes = b"mp3"

    def _fake_generate(text):
        assert text == "Варим до мягкости."
        return _Result()

    monkeypatch.setattr("app.handlers.handlers_state_scene.generate_elevenlabs_speech", _fake_generate)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1015))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "Генерируем звук" in captured[0][0]
    assert SESSIONS[1015]["state"] == ""
    assert SESSIONS[1015]["selected_project_key"] == "grandma_kitchen"


def test_on_text_project_image_adds_character_then_generates_with_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1013] = {
        "state": "state_waiting_first_step_project_image_characters",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "grandfather"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"project-img"
            assert "дедушка" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert "grandfather" in SESSIONS[1013]["first_step_project_image_characters"]
    assert "Добавила" in captured[-1][0]

    class _DoneMessage(_Message):
        text = "больше никто не нужен"

    update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1013]["state"] == "state_waiting_first_step_project_image_prompt"
    assert "Теперь пришлите prompt" in captured[-1][0]

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"ref1", b"ref2"],
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Крупный план на кухне, все смотрят на готовый суп"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1013))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "дедушка" in str(seen["prompt"])
    assert seen["reference_images"] == [b"ref1", b"ref2"]
    assert SESSIONS[1013]["state"] == ""


def test_on_text_project_image_accepts_custom_references(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent = {"ok": False}
    seen: dict[str, object] = {}
    SESSIONS[1017] = {
        "state": "state_waiting_first_step_project_image_characters",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "Добавить свое"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            data = photo.getvalue() if hasattr(photo, "getvalue") else b""
            assert data == b"project-img"
            assert "Изображение проекта готово" in caption
            sent["ok"] = True

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_references"
    assert "Пришлите одно или несколько" in captured[-1][0]

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"custom-project-ref"

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_photo(photo_update, context=None))
    assert SESSIONS[1017]["first_step_custom_reference_images"] == [b"custom-project-ref"]

    class _FilesDoneMessage(_Message):
        text = "больше файлов не нужно"

    update = SimpleNamespace(message=_FilesDoneMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_characters"

    class _CharactersDoneMessage(_Message):
        text = "больше никто не нужен"

    update = SimpleNamespace(message=_CharactersDoneMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_prompt"

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"project-ref"],
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    class _PromptMessage(_Message):
        text = "Бабушка и кот рядом с новым предметом из референса"

    update = SimpleNamespace(message=_PromptMessage(), effective_chat=SimpleNamespace(id=1017))
    asyncio.run(on_text(update, context=None))
    assert sent["ok"] is True
    assert "бабушка, рыжий кот" in str(seen["prompt"])
    assert seen["reference_images"] == [b"project-ref", b"custom-project-ref"]
    assert SESSIONS[1017]["state"] == ""


def test_generate_project_image_sends_prompt_and_character_references(monkeypatch):
    prompt = """Уютная мультяшная кухня, пиксаровский стиль референса, яркий цветной мультфильм, мягкий стилизованный свет, никакого реализма и фотореализма. Крупный план кухонного стола сбоку под углом сверху.

На переднем плане стоит глубокая красивая керамическая тарелка, уже наполненная густым сырным супом пюре нежного кремово желтого цвета. Поверхность супа гладкая, бархатная, густая, от супа поднимается мягкий пар. Суп уже полностью взбит в однородный крем.

Рядом с тарелкой стоит небольшая деревянная мисочка с золотистыми хрустящими сухариками. Рядом лежит маленькая щепотка свежей зелени для подачи. Возле тарелки лежит серебряная ложка. На заднем плане на столе стоит светлая кастрюля с остатками готового сырного супа пюре, рядом аккуратно лежит погружной блендер, чтобы было понятно, что суп уже взбит.

Мультяшная бабушка видна по пояс, стоит за столом, смотрит в камеру с теплой спокойной улыбкой. Одна рука бабушки находится рядом с мисочкой сухариков, как будто она сейчас добавит их в тарелку. Вторая рука мягко придерживает край стола рядом с тарелкой.

Рыжий мультяшный кот очень крупно сидит на столе рядом с мисочкой сухариков, на безопасном расстоянии от горячей тарелки. Кот смотрит на сухарики с любопытством и восторгом, глаза широко открыты, уши навострены, хвост поднят и слегка изогнут. Кот не облизывается.

На фоне видна чистая уютная мультяшная кухня: светлые шкафчики, теплая подсветка, аккуратная столешница, без беспорядка. Без надписей, без текста, без логотипов, без музыки, без визуальных эффектов, без реалистичной фактуры кожи и еды."""
    captured: dict[str, object] = {}
    replies: list[str] = []

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            captured["photo_bytes"] = photo.getvalue() if hasattr(photo, "getvalue") else b""
            captured["caption"] = caption

    async def _fake_generate(effective_prompt: str, reference_images=None):
        captured["prompt"] = effective_prompt
        captured["reference_images"] = reference_images
        return b"project-img"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_image_with_lenient_fallback",
        _fake_generate,
    )

    update = SimpleNamespace(message=_Message())
    asyncio.run(_generate_project_image(update, prompt, ["grandma", "cat"]))

    sent_prompt = str(captured["prompt"])
    refs = captured["reference_images"]
    assert prompt.splitlines()[0] in sent_prompt
    assert "Match art style" in sent_prompt
    assert "Персонажи в кадре: бабушка, рыжий кот" in sent_prompt
    assert "Локация кадра" not in sent_prompt
    assert isinstance(refs, list)
    assert len(refs) == 2
    assert all(len(r) > 0 for r in refs)
    assert captured["photo_bytes"] == b"project-img"
    assert any("Генерируем изображение проекта" in r for r in replies)


def test_on_text_first_step_video_text_switches_to_photo_step():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1006] = {"state": "state_waiting_first_step_video_text"}

    class _Message:
        text = "6 Крупный план бабушки с котом"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1006))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1006]["state"] == "state_waiting_first_step_video_image"
    assert SESSIONS[1006]["first_step_video_duration_sec"] in {6, 8}
    assert "крупный план" in SESSIONS[1006]["first_step_video_prompt"].lower()
    assert "Шаг 2/2" in captured[0][0]


def test_on_photo_first_step_video_image_generates_video(monkeypatch):
    captured_texts: list[str] = []
    sent = {"ok": False}
    SESSIONS[1007] = {
        "state": "state_waiting_first_step_video_image",
        "first_step_video_duration_sec": 6,
        "first_step_video_prompt": "Крупный план бабушки",
    }

    class _Message:
        photo = [object()]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured_texts.append(text)

        async def reply_video(self, video, caption):
            assert isinstance(video, io.BytesIO)
            assert video.getvalue() == b"mp4"
            assert "Видео готово" in caption
            sent["ok"] = True

    class _Context:
        bot = object()

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"img"

    async def _fake_generate_video(_update, **kwargs):
        _ = _update
        assert kwargs["duration_sec"] == 6
        assert kwargs["start_image"] == b"img"
        return b"mp4"

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._extract_video_source_image",
        _fake_extract,
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._generate_video_bytes_with_high_load_retry",
        _fake_generate_video,
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene.get_last_video_result_source", lambda: "bytes")

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1007))
    asyncio.run(on_photo(update, _Context()))
    assert sent["ok"] is True
    assert any("Генерируем видео" in t for t in captured_texts)
    assert SESSIONS[1007]["state"] == ""
    assert SESSIONS[1007]["selected_project_key"] == "grandma_kitchen"


def test_on_text_first_step_project_button_opens_project_choice():
    captured: list[tuple[str, object | None]] = []
    SESSIONS.pop(1010, None)

    class _Message:
        text = "Проект"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1010))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Выберите проект кнопкой" in captured[0][0]
    assert SESSIONS[1010]["state"] == "state_waiting_first_step_project_selection"
    assert SESSIONS[1010]["selected_project_key"] == "grandma_kitchen"


def test_on_text_project_selection_saves_project_and_returns_menu():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1011] = {
        "state": "state_waiting_first_step_project_selection",
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "Grandma Kitchen"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1011))
    asyncio.run(on_text(update, context=None))
    assert captured
    assert "Проект выбран" in captured[0][0]
    assert SESSIONS[1011]["state"] == ""
    assert SESSIONS[1011]["selected_project_key"] == "grandma_kitchen"


def test_on_text_navigation_start_over_resets_session():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1020] = {"state": "waiting_recipe_approval", "selected_project_key": "grandma_kitchen"}

    class _Message:
        text = NAV_BUTTON_START_OVER

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1020))
    asyncio.run(on_text(update, context=None))
    assert 1020 not in SESSIONS
    assert "Начинаем сначала" in captured[0][0]
    assert captured[0][1] is not None


@pytest.mark.parametrize(
    "draft_state",
    [
        "state_waiting_draft_scenes",
        "state_waiting_draft_language",
        "state_waiting_draft_stage",
    ],
)
def test_on_text_navigation_start_over_wins_over_draft_repair_states(draft_state):
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1022] = {
        "state": draft_state,
        "draft_repair": {
            "selected_scene_indexes": [0, 2],
            "pending_scene_queue": [0, 2],
            "current_scene_index": 0,
        },
        "production": {"json_plan": [{"scene": 1}, {"scene": 2}, {"scene": 3}]},
    }

    class _Message:
        text = NAV_BUTTON_START_OVER

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1022))
    asyncio.run(on_text(update, context=None))

    assert 1022 not in SESSIONS
    assert "Начинаем сначала" in captured[0][0]
    assert captured[0][1] is not None


def test_on_text_navigation_previous_moves_from_recipe_to_concept():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1021] = {
        "state": "waiting_recipe_approval",
        "concepts": [{"name": "сырники"}],
        "recipe_full_text": "текст",
        "scenes_plan": [{"scene": 1}],
        "production": {"json_plan": [{"scene": 1}]},
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1021))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1021]["state"] == "waiting_concept"
    assert SESSIONS[1021]["recipe_full_text"] is None
    assert SESSIONS[1021]["scenes_plan"] is None
    assert SESSIONS[1021]["production"] is None
    assert "Откатила шаг рецепта" in captured[0][0]


def test_on_text_navigation_previous_moves_from_media_video_to_start_image_approval(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    send_calls = {"count": 0}
    SESSIONS[1022] = {
        "state": "waiting_scene_media",
        "image_scene_index": 1,
        "scene_media_substep": "video_prompt",
        "production": {
            "json_plan": [
                {"scene": 1, "start_frame_image_b64": "x", "end_frame_image_b64": "y"},
                {"scene": 2, "start_frame_image_b64": "x2", "end_frame_prompt": "дельта конца"},
            ]
        },
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = (update, session)
        send_calls["count"] += 1

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1022))
    asyncio.run(on_text(update, context=None))
    sess = SESSIONS[1022]
    assert sess["state"] == "waiting_scene_media"
    assert sess["image_scene_index"] == 1
    assert sess["scene_media_substep"] == "start_image_approval"
    assert send_calls["count"] == 1
    assert "сцена 2/2" in captured[0][0].lower()
    assert "апрув стартового изображения" in captured[0][0]


def test_on_text_navigation_previous_moves_from_media_video_to_end_prompt_when_not_generated(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    send_calls = {"count": 0}
    SESSIONS[1023] = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {"json_plan": [{"scene": 1}]},
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = (update, session)
        send_calls["count"] += 1

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._scene_needs_end_frame_step",
        lambda _scene: True,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1023))
    asyncio.run(on_text(update, context=None))
    sess = SESSIONS[1023]
    assert sess["state"] == "waiting_scene_media"
    assert sess["scene_media_substep"] == "end_prompt"
    assert send_calls["count"] == 1
    assert "промпт конечного кадра" in captured[0][0]


def test_on_text_navigation_previous_moves_from_media_video_to_end_image_approval(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    send_calls = {"count": 0}
    SESSIONS[1025] = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_image_b64": "c3RhcnQ=",
                    "end_frame_prompt": "Финальный кадр",
                    "end_frame_requested": True,
                    "end_frame_image_b64": "ZW5k",
                }
            ]
        },
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = (update, session)
        send_calls["count"] += 1

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1025))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1025]["scene_media_substep"] == "end_image_approval"
    assert send_calls["count"] == 1
    assert "апрув конечного изображения" in captured[0][0]


def test_on_text_navigation_previous_moves_from_next_scene_start_to_previous_scene_end(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    sent_steps: list[tuple[int, str]] = []
    SESSIONS[1026] = {
        "state": "waiting_scene_media",
        "image_scene_index": 3,
        "scene_media_substep": "start_prompt",
        "scene_media_language_index": 0,
        "production": {
            "json_plan": [
                {"scene": 1, "video_prompt": "v1"},
                {"scene": 2, "video_prompt": "v2"},
                {
                    "scene": 3,
                    "start_frame_image_b64": "c3RhcnQ=",
                    "end_frame_prompt": "Финальный кадр",
                    "end_frame_requested": True,
                    "end_frame_image_b64": "ZW5k",
                    "video_prompt": "v3",
                    "video_results_by_lang": {"ru": {"video_b64": "djM="}},
                },
                {"scene": 4, "start_frame_prompt": "s4"},
            ]
        },
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    async def _fake_send_next_scene_media_step(update, session):
        _ = update
        sent_steps.append((session["image_scene_index"], session["scene_media_substep"]))

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1026))
    asyncio.run(on_text(update, context=None))

    sess = SESSIONS[1026]
    assert sess["state"] == "waiting_scene_media"
    assert sess["image_scene_index"] == 2
    assert sess["scene_media_substep"] == "video_prompt"
    assert sess["scene_media_language_index"] == 0
    assert sent_steps == [(2, "video_prompt")]
    assert "сцена 3/4" in captured[0][0].lower()
    assert "апрув video prompt" in captured[0][0]


def test_on_text_navigation_previous_clears_media_pending_state(monkeypatch):
    sent_steps: list[tuple[int, str]] = []
    SESSIONS[1027] = {
        "state": "waiting_scene_media",
        "image_scene_index": 0,
        "scene_media_substep": "video_prompt",
        "scene_prompt_input_pending": {"scene_idx": 0, "target": "video"},
        "scene_phrase_translation_pending": {"scene_idx": 0, "stage": "approval"},
        "production": {
            "json_plan": [
                {
                    "scene": 1,
                    "start_frame_image_b64": "c3RhcnQ=",
                    "video_provider_choice_pending": {"source": "scene", "language": "ru"},
                    "syntax_fallback_pending": {"source": "scene"},
                    "image_model_choice_pending": {"lower_model": "imagen-fast"},
                }
            ]
        },
    }

    class _Message:
        text = NAV_BUTTON_PREVIOUS_STEP

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_send_next_scene_media_step(update, session):
        _ = update
        sent_steps.append((session["image_scene_index"], session["scene_media_substep"]))

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._send_next_scene_media_step",
        _fake_send_next_scene_media_step,
    )
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1027))
    asyncio.run(on_text(update, context=None))

    scene = SESSIONS[1027]["production"]["json_plan"][0]
    assert "scene_prompt_input_pending" not in SESSIONS[1027]
    assert "scene_phrase_translation_pending" not in SESSIONS[1027]
    assert "video_provider_choice_pending" not in scene
    assert "syntax_fallback_pending" not in scene
    assert "image_model_choice_pending" not in scene
    assert sent_steps == [(0, "start_image_approval")]
