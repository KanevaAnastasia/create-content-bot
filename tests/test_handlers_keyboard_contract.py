from __future__ import annotations

import pytest

from app.constants import (
    STATE_READY_FOR_PRODUCTION,
    STATE_REFINING_CONCEPT,
    STATE_REFINING_FRAME_PROMPTS,
    STATE_REFINING_PRODUCTION_BUNDLE,
    STATE_REFINING_RECIPE,
    STATE_REFINING_SCENES,
    STATE_WAITING_CONCEPT,
    STATE_WAITING_FRAME_PROMPTS_APPROVAL,
    STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    STATE_WAITING_SCENE_MEDIA,
)
from app.handlers import handlers_state_media, handlers_state_scene
from app.handlers.handlers_generation_utils import (
    STATE_WAITING_OVERLAY_SUBTITLE_EN_APPROVAL,
    STATE_WAITING_OVERLAY_SUBTITLE_RU,
    STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL,
    STATE_WAITING_OVERLAY_TITLE_RU,
    STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL,
)
from app.handlers.handlers_state_start import (
    STATE_WAITING_GENERATION_MODE,
    STATE_WAITING_SERIES_CHOICE,
    STATE_WAITING_SERIES_CREATE,
    STATE_WAITING_SERIES_TRANSLATION_APPROVAL,
)
from app.advertising_generation import (
    STATE_GENERATING_ADVERTISING_POST,
    STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
    STATE_WAITING_ADVERTISING_CUSTOM_CAMPAIGN,
    STATE_WAITING_ADVERTISING_FORMAT_CHOICE,
    STATE_WAITING_ADVERTISING_POST_IMAGE_APPROVAL,
    STATE_WAITING_ADVERTISING_POST_IMAGE_FEEDBACK,
    STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL,
    STATE_WAITING_ADVERTISING_POST_TEXT_FEEDBACK,
)
from app.guest_handlers import STATE_MENU as GUEST_STATE_MENU, guest_keyboard_for_session


def _rows(markup: object) -> list[list[str]]:
    keyboard = getattr(markup, "keyboard", None)
    if not isinstance(keyboard, (list, tuple)):
        return []
    return [
        [str(getattr(button, "text", button)) for button in row]
        for row in keyboard
    ]


def _assert_rows(session: dict, expected_rows: list[list[str]]) -> None:
    assert _rows(handlers_state_scene.keyboard_for_session_state(session)) == expected_rows


FIRST_STEP_ROWS = [
    ["Генерировать", "Описание"],
    ["Изображение", "Изображение проект"],
    ["Видео", "Звук"],
    ["Проект"],
    ["Сохранить черновик", "Черновики"],
]
OK_ONLY_ROWS = [
    ["ОК"],
    ["Добавь"],
    ["Исправь"],
    ["Сохранить черновик"],
    ["Предыдущий шаг"],
    ["В начало"],
]
CUSTOM_REFERENCE_ROWS = [["больше файлов не нужно"], ["В начало"]]


