import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers import handlers_state_commands as commands
from app.handlers import handlers_state_scene as scene
from app.handlers import handlers_state_start as generation


def test_rejected_advice_start_then_project_opens_project_menu(monkeypatch):
    chat_id = 987650
    production = {"json_plan": [{"scene": 1}]}
    session = {
        "state": generation.STATE_WAITING_GENERATION_MODE,
        commands.PROJECT_KEY_SESSION: "discipline",
        "generation_mode": "normal",
        "production": production,
    }
    sessions = {chat_id: session}
    for module in (commands, scene, generation):
        monkeypatch.setattr(module, "SESSIONS", sessions)
    monkeypatch.setattr(commands, "_elevenlabs_voice_warning", lambda: None)
    bumped = []
    monkeypatch.setattr(commands, "_bump_debounce_generation", bumped.append)
    monkeypatch.setattr(scene, "load_session_if_possible", lambda _: dict(session))
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock(), text="Проект"),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    asyncio.run(generation._handle_waiting_generation_mode_state(
        update, None, chat_id=chat_id, session=session, text="Совет",
    ))
    assert session["state"] == generation.STATE_WAITING_GENERATION_MODE
    asyncio.run(commands.start(update, None))
    assert session["state"] == ""
    assert session["production"] is production
    assert session[commands.PROJECT_KEY_SESSION] == "discipline"
    assert bumped == [chat_id]
    monkeypatch.setattr(scene, "_debounced_session", AsyncMock(return_value=(chat_id, session, "Проект")))
    asyncio.run(scene._on_text_impl(update, None))
    assert sessions[chat_id]["state"] == scene._STATE_WAITING_FIRST_STEP_PROJECT_SELECTION
    assert "Выберите проект" in update.message.reply_text.call_args.args[0]
