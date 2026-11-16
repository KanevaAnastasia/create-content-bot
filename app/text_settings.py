"""Persistent administrator text model selection, isolated from guest workflows."""
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
import re
import tempfile

LEGACY_GUEST_TEXT = ContextVar("legacy_guest_text", default=False)
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "runtime" / "text_model.json"
_MODEL_ID = re.compile(r"gpt-[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


def guest_text_context(callback):
    @wraps(callback)
    async def wrapped(update, context, identity):
        token = LEGACY_GUEST_TEXT.set(identity.role != "admin")
        try:
            return await callback(update, context, identity)
        finally:
            LEGACY_GUEST_TEXT.reset(token)
    return wrapped


def selected_text_model() -> str | None:
    if LEGACY_GUEST_TEXT.get():
        return None
    try:
        value = json.loads(SETTINGS_PATH.read_text())["model"]
    except FileNotFoundError:
        return None
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise ValueError("Invalid saved OpenAI text model")
    return value


def save_text_model(model: str) -> None:
    if not _MODEL_ID.fullmatch(model):
        raise ValueError("Укажите идентификатор текстовой модели OpenAI, например gpt-6-astra.")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=SETTINGS_PATH.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump({"model": model}, file)
    try:
        os.replace(temporary, SETTINGS_PATH)
    finally:
        temporary.unlink(missing_ok=True)


async def text_model_command(update, context):
    args = context.args or []
    if args:
        if len(args) != 1:
            await update.effective_message.reply_text("Формат: /text_model gpt-6-astra")
            return
        try:
            save_text_model(args[0])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
    from .llm_utils import text_llm_provider
    provider = text_llm_provider()
    model = selected_text_model() or (os.getenv("OPENAI_MODEL", "gpt-6-astra") if provider == "openai" else os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    await update.effective_message.reply_text(
        f"Тексты основных проектов: {provider} · {model}.\n"
        "Сменить: /text_model <идентификатор модели GPT>\n"
        "Например: /text_model gpt-6-astra\n"
        "Другой вариант: /text_model gpt-6.1-sol\n"
        "Выбор общий для основных проектов и сохраняется после перезапуска. "
        "Модель должна быть доступна API-аккаунту; доступ проверяется при генерации. "
        "Гостевые тексты и модели изображений этой командой не меняются."
    )