@pytest.mark.parametrize(
    ("state", "expected_rows"),
    [
        ("", FIRST_STEP_ROWS),
        ("state_waiting_first_step_generate_text", FIRST_STEP_ROWS),
        ("state_waiting_first_step_description_text", FIRST_STEP_ROWS),
        ("state_waiting_first_step_sound_text", FIRST_STEP_ROWS),
        ("state_waiting_first_step_video_text", FIRST_STEP_ROWS),
        ("state_waiting_first_step_video_image", FIRST_STEP_ROWS),
        ("state_waiting_first_step_copy_video", []),
        ("waiting_visualization_media", []),
        (
            "state_waiting_copy_plan_approval",
            [["Запустить"], ["Добавить корректировку"], ["В начало"]],
        ),
        ("state_waiting_copy_chunk_approval", [["ОК"], ["Переделать"], ["ФИДБЭК"], ["В начало"]]),
        ("state_waiting_first_step_project_image_prompt", FIRST_STEP_ROWS),
        ("state_waiting_first_step_image_text", [["Добавить свое"], ["В начало"]]),
        ("state_waiting_first_step_image_references", CUSTOM_REFERENCE_ROWS),
        ("state_waiting_first_step_project_image_references", CUSTOM_REFERENCE_ROWS),
        (
            "state_waiting_first_step_project_selection",
            [["Grandma Kitchen"], ["Дисциплина"], ["Назад"], ["В начало"]],
        ),
        (STATE_WAITING_GENERATION_MODE, [["Обычный", "Совет"], ["Silence"], ["Длинный"], ["Копия"], ["История"], ["Визуализация"], ["Реклама"]]),
        (
            STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
            [["1", "2", "3"], ["4", "5"], ["Другие варианты", "Усилить варианты"], ["Свой вариант"], ["В начало"]],
        ),
        (STATE_WAITING_ADVERTISING_FORMAT_CHOICE, [["Клип", "Клип — совет"], ["Пост"], ["В начало"]]),
        (
            STATE_WAITING_ADVERTISING_POST_TEXT_APPROVAL,
            [["АПРУВ"], ["ПЕРЕГЕНЕРИРОВАТЬ"], ["ФИДБЭК"], ["В начало"]],
        ),
        (
            STATE_WAITING_ADVERTISING_POST_IMAGE_APPROVAL,
            [["АПРУВ"], ["ПЕРЕГЕНЕРИРОВАТЬ"], ["ФИДБЭК"], ["В начало"]],
        ),
        (STATE_WAITING_OVERLAY_TITLE_EN_APPROVAL, OK_ONLY_ROWS),
        (STATE_WAITING_OVERLAY_SUBTITLE_RU, OK_ONLY_ROWS),
        (STATE_WAITING_OVERLAY_SUBTITLE_EN_APPROVAL, OK_ONLY_ROWS),
        (STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL, OK_ONLY_ROWS),
        (STATE_WAITING_SERIES_CREATE, OK_ONLY_ROWS),
        (STATE_WAITING_SERIES_TRANSLATION_APPROVAL, OK_ONLY_ROWS),
        (STATE_WAITING_RECIPE_APPROVAL, OK_ONLY_ROWS),
        (STATE_WAITING_SCENE_APPROVAL, OK_ONLY_ROWS),
        (
            STATE_WAITING_FRAME_PROMPTS_APPROVAL,
            [
                ["ОК"],
                ["Добавить"],
                ["Добавь"],
                ["Исправь"],
                ["Предыдущий шаг"],
                ["В начало"],
            ],
        ),
        (
            "state_waiting_frame_prompt_references",
            [["больше файлов не нужно"], ["Предыдущий шаг"], ["В начало"]],
        ),
        (
            STATE_WAITING_PRODUCTION_BUNDLE_APPROVAL,
            [
                ["ОК"],
                ["Добавь"],
                ["Исправь"],
                ["ФИДБЕК:"],
                ["ПРОМПТ:"],
                ["Предыдущий шаг"],
                ["В начало"],
            ],
        ),
        (STATE_READY_FOR_PRODUCTION, FIRST_STEP_ROWS),
        ("state_waiting_draft_language", [["RU", "EN", "RU+EN"], ["В начало"]]),
        ("state_waiting_draft_stage", [["Промпт", "Изображение", "Видео"], ["В начало"]]),
    ],
)
def test_keyboard_for_session_state_has_exact_rows_for_visible_states(state, expected_rows):
    _assert_rows({"state": state}, expected_rows)


def test_keyboard_for_session_state_removes_keyboard_while_waiting_for_overlay_title_ru():
    markup = handlers_state_scene.keyboard_for_session_state(
        {"state": STATE_WAITING_OVERLAY_TITLE_RU}
    )

    assert markup.__class__.__name__ == "ReplyKeyboardRemove"


@pytest.mark.parametrize(
    "state",
    [
        STATE_WAITING_ADVERTISING_CUSTOM_CAMPAIGN,
        STATE_GENERATING_ADVERTISING_POST,
        STATE_WAITING_ADVERTISING_POST_TEXT_FEEDBACK,
        STATE_WAITING_ADVERTISING_POST_IMAGE_FEEDBACK,
    ],
)
def test_advertising_free_text_and_generation_states_remove_keyboard(state):
    markup = handlers_state_scene.keyboard_for_session_state({"state": state})
    assert markup.__class__.__name__ == "ReplyKeyboardRemove"


