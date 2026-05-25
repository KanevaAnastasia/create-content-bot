from __future__ import annotations

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

FIRST_STEP_BUTTON_GENERATE = "Генерировать"
FIRST_STEP_BUTTON_DESCRIPTION = "Описание"
FIRST_STEP_BUTTON_IMAGE = "Изображение"
FIRST_STEP_BUTTON_PROJECT_IMAGE = "Изображение проект"
FIRST_STEP_BUTTON_VIDEO = "Видео"
FIRST_STEP_BUTTON_SOUND = "Звук"
FIRST_STEP_BUTTON_PROJECT = "Проект"


def first_step_menu_markup() -> ReplyKeyboardMarkup:
    """Кнопки первого шага: выбор режима генерации."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [FIRST_STEP_BUTTON_GENERATE, FIRST_STEP_BUTTON_DESCRIPTION],
            [FIRST_STEP_BUTTON_IMAGE, FIRST_STEP_BUTTON_PROJECT_IMAGE],
            [FIRST_STEP_BUTTON_VIDEO, FIRST_STEP_BUTTON_SOUND],
            [FIRST_STEP_BUTTON_PROJECT],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def remove_keyboard_markup() -> ReplyKeyboardRemove:
    """Скрывает старые кнопки, когда от пользователя сейчас не ждём действия."""
    return ReplyKeyboardRemove()


async def reply_text_without_keyboard(message, text: str) -> None:
    """Отправляет статус и убирает текущую клавиатуру; безопасно для простых тестовых стабов."""
    try:
        await message.reply_text(text, reply_markup=remove_keyboard_markup())
    except TypeError:
        await message.reply_text(text)
