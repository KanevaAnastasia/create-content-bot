import asyncio
from types import SimpleNamespace

from app.handlers.handlers_state_commands import (
    FIRST_STEP_BUTTON_DESCRIPTION,
    FIRST_STEP_BUTTON_GENERATE,
    FIRST_STEP_BUTTON_IMAGE,
    FIRST_STEP_BUTTON_PROJECT,
    FIRST_STEP_BUTTON_VIDEO,
    _handle_video_provider_choice_pending_video_command,
    _regenerate_image_prompt_from_feedback,
    _handle_syntax_fallback_pending_video_command,
    check_env,
    first_step_menu_markup,
    start,
)


def test_check_env_reports_syntax_and_telethon_status(monkeypatch, tmp_path):
    session_path = tmp_path / "syntax.session"
    session_path.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("SYNTAX_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_SESSION_PATH", str(session_path))
    captured: list[str] = []

    class _Message:
        async def reply_text(self, text):
            captured.append(text)

    update = SimpleNamespace(message=_Message())
    asyncio.run(check_env(update, context=None))
    assert captured
    text = captured[0]
    assert "SYNTAX_FALLBACK_ENABLED: set" in text
    assert "TELEGRAM_API_ID: set" in text
    assert "TELETHON_SESSION_FILE_EXISTS: yes" in text


def test_first_step_menu_markup_contains_expected_buttons():
    markup = first_step_menu_markup()
    assert markup is not None
    buttons = [btn.text for row in markup.keyboard for btn in row]
    assert FIRST_STEP_BUTTON_GENERATE in buttons
    assert FIRST_STEP_BUTTON_DESCRIPTION in buttons
    assert FIRST_STEP_BUTTON_IMAGE in buttons
    assert FIRST_STEP_BUTTON_VIDEO in buttons
    assert FIRST_STEP_BUTTON_PROJECT in buttons


def test_start_sends_first_step_menu():
    captured: list[tuple[str, object | None]] = []

    class _Message:
        async def reply_text(self, text, reply_markup=None):
            captured.append((text, reply_markup))

    update = SimpleNamespace(message=_Message())
    asyncio.run(start(update, context=None))
    assert captured
    assert "Выберите первый шаг кнопкой" in captured[0][0]
    assert "Описание" in captured[0][0]
    assert captured[0][1] is not None


def test_pending_video_command_waits_for_clear_yes_no():
    captured: list[str] = []

    class _Message:
        async def reply_text(self, text):
            captured.append(text)

    update = SimpleNamespace(message=_Message())
    session = {"syntax_fallback_pending": {"source": "video_command"}}
    out = asyncio.run(
        _handle_syntax_fallback_pending_video_command(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="возможно",
        )
    )
    assert out is True
    assert "Напишите «да»" in captured[0]


def test_pending_video_command_yes_runs_syntax_fallback(monkeypatch):
    captured: list[str] = []
    sent_video = {"ok": False}

    class _Message:
        async def reply_text(self, text):
            captured.append(text)

        async def reply_video(self, video, caption):
            assert video is not None
            assert "Syntax fallback" in caption
            sent_video["ok"] = True

    async def _fake_run(_payload):
        return b"mp4"

    monkeypatch.setattr(
        "app.handlers.handlers_state_commands.run_syntax_fallback_from_session_payload",
        _fake_run,
    )
    update = SimpleNamespace(message=_Message())
    session = {
        "syntax_fallback_pending": {
            "source": "video_command",
            "duration_for_caption": 6,
            "prompt": "x",
            "vertex_error_message": "policy",
            "start_image_b64": "eA==",
        }
    }
    out = asyncio.run(
        _handle_syntax_fallback_pending_video_command(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="да",
        )
    )
    assert out is True
    assert sent_video["ok"] is True
    assert "syntax_fallback_pending" not in session


def test_pending_video_provider_choice_waits_for_clear_choice():
    captured: list[str] = []

    class _Message:
        async def reply_text(self, text):
            captured.append(text)

    update = SimpleNamespace(message=_Message())
    session = {"video_provider_choice_pending": {"source": "video_command"}}
    out = asyncio.run(
        _handle_video_provider_choice_pending_video_command(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="может быть",
        )
    )
    assert out is True
    assert "Syntax" in captured[0]
    assert "Vertex" in captured[0]


def test_pending_video_provider_choice_vertex_runs_vertex(monkeypatch):
    captured: list[str] = []
    sent_video = {"ok": False}

    class _Message:
        async def reply_text(self, text):
            captured.append(text)

        async def reply_video(self, video, caption):
            assert video is not None
            assert "Vertex fallback" in caption
            sent_video["ok"] = True

    async def _fake_run(_payload):
        return b"mp4"

    monkeypatch.setattr(
        "app.handlers.handlers_state_commands.run_vertex_fallback_from_session_payload",
        _fake_run,
    )
    update = SimpleNamespace(message=_Message())
    session = {
        "video_provider_choice_pending": {
            "source": "video_command",
            "duration_for_caption": 6,
            "vertex_duration_sec": 8,
            "prompt": "x",
            "start_image_b64": "eA==",
        }
    }
    out = asyncio.run(
        _handle_video_provider_choice_pending_video_command(
            update,
            context=None,
            chat_id=1,
            session=session,
            text="Vertex",
        )
    )
    assert out is True
    assert sent_video["ok"] is True
    assert "video_provider_choice_pending" not in session


def test_regenerate_image_prompt_from_feedback_uses_gemini(monkeypatch):
    captured: list[str] = []

    def _fake_gemini(prompt: str) -> str:
        captured.append(prompt)
        return "Новый промпт"

    monkeypatch.setattr(
        "app.handlers.handlers_state_commands.gemini_generate_sync",
        _fake_gemini,
    )
    scene = {"location": "kitchen", "shot_type": "medium", "start_frame_prompt": "старый"}
    out = asyncio.run(_regenerate_image_prompt_from_feedback(scene, "добавь пар от еды"))
    assert out == "Новый промпт"
    assert captured