@pytest.mark.parametrize(
    "state",
    [
        STATE_REFINING_CONCEPT,
        STATE_REFINING_RECIPE,
        STATE_REFINING_SCENES,
        STATE_REFINING_FRAME_PROMPTS,
        STATE_REFINING_PRODUCTION_BUNDLE,
        handlers_state_media.STATE_REFINING_SOCIAL_DESCRIPTIONS,
    ],
)
def test_keyboard_for_session_state_removes_keyboard_for_background_refining_states(state):
    markup = handlers_state_scene.keyboard_for_session_state({"state": state})

    assert markup.__class__.__name__ == "ReplyKeyboardRemove"


def test_keyboard_for_session_state_has_exact_rows_for_concept_choice():
    _assert_rows(
        {
            "state": STATE_WAITING_CONCEPT,
            "concepts": [{"title": "1"}, {"title": "2"}, {"title": "3"}],
        },
        [["1", "2"], ["3"], ["Добавь"], ["Исправь"], ["В начало"]],
    )


def test_keyboard_for_session_state_has_exact_rows_for_series_choice():
    _assert_rows(
        {"state": STATE_WAITING_SERIES_CHOICE, "series_choice_ids": ["a", "b"]},
        [["1"], ["2"], ["Создать серию"], ["Без серии"]],
    )


def test_keyboard_for_session_state_has_exact_rows_for_project_image_characters():
    _assert_rows(
        {
            "state": "state_waiting_first_step_project_image_characters",
            "first_step_project_image_characters": ["grandma", "cat"],
        },
        [
            ["✓ grandma", "✓ cat"],
            ["daughter", "dog"],
            ["granddaughter", "grandfather"],
            ["grandson", "markiz"],
            ["zaya"],
            ["Добавить свое"],
            ["больше никто не нужен"],
            ["В начало"],
        ],
    )


def test_keyboard_for_session_state_has_exact_rows_for_draft_scene_selection():
    _assert_rows(
        {
            "state": "state_waiting_draft_scenes",
            "production": {"json_plan": [{}, {}, {}]},
            "draft_repair": {
                "selected_scene_indexes": [0, 2],
                "plan": [{"scene_index": 1, "languages": ["ru"], "stage": "video"}],
            },
        },
        [
            ["✓ Сцена 1", "Сцена 2", "✓ Сцена 3"],
            ["Все"],
            ["Далее"],
            ["Добавить сцену", "Начать правку"],
            ["В начало"],
        ],
    )


def test_keyboard_for_session_state_delegates_scene_media_contract(monkeypatch):
    monkeypatch.setattr(handlers_state_media, "_can_escalate_scene_image_model", lambda *_args: False)
    session = {
        "state": STATE_WAITING_SCENE_MEDIA,
        "scene_media_substep": "video_prompt",
        "target_languages": ["ru", "en"],
        "production": {"json_plan": [{"video_prompt_by_lang": {"ru": "ru", "en": "en"}}]},
    }

    snapshot = handlers_state_media.scene_media_keyboard_snapshot(session)

    assert _rows(handlers_state_scene.keyboard_for_session_state(session)) == [
        list(row) for row in snapshot.rows
    ]


def test_keyboard_for_session_state_has_social_descriptions_rows_without_pending_copy():
    _assert_rows(
        {"state": handlers_state_media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL},
        [["ОК"], ["ФИДБЕК"], ["Исправь"], ["В начало"]],
    )


def test_guest_keyboard_contract_has_isolated_main_menu():
    assert _rows(guest_keyboard_for_session({"state": GUEST_STATE_MENU})) == [
        ["Добавить персонажа"],
        ["Генерация изображения"],
        ["Генерация видео"],
        ["Генерация мультфильма"],
        ["Мои генерации"],
    ]
