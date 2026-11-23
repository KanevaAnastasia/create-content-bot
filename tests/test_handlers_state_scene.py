"""Тесты вспомогательных функций сценарных хендлеров."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.models import ChatSession, GenerationArtifact
from app.handlers.handlers_state_scene import (
    _apply_archive_subject_overlay_titles,
    _apply_overlay_title_style_feedback,
    _apply_overlay_title_to_production,
    _clean_translated_title,
    _cumulative_scene_plan_feedback,
    _default_overlay_title_style,
    _ensure_end_frame_scene_requests_from_session_text,
    _handle_scene_approval_refine,
    _is_explicit_veo3_speech_format_request,
    _frame_prompts_slice_from_production,
    _scene_numbers_from_end_frame_feedback,
    _update_end_frame_scene_requests,
    _generate_project_image,
    _generate_and_send_story_audio,
    _generate_advertising_post,
    _handle_advertising_post_review,
    _attach_story_english_track_to_production,
    _attach_story_character_references,
    _handle_advertising_campaign_choice,
    _handle_advertising_format_choice,
    _handle_advertising_product_photo,
    _handle_draft_buttons,
    _handle_waiting_story_english_script_approval_state,
    _handle_waiting_story_script_approval_state,
    _handle_waiting_story_audio_approval_state,
    _handle_visualization_media_upload,
    _story_character_reference_prompt,
    _story_character_reference_is_approved,
    _story_primary_style_reference_bytes,
    keyboard_for_session_state,
    on_photo,
    on_text,
    _translate_overlay_title_to_en,
)
from app.handlers.handlers_media_core import ServiceUnavailableRetriesExhaustedError
from app.handlers.handlers_state_start import (
    SERIES_CHOICE_CONFIRMED_SESSION_KEY,
    SERIES_NEXT_OVERLAY_TITLE,
    SERIES_NEXT_ADVERTISING_PLAN,
    SERIES_PENDING_NEXT_SESSION_KEY,
    STATE_WAITING_GENERATION_MODE,
    STATE_WAITING_SERIES_CHOICE,
    STATE_WAITING_SERIES_CREATE,
    STATE_WAITING_SERIES_TRANSLATION_APPROVAL,
    generate,
    _handle_waiting_generation_mode_state,
    _handle_waiting_story_english_choice_state,
    _handle_waiting_story_style_choice_state,
    _parse_series_create_text,
    _story_style_choice_markup,
)
from app.handlers.handlers_generation_utils import (
    OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY,
    OVERLAY_SUBTITLE_DECIDED_SESSION_KEY,
    OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY,
    OVERLAY_TITLE_NEXT_RECIPE_PLAN,
    OVERLAY_TITLE_NEXT_SERIES_CHOICE,
    OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY,
)
from app.handlers.handlers_state_commands import NAV_BUTTON_PREVIOUS_STEP, NAV_BUTTON_START_OVER
from app.series_store import (
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_PENDING_PREFER_VERTEX_SESSION_KEY,
    SERIES_PENDING_RECIPE_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SeriesPayload,
)
from app.story_generation import (
    STATE_WAITING_STORY_CHARACTER_APPROVAL,
    STATE_WAITING_STORY_AUDIO_APPROVAL,
    STATE_WAITING_STORY_ENGLISH_CHOICE,
    STATE_WAITING_STORY_ENGLISH_SCRIPT_APPROVAL,
    STATE_WAITING_STORY_STYLE_CHOICE,
    STATE_WAITING_STORY_SCRIPT_APPROVAL,
    STORY_AUDIO_PATH_SESSION_KEY,
    STORY_AUDIO_VERSIONS_SESSION_KEY,
    STORY_CHARACTER_BIBLE_SESSION_KEY,
    STORY_CHARACTER_REFERENCES_SESSION_KEY,
    STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY,
    STORY_MAX_DURATION_SEC,
    STORY_SOURCE_SESSION_KEY,
    STORY_TITLE_EN_SESSION_KEY,
    STORY_TITLE_SESSION_KEY,
    STORY_VISUAL_STYLE_SESSION_KEY,
    STORY_VOICEOVER_SESSION_KEY,
    STORY_VOICEOVER_EN_SESSION_KEY,
    STORY_VOICEOVER_VERSIONS_SESSION_KEY,
    STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY,
    StoryAudioResult,
)
from app.advertising_generation import (
    ADVERTISING_CAMPAIGNS_SESSION_KEY,
    ADVERTISING_FORMAT_SESSION_KEY,
    ADVERTISING_POST_CURRENT_IMAGE_PATH_SESSION_KEY,
    ADVERTISING_POST_IMAGE_INDEX_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY,
    ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY,
    ADVERTISING_POST_TEXT_SESSION_KEY,
    ADVERTISING_PRODUCT_BRIEF_SESSION_KEY,
    ADVERTISING_PRODUCT_PATH_SESSION_KEY,
    ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY,
    ADVERTISING_SOURCE_SESSION_KEY,
    STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
    STATE_WAITING_ADVERTISING_FORMAT_CHOICE,
    STATE_WAITING_ADVERTISING_POST_IMAGE_APPROVAL,
    STATE_WAITING_ADVERTISING_POST_IMAGE_FEEDBACK,
    STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL,
    STATE_WAITING_ADVERTISING_POST_TEXT_FEEDBACK,
    STATE_WAITING_ADVERTISING_PRODUCT_IMAGE,
)
from app.state import SESSIONS
from app.visualization_generation import PreparedVisualizationMedia, VisualizationTranscript


def test_archive_overlay_uses_series_subject_and_story_counter_hierarchy():
    session = {
        "selected_project_key": "discipline",
        "discipline_narrative_mode": "lives_archive",
        "series_title_by_lang": {
            "ru": "Истории несовершенных ошибок",
            "en": "Stories of Imperfect Mistakes",
        },
        "series_counter_by_lang": {"ru": "История 1", "en": "Story 1"},
        STORY_CHARACTER_BIBLE_SESSION_KEY: [
            {
                "id": "stephen_king_1973",
                "display_name": "Стивен Кинг",
                "same_person_group": "stephen_king",
                "is_primary": True,
            }
        ],
    }
    titles = {"ru": "Кто достал «Кэрри» из корзины", "en": "Who Took Carrie Out?"}

    applied = _apply_archive_subject_overlay_titles(session, titles, english_requested=True)

    assert applied is True
    assert session["series_title_by_lang"]["ru"] == "Истории несовершенных ошибок"
    assert titles == {"ru": "Стивен Кинг", "en": "Stephen King"}
    assert session["series_counter_by_lang"] == {"ru": "История 1", "en": "Story 1"}
    assert session["overlay_title"] == "Стивен Кинг"


@pytest.fixture(autouse=True)
def _isolate_persistent_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db.create_tables(engine=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    yield
    engine.dispose()


def test_story_style_choice_keyboard_starts_with_primary_then_named_styles():
    markup = _story_style_choice_markup()
    labels = [button["text"] for row in markup.to_dict()["keyboard"] for button in row]

    assert labels[:5] == [
        "1. Сухая акварель", "2. Ретро-мультфильм", "3. Книжная гуашь",
        "4. Пластилиновый мультфильм", "5. Кукольный стоп-моушен",
    ]
    assert "Дальше →" not in labels
    assert "14. Минималистичный ретро-мультфильм" in labels
    assert "Основной" in labels


def test_primary_story_character_reference_prompt_has_no_separate_style_instruction():
    prompt = _story_character_reference_prompt(
        {
            "id": "hero",
            "same_person_group": "hero",
            "age_stage": "adult",
            "description": "утверждённая внешность",
            "reference_prompt": "сохранить черты лица",
        },
        story_visual_style="primary",
    )

    assert "Строго заданный стиль" not in prompt
    assert "акварель" not in prompt.lower()


def test_primary_story_character_generation_uses_primary_project_reference_for_style(tmp_path):
    reference = tmp_path / "knopka.jpg"
    reference.write_bytes(b"knopka-style-reference")
    session = {
        "story_character_bible": [
            {"id": "discipline_knopka", "is_primary": True},
            {"id": "new_character", "is_primary": False},
        ],
        "story_character_references": {
            "discipline_knopka": {
                "selected_path": str(reference),
                "approved": True,
            }
        },
    }

    payloads = _story_primary_style_reference_bytes(session, {"id": "new_character"})
    prompt = _story_character_reference_prompt(
        {
            "id": "new_character",
            "same_person_group": "new_character",
            "age_stage": "adult",
            "description": "новый человек в деловой одежде",
            "reference_prompt": "сохранить заданную внешность",
        },
        story_visual_style="primary",
        has_project_style_reference=True,
    )

    assert payloads == [b"knopka-style-reference"]
    assert "ТОЛЬКО как референс художественной стилистики" in prompt
    assert "Не копируй его лицо, вид, анатомию" in prompt


def test_story_character_approval_is_invalid_after_style_change(tmp_path):
    reference = tmp_path / "tourist.png"
    reference.write_bytes(b"old-style")
    character = {"id": "tourist", "description": "молодой турист"}
    session = {
        "story_visual_style": "crochet",
        "story_character_references": {
            "tourist": {
                "selected_path": str(reference),
                "approved": True,
                "description": "молодой турист",
                "story_visual_style_key": "folk_fairytale",
            }
        },
    }

    assert _story_character_reference_is_approved(session, character) is False

    session["story_character_references"]["tourist"]["story_visual_style_key"] = "crochet"
    assert _story_character_reference_is_approved(session, character) is True


@pytest.mark.parametrize(
    ("label", "expected_key"),
    [
        ("Основной", "primary"),
        ("Сумеречная акварель", "twilight_watercolor"),
        ("Светлая анимационная пастель", "soft_animation"),
        ("Классический рисованный мультфильм", "classic_cel_cartoon"),
        ("Фетровая аппликация", "felt_applique"),
        ("Народная рисованная сказка", "folk_fairytale"),
        ("Пастельные мелки", "oil_pastel"),
        ("Вязаная иллюстрация", "crochet"),
        ("Минималистичный ретро-мультфильм", "minimal_retro_cartoon"),
    ],
)
def test_story_style_choice_accepts_each_new_style(monkeypatch, label, expected_key):
    replies: list[str] = []
    session = {"state": STATE_WAITING_STORY_STYLE_CHOICE}

    async def fake_start(_update, _context, *, chat_id, session):
        assert chat_id == 4701
        return True

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            replies.append(text)

    monkeypatch.setattr("app.handlers.handlers_state_start._start_initial_story_voiceover", fake_start)
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=4701))

    result = asyncio.run(
        _handle_waiting_story_style_choice_state(
            update,
            context=None,
            chat_id=4701,
            session=session,
            text=label,
        )
    )

    assert result is True
    assert session[STORY_VISUAL_STYLE_SESSION_KEY] == expected_key
    assert any(label in reply for reply in replies)


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


def test_cumulative_advice_scene_feedback_preserves_earlier_corrections():
    session = {}

    first = _cumulative_scene_plan_feedback(
        session,
        "Исправь: Тут непонятно, сколько соли надо для картошки",
    )
    second = _cumulative_scene_plan_feedback(
        session,
        "Добавь: Соль чувствуется не сразу, а суп перенеси во вторую сцену",
    )

    assert "сколько соли надо для картошки" in first
    assert "1. Тут непонятно, сколько соли надо для картошки" in second
    assert "2. Соль чувствуется не сразу" in second
    assert session["scene_plan_feedback_history"] == [
        "Тут непонятно, сколько соли надо для картошки",
        "Соль чувствуется не сразу, а суп перенеси во вторую сцену",
    ]


def test_advice_scene_refinement_sends_cumulative_feedback_to_model(monkeypatch):
    captured_feedback: list[str] = []
    session = {
        "state": "waiting_scene_approval",
        "generation_mode": "advice",
        "selected_concept": "Как солить разные блюда",
        "recipe_full_text": "Суп, картошка, макароны и мясо.",
        "scenes_plan": [{"scene": 1, "text": "Старый план", "model": "veo3_fast"}],
        "viral_concept_brief": {},
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

    async def _fake_generate(*_args, **kwargs):
        captured_feedback.append(kwargs["user_feedback"])
        return {
            "json_plan": [{"scene": 1, "text": "Новый план", "model": "veo3_fast"}],
            "text_plan": "СЦЕНА 1",
            "viral_concept_brief": {},
            "romantic_drama_subplot": {},
            "retention_review": {},
            "regeneration_events": [],
        }

    async def _fake_call(_update, _context, _chat_id, _name, fn):
        return await fn()

    monkeypatch.setattr("app.handlers.handlers_state_scene.generate_scenes_plan", _fake_generate)
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    update = SimpleNamespace(message=_Message())

    asyncio.run(_handle_scene_approval_refine(
        update,
        context=None,
        chat_id=1031,
        session=session,
        text="Исправь: укажи количество соли для картошки",
    ))
    asyncio.run(_handle_scene_approval_refine(
        update,
        context=None,
        chat_id=1031,
        session=session,
        text="Добавь: скажи, что соль чувствуется не сразу",
    ))

    assert "количество соли для картошки" in captured_feedback[0]
    assert "1. укажи количество соли для картошки" in captured_feedback[1]
    assert "2. скажи, что соль чувствуется не сразу" in captured_feedback[1]


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


def test_start_generation_with_recipe_asks_for_generation_mode_then_series(monkeypatch):
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

    assert SESSIONS[1060]["state"] == STATE_WAITING_GENERATION_MODE
    assert SESSIONS[1060][SERIES_PENDING_RECIPE_SESSION_KEY].startswith("Клубника")
    assert any("Выберите режим генерации" in text for text, _markup in captured)

    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1060,
            session=SESSIONS[1060],
            text="Обычный",
        )
    )

    assert SESSIONS[1060]["state"] == STATE_WAITING_SERIES_CHOICE
    assert any("Можно привязать" in text for text, _markup in captured)
    assert any(markup is not None for _text, markup in captured)


def test_start_generation_with_recipe_drops_previous_run_and_content():
    from app.handlers.handlers_state_start import _start_generation_with_recipe

    chat_id = 1061
    SESSIONS[chat_id] = {
        "state": "final_videos_delivered",
        "generation_run_id": "11111111-1111-4111-8111-111111111111",
        "scene_db_ids_by_index": {0: "old-scene"},
        "production": {"json_plan": [{"text": "old"}]},
        "scenes_plan": [{"text": "old"}],
        "overlay_title_by_lang": {"ru": "Старый заголовок"},
        "story_title": "Старая история",
        "series": {"id": "old-series"},
        "selected_project_key": "grandma-kitchen",
    }

    class _Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(
        _start_generation_with_recipe(
            update,
            context=None,
            recipe="Новый рецепт",
            prefer_vertex_api=False,
        )
    )

    session = SESSIONS[chat_id]
    assert "generation_run_id" not in session
    assert "scene_db_ids_by_index" not in session
    assert "production" not in session
    assert "scenes_plan" not in session
    assert "overlay_title_by_lang" not in session
    assert "story_title" not in session
    assert "series" not in session
    assert session["selected_project_key"] == "grandma-kitchen"
    assert session[SERIES_PENDING_RECIPE_SESSION_KEY] == "Новый рецепт"


def test_silence_generation_mode_asks_for_title_before_series(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1066] = {
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Клубника 350 г. Взбить с молоком.",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }
    monkeypatch.setattr(
        "app.handlers.handlers_state_start.list_active_series",
        lambda _project_slug: pytest.fail("series choice should not be requested before title"),
    )

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1066))
    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1066,
            session=SESSIONS[1066],
            text="Silence",
        )
    )

    assert SESSIONS[1066]["state"] == "state_waiting_overlay_title_ru"
    assert SESSIONS[1066][OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] == OVERLAY_TITLE_NEXT_SERIES_CHOICE
    assert SESSIONS[1066]["target_languages"] == ["ru"]
    assert any("русский заголовок" in text for text, _markup in captured)
    assert not any("Можно привязать" in text for text, _markup in captured)


def test_visualization_generation_mode_waits_for_audio_or_video():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1068] = {
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Исходный текст не используется для расшифровки.",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1068))
    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1068,
            session=SESSIONS[1068],
            text="Визуализация",
        )
    )

    session = SESSIONS[1068]
    assert session["state"] == "waiting_visualization_media"
    assert session["generation_mode"] == "visualization"
    assert session["target_languages"] == ["ru"]
    assert SERIES_PENDING_RECIPE_SESSION_KEY not in session
    assert any("аудиофайл" in text for text, _markup in captured)


def test_visualization_style_choice_builds_scenes_without_generating_tts(monkeypatch):
    captured: list[str] = []
    calls: list[str] = []
    session = {
        "state": STATE_WAITING_STORY_STYLE_CHOICE,
        "generation_mode": "visualization",
        STORY_SOURCE_SESSION_KEY: "Готовая расшифровка.",
        STORY_VOICEOVER_SESSION_KEY: "Готовая расшифровка.",
    }
    SESSIONS[1069] = session

    async def fake_build(_update, _context, *, chat_id, session):
        assert chat_id == 1069
        calls.append(session[STORY_VISUAL_STYLE_SESSION_KEY])
        return True

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._approve_story_audio_and_build_scenes",
        fake_build,
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_start.generate_story_voiceover",
        lambda *_args, **_kwargs: pytest.fail("visualization must not generate a new voiceover"),
    )

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1069))
    result = asyncio.run(
        _handle_waiting_story_style_choice_state(
            update,
            context=None,
            chat_id=1069,
            session=session,
            text="Книжная гуашь",
        )
    )

    assert result is True
    assert calls == ["storybook_gouache"]
    assert any("Книжная гуашь" in text for text in captured)


def test_visualization_upload_saves_source_track_transcript_and_timing(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    session = {
        "state": "waiting_visualization_media",
        "generation_mode": "visualization",
        "selected_project_key": "discipline",
    }
    SESSIONS[1071] = session
    alignment = {
        "characters": list("Точный текст."),
        "character_start_times_seconds": [0.0] * len("Точный текст."),
        "character_end_times_seconds": [2.0] * len("Точный текст."),
    }
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.prepare_visualization_media",
        lambda *_args, **_kwargs: PreparedVisualizationMedia(
            audio_bytes=b"mp3",
            audio_path="/tmp/source-track.mp3",
            duration_sec=2.0,
            analysis_video_bytes=b"proxy",
        ),
    )
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.transcribe_visualization_media",
        lambda *_args, **_kwargs: VisualizationTranscript(
            title="Тема",
            text="Точный текст.",
            segments=[{"start_sec": 0.0, "end_sec": 2.0, "text": "Точный текст."}],
            alignment=alignment,
        ),
    )

    async def fake_call(_update, _context, _chat_id, _name, fn):
        return await fn()

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", fake_call)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1071))
    result = asyncio.run(
        _handle_visualization_media_upload(
            update,
            context=None,
            chat_id=1071,
            session=session,
            media_bytes=b"source",
            input_kind="video",
            media_meta={"file_name": "source.mp4", "mime_type": "video/mp4"},
        )
    )

    assert result is True
    assert session["state"] == STATE_WAITING_STORY_STYLE_CHOICE
    assert session[STORY_VOICEOVER_SESSION_KEY] == "Точный текст."
    assert session["story_audio_path"] == "/tmp/source-track.mp3"
    assert session["story_audio_alignment"] == alignment
    assert session["visualization_input_kind"] == "video"
    assert any("РАСШИФРОВКА" in text for text, _markup in captured)
    assert captured[-1][1] is not None


def test_story_generation_mode_creates_approvable_voiceover_before_audio(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1067] = {
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "История о старом доме и семейной жемчужине.",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }
    monkeypatch.setattr(
        "app.handlers.handlers_state_start.generate_story_voiceover",
        lambda source: {"title": "Цена жемчуга", "voiceover_text": f"Когда-то всё началось так: {source}"},
    )

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1067))
    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1067,
            session=SESSIONS[1067],
            text="История",
        )
    )

    assert SESSIONS[1067]["state"] == STATE_WAITING_STORY_ENGLISH_CHOICE
    assert any("Нужна английская версия" in text for text, _markup in captured)
    asyncio.run(
        _handle_waiting_story_english_choice_state(
            update,
            context=None,
            chat_id=1067,
            session=SESSIONS[1067],
            text="Нет, только RU",
        )
    )

    assert SESSIONS[1067]["state"] == STATE_WAITING_STORY_STYLE_CHOICE
    asyncio.run(
        _handle_waiting_story_style_choice_state(
            update,
            context=None,
            chat_id=1067,
            session=SESSIONS[1067],
            text="Ретро-мультфильм",
        )
    )

    session = SESSIONS[1067]
    assert session["state"] == STATE_WAITING_STORY_SCRIPT_APPROVAL
    assert session[STORY_SOURCE_SESSION_KEY].startswith("История о старом доме")
    assert session[STORY_VOICEOVER_SESSION_KEY].startswith("Когда-то всё началось")
    assert session[STORY_VOICEOVER_VERSIONS_SESSION_KEY][0]["voiceover_text"].startswith("Когда-то всё началось")
    assert session[STORY_VISUAL_STYLE_SESSION_KEY] == "retro_cartoon"
    assert session["target_languages"] == ["ru"]
    assert any("ТЕКСТ RU-ОЗВУЧКИ" in text for text, _markup in captured)


def test_story_english_track_adds_metadata_without_creating_another_visual_prompt():
    production = {
        "json_plan": [
            {
                "generation_mode": "story",
                "duration_sec": 3.0,
                "timeline_duration_sec": 3.0,
                "video_prompt": "one silent Kling prompt",
                "video_prompt_by_lang": {"ru": "one silent Kling prompt"},
                "speech_by_lang": {"ru": "Русская фраза"},
                "speech_cues_by_lang": {},
            }
        ]
    }
    session = {
        "story_english_requested": True,
        "story_voiceover_text_en": "The English line.",
        "story_audio_duration_sec_en": 3.0,
        "story_audio_alignment_en": {},
        "story_visual_duration_sec": 3.0,
    }

    _attach_story_english_track_to_production(production, session)

    scene = production["json_plan"][0]
    assert scene["video_prompt_by_lang"]["en"] == "one silent Kling prompt"
    assert scene["speech_by_lang"]["en"] == "The English line"
    assert scene["tts"]["text_en"] == "The English line"
    assert scene["timeline_duration_by_lang"] == {"ru": 3.0, "en": 3.0}


def test_story_voiceover_failure_keeps_source_and_keyboard_can_regenerate(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    calls = 0
    SESSIONS[1069] = {
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "История происхождения шарлотки.",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }

    def fake_generate(source):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("Gemini вернул слишком длинный текст озвучки")
        return {"title": "Шарлотка", "voiceover_text": f"Короткая версия: {source}"}

    monkeypatch.setattr("app.handlers.handlers_state_start.generate_story_voiceover", fake_generate)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1069))
    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="История",
        )
    )
    asyncio.run(
        _handle_waiting_story_english_choice_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="Нет, только RU",
        )
    )
    asyncio.run(
        _handle_waiting_story_style_choice_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="Сухая акварель",
        )
    )

    failed_session = SESSIONS[1069]
    assert failed_session["state"] == STATE_WAITING_GENERATION_MODE
    assert failed_session[SERIES_PENDING_RECIPE_SESSION_KEY] == "История происхождения шарлотки."
    assert failed_session[STORY_SOURCE_SESSION_KEY] == "История происхождения шарлотки."
    error_text, error_markup = captured[-1]
    assert "Исходная история сохранена" in error_text
    assert error_markup is not None
    assert "ПЕРЕГЕНЕРИРОВАТЬ" in str(error_markup.to_dict())

    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="ПЕРЕГЕНЕРИРОВАТЬ",
        )
    )
    asyncio.run(
        _handle_waiting_story_english_choice_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="Нет, только RU",
        )
    )
    asyncio.run(
        _handle_waiting_story_style_choice_state(
            update,
            context=None,
            chat_id=1069,
            session=SESSIONS[1069],
            text="Сухая акварель",
        )
    )

    regenerated_session = SESSIONS[1069]
    assert calls == 2
    assert regenerated_session["state"] == STATE_WAITING_STORY_SCRIPT_APPROVAL
    assert regenerated_session[STORY_VOICEOVER_SESSION_KEY].startswith("Короткая версия")
    assert regenerated_session[STORY_VOICEOVER_VERSIONS_SESSION_KEY][0]["voiceover_text"].startswith("Короткая версия")
    assert SERIES_PENDING_RECIPE_SESSION_KEY not in regenerated_session


def test_paid_story_audio_over_limit_is_saved_and_sent_without_auto_refine(monkeypatch):
    messages: list[str] = []
    audio_captions: list[str] = []
    stored: list[bytes] = []
    session = {
        "state": STATE_WAITING_STORY_SCRIPT_APPROVAL,
        STORY_VOICEOVER_SESSION_KEY: "Утверждённый текст истории.",
    }

    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.generate_story_audio",
        lambda _text: StoryAudioResult(
            audio_bytes=b"paid-elevenlabs-audio",
            content_type="audio/mpeg",
            duration_sec=STORY_MAX_DURATION_SEC + 0.1,
            alignment={"characters": ["У"]},
            request_id="request-1",
            history_item_id="history-1",
        ),
    )

    def fake_store(_chat_id, result):
        stored.append(result.audio_bytes)
        return "/tmp/story-over-limit.mp3"

    monkeypatch.setattr("app.handlers.handlers_state_scene.store_story_audio", fake_store)

    async def forbid_auto_refine(*_args, **_kwargs):
        pytest.fail("paid audio must never trigger automatic text refinement")

    monkeypatch.setattr("app.handlers.handlers_state_scene._refine_story_voiceover", forbid_auto_refine)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            messages.append(text)

        async def reply_audio(self, audio, caption=None):
            assert audio.read() == b"paid-elevenlabs-audio"
            audio_captions.append(str(caption or ""))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1070))
    asyncio.run(
        _generate_and_send_story_audio(
            update,
            context=None,
            chat_id=1070,
            session=session,
        )
    )

    assert stored == [b"paid-elevenlabs-audio"]
    assert session["state"] == STATE_WAITING_STORY_AUDIO_APPROVAL
    assert session[STORY_AUDIO_PATH_SESSION_KEY] == "/tmp/story-over-limit.mp3"
    assert session[STORY_AUDIO_VERSIONS_SESSION_KEY][0]["duration_sec"] == STORY_MAX_DURATION_SEC + 0.1
    assert session[STORY_AUDIO_VERSIONS_SESSION_KEY][0]["history_item_id"] == "history-1"
    assert any("Ничего не переделываю" in caption for caption in audio_captions)
    assert any("Сократить текст" in text for text in messages)


def test_story_audio_shorten_button_changes_only_text(monkeypatch):
    captured: list[str] = []
    session = {
        "state": STATE_WAITING_STORY_AUDIO_APPROVAL,
        "story_audio_duration_sec": 60.1,
    }

    async def fake_refine(_update, _context, **kwargs):
        captured.append(kwargs["feedback"])
        return True

    async def forbid_new_audio(*_args, **_kwargs):
        pytest.fail("shorten text must not create another ElevenLabs generation")

    monkeypatch.setattr("app.handlers.handlers_state_scene._refine_story_voiceover", fake_refine)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_and_send_story_audio", forbid_new_audio)
    update = SimpleNamespace(message=SimpleNamespace(), effective_chat=SimpleNamespace(id=1071))

    asyncio.run(
        _handle_waiting_story_audio_approval_state(
            update,
            context=None,
            chat_id=1071,
            session=session,
            text="Сократить текст",
        )
    )

    assert captured
    assert "новую озвучку не создавай" in captured[0]


def test_approved_ru_story_audio_starts_english_text_before_scene_plan(monkeypatch):
    captured: list[str] = []
    session = {
        "state": STATE_WAITING_STORY_AUDIO_APPROVAL,
        "story_english_requested": True,
    }

    async def fake_prepare(_update, _context, **_kwargs):
        captured.append("english")
        return True

    async def forbid_scene_plan(*_args, **_kwargs):
        pytest.fail("scene plan must wait until EN text and EN audio are approved")

    monkeypatch.setattr("app.handlers.handlers_state_scene._prepare_story_english_voiceover", fake_prepare)
    monkeypatch.setattr("app.handlers.handlers_state_scene._approve_story_audio_and_build_scenes", forbid_scene_plan)
    update = SimpleNamespace(message=SimpleNamespace(), effective_chat=SimpleNamespace(id=10715))

    asyncio.run(
        _handle_waiting_story_audio_approval_state(
            update,
            context=None,
            chat_id=10715,
            session=session,
            text="ОК",
        )
    )

    assert captured == ["english"]


def test_story_text_versions_can_be_listed_and_restored_without_generation(monkeypatch):
    messages: list[str] = []
    session = {
        "state": STATE_WAITING_STORY_SCRIPT_APPROVAL,
        STORY_VOICEOVER_SESSION_KEY: "Текущая версия.",
        STORY_VOICEOVER_VERSIONS_SESSION_KEY: [
            {"title": "Первая", "voiceover_text": "Первый сохранённый текст."},
            {"title": "Вторая", "voiceover_text": "Второй сохранённый текст."},
        ],
    }

    async def forbid_new_audio(*_args, **_kwargs):
        pytest.fail("listing or restoring text must not generate audio")

    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_and_send_story_audio", forbid_new_audio)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            messages.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1072))
    asyncio.run(
        _handle_waiting_story_script_approval_state(
            update,
            context=None,
            chat_id=1072,
            session=session,
            text="Версии текста",
        )
    )
    assert any("ВЕРСИЯ 1" in text and "Первый сохранённый текст" in text for text in messages)

    asyncio.run(
        _handle_waiting_story_script_approval_state(
            update,
            context=None,
            chat_id=1072,
            session=session,
            text="Версия 1",
        )
    )
    assert session[STORY_VOICEOVER_SESSION_KEY] == "Первый сохранённый текст."


def test_story_custom_russian_script_is_saved_verbatim_without_gemini_or_audio(monkeypatch):
    messages: list[tuple[str, object | None]] = []
    session = {
        "state": STATE_WAITING_STORY_SCRIPT_APPROVAL,
        STORY_TITLE_SESSION_KEY: "Шарлотка",
        STORY_VOICEOVER_SESSION_KEY: "Текст, который предложил бот.",
        STORY_VOICEOVER_VERSIONS_SESSION_KEY: [
            {"title": "Шарлотка", "voiceover_text": "Текст, который предложил бот."},
        ],
    }

    async def forbid_gemini(*_args, **_kwargs):
        pytest.fail("custom script must not be sent to Gemini")

    async def forbid_audio(*_args, **_kwargs):
        pytest.fail("custom script must be approved before paid audio generation")

    monkeypatch.setattr("app.handlers.handlers_state_scene._refine_story_voiceover", forbid_gemini)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_and_send_story_audio", forbid_audio)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            messages.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=10721))
    asyncio.run(
        _handle_waiting_story_script_approval_state(
            update,
            context=None,
            chat_id=10721,
            session=session,
            text="СВОЙ ТЕКСТ",
        )
    )

    assert session[STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY] == {"language": "ru"}
    assert any("дословно" in text and "Gemini" in text for text, _markup in messages)

    custom_text = "А вы знаете, откуда появилась шарлотка?\nВот моя версия этой истории."
    asyncio.run(
        _handle_waiting_story_script_approval_state(
            update,
            context=None,
            chat_id=10721,
            session=session,
            text=custom_text,
        )
    )

    assert STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY not in session
    assert session[STORY_VOICEOVER_SESSION_KEY] == custom_text
    assert len(session[STORY_VOICEOVER_VERSIONS_SESSION_KEY]) == 2
    assert session[STORY_VOICEOVER_VERSIONS_SESSION_KEY][0]["voiceover_text"] == "Текст, который предложил бот."
    saved = session[STORY_VOICEOVER_VERSIONS_SESSION_KEY][1]
    assert saved["stage"] == "user_supplied"
    assert saved["voiceover_text"] == custom_text
    assert any(custom_text in text for text, _markup in messages)


def test_story_custom_english_script_is_saved_verbatim_without_gemini_or_audio(monkeypatch):
    messages: list[str] = []
    session = {
        "state": STATE_WAITING_STORY_ENGLISH_SCRIPT_APPROVAL,
        STORY_TITLE_EN_SESSION_KEY: "Charlotte",
        STORY_VOICEOVER_EN_SESSION_KEY: "The bot's proposed version.",
        STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY: [
            {"title": "Charlotte", "voiceover_text": "The bot's proposed version."},
        ],
    }

    async def forbid_gemini(*_args, **_kwargs):
        pytest.fail("custom English script must not be sent to Gemini")

    async def forbid_audio(*_args, **_kwargs):
        pytest.fail("custom English script must be approved before paid audio generation")

    monkeypatch.setattr("app.handlers.handlers_state_scene._prepare_story_english_voiceover", forbid_gemini)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_and_send_story_english_audio", forbid_audio)

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            messages.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=10722))
    asyncio.run(
        _handle_waiting_story_english_script_approval_state(
            update,
            context=None,
            chat_id=10722,
            session=session,
            text="СВОЙ ТЕКСТ",
        )
    )

    assert session[STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY] == {"language": "en"}

    custom_text = "Do you know where apple charlotte came from?\nHere is my version of the story."
    asyncio.run(
        _handle_waiting_story_english_script_approval_state(
            update,
            context=None,
            chat_id=10722,
            session=session,
            text=custom_text,
        )
    )

    assert STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY not in session
    assert session[STORY_VOICEOVER_EN_SESSION_KEY] == custom_text
    assert len(session[STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY]) == 2
    saved = session[STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY][1]
    assert saved["stage"] == "user_supplied"
    assert saved["voiceover_text"] == custom_text
    assert any(custom_text in text for text in messages)


def test_story_audio_variants_can_be_replayed_and_selected_without_generation(monkeypatch, tmp_path):
    first_path = tmp_path / "first.mp3"
    second_path = tmp_path / "second.mp3"
    first_path.write_bytes(b"first-audio")
    second_path.write_bytes(b"second-audio")
    sent: list[tuple[bytes, str]] = []
    session = {
        "state": STATE_WAITING_STORY_AUDIO_APPROVAL,
        "story_audio_versions": [
            {
                "path": str(first_path),
                "duration_sec": 50.0,
                "model_id": "eleven_v4",
                "alignment": {"characters": ["1"]},
            },
            {
                "path": str(second_path),
                "duration_sec": 51.0,
                "model_id": "eleven_v4",
                "alignment": {"characters": ["2"]},
            },
        ],
    }

    async def forbid_new_audio(*_args, **_kwargs):
        pytest.fail("replaying or selecting a saved variant must not generate audio")

    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_and_send_story_audio", forbid_new_audio)

    class _Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

        async def reply_audio(self, audio, caption=None):
            sent.append((audio.read(), str(caption or "")))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1073))
    asyncio.run(
        _handle_waiting_story_audio_approval_state(
            update,
            context=None,
            chat_id=1073,
            session=session,
            text="Варианты аудио",
        )
    )
    assert [payload for payload, _caption in sent] == [b"first-audio", b"second-audio"]

    asyncio.run(
        _handle_waiting_story_audio_approval_state(
            update,
            context=None,
            chat_id=1073,
            session=session,
            text="Аудио 1",
        )
    )
    assert session["story_audio_path"] == str(first_path)
    assert session["story_audio_alignment"] == {"characters": ["1"]}


def test_approved_story_character_reference_is_attached_only_to_matching_scenes(tmp_path):
    antonin_ref = tmp_path / "antonin-young.png"
    antonin_ref.write_bytes(b"approved-character")
    session = {
        "state": STATE_WAITING_STORY_CHARACTER_APPROVAL,
        STORY_CHARACTER_BIBLE_SESSION_KEY: [
            {
                "id": "antonin_young",
                "description": "Юный Антонен, тёмные вьющиеся волосы и карие глаза.",
                "requires_reference": True,
            }
        ],
        STORY_CHARACTER_REFERENCES_SESSION_KEY: {
            "antonin_young": {
                "selected_path": str(antonin_ref),
                "approved": True,
                "description": "Юный Антонен, тёмные вьющиеся волосы и карие глаза.",
            }
        },
        "scenes_plan": [
            {"characters": ["antonin_young", "cat"]},
            {"characters": ["grandma", "cat"]},
        ],
    }

    _attach_story_character_references(session)

    assert session["scenes_plan"][0]["references"]["antonin_young"] == str(antonin_ref)
    assert "antonin_young" not in session["scenes_plan"][1]["references"]


def test_advertising_generation_mode_requires_product_photo_before_planning():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1068] = {
        "state": STATE_WAITING_GENERATION_MODE,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Покажи, как почистить белые кроссовки.",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1068))
    asyncio.run(
        _handle_waiting_generation_mode_state(
            update,
            context=None,
            chat_id=1068,
            session=SESSIONS[1068],
            text="Реклама",
        )
    )

    session = SESSIONS[1068]
    assert session["state"] == STATE_WAITING_ADVERTISING_PRODUCT_IMAGE
    assert session["target_languages"] == ["ru"]
    assert session[ADVERTISING_SOURCE_SESSION_KEY].startswith("Покажи, как почистить")
    assert session["sponsored_content"] is True
    assert any("фотографию рекламируемого товара" in text for text, _markup in captured)


def test_advertising_product_photo_is_saved_before_campaign_choice(monkeypatch):
    captured: list[str] = []
    campaign_started: list[bool] = []
    session = {
        "state": STATE_WAITING_ADVERTISING_PRODUCT_IMAGE,
        ADVERTISING_SOURCE_SESSION_KEY: "Как почистить белые кроссовки",
    }

    class _Message:
        caption = "Пена CleanStep"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    async def fake_extract(_update, _context):
        return b"product-image"

    async def fake_call(_update, _context, _chat_id, _name, _op, **_kwargs):
        return {"product_name": "CleanStep", "short_product_lock": "белый флакон"}

    async def fake_start_campaign(_update, _context, *, chat_id, session, direction="new"):
        _ = (chat_id, direction)
        campaign_started.append(True)
        session["state"] = STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE
        return True

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.store_advertising_product_reference",
        lambda _chat_id, _image: "/tmp/product.jpg",
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene._start_advertising_campaign_choice", fake_start_campaign)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1069))
    asyncio.run(
        _handle_advertising_product_photo(
            update,
            context=None,
            chat_id=1069,
            session=session,
        )
    )

    assert session["advertising_product_path"] == "/tmp/product.jpg"
    assert session["advertising_product_brief"]["product_name"] == "CleanStep"
    assert campaign_started == [True]
    assert session["state"] == STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE
    assert any("Фото товара принято" in text for text in captured)


@pytest.mark.parametrize("format_choice", ["Клип", "ОК"])
def test_advertising_campaign_choice_then_clip_asks_for_series(monkeypatch, format_choice):
    captured: list[str] = []
    series_asked: list[bool] = []
    session = {
        "state": STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
        ADVERTISING_CAMPAIGNS_SESSION_KEY: [
            {"title": f"Кампания {index}", "hook": "Хук", "idea": f"Сюжет {index}"}
            for index in range(1, 6)
        ],
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            captured.append(text)

    async def fake_ask_series(_update, current):
        series_asked.append(True)
        current["state"] = STATE_WAITING_SERIES_CHOICE

    monkeypatch.setattr("app.handlers.handlers_state_scene._ask_series_choice", fake_ask_series)
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1070))

    asyncio.run(
        _handle_advertising_campaign_choice(
            update,
            context=None,
            chat_id=1070,
            session=session,
            text="3",
        )
    )
    assert session[ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY]["title"] == "Кампания 3"
    assert session["state"] == STATE_WAITING_ADVERTISING_FORMAT_CHOICE

    asyncio.run(
        _handle_advertising_format_choice(
            update,
            context=None,
            chat_id=1070,
            session=session,
            text=format_choice,
        )
    )
    assert session[ADVERTISING_FORMAT_SESSION_KEY] == "clip"
    assert session[SERIES_PENDING_NEXT_SESSION_KEY] == SERIES_NEXT_ADVERTISING_PLAN
    assert series_asked == [True]
    assert any("от 3 до 6 Veo-сцен" in text for text in captured)


def test_advertising_post_starts_with_copy_approval_before_images(monkeypatch):
    photos: list[str] = []
    texts: list[str] = []
    generated_prompts: list[str] = []
    session = {
        "state": STATE_WAITING_ADVERTISING_FORMAT_CHOICE,
        ADVERTISING_SOURCE_SESSION_KEY: "Как почистить белые кроссовки",
        ADVERTISING_PRODUCT_BRIEF_SESSION_KEY: {"product_name": "CleanStep"},
        ADVERTISING_PRODUCT_PATH_SESSION_KEY: "/tmp/product.jpg",
        ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY: {"title": "Чистая победа", "idea": "Мини-история"},
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            texts.append(text)

        async def reply_photo(self, photo, caption=None, **_kwargs):
            _ = photo
            photos.append(str(caption or ""))

    async def fake_call(_update, _context, _chat_id, _name, _op, **_kwargs):
        return {
            "post_text": "Красивый рекламный текст\n\n#реклама",
            "image_prompts": ["Кадр 1", "Кадр 2", "Кадр 3"],
        }

    async def fake_image(prompt, **_kwargs):
        generated_prompts.append(prompt)
        return f"image-{len(generated_prompts)}".encode()

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", fake_call)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_image_with_lenient_fallback", fake_image)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1071))
    asyncio.run(
        _generate_advertising_post(
            update,
            context=None,
            chat_id=1071,
            session=session,
        )
    )

    assert generated_prompts == []
    assert photos == []
    assert session[ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY] == ["Кадр 1"]
    assert session[ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY] == []
    assert session[ADVERTISING_POST_TEXT_SESSION_KEY].startswith("Красивый")
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL
    assert any("Текст поста на проверку" in text for text in texts)


def test_advertising_post_reviews_copy_and_one_image(monkeypatch):
    photos: list[str] = []
    texts: list[str] = []
    generated_prompts: list[str] = []
    stored: list[tuple[int, bytes]] = []
    artifacts: list[str] = []
    session = {
        "state": STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL,
        ADVERTISING_SOURCE_SESSION_KEY: "Как почистить белые кроссовки",
        ADVERTISING_PRODUCT_BRIEF_SESSION_KEY: {"product_name": "CleanStep"},
        ADVERTISING_PRODUCT_PATH_SESSION_KEY: "/tmp/product.jpg",
        ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY: {"title": "Чистая победа"},
        ADVERTISING_POST_TEXT_SESSION_KEY: "Исходный текст",
        ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY: ["Кадр 1", "Кадр 2", "Кадр 3"],
        ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY: [],
        ADVERTISING_POST_IMAGE_INDEX_SESSION_KEY: 0,
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            texts.append(text)

        async def reply_photo(self, photo, caption=None, **_kwargs):
            _ = photo
            photos.append(str(caption or ""))

    async def fake_image(prompt, **_kwargs):
        generated_prompts.append(prompt)
        return f"image-{len(generated_prompts)}".encode()

    async def fake_rewrite(_scene, feedback):
        return f"Промпт после фидбэка: {feedback}"

    def fake_store(_chat_id, payload, *, index):
        stored.append((index, payload))
        return f"/tmp/post-{index}-candidate-{len(stored)}.png"

    monkeypatch.setattr("app.handlers.handlers_state_scene.advertising_product_reference_bytes", lambda _session: [b"product"])
    monkeypatch.setattr("app.handlers.handlers_state_scene._load_scene_reference_images", lambda *_args, **_kwargs: [b"grandma", b"cat"])
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_image_with_lenient_fallback", fake_image)
    monkeypatch.setattr("app.handlers.handlers_state_scene._regenerate_image_prompt_from_feedback", fake_rewrite)
    monkeypatch.setattr("app.handlers.handlers_state_scene.store_advertising_post_image", fake_store)
    monkeypatch.setattr("app.handlers.handlers_state_scene.ensure_generation_run_for_session", lambda _session: (None, {}))
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.persist_generation_artifact",
        lambda _session, *, artifact_type, **_kwargs: artifacts.append(artifact_type),
    )
    monkeypatch.setattr("app.handlers.handlers_state_scene.file_uri", lambda path: f"file://{path}")

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1072))

    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1072, session=session, text="АПРУВ"
        )
    )
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_IMAGE_APPROVAL
    assert session[ADVERTISING_POST_IMAGE_INDEX_SESSION_KEY] == 0
    assert photos == ["Рекламный пост · изображение"]

    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1072, session=session, text="ПЕРЕГЕНЕРИРОВАТЬ"
        )
    )
    assert photos[-1] == "Рекламный пост · изображение"
    assert len(generated_prompts) == 2

    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1072, session=session, text="ФИДБЭК"
        )
    )
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_IMAGE_FEEDBACK
    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1072, session=session, text="Сделай фон светлее"
        )
    )
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_IMAGE_APPROVAL
    assert session[ADVERTISING_POST_IMAGE_PROMPTS_SESSION_KEY][0].startswith("Промпт после фидбэка")
    assert len(generated_prompts) == 3

    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1072, session=session, text="АПРУВ"
        )
    )
    assert session["state"] == ""
    assert len(session[ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY]) == 1
    assert ADVERTISING_POST_CURRENT_IMAGE_PATH_SESSION_KEY not in session
    assert photos == ["Рекламный пост · изображение"] * 3
    assert session[ADVERTISING_POST_IMAGE_PATHS_SESSION_KEY] == ["/tmp/post-1-candidate-3.png"]
    assert len(generated_prompts) == 3
    assert artifacts.count("advertising_post_image") == 1
    assert any("утверждён" in text for text in texts)


def test_advertising_post_copy_feedback_regenerates_text(monkeypatch):
    texts: list[str] = []
    session = {
        "state": STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL,
        ADVERTISING_SOURCE_SESSION_KEY: "Исходник",
        ADVERTISING_PRODUCT_BRIEF_SESSION_KEY: {"product_name": "CleanStep"},
        ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY: {"title": "Кампания"},
        ADVERTISING_POST_TEXT_SESSION_KEY: "Старый текст",
    }

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            texts.append(text)

    async def fake_call(_update, _context, _chat_id, _name, _op, **_kwargs):
        return "Новый текст с учётом правки"

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", fake_call)
    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1073))

    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1073, session=session, text="ФИДБЭК"
        )
    )
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_TEXT_FEEDBACK
    asyncio.run(
        _handle_advertising_post_review(
            update, context=None, chat_id=1073, session=session, text="Сделай короче"
        )
    )
    assert session["state"] == STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL
    assert session[ADVERTISING_POST_TEXT_SESSION_KEY] == "Новый текст с учётом правки"
    assert any("Новый текст поста на проверку" in text for text in texts)


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


def test_series_skip_preserves_early_overlay_title_when_generation_session_is_rebuilt(monkeypatch):
    replies: list[str] = []
    SESSIONS[1067] = {
        "state": STATE_WAITING_SERIES_CHOICE,
        "generation_mode": "silence",
        "target_languages": ["ru"],
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея борща",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Борщ"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15},
    }

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "generate_recipe_concepts"
        return {"concepts": [{"name": "Борщ"}], "text_plan": "1. Борщ"}

    monkeypatch.setattr("app.handlers.handlers_state_start._call_with_503_retries", _fake_call)

    class _Message:
        text = "Без серии"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1067))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1067]["state"] == "waiting_concept"
    assert SESSIONS[1067]["overlay_title"] == "Борщ"
    assert SESSIONS[1067]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Борщ"}
    assert SESSIONS[1067]["overlay_title_style"]["title_color"] == "#c72638"
    assert not any("русский заголовок" in text.lower() for text in replies)


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


def test_on_photo_first_step_description_extracts_source_and_starts_descriptions(monkeypatch):
    captured_recipes: list[str] = []
    replies: list[str] = []
    SESSIONS[1033] = {"state": "state_waiting_first_step_description_text"}

    class _Message:
        caption = "Нужно описание по этому скриншоту"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"photo"

    async def _fake_source(image_bytes, caption=""):
        assert image_bytes == b"photo"
        assert caption == "Нужно описание по этому скриншоту"
        return "Рецепт домашнего борща"

    async def _fake_call(_update, _context, _chat_id, _name, fn):
        return await fn()

    async def _fake_start_description_with_recipe(update, context, *, recipe):
        _ = (update, context)
        captured_recipes.append(recipe)

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_scene._description_source_from_image", _fake_source)
    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._start_description_with_recipe",
        _fake_start_description_with_recipe,
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1033))
    asyncio.run(on_photo(update, context=None))

    assert captured_recipes == ["Рецепт домашнего борща"]
    assert any("Распознаю рецепт или блюдо по фото" in text for text in replies)


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


def test_scene_approval_with_existing_overlay_title_generates_production(monkeypatch):
    replies: list[str] = []
    message_text = {"value": "ОК"}
    SESSIONS[1047] = {
        "state": "waiting_scene_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "viral_concept_brief": {},
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Борщ"},
        "overlay_title_style": {"title_color": "#c72638"},
        SERIES_CHOICE_CONFIRMED_SESSION_KEY: True,
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "generate_full_production"
        return {"json_plan": [{"scene": 1, "start_frame_prompt": "s", "video_prompt": "v"}]}

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1047))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1047]["state"] == "state_waiting_overlay_subtitle_ru"
    assert any("третью строку" in text.lower() for text in replies)
    message_text["value"] = "ОК"
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1047][OVERLAY_SUBTITLE_DECIDED_SESSION_KEY] is True
    assert any("после утверждения первого стартового кадра" in text for text in replies)

    assert SESSIONS[1047]["state"] == "waiting_production_bundle_approval"
    assert not any("русский заголовок" in text.lower() for text in replies)
    assert SESSIONS[1047]["production"]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Борщ"}
    assert OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY not in SESSIONS[1047]["production"]


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


def test_overlay_title_ru_only_skips_english_candidate(monkeypatch):
    replies: list[str] = []
    SESSIONS[1048] = {
        "state": "state_waiting_overlay_title_ru",
        "generation_mode": "silence",
        "target_languages": ["ru"],
    }

    class _Message:
        text = "Борщ как у бабушки"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _unexpected_call(*_args, **_kwargs):
        raise AssertionError("RU-only title flow should not request English translation")

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _unexpected_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1048))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1048]["state"] == "state_waiting_overlay_subtitle_ru"
    assert SESSIONS[1048]["overlay_title_by_lang"] == {
        "ru": "Борщ как у бабушки",
        "en": "Борщ как у бабушки",
    }
    assert "overlay_title_translation_pending" not in SESSIONS[1048]
    assert any("третью строку" in text.lower() for text in replies)


def test_overlay_subtitle_text_translates_to_en_before_style_approval(monkeypatch, generated_title_frame):
    replies: list[str] = []
    photos: list[str] = []
    message_text = {"value": "КБЖУ на 100 гр: 126/10/4/24"}
    SESSIONS[1053] = {
        "state": "state_waiting_overlay_subtitle_ru",
        "target_languages": ["ru", "en"],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        assert name == "translate_overlay_subtitle_en"
        return "per 100g: 126/10/4/24"

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1053))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1053]["state"] == "state_waiting_overlay_subtitle_en_approval"
    assert SESSIONS[1053][OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY] == {
        "ru": "КБЖУ на 100 гр: 126/10/4/24",
        "en": "per 100g: 126/10/4/24",
    }
    assert any("Предлагаю английскую третью строку" in text for text in replies)

    message_text["value"] = "ОК"
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1053]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1053][OVERLAY_SUBTITLE_DECIDED_SESSION_KEY] is True
    assert SESSIONS[1053][OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY] == {
        "ru": "КБЖУ на 100 гр: 126/10/4/24",
        "en": "per 100g: 126/10/4/24",
    }
    assert photos == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]


def test_overlay_subtitle_en_correction_is_saved_before_style_approval(generated_title_frame):
    replies: list[str] = []
    photos: list[str] = []
    SESSIONS[1055] = {
        "state": "state_waiting_overlay_subtitle_en_approval",
        "target_languages": ["ru", "en"],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_subtitle_translation_pending": {
            "ru": "КБЖУ на 100 гр: 126/10/4/24",
            "en": "bad translation",
        },
    }

    class _Message:
        text = "Macros per 100g: 126/10/4/24"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1055))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1055]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1055][OVERLAY_SUBTITLE_BY_LANG_SESSION_KEY] == {
        "ru": "КБЖУ на 100 гр: 126/10/4/24",
        "en": "Macros per 100g: 126/10/4/24",
    }
    assert OVERLAY_SUBTITLE_TRANSLATION_PENDING_SESSION_KEY not in SESSIONS[1055]
    assert photos == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]
    assert any("Английскую третью строку исправила" in text for text in replies)


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


def test_overlay_title_en_ok_asks_for_style_approval(generated_title_frame):
    replies: list[str] = []
    photos: list[tuple[str, str]] = []
    message_text = {"value": "ОК"}
    SESSIONS[1042] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            photos.append((getattr(photo, "name", ""), caption))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1042))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1042]["state"] == "state_waiting_overlay_subtitle_ru"
    assert any("третью строку" in text.lower() for text in replies)
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1042]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1042]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}
    assert SESSIONS[1042]["overlay_title_style"]["title_color"] == "#c72638"
    assert [name for name, _caption in photos] == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]
    assert [caption for _name, caption in photos] == ["Превью верхнего заголовка RU.", "Превью верхнего заголовка EN."]
    assert any("Цвет заголовка: #c72638" in text for text in replies)

    with db.SessionLocal() as check_db:
        saved_session = check_db.query(ChatSession).filter_by(chat_id=1042).one()
        artifact = (
            check_db.query(GenerationArtifact)
            .filter_by(
                run_id=saved_session.generation_run_id,
                artifact_type="overlay_title_by_lang",
            )
            .one()
        )

    assert saved_session.session_json["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}
    assert artifact.payload_json["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Borscht"}


def test_overlay_title_preview_receives_series_context(monkeypatch, generated_title_frame):
    calls: list[tuple[str, str, str, str, str]] = []
    message_text = {"value": "ОК"}
    SESSIONS[1051] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
        SERIES_TITLE_BY_LANG_SESSION_KEY: {"ru": "Супы у бабушки", "en": "Grandma's Soups"},
        SERIES_COUNTER_BY_LANG_SESSION_KEY: {"ru": "день 2 из 7", "en": "day 2 of 7"},
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = (text, reply_markup)

        async def reply_photo(self, photo, caption):
            _ = (photo, caption)

    def _fake_preview(language, title, _style, series_title="", series_counter="", subtitle="", background=None):
        calls.append((language, title, series_title, series_counter, subtitle))
        return b"png"

    monkeypatch.setattr("app.handlers.handlers_state_scene._render_overlay_title_preview", _fake_preview)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1051))
    asyncio.run(on_text(update, context=None))
    asyncio.run(on_text(update, context=None))

    assert calls == [
        ("ru", "Борщ", "Супы у бабушки", "день 2 из 7", ""),
        ("en", "Borscht", "Grandma's Soups", "day 2 of 7", ""),
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


def test_overlay_title_en_correction_asks_for_style_approval(generated_title_frame):
    replies: list[str] = []
    photos: list[str] = []
    message_text = {"value": "Real Borscht"}
    SESSIONS[1043] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1043))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1043]["state"] == "state_waiting_overlay_subtitle_ru"
    assert any("Английский заголовок исправила" in text for text in replies)
    message_text["value"] = "ОК"
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1043]["state"] == "state_waiting_overlay_title_style_approval"
    assert SESSIONS[1043]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Real Borscht"}
    assert photos == ["overlay_title_preview_ru.png", "overlay_title_preview_en.png"]


def test_overlay_title_style_preview_failure_does_not_block_approval(monkeypatch, generated_title_frame):
    replies: list[str] = []
    photos: list[str] = []
    message_text = {"value": "ОК"}
    SESSIONS[1049] = {
        "state": "state_waiting_overlay_title_en_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title_translation_pending": {"ru": "Борщ", "en": "Borscht"},
    }

    class _Message:
        @property
        def text(self):
            return message_text["value"]

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

        async def reply_photo(self, photo, caption):
            _ = caption
            photos.append(getattr(photo, "name", ""))

    def _raise_preview(_language, _title, _style, _series_title="", _series_counter="", _subtitle="", _background=None):
        raise RuntimeError("font missing")

    monkeypatch.setattr("app.handlers.handlers_state_scene._render_overlay_title_preview", _raise_preview)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1049))
    asyncio.run(on_text(update, context=None))
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
        OVERLAY_SUBTITLE_DECIDED_SESSION_KEY: True,
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


def test_legacy_overlay_title_style_ok_asks_for_subtitle_before_generation(monkeypatch):
    replies: list[str] = []
    SESSIONS[1054] = {
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

    async def _unexpected_call(*_args, **_kwargs):
        raise AssertionError("must ask optional subtitle before generating production")

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _unexpected_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1054))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1054]["state"] == "state_waiting_overlay_subtitle_ru"
    assert any("третью строку" in text.lower() for text in replies)


def test_early_silence_overlay_title_style_ok_asks_for_series(monkeypatch):
    replies: list[str] = []
    SESSIONS[1052] = {
        "state": "state_waiting_overlay_title_style_approval",
        "generation_mode": "silence",
        "target_languages": ["ru"],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Борщ"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.1},
        OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY: OVERLAY_TITLE_NEXT_SERIES_CHOICE,
        OVERLAY_SUBTITLE_DECIDED_SESSION_KEY: True,
        SERIES_PENDING_RECIPE_SESSION_KEY: "Идея",
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: False,
    }
    monkeypatch.setattr("app.handlers.handlers_state_start.list_active_series", lambda _project_slug: [])

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1052))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1052]["state"] == STATE_WAITING_SERIES_CHOICE
    assert OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY not in SESSIONS[1052]
    assert SESSIONS[1052]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Борщ"}
    assert any("Можно привязать это видео к серии" in text for text in replies)


def test_overlay_title_style_generation_empty_plan_offers_retry(monkeypatch):
    replies: list[str] = []
    SESSIONS[1050] = {
        "state": "state_waiting_overlay_title_style_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Borscht"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.1},
        OVERLAY_SUBTITLE_DECIDED_SESSION_KEY: True,
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

    assert SESSIONS[1050]["state"] == "state_waiting_production_retry"
    assert "production" not in SESSIONS[1050]
    assert any("Модель не вернула production-план" in text for text in replies)


def test_production_bundle_empty_feedback_does_not_regenerate(monkeypatch):
    replies: list[str] = []
    SESSIONS[1053] = {
        "state": "waiting_production_bundle_approval",
        "scenes_plan": [{"scene": 1, "text": "Сцена"}],
        "production": {"json_plan": [{"scene": 1, "video_prompt": "old"}]},
    }

    class _Message:
        text = "ФИДБЕК:"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _unexpected_call(*_args, **_kwargs):
        raise AssertionError("empty feedback must not regenerate production")

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _unexpected_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1053))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1053]["state"] == "waiting_production_bundle_approval"
    assert SESSIONS[1053]["production"]["json_plan"][0]["video_prompt"] == "old"
    assert any("конкретную правку" in text for text in replies)
    assert not any("Перегенерирую production" in text for text in replies)


def test_overlay_title_style_feedback_updates_settings_without_generating(monkeypatch, generated_title_frame):
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


def test_overlay_title_translation_retries_plain_transliteration(monkeypatch):
    calls: list[str] = []

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        calls.append(name)
        if name == "translate_overlay_title_en":
            return "Kabachkovaya ikra"
        return "Zucchini Caviar"

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)

    translated = asyncio.run(
        _translate_overlay_title_to_en(
            SimpleNamespace(),
            context=None,
            chat_id=1049,
            ru_title="Кабачковая икра",
        )
    )

    assert translated == "Zucchini Caviar"
    assert calls == ["translate_overlay_title_en", "translate_overlay_title_en_semantic_retry"]


def test_overlay_title_helpers_clean_sanitize_and_clamp_values():
    assert _clean_translated_title('"EN: Grandma Borscht"') == "Grandma Borscht"

    session = {
        "overlay_title": "  Борщ  ",
        "overlay_title_by_lang": {"ru": " Борщ ", "": "skip", "en": "", "es": " Sopa "},
        "overlay_title_style": {"title_color": "#C72638", "": "skip", "top_margin_ratio": 15},
        "overlay_subtitle_by_lang": {"ru": " КБЖУ на 100 гр: 126/10/4/24 ", "en": ""},
    }
    production: dict[str, object] = {}

    _apply_overlay_title_to_production(session, production)

    assert production["overlay_title"] == "Борщ"
    assert production["overlay_title_by_lang"] == {"ru": "Борщ", "es": "Sopa"}
    assert production["overlay_title_style"] == {"title_color": "#C72638", "top_margin_ratio": 15}
    assert production["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}

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


def test_silence_recipe_approval_shows_scene_plan_before_production(monkeypatch):
    replies: list[str] = []
    calls: list[str] = []
    SESSIONS[1034] = {
        "state": "waiting_recipe_approval",
        "generation_mode": "silence",
        "target_languages": ["ru"],
        "selected_concept": "Борщ в уютной кухне",
        "selected_concept_name": "Борщ",
        "recipe_full_text": "Свеклу потушить, бульон сварить, всё соединить.",
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        calls.append(name)
        assert name == "generate_silence_scenes_plan"
        return {
            "json_plan": [{"scene": 1, "recipe_step": "Свекла тушится", "action": "close-up"}],
            "text_plan": "1. Свекла тушится",
        }

    monkeypatch.setattr("app.handlers.handlers_state_start._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1034))
    asyncio.run(on_text(update, context=None))

    assert calls == ["generate_silence_scenes_plan"]
    assert SESSIONS[1034]["state"] == "waiting_scene_approval"
    assert SESSIONS[1034]["scenes_plan"] == [
        {
            "scene": 1,
            "recipe_step": "Свекла тушится",
            "action": "close-up",
            "characters": ["grandma", "cat"],
        }
    ]
    assert SESSIONS[1034]["target_languages"] == ["ru"]
    assert SESSIONS[1034]["dish_visual_brief"]
    assert "production" not in SESSIONS[1034]
    assert any("Это сценарий по сценам" in text for text in replies)


def test_silence_recipe_approval_with_early_title_still_waits_for_scene_approval(monkeypatch):
    replies: list[str] = []
    calls: list[str] = []
    SESSIONS[1035] = {
        "state": "waiting_recipe_approval",
        "generation_mode": "silence",
        "target_languages": ["ru"],
        "selected_concept": "Борщ в уютной кухне",
        "selected_concept_name": "Борщ",
        "recipe_full_text": "Свеклу потушить, бульон сварить, всё соединить.",
        "overlay_title": "Борщ",
        "overlay_title_by_lang": {"ru": "Борщ", "en": "Борщ"},
        "overlay_title_style": {"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.1},
        SERIES_CHOICE_CONFIRMED_SESSION_KEY: True,
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        calls.append(name)
        assert name == "generate_silence_scenes_plan"
        return {
            "json_plan": [{"scene": 1, "recipe_step": "Свекла тушится", "action": "close-up"}],
            "text_plan": "1. Свекла тушится",
        }

    monkeypatch.setattr("app.handlers.handlers_state_start._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1035))
    asyncio.run(on_text(update, context=None))

    assert calls == ["generate_silence_scenes_plan"]
    assert SESSIONS[1035]["state"] == "waiting_scene_approval"
    assert SESSIONS[1035]["overlay_title_by_lang"] == {"ru": "Борщ", "en": "Борщ"}
    assert "production" not in SESSIONS[1035]
    assert any("Это сценарий по сценам" in text for text in replies)


def test_normal_recipe_approval_asks_overlay_title_before_viral_plan(monkeypatch):
    replies: list[str] = []
    SESSIONS[1037] = {
        "state": "waiting_recipe_approval",
        "selected_concept": "Белковый салат",
        "selected_concept_name": "Белковый салат",
        "recipe_full_text": "Курица, черри, сыр, йогурт.",
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _unexpected_call(*_args, **_kwargs):
        raise AssertionError("viral plan must wait until title and optional third line are answered")

    monkeypatch.setattr("app.handlers.handlers_state_start._call_with_503_retries", _unexpected_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1037))
    asyncio.run(on_text(update, context=None))

    assert SESSIONS[1037]["state"] == "state_waiting_overlay_title_ru"
    assert SESSIONS[1037][OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] == OVERLAY_TITLE_NEXT_RECIPE_PLAN
    assert any("До viral plan" in text for text in replies)


def test_overlay_title_recipe_plan_continues_to_viral_plan_after_subtitle_and_style(monkeypatch):
    replies: list[str] = []
    calls: list[str] = []
    SESSIONS[1038] = {
        "state": "state_waiting_overlay_title_style_approval",
        "selected_concept": "Белковый салат",
        "selected_concept_name": "Белковый салат",
        "recipe_full_text": "Курица, черри, сыр, йогурт.",
        "overlay_title": "Белковый салат",
        "overlay_title_by_lang": {"ru": "Белковый салат", "en": "Protein salad"},
        "overlay_subtitle_by_lang": {"ru": "КБЖУ на 100 гр: 126/10/4/24"},
        "overlay_subtitle_decided": True,
        "overlay_title_style": {"title_color": "#fff7e8"},
        OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY: OVERLAY_TITLE_NEXT_RECIPE_PLAN,
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, _fn):
        calls.append(name)
        if name == "generate_viral_concept_brief":
            return {"dish_name": "Белковый салат", "strongest_first_phrase": "Лёгкий ужин"}
        assert name == "generate_scenes_plan"
        return {
            "json_plan": [{"scene": 1, "text": "Сцена", "characters": ["grandma"]}],
            "text_plan": "1. Сцена",
            "viral_concept_brief": {"dish_name": "Белковый салат"},
        }

    monkeypatch.setattr("app.handlers.handlers_state_start._call_with_503_retries", _fake_call)

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1038))
    asyncio.run(on_text(update, context=None))

    assert calls == ["generate_viral_concept_brief", "generate_scenes_plan"]
    assert SESSIONS[1038]["state"] == "waiting_scene_approval"
    assert SESSIONS[1038]["overlay_subtitle_by_lang"] == {"ru": "КБЖУ на 100 гр: 126/10/4/24"}
    assert OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY not in SESSIONS[1038]
    assert any("Если план подходит" in text for text in replies)


def test_silence_scene_plan_feedback_regenerates_scene_plan_before_production(monkeypatch):
    replies: list[str] = []
    captured: dict[str, object] = {}
    SESSIONS[1036] = {
        "state": "waiting_scene_approval",
        "generation_mode": "silence",
        "target_languages": ["ru"],
        "selected_concept": "Борщ в уютной кухне",
        "recipe_full_text": "Свеклу потушить, бульон сварить, всё соединить.",
        "scenes_plan": [{"scene": 1, "recipe_step": "старая подача", "action": "old"}],
        "viral_concept_brief": {},
    }

    class _Message:
        text = "в последней сцене крупно пюре с 2 котлетами"

        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def _fake_call(_update, _context, _chat_id, name, fn):
        captured["name"] = name
        result = fn()
        captured["result"] = result
        return {
            "json_plan": [
                {
                    "scene": 1,
                    "recipe_step": "крупно пюре с 2 котлетами",
                    "action": "крупно пюре с 2 котлетами",
                }
            ],
            "text_plan": "1. крупно пюре с 2 котлетами",
            "viral_concept_brief": {},
            "romantic_drama_subplot": {},
            "retention_review": {},
        }

    monkeypatch.setattr("app.handlers.handlers_state_scene._call_with_503_retries", _fake_call)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene.generate_scenes_plan",
        lambda *args, **kwargs: captured.setdefault("kwargs", kwargs) or {"json_plan": [], "text_plan": ""},
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1036))
    asyncio.run(on_text(update, context=None))

    assert captured["name"] == "refine_scenes_plan"
    assert captured["kwargs"]["generation_mode"] == "silence"  # type: ignore[index]
    assert captured["kwargs"]["user_feedback"] == "в последней сцене крупно пюре с 2 котлетами"  # type: ignore[index]
    assert SESSIONS[1036]["state"] == "waiting_scene_approval"
    assert SESSIONS[1036]["scenes_plan"][0]["action"] == "крупно пюре с 2 котлетами"
    assert SESSIONS[1036]["recipe_steps"] == ["крупно пюре с 2 котлетами"]
    assert "production" not in SESSIONS[1036]
    assert any("Обновила план по правкам" in text for text in replies)


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
    assert SESSIONS[1005]["state"] == "state_waiting_first_step_image_approval"
    assert SESSIONS[1005]["first_step_image_prompt"] == "Бабушка с котом и пирогом"
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
    assert SESSIONS[1016]["state"] == "state_waiting_first_step_image_approval"


def test_on_text_first_step_image_approval_accepts_ok():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1021] = {
        "state": "state_waiting_first_step_image_approval",
        "first_step_image_prompt": "Красивая тарелка супа",
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1021))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1021]["state"] == ""
    assert SESSIONS[1021]["selected_project_key"] == "grandma_kitchen"
    assert "Принято" in captured[-1][0]


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
    assert SESSIONS[1013]["state"] == "state_waiting_first_step_project_image_approval"


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
    assert SESSIONS[1017]["state"] == "state_waiting_first_step_project_image_approval"


def test_on_text_project_image_approval_accepts_ok():
    captured: list[tuple[str, object | None]] = []
    SESSIONS[1018] = {
        "state": "state_waiting_first_step_project_image_approval",
        "first_step_image_prompt": "Бабушка и кот у супа",
        "first_step_project_image_characters": ["grandma", "cat"],
        "selected_project_key": "grandma_kitchen",
    }

    class _Message:
        text = "ОК"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1018))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1018]["state"] == ""
    assert SESSIONS[1018]["selected_project_key"] == "grandma_kitchen"
    assert "Принято" in captured[-1][0]


def test_on_text_project_image_approval_regenerates_from_feedback(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    seen: dict[str, object] = {}
    SESSIONS[1019] = {
        "state": "state_waiting_first_step_project_image_approval",
        "first_step_image_prompt": "Бабушка и кот у супа",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "Изменить"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            seen["photo"] = photo.getvalue() if hasattr(photo, "getvalue") else b""
            seen["caption"] = caption

    async def _fake_regenerate(scene, feedback):
        seen["feedback"] = feedback
        seen["scene"] = scene
        return "Обновленный prompt с большим котом"

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img-2"

    monkeypatch.setattr("app.handlers.handlers_state_scene._regenerate_image_prompt_from_feedback", _fake_regenerate)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_image_with_lenient_fallback", _fake_generate)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"project-ref"],
    )

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1019))
    asyncio.run(on_text(update, context=None))
    assert "Что изменить" in captured[-1][0]

    class _FeedbackMessage(_Message):
        text = "сделай кота крупнее"

    update = SimpleNamespace(message=_FeedbackMessage(), effective_chat=SimpleNamespace(id=1019))
    asyncio.run(on_text(update, context=None))
    assert seen["feedback"] == "Исправь: сделай кота крупнее"
    assert "Обновленный prompt с большим котом" in str(seen["prompt"])
    assert seen["reference_images"] == [b"project-ref"]
    assert seen["photo"] == b"project-img-2"
    assert SESSIONS[1019]["state"] == "state_waiting_first_step_project_image_approval"
    assert SESSIONS[1019]["first_step_image_prompt"] == "Обновленный prompt с большим котом"


def test_on_text_project_image_approval_adds_reference_and_regenerates(monkeypatch):
    captured: list[tuple[str, object | None]] = []
    seen: dict[str, object] = {}
    SESSIONS[1020] = {
        "state": "state_waiting_first_step_project_image_approval",
        "first_step_image_prompt": "Бабушка и кот у супа",
        "first_step_project_image_characters": ["grandma", "cat"],
    }

    class _Message:
        text = "Добавить свое"

        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

        async def reply_photo(self, photo, caption):
            seen["photo"] = photo.getvalue() if hasattr(photo, "getvalue") else b""
            seen["caption"] = caption

    update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1020))
    asyncio.run(on_text(update, context=None))
    assert SESSIONS[1020]["state"] == "state_waiting_first_step_project_image_references"
    assert SESSIONS[1020]["first_step_reference_return_state"] == "state_waiting_first_step_project_image_approval"

    async def _fake_extract(update, context):
        _ = (update, context)
        return b"new-project-ref"

    async def _fake_generate(prompt: str, reference_images=None):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return b"project-img-3"

    monkeypatch.setattr("app.handlers.handlers_state_scene._extract_any_image_from_update", _fake_extract)
    monkeypatch.setattr("app.handlers.handlers_state_scene._generate_image_with_lenient_fallback", _fake_generate)
    monkeypatch.setattr(
        "app.handlers.handlers_state_scene._load_scene_reference_images",
        lambda scene, include_location=False: [b"project-ref"],
    )

    photo_update = SimpleNamespace(message=_Message(), effective_chat=SimpleNamespace(id=1020))
    asyncio.run(on_photo(photo_update, context=None))
    assert SESSIONS[1020]["first_step_custom_reference_images"] == [b"new-project-ref"]

    class _DoneMessage(_Message):
        text = "больше файлов не нужно"

    update = SimpleNamespace(message=_DoneMessage(), effective_chat=SimpleNamespace(id=1020))
    asyncio.run(on_text(update, context=None))
    assert "Бабушка и кот у супа" in str(seen["prompt"])
    assert seen["reference_images"] == [b"project-ref", b"new-project-ref"]
    assert seen["photo"] == b"project-img-3"
    assert SESSIONS[1020]["state"] == "state_waiting_first_step_project_image_approval"


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
        "discipline_intro_enabled": True,
        "discipline_intro_dialogue": {"vasily": "Проблема", "anastasia": "Решение"},
        "discipline_intro_video_path": "/tmp/discipline-intro.mp4",
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
    assert "discipline_intro_dialogue" not in SESSIONS[1011]
    assert "discipline_intro_video_path" not in SESSIONS[1011]


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


def test_advertising_post_default_survives_format_review_and_ok(monkeypatch):
    from app.handlers import handlers_state_scene as module
    from unittest.mock import AsyncMock
    session = {ADVERTISING_FORMAT_SESSION_KEY: "post"}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    generate = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "_generate_advertising_post", generate)
    asyncio.run(module._ask_advertising_format(update, chat_id=1234, session=session))
    assert session[ADVERTISING_FORMAT_SESSION_KEY] == "post"
    asyncio.run(module._handle_advertising_format_choice(update, None, chat_id=1234, session=session, text="ОК"))
    generate.assert_awaited_once()


@pytest.fixture
def generated_title_frame(monkeypatch):
    from PIL import Image
    data = io.BytesIO()
    Image.new("RGB", (1080, 1920), "#123456").save(data, format="PNG")
    monkeypatch.setattr("app.handlers.handlers_state_scene._overlay_first_frame", lambda session: data.getvalue())
    return data.getvalue()


@pytest.mark.parametrize('initial_state', [
    'waiting_scene_approval', 'refining_scenes',
    'waiting_recipe_approval', STATE_WAITING_ADVERTISING_FORMAT_CHOICE,
    STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
])
def test_advertising_previous_twice_then_one_selects_campaign_not_recipe(initial_state):
    from app.handlers import handlers_state_scene as module
    from unittest.mock import AsyncMock
    source = 'Товар: комбайн. Комментарий: оливье и подарок.'
    session = {
        'state': initial_state, 'generation_mode': 'advertising',
        ADVERTISING_SOURCE_SESSION_KEY: source,
        ADVERTISING_PRODUCT_PATH_SESSION_KEY: '/tmp/product.jpg',
        ADVERTISING_PRODUCT_BRIEF_SESSION_KEY: {'product_name': 'Pioneer'},
        ADVERTISING_CAMPAIGNS_SESSION_KEY: [{'title': f'Оливье {i}', 'idea': 'Нарезка и другие функции'} for i in range(1, 6)],
        'recipe_full_text': 'Случайный яблочный пирог', 'scenes_plan': [{'text': 'old'}],
    }
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    for _ in range(2):
        assert asyncio.run(module._handle_navigation_buttons(update, chat_id=1991, session=session, text='Предыдущий шаг'))
        assert session['state'] == STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE
        assert session[ADVERTISING_PRODUCT_PATH_SESSION_KEY] == '/tmp/product.jpg'
        assert session['recipe_full_text'] == source
    asyncio.run(module._handle_advertising_campaign_choice(update, None, chat_id=1991, session=session, text='1'))
    assert session[ADVERTISING_SELECTED_CAMPAIGN_SESSION_KEY]['title'] == 'Оливье 1'
    assert session['state'] == STATE_WAITING_ADVERTISING_FORMAT_CHOICE
    assert all('апруву рецепта' not in call.args[0] for call in update.message.reply_text.call_args_list)


def test_advertising_advice_choice_keeps_advertising_mode(monkeypatch):
    from unittest.mock import AsyncMock
    from app.handlers import handlers_state_scene as module
    from app.handlers.handlers_state_start import _advertising_format_choice_markup
    session = {'generation_mode': 'advertising', 'advertising_product_brief': {}}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    monkeypatch.setattr(module, '_ask_series_choice', AsyncMock())
    asyncio.run(module._handle_advertising_format_choice(update, None, chat_id=778, session=session, text='Клип — совет'))
    assert session['advertising_format'] == 'clip'
    assert session['advertising_product_brief']['video_style'] == 'advice'
    assert session['generation_mode'] == 'advertising'
    assert any(button.text == 'Клип — совет' for row in _advertising_format_choice_markup().keyboard for button in row)
    asyncio.run(module._handle_advertising_format_choice(update, None, chat_id=778, session=session, text='ОК'))
    assert session['advertising_product_brief']['video_style'] == 'advice'
    asyncio.run(module._handle_advertising_format_choice(update, None, chat_id=778, session=session, text='Клип'))
    assert session['advertising_product_brief']['video_style'] == 'story'


def test_advertising_advice_does_not_reuse_prepared_monologue(monkeypatch):
    from unittest.mock import AsyncMock
    from app.handlers import handlers_state_start as module
    cached = {'json_plan': [{'text': 'old monologue'}] * 3, 'text_plan': 'old'}
    session = {'advertising_source_text': 'Полотенца', 'advertising_product_brief': {'video_style': 'advice'},
               'advertising_product_path': '/tmp/product.jpg', 'advertising_selected_campaign': {'prepared_scene_plan': cached}}
    fresh = {'json_plan': [{'text': 'Дочь и бабушка'}] * 3, 'text_plan': 'new', 'video_style': 'advice'}
    calls = []
    async def generate(*args):
        calls.append(True)
        return fresh
    monkeypatch.setattr(module, '_call_with_503_retries', generate)
    monkeypatch.setattr(module, 'ensure_generation_run_for_session', lambda s: (None, {}))
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(module._start_advertising_scene_plan(update, None, chat_id=779, session=session))
    assert calls == [True]
    assert session['scenes_text'] == 'new'
    assert cached['text_plan'] == 'old'


@pytest.mark.parametrize('languages', [None, ['ru'], ['ru', 'en']])
def test_advertising_titles_and_preview_are_russian_only(monkeypatch, languages):
    from unittest.mock import AsyncMock
    from app.handlers import handlers_state_scene as module
    session = {'generation_mode': 'advertising', 'target_languages': languages}
    assert module._overlay_title_requires_english(session) is False
    module._store_overlay_titles(session, ru_title='Мягкие полотенца', en_title='Soft towels')
    module._store_overlay_subtitle(session, 'Совет', en_text='Advice')
    assert session['overlay_title_by_lang'] == {'ru': 'Мягкие полотенца'}
    assert session['overlay_subtitle_by_lang'] == {'ru': 'Совет'}
    # Even a legacy EN title must not produce an EN preview.
    session['overlay_title_by_lang']['en'] = 'Old English title'
    rendered = []
    def render(language, *args):
        rendered.append(language)
        return b'image'
    monkeypatch.setattr(module, '_render_overlay_title_preview', render)
    monkeypatch.setattr(module, '_overlay_first_frame', lambda s: b'frame')
    monkeypatch.setattr(module, 'safe_reply_photo', AsyncMock())
    update = SimpleNamespace(message=SimpleNamespace(reply_photo=AsyncMock()))
    asyncio.run(module._send_overlay_title_previews(update, session, {}))
    assert rendered == ['ru']


def test_advertising_production_ignores_stale_english_overlay():
    from app.handlers import handlers_state_scene as module
    session = {'generation_mode': 'advertising', 'overlay_title_by_lang': {'ru': 'Совет', 'en': 'Advice'},
               'overlay_subtitle_by_lang': {'ru': 'Мягкость', 'en': 'Softness'}}
    production = {}
    module._apply_overlay_title_to_production(session, production)
    assert production['overlay_title_by_lang'] == {'ru': 'Совет'}
    assert production['overlay_subtitle_by_lang'] == {'ru': 'Мягкость'}
