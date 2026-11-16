import asyncio
import base64
import sys
import threading
from types import ModuleType

import pytest
from app.syntax_video_fallback import BatchFallbackRequest, FallbackResult, try_generate_via_syntax, try_generate_via_syntax_batch

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQBAAAAAQAAVD4F2kAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _skip_syntax_frame_resize(monkeypatch):
    """Keep Telethon transport tests independent from optional Pillow."""
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setattr(telethon_fallback, "prepare_syntax_frame_bytes", lambda data: data)
    async def _fake_configure_settings(_client, _target, **kwargs):
        return f"settings ok {kwargs.get('aspect_ratio')}"

    monkeypatch.setattr(telethon_fallback, "_try_configure_syntax_model_settings", _fake_configure_settings)


def _syntax_test_frame_bytes() -> bytes:
    return _ONE_PIXEL_PNG


def test_process_bot_message_times_out_hung_video_download(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        reply_to_msg_id = 100

    class _Message:
        id = 200
        out = False
        reply_to = _ReplyTo()
        video = object()

    class _Client:
        async def download_media(self, _message, file):
            assert file is bytes
            await asyncio.sleep(60)
            return b"late"

    monkeypatch.setattr(telethon_fallback, "_syntax_media_download_timeout_sec", lambda: 0.01)

    result = asyncio.run(
        telethon_fallback._process_bot_message(
            _Client(),
            message=_Message(),
            sent_ids={100},
            expected_lang_token=None,
        )
    )

    assert result == (
        None,
        None,
        "Syntax sent a video attachment but download failed or timed out.",
        True,
        False,
    )


def test_prepare_long_prompt_uses_local_fallback_when_gemini_fails(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    prompt = (
        "lang:en A grandmother pours orange juice into molds. "
        + "Detailed camera and visual instruction. " * 100
        + "voice_lock_full: elderly English female voice. "
        + "CHARACTERS SPEAKS: We freeze it and insert the sticks."
    )
    monkeypatch.setattr(
        telethon_fallback,
        "_shorten_prompt_for_syntax_with_gemini_sync",
        lambda *_args, **_kwargs: None,
    )

    prepared = telethon_fallback._prepare_prompt_for_syntax(prompt, max_len=2000)

    assert prepared is not None
    shortened, was_shortened = prepared
    assert was_shortened is True
    assert len(shortened) <= 2000
    assert shortened.startswith("lang:en")
    assert "voice_lock_full: elderly English female voice." in shortened
    assert "CHARACTERS SPEAKS: We freeze it and insert the sticks." in shortened


def test_prepare_empty_prompt_for_syntax_fails_before_dispatch():
    import app.syntax_telethon_fallback as telethon_fallback

    assert telethon_fallback._prepare_prompt_for_syntax("   ", max_len=2000) is None


def test_expected_lang_token_from_prompt_finds_token_anywhere():
    import app.syntax_telethon_fallback as telethon_fallback

    assert telethon_fallback._expected_lang_token_from_prompt("Render lang:en scene") == "lang:en"
    assert telethon_fallback._expected_lang_token_from_prompt("Render LANG=RU scene") == "lang:ru"
    assert telethon_fallback._expected_lang_token_from_prompt("Render scene") is None


def test_syntax_kling_submission_state_detects_started_text():
    import app.syntax_telethon_fallback as telethon_fallback

    assert telethon_fallback._syntax_kling_submission_state_from_text("Видео добавлено в очередь") == "started"
    assert telethon_fallback._syntax_kling_submission_state_from_text("Generating video, please wait") == "started"
    assert (
        telethon_fallback._syntax_kling_submission_state_from_text(
            "Достигнут лимит очереди Veo. Эта генерация уже отправлена"
        )
        == "started"
    )


def test_syntax_submission_change_ignores_static_queue_warning():
    import app.syntax_telethon_fallback as telethon_fallback

    before = "Настройки Veo Достигнут лимит очереди Veo. Максимум активных генераций: 2."
    assert telethon_fallback._syntax_kling_submission_state_after_change(before, before + " Обновлено") == ""
    assert (
        telethon_fallback._syntax_kling_submission_state_after_change(
            before,
            before + " Эта генерация уже отправлена",
        )
        == "started"
    )
    assert (
        telethon_fallback._syntax_kling_submission_state_after_change(
            before,
            before + " Достигнут лимит очереди Veo.",
        )
        == "blocked"
    )


def test_syntax_kling_submission_state_detects_blocked_text_first():
    import app.syntax_telethon_fallback as telethon_fallback

    assert (
        telethon_fallback._syntax_kling_submission_state_from_text(
            "Ошибка: недостаточно кредитов для генерации"
        )
        == "blocked"
    )
    assert telethon_fallback._syntax_kling_submission_state_from_text("Ready to generate") == ""


def test_click_syntax_kling_generate_waits_for_livewire_submit_response(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        method = "POST"

    class _Response:
        request = _Request()
        url = "https://bot.syntx.ai/livewire/update"
        status = 200

    class _Locator:
        def __init__(self, page):
            self.page = page

        def click(self, timeout=None):
            _ = timeout
            self.page.response_callback(_Response())
            self.page.response_callback(_Response())

    class _Page:
        response_callback = lambda *_args: None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, _timeout_ms):
            return None

        def on(self, event, callback):
            assert event == "response"
            self.response_callback = callback

        def locator(self, selector):
            assert selector == "#btn_submit"
            return _Locator(self)

        def wait_for_timeout(self, _timeout_ms):
            return None

    monkeypatch.setattr(
        telethon_fallback,
        "_syntax_kling_submit_snapshot_sync",
        lambda _page: {"text": "Настройте параметры генерации", "generateButtons": [{"disabled": False}]},
    )

    telethon_fallback._click_syntax_kling_generate_sync(_Page())


def test_click_syntax_kling_generate_waits_past_processing_button_for_parent_response(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        method = "POST"

    class _Response:
        request = _Request()
        url = "https://bot.syntx.ai/livewire/update"
        status = 200

    class _Locator:
        def __init__(self, page):
            self.page = page

        def click(self, timeout=None):
            _ = timeout

    class _Page:
        response_callback = lambda *_args: None
        waits = 0

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def on(self, event, callback):
            assert event == "response"
            self.response_callback = callback

        def locator(self, _selector):
            return _Locator(self)

        def wait_for_timeout(self, _timeout_ms):
            self.waits += 1
            if self.waits == 2:
                self.response_callback(_Response())
                self.response_callback(_Response())

    snapshots = [
        {
            "text": "Настройки Kling",
            "generateButtons": [{"text": "Сгенерировать", "disabled": False}],
        },
        {
            "text": "Настройки Kling",
            "generateButtons": [{"text": "Сгенерировать Обработка...", "disabled": False}],
        },
    ]
    calls = {"count": 0}

    def _snapshot(_page):
        calls["count"] += 1
        return snapshots[0] if calls["count"] == 1 else snapshots[1]

    monkeypatch.setattr(
        telethon_fallback,
        "_syntax_kling_submit_snapshot_sync",
        _snapshot,
    )

    telethon_fallback._click_syntax_kling_generate_sync(_Page())


def test_click_syntax_kling_generate_keeps_waiting_for_video_after_processing_button(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _Locator:
        def click(self, timeout=None):
            _ = timeout

    class _Page:
        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

        def locator(self, _selector):
            return _Locator()

        def wait_for_timeout(self, _timeout_ms):
            return None

    snapshots = {"count": 0}

    def _snapshot(_page):
        snapshots["count"] += 1
        button = "Сгенерировать" if snapshots["count"] == 1 else "Сгенерировать Обработка..."
        return {
            "text": "Настройки Kling",
            "generateButtons": [{"text": button, "disabled": False}],
        }

    monkeypatch.setattr(telethon_fallback, "_syntax_kling_submit_snapshot_sync", _snapshot)
    monkeypatch.setattr(telethon_fallback, "_syntax_kling_submit_confirm_timeout_sec", lambda: 0.001)

    telethon_fallback._click_syntax_kling_generate_sync(_Page())


def test_click_syntax_kling_generate_does_not_accept_static_generation_text(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _Locator:
        def click(self, timeout=None):
            _ = timeout

    class _Page:
        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

        def locator(self, _selector):
            return _Locator()

        def wait_for_timeout(self, _timeout_ms):
            return None

    monkeypatch.setattr(
        telethon_fallback,
        "_syntax_kling_submit_snapshot_sync",
        lambda _page: {"text": "Настройте параметры генерации", "generateButtons": [{"disabled": False}]},
    )
    monkeypatch.setattr(telethon_fallback, "_syntax_kling_submit_confirm_timeout_sec", lambda: 0.001)

    with pytest.raises(RuntimeError, match="did not confirm generation start"):
        telethon_fallback._click_syntax_kling_generate_sync(_Page())


def test_prompt_failure_does_not_acquire_syntax_session(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    acquired: list[bool] = []
    monkeypatch.setattr(telethon_fallback, "_prepare_prompt_for_syntax", lambda _prompt: None)
    monkeypatch.setattr(
        telethon_fallback,
        "_acquire_syntax_session_candidate",
        lambda _timeout: acquired.append(True),
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")

    result = try_generate_via_syntax(
        prompt="long prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
    )

    assert result.success is False
    assert acquired == []


def test_empty_prompt_does_not_acquire_syntax_session(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    acquired: list[bool] = []
    monkeypatch.setattr(
        telethon_fallback,
        "_acquire_syntax_session_candidate",
        lambda _timeout: acquired.append(True),
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")

    result = try_generate_via_syntax(
        prompt="   ",
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
    )

    assert result.success is False
    assert acquired == []
    assert "Syntax prompt is empty" in result.provider_message


def test_fallback_result_accepts_bytes_payload():
    result = FallbackResult(
        success=True,
        video_bytes=b"mp4-bytes",
        video_uri=None,
        provider_message="ok",
        attempt_id="attempt-1",
    )
    assert result.success is True
    assert result.video_bytes == b"mp4-bytes"
    assert result.video_uri is None


def test_fallback_result_accepts_uri_payload():
    result = FallbackResult(
        success=True,
        video_bytes=None,
        video_uri="gs://bucket/video.mp4",
        provider_message="ok",
        attempt_id="attempt-2",
    )
    assert result.success is True
    assert result.video_bytes is None
    assert result.video_uri == "gs://bucket/video.mp4"


def test_try_generate_via_syntax_without_telethon_dependency_returns_failure():
    result = try_generate_via_syntax(
        prompt="video prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=b"start-image",
    )
    assert result.success is False
    assert result.video_bytes is None
    assert result.video_uri is None
    assert result.provider_message
    assert result.attempt_id


def test_try_generate_via_syntax_telethon_happy_path(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        def __init__(self, msg_id: int, reply_to: int):
            self.id = msg_id
            self.out = False
            self.reply_to = _ReplyTo(reply_to)
            self.raw_text = "done"
            self.message = self.raw_text
            self.entities = []
            self.video = object()
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id):
            self.id = msg_id

    class _Entity:
        id = 999

    call_order: list[str] = []
    file_count = {"value": 0}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self._iter_once = False

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, _text, reply_to=None):
            call_order.append(f"send_message:{_text}")
            assert len(_text) <= 2000
            if _text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90 + len(call_order))
            assert reply_to is not None
            return _SentMessage(103)

        async def send_file(self, _target, file, caption=None, force_document=False):
            call_order.append("send_file")
            assert getattr(file, "name", "") in ("start_frame.png", "end_frame.png")
            assert caption is None
            assert force_document is False
            file_count["value"] += 1
            if file_count["value"] == 1:
                return _SentMessage(101)
            return _SentMessage(102)

        async def download_media(self, _message, file):
            assert file is bytes
            return b"mp4-content"

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=30):
            del limit
            if self._iter_once:
                return
            self._iter_once = True
            yield _IncomingMessage(200, reply_to=103)

    async def _noop_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "fixed-attempt"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")
    monkeypatch.setenv("SYNTAX_BOT_USERNAME", "@syntxaibot")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
        end_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
        aspect_ratio="16:9",
    )
    assert result.success is True
    assert result.video_bytes == b"mp4-content"
    assert result.video_uri is None
    assert result.attempt_id == "fixed-attempt"
    assert call_order[:6] == [
        "send_message:/video",
        "send_message:Veo",
        "send_message:Сгенерировать",
        "send_file",
        "send_file",
        "send_message:video prompt",
    ]


def test_try_generate_via_syntax_kling_uses_configure_ui_flow(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    events: list[tuple[str, str | None, int | None]] = []
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, text, reply_to=None):
            events.append(("send_message", str(text), reply_to))
            assert reply_to is None
            return _SentMessage(100 + len(events))

        async def send_file(self, _target, file, caption=None, force_document=False):
            del caption, force_document
            events.append(("send_file", getattr(file, "name", ""), None))
            return _SentMessage(103)

    async def _noop_sleep(_sec: float) -> None:
        return None

    async def _fake_kling_flow(_client, _target, **kwargs):
        captured.update(kwargs)
        return telethon_fallback._result(
            success=True,
            video_bytes=b"kling-mp4",
            video_uri=None,
            provider_message="kling ok",
            attempt_id=str(kwargs["attempt_id"]),
        )

    async def _no_configure_button(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "kling-attempt"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(telethon_fallback, "_run_syntax_kling_configure_flow", _fake_kling_flow)
    monkeypatch.setattr(telethon_fallback, "_wait_for_syntax_configure_button", _no_configure_button)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="kling_3_0",
        duration_sec=3,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
        syntax_provider="kling",
    )

    assert result.success is True
    assert result.video_bytes == b"kling-mp4"
    assert events == [
        ("send_message", "/video", None),
        ("send_message", "Kling", None),
        ("send_file", "start_frame.png", None),
    ]
    assert captured["prompt"] == "video prompt"
    assert captured["duration_sec"] == 3
    assert captured["aspect_ratio"] == "9:16"
    assert captured["reply_anchor"] == 103


def test_try_generate_via_syntax_veo_uses_mandatory_settings_webview(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    class _ConfigureButton:
        text = "Настроить и запустить"

    class _ConfigureMessage:
        id = 200
        out = False
        raw_text = "Промпт сохранён ✅"
        buttons = [[_ConfigureButton()]]

    events: list[tuple[str, str | None, int | None]] = []
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, text, reply_to=None):
            events.append(("send_message", str(text), reply_to))
            return _SentMessage(100 + len(events))

        async def send_file(self, _target, file, caption=None, force_document=False):
            del caption, force_document
            events.append(("send_file", getattr(file, "name", ""), None))
            return _SentMessage(110)

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return _ConfigureMessage()

    async def _noop_sleep(_sec: float) -> None:
        return None

    async def _fake_veo_flow(_client, _target, **kwargs):
        captured.update(kwargs)
        return telethon_fallback._result(
            success=True,
            video_bytes=b"veo-mp4",
            video_uri=None,
            provider_message="veo settings ok",
            attempt_id=str(kwargs["attempt_id"]),
        )

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "veo-settings-attempt"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(telethon_fallback, "_run_syntax_veo_configure_flow", _fake_veo_flow)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="veo3_fast",
        duration_sec=8,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
        syntax_provider="veo",
        aspect_ratio="16:9",
    )

    assert result.success is True
    assert result.video_bytes == b"veo-mp4"
    assert captured["prompt"] == "video prompt"
    assert captured["duration_sec"] == 8
    assert captured["aspect_ratio"] == "16:9"
    assert captured["configure_message"].id == 200
    assert events == [
        ("send_message", "/video", None),
        ("send_message", "Veo", None),
        ("send_message", "Сгенерировать", None),
        ("send_file", "start_frame.png", None),
        ("send_message", "video prompt", 110),
    ]


def test_veo_waits_for_prompt_saved_configure_button_instead_of_file_button(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _Button:
        text = "Настроить и запустить"

    class _Message:
        out = False
        buttons = [[_Button()]]

        def __init__(self, msg_id: int, text: str):
            self.id = msg_id
            self.raw_text = text

    file_message = _Message(201, "Файл сохранён ✅")
    prompt_message = _Message(202, "Промпт сохранён ✅")

    class _Client:
        calls = 0

        async def get_messages(self, _target, limit=None):
            assert limit == 5
            self.calls += 1
            return [file_message] if self.calls == 1 else [prompt_message, file_message]

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _no_sleep)
    message, button = asyncio.run(
        telethon_fallback._wait_for_latest_syntax_configure_button(
            _Client(),
            object(),
            reply_anchor=200,
            poll=0.5,
            require_prompt_saved=True,
        )
    )

    assert message is prompt_message
    assert button is prompt_message.buttons[0][0]


def test_syntax_veo_webview_helpers_read_uploaded_count_and_queue_limit():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Body:
        def inner_text(self):
            return "Загружено (1/2)\nДостигнут лимит очереди Veo. Максимум активных генераций: 2."

    class _Page:
        def locator(self, selector):
            assert selector == "body"
            return _Body()

    page = _Page()
    assert telethon_fallback._syntax_veo_uploaded_reference_count_sync(page) == 1
    assert telethon_fallback._syntax_veo_queue_is_full_sync(page) is True


def test_syntax_veo_submit_clicks_before_waiting_and_retries_real_queue_rejection(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[str] = []

    def click(_page, *, timeout_ms, wait_for_form_ready):
        calls.append(f"click:{timeout_ms}:ready={wait_for_form_ready}")
        if len([call for call in calls if call.startswith("click:")]) == 1:
            raise telethon_fallback.SyntaxVeoQueueFullError("queue full")
        return "started"

    monkeypatch.setattr(telethon_fallback, "_click_syntax_kling_generate_sync", click)
    monkeypatch.setattr(
        telethon_fallback,
        "_wait_for_syntax_veo_queue_slot_sync",
        lambda _page, *, timeout_ms: calls.append(f"wait:{timeout_ms}"),
    )

    result = telethon_fallback._submit_syntax_veo_generate_sync(object(), timeout_ms=120_000)

    assert result == "started"
    assert calls == [
        "click:15000:ready=False",
        "wait:120000",
        "click:15000:ready=False",
    ]


def test_syntax_veo_submit_does_not_wait_when_form_was_already_sent(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    waits: list[int] = []
    monkeypatch.setattr(
        telethon_fallback,
        "_click_syntax_kling_generate_sync",
        lambda _page, *, timeout_ms, wait_for_form_ready: (
            "started" if timeout_ms == 15_000 and wait_for_form_ready is False else "unexpected"
        ),
    )
    monkeypatch.setattr(
        telethon_fallback,
        "_wait_for_syntax_veo_queue_slot_sync",
        lambda _page, *, timeout_ms: waits.append(timeout_ms),
    )

    assert telethon_fallback._submit_syntax_veo_generate_sync(object(), timeout_ms=120_000) == "started"
    assert waits == []


def test_syntax_veo_clears_all_stale_uploaded_references():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Body:
        def __init__(self, page):
            self.page = page

        def inner_text(self):
            return f"Загружено ({self.page.uploaded}/2)"

    class _Button:
        def __init__(self, page, index):
            self.page = page
            self.index = index

        def is_visible(self):
            return self.index < self.page.uploaded

        def click(self, timeout=None):
            del timeout
            self.page.uploaded -= 1

    class _Buttons:
        def __init__(self, page):
            self.page = page

        def count(self):
            return self.page.uploaded

        def nth(self, index):
            return _Button(self.page, index)

    class _Page:
        uploaded = 2

        def locator(self, selector):
            if selector == "body":
                return _Body(self)
            assert selector == "button[aria-label='Remove file']"
            return _Buttons(self)

        def wait_for_timeout(self, _ms):
            return None

    page = _Page()
    telethon_fallback._clear_syntax_veo_uploaded_references_sync(page)
    assert page.uploaded == 0


def test_syntax_veo_reloads_and_retries_stale_remove_counter(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    wait_calls: list[int] = []

    def _flaky_wait(_page, expected, *, timeout_ms):
        del timeout_ms
        wait_calls.append(expected)
        if len(wait_calls) == 1:
            raise RuntimeError("stale Livewire counter")

    class _Body:
        def __init__(self, page):
            self.page = page

        def inner_text(self):
            return f"Загружено ({self.page.uploaded}/2)"

    class _Button:
        def is_visible(self):
            return True

        def click(self, timeout=None):
            del timeout

    class _Buttons:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return _Button()

    class _Page:
        uploaded = 1
        reloads = 0

        def locator(self, selector):
            if selector == "body":
                return _Body(self)
            assert selector == "button[aria-label='Remove file']"
            return _Buttons()

        def reload(self, *, wait_until, timeout):
            assert wait_until == "commit"
            assert timeout >= 30_000
            self.reloads += 1
            self.uploaded = 0

        def wait_for_load_state(self, state, *, timeout):
            assert state == "networkidle"
            assert timeout >= 15_000

    monkeypatch.setattr(telethon_fallback, "_wait_for_syntax_veo_uploaded_count_sync", _flaky_wait)

    page = _Page()
    telethon_fallback._clear_syntax_veo_uploaded_references_sync(page)

    assert wait_calls == [0]
    assert page.reloads == 1
    assert page.uploaded == 0


def test_try_generate_via_syntax_kling_passes_horizontal_aspect_ratio(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, _text, reply_to=None):
            assert reply_to is None
            return _SentMessage(100)

        async def send_file(self, _target, file, caption=None, force_document=False):
            del file, caption, force_document
            return _SentMessage(103)

    async def _noop_sleep(_sec: float) -> None:
        return None

    async def _fake_kling_flow(_client, _target, **kwargs):
        captured.update(kwargs)
        return telethon_fallback._result(
            success=True,
            video_bytes=b"kling-mp4",
            video_uri=None,
            provider_message="kling ok",
            attempt_id=str(kwargs["attempt_id"]),
        )

    async def _no_configure_button(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "kling-attempt"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(telethon_fallback, "_run_syntax_kling_configure_flow", _fake_kling_flow)
    monkeypatch.setattr(telethon_fallback, "_wait_for_syntax_configure_button", _no_configure_button)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="kling_3_0",
        duration_sec=3,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
        syntax_provider="kling",
        aspect_ratio="16:9",
    )

    assert result.success is True
    assert captured["aspect_ratio"] == "16:9"


def test_set_syntax_aspect_ratio_uses_dom_fallback(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[str] = []

    monkeypatch.setattr(telethon_fallback, "_try_click_syntax_option_sync", lambda *_args, **_kwargs: False)

    def _dom_click(_page, ratio, **_kwargs):
        calls.append(ratio)
        return True

    monkeypatch.setattr(telethon_fallback, "_click_syntax_ratio_by_dom_sync", _dom_click)
    contains_calls: list[str] = []

    def _contains(_page, ratio):
        contains_calls.append(ratio)
        if len(contains_calls) <= 2:
            return True
        return ratio == "16:9"

    monkeypatch.setattr(telethon_fallback, "_syntax_page_contains_aspect_ratio_sync", _contains)

    class _Page:
        def wait_for_timeout(self, _ms):
            return None

    telethon_fallback._set_syntax_aspect_ratio_sync(_Page(), "16:9")

    assert calls == ["16:9"]


def test_click_syntax_option_accepts_already_selected_updated_ui():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Page:
        def evaluate(self, _script, payload=None):
            return bool(payload and payload.get("value") == "3.0")

        def get_by_label(self, _pattern):
            raise AssertionError("already-selected option must not be clicked")

        def locator(self, _selector):
            raise AssertionError("already-selected option must not be clicked")

        def get_by_text(self, _pattern):
            raise AssertionError("already-selected option must not be clicked")

    telethon_fallback._click_syntax_option_sync(_Page(), "Версия", "3.0")


def test_set_syntax_kling_duration_uses_range_slider_for_three_seconds(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[object] = []

    class _Page:
        def evaluate(self, _script, payload):
            calls.append(payload)
            return payload == {"duration": 3}

        def wait_for_timeout(self, timeout_ms):
            calls.append(timeout_ms)

    monkeypatch.setattr(
        telethon_fallback,
        "_try_click_syntax_option_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy select must not be used")),
    )

    selected = telethon_fallback._set_syntax_kling_duration_sync(_Page(), 3)

    assert selected == 3
    assert calls == [{"duration": 3}, 250]


def test_set_syntax_aspect_ratio_reports_visible_controls(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setattr(telethon_fallback, "_syntax_page_contains_aspect_ratio_sync", lambda _page, _ratio: False)
    monkeypatch.setattr(telethon_fallback, "_try_click_syntax_option_sync", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(telethon_fallback, "_click_syntax_ratio_by_dom_sync", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(telethon_fallback, "_syntax_visible_controls_snapshot_sync", lambda _page: "BUTTON Generate | BUTTON 9:16")

    try:
        telethon_fallback._set_syntax_aspect_ratio_sync(object(), "16:9")
    except RuntimeError as exc:
        assert "Syntax aspect ratio option was not set to 16:9" in str(exc)
        assert "BUTTON Generate" in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")


def test_fill_syntax_kling_webapp_does_not_set_aspect_ratio(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[tuple[str, str]] = []

    class _Fillable:
        value = "stale prompt"

        def fill(self, value, timeout=None):
            calls.append(("fill", str(value)))
            self.value = str(value)

        def input_value(self, timeout=None):
            return self.value

        def evaluate(self, _script):
            calls.append(("commit", "blur"))

    class _Locator:
        @property
        def first(self):
            return _Fillable()

    class _Page:
        def goto(self, *args, **kwargs):
            calls.append(("goto", str(args[0])))

        def wait_for_load_state(self, *args, **kwargs):
            calls.append(("load", str(args[0])))

        def wait_for_timeout(self, ms):
            calls.append(("wait", str(ms)))

        def get_by_label(self, _pattern):
            return _Fillable()

        def locator(self, _selector):
            return _Locator()

    class _Browser:
        def new_page(self, **_kwargs):
            return _Page()

        def close(self):
            calls.append(("close", ""))

    class _Chromium:
        def launch(self, **_kwargs):
            return _Browser()

    class _PlaywrightContext:
        def __enter__(self):
            return type("_Playwright", (), {"chromium": _Chromium()})()

        def __exit__(self, *_args):
            return False

    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _PlaywrightContext()
    package = ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    def _option(_page, label, value, **_kwargs):
        calls.append((label, value))

    monkeypatch.setattr(telethon_fallback, "_click_syntax_option_sync", _option)
    monkeypatch.setattr(
        telethon_fallback,
        "_set_syntax_kling_duration_sync",
        lambda _page, duration_sec, **_kwargs: calls.append(("Длительность", f"{duration_sec} сек")),
    )
    monkeypatch.setattr(
        telethon_fallback,
        "_set_syntax_aspect_ratio_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Kling must not set aspect ratio")),
    )
    monkeypatch.setattr(telethon_fallback, "_enable_syntax_audio_sync", lambda *_args, **_kwargs: calls.append(("audio", "on")))
    monkeypatch.setattr(telethon_fallback, "_click_syntax_kling_generate_sync", lambda *_args, **_kwargs: calls.append(("generate", "")))
    monkeypatch.setattr(telethon_fallback, "_wait_for_syntax_kling_form_ready_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        telethon_fallback,
        "_upload_syntax_kling_start_frame_sync",
        lambda _page, image, **_kwargs: calls.append(("upload", str(len(image)))),
    )

    telethon_fallback._fill_syntax_kling_webapp_sync(
        url="https://example.invalid/kling",
        prompt="prompt",
        start_image=b"\x89PNG\r\n\x1a\nframe",
        duration_sec=3,
        quality="720",
        aspect_ratio="16:9",
        version="3.0",
        audio_enabled=True,
        timeout_sec=5,
    )

    assert ("Версия", "3.0") in calls
    assert ("upload", "13") in calls
    assert ("fill", "prompt") in calls
    assert ("commit", "blur") in calls
    assert ("Качество", "720") in calls
    assert ("Длительность", "3 сек") in calls
    assert ("audio", "on") in calls
    assert ("generate", "") in calls


def test_fill_syntax_kling_webapp_retries_navigation_in_fresh_page(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[str] = []

    class _Page:
        def __init__(self, should_fail: bool):
            self.should_fail = should_fail

        def goto(self, *_args, **_kwargs):
            if self.should_fail:
                raise RuntimeError("net::ERR_SOCKET_NOT_CONNECTED")
            calls.append("goto-success")

        def close(self):
            calls.append("failed-page-closed")

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, _timeout_ms):
            return None

    failed_page = _Page(True)
    ready_page = _Page(False)

    class _Browser:
        def __init__(self):
            self.pages = iter((failed_page, ready_page))

        def new_page(self, **_kwargs):
            return next(self.pages)

        def close(self):
            calls.append("browser-closed")

    class _Chromium:
        def launch(self, **_kwargs):
            return _Browser()

    class _PlaywrightContext:
        def __enter__(self):
            return type("_Playwright", (), {"chromium": _Chromium()})()

        def __exit__(self, *_args):
            return False

    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _PlaywrightContext()
    package = ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(telethon_fallback.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(telethon_fallback, "_upload_syntax_kling_start_frame_sync", lambda _page, _image, **_kwargs: calls.append("upload"))
    monkeypatch.setattr(telethon_fallback, "_fill_and_commit_syntax_kling_prompt_sync", lambda page, _prompt: calls.append("prompt"))
    monkeypatch.setattr(telethon_fallback, "_click_syntax_option_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(telethon_fallback, "_set_syntax_kling_duration_sync", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(telethon_fallback, "_enable_syntax_audio_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(telethon_fallback, "_click_syntax_kling_generate_sync", lambda *_args, **_kwargs: calls.append("generate"))

    telethon_fallback._fill_syntax_kling_webapp_sync(
        url="https://example.invalid/kling",
        prompt="prompt",
        start_image=b"\x89PNG\r\n\x1a\nframe",
        duration_sec=3,
        quality="720",
        aspect_ratio="9:16",
        version="3.0",
        audio_enabled=True,
        timeout_sec=5,
    )

    assert calls == ["failed-page-closed", "goto-success", "upload", "prompt", "generate", "browser-closed"]


def test_disable_syntax_audio_explicitly_turns_off_checked_switch():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Control:
        def __init__(self):
            self.checked = True
            self.clicks = 0

        def get_attribute(self, name, timeout=None):
            _ = timeout
            if name == "aria-checked":
                return "true" if self.checked else "false"
            return None

        def click(self, timeout=None):
            _ = timeout
            self.clicks += 1
            self.checked = False

    control = _Control()

    class _Locator:
        @property
        def last(self):
            return control

        @property
        def first(self):
            return control

    class _Page:
        def get_by_role(self, _role, name=None):
            _ = name
            return _Locator()

        def locator(self, _selector):
            return _Locator()

        def wait_for_timeout(self, _ms):
            return None

    telethon_fallback._disable_syntax_audio_sync(_Page())

    assert control.clicks == 1
    assert control.checked is False


def test_disable_syntax_audio_does_not_toggle_already_disabled_switch():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Control:
        clicks = 0

        def get_attribute(self, name, timeout=None):
            _ = (name, timeout)
            return "false"

        def click(self, timeout=None):
            _ = timeout
            self.clicks += 1

    control = _Control()

    class _Locator:
        @property
        def last(self):
            return control

        @property
        def first(self):
            return control

    class _Page:
        def get_by_role(self, _role, name=None):
            _ = name
            return _Locator()

        def locator(self, _selector):
            return _Locator()

    telethon_fallback._disable_syntax_audio_sync(_Page())

    assert control.clicks == 0


def test_try_generate_via_syntax_batch_kling_runs_sequential_single_flows(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    calls: list[tuple[str, int, str]] = []

    async def _fake_single(**kwargs):
        calls.append((kwargs["prompt"], kwargs["duration_sec"], kwargs["syntax_provider"]))
        return telethon_fallback._result(
            success=True,
            video_bytes=f"mp4-{kwargs['prompt']}".encode(),
            video_uri=None,
            provider_message="ok",
            attempt_id=kwargs["attempt_id"],
        )

    monkeypatch.setattr(telethon_fallback, "_run_fallback_async", _fake_single)

    results = try_generate_via_syntax_batch(
        requests=[
            BatchFallbackRequest(key="ru", prompt="one", start_image=_syntax_test_frame_bytes()),
            BatchFallbackRequest(key="en", prompt="two", start_image=_syntax_test_frame_bytes()),
        ],
        model="kling_3_0",
        duration_sec=3,
        timeout_sec=5,
        acquire_timeout_sec=5,
        syntax_provider="kling",
    )

    assert calls == [("one", 3, "kling"), ("two", 3, "kling")]
    assert results["ru"].video_bytes == b"mp4-one"
    assert results["en"].video_bytes == b"mp4-two"


def test_try_generate_via_syntax_stops_when_veo_is_updating(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        id = 200
        out = False
        reply_to = _ReplyTo(102)
        raw_text = "Veo на обновлении, попробуйте позже"
        message = raw_text
        entities = []
        video = None
        document = None

    class _SentMessage:
        def __init__(self, msg_id):
            self.id = msg_id

    class _Entity:
        id = 999

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_file(self, _target, file, caption=None, force_document=False):
            del file, caption, force_document
            return _SentMessage(101)

        async def send_message(self, _target, _text, reply_to=None):
            if _text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90)
            assert reply_to == 101
            return _SentMessage(102)

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=30):
            del limit
            yield _IncomingMessage()

    async def _noop_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "veo-update"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=30,
    )
    assert result.success is False
    assert "Veo на обновлении" in result.provider_message
    assert result.attempt_id == "veo-update"


def test_syntax_hard_reject_detects_veo_update_message_with_decorations():
    import app.syntax_telethon_fallback as telethon_fallback

    text = (
        '🔔 В данный момент "⭕ Veo" на обновлении. Пожалуйста, попробуйте еще раз немного позже.\n'
        "Следите за новостями в нашем сообществе 👇"
    )

    assert telethon_fallback._syntax_bot_message_is_hard_reject(text, []) is True


def test_syntax_hard_reject_detects_missing_tool_selection():
    import app.syntax_telethon_fallback as telethon_fallback

    text = (
        "⚠️ Не выбран инструмент для работы с чат-ботом.\n\n"
        "Пожалуйста, воспользуйтесь навигацией для активации нужной функции."
    )

    assert telethon_fallback._syntax_bot_message_is_hard_reject(text, []) is True


def test_syntax_settings_webview_message_is_not_a_hard_reject_after_submit():
    import app.syntax_telethon_fallback as telethon_fallback

    text = (
        "Файл сохранён ✅\n"
        "Чтобы выбрать параметры и запустить генерацию, перейдите в «Настройки» 👇"
    )

    assert telethon_fallback._syntax_bot_message_is_hard_reject(text, []) is False


def test_syntax_configure_button_matches_combined_configure_and_launch_action():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Button:
        text = "Настроить и запустить"

    class _Message:
        buttons = [[_Button()]]

    assert telethon_fallback._syntax_find_configure_button(_Message()) is not None


def test_try_generate_via_syntax_caps_long_prompt_to_2000(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        def __init__(self, msg_id: int, reply_to: int):
            self.id = msg_id
            self.out = False
            self.reply_to = _ReplyTo(reply_to)
            self.raw_text = "done"
            self.message = self.raw_text
            self.entities = []
            self.video = object()
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id):
            self.id = msg_id

    class _Entity:
        id = 999

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self._iter_once = False

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, text, reply_to=None):
            if text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90)
            assert text == "short Gemini prompt"
            assert len(text) <= 2000
            assert reply_to is not None
            return _SentMessage(103)

        async def send_file(self, _target, file, caption=None, force_document=False):
            assert getattr(file, "name", "") == "start_frame.png"
            assert caption is None
            assert force_document is False
            return _SentMessage(102)

        async def download_media(self, _message, file):
            assert file is bytes
            return b"mp4-content"

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=30):
            del limit
            if self._iter_once:
                return
            self._iter_once = True
            yield _IncomingMessage(201, reply_to=103)

    async def _noop_sleep2(_sec: float) -> None:
        return None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "fixed-attempt"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep2)
    monkeypatch.setattr(
        telethon_fallback,
        "gemini_generate_sync",
        lambda _prompt, **_kwargs: '{"prompt":"short Gemini prompt"}',
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")
    monkeypatch.setenv("SYNTAX_BOT_USERNAME", "@syntxaibot")

    result = try_generate_via_syntax(
        prompt="x" * 5000,
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
    )
    assert result.success is True
    assert result.video_bytes == b"mp4-content"


def test_syntax_prompt_shortens_long_prompt_with_gemini(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    seen_prompt = {}

    def _fake_gemini(prompt: str, **kwargs) -> str:
        seen_prompt["prompt"] = prompt
        seen_prompt.update(kwargs)
        return '{"prompt":"Gemini kept CHARACTERS SPEAKS: точная реплика"}'

    monkeypatch.setattr(telethon_fallback, "gemini_generate_sync", _fake_gemini)

    prepared = telethon_fallback._prepare_prompt_for_syntax(
        "A" * 430 + " CHARACTERS SPEAKS: точная реплика",
        max_len=120,
    )

    assert prepared is not None
    compact, changed = prepared
    assert changed is True
    assert compact == "Gemini kept CHARACTERS SPEAKS: точная реплика"
    assert len(compact) <= 120
    assert '"max_chars": 120' in seen_prompt["prompt"]
    assert seen_prompt["rate_limit_max_attempts"] == 1


def test_syntax_prompt_uses_local_fallback_when_gemini_returns_too_long(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setattr(
        telethon_fallback,
        "gemini_generate_sync",
        lambda _prompt, **_kwargs: '{"prompt":"' + ("x" * 121) + '"}',
    )

    prepared = telethon_fallback._prepare_prompt_for_syntax("y" * 200, max_len=120)
    assert prepared is not None
    shortened, was_shortened = prepared
    assert was_shortened is True
    assert len(shortened) <= 120


def test_syntax_prompt_uses_local_fallback_after_single_gemini_429(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback
    from app.llm_utils import GeminiQuotaError

    calls: list[dict[str, object]] = []

    def _fake_gemini(prompt: str, **kwargs) -> str:
        calls.append({"prompt": prompt, **kwargs})
        raise GeminiQuotaError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(telethon_fallback, "gemini_generate_sync", _fake_gemini)

    prepared = telethon_fallback._prepare_prompt_for_syntax(
        "A" * 430 + " CHARACTERS SPEAKS: точная реплика",
        max_len=120,
    )

    assert prepared is not None
    shortened, was_shortened = prepared
    assert was_shortened is True
    assert len(shortened) <= 120
    assert len(calls) == 1
    assert calls[0]["rate_limit_max_attempts"] == 1


def test_syntax_prompt_limit_defaults_to_2000(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.delenv("SYNTAX_PROMPT_MAX_CHARS", raising=False)

    assert telethon_fallback._syntax_prompt_max_chars() == 2000


def test_syntax_prompt_limit_clamps_env_to_hard_2000(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setenv("SYNTAX_PROMPT_MAX_CHARS", "3900")

    assert telethon_fallback._syntax_prompt_max_chars() == 2000


def test_try_generate_via_syntax_requires_authorized_session(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.connected = False

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.connected = False

        async def is_user_authorized(self):
            return False

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "fixed-attempt"})())
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    result = try_generate_via_syntax(
        prompt="video prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=_syntax_test_frame_bytes(),
        timeout_sec=5,
    )
    assert result.success is False
    assert "not authorized" in result.provider_message.lower()


def test_syntax_session_candidates_include_multi_and_single_envs(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setenv("TELETHON_STRING_SESSIONS", "s1,s2")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "s2")
    monkeypatch.setenv("TELETHON_SESSION_PATHS", "/tmp/a.session,/tmp/b.session")
    monkeypatch.setenv("TELETHON_SESSION_PATH", "/tmp/b.session")

    candidates = telethon_fallback._syntax_session_candidates()

    assert candidates == [
        telethon_fallback._SessionCandidate("string", "s1"),
        telethon_fallback._SessionCandidate("string", "s2"),
        telethon_fallback._SessionCandidate("path", "/tmp/a.session"),
        telethon_fallback._SessionCandidate("path", "/tmp/b.session"),
    ]


def test_try_generate_via_syntax_parallel_uses_distinct_sessions(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        def __init__(self, msg_id: int, reply_to: int):
            self.id = msg_id
            self.out = False
            self.reply_to = _ReplyTo(reply_to)
            self.raw_text = "done"
            self.message = self.raw_text
            self.entities = []
            self.video = object()
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    barrier = threading.Barrier(2)
    seen_sessions: list[str] = []
    seen_lock = threading.Lock()

    class _FakeClient:
        def __init__(self, session_obj, *_args, **_kwargs):
            self._iter_once = False
            with seen_lock:
                seen_sessions.append(str(session_obj))

        async def connect(self):
            barrier.wait(timeout=1)
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_message(self, _target, _text, reply_to=None):
            if _text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90)
            assert reply_to is not None
            return _SentMessage(103)

        async def send_file(self, _target, file, caption=None, force_document=False):
            del file, caption, force_document
            return _SentMessage(101)

        async def download_media(self, _message, file):
            assert file is bytes
            return b"mp4-content"

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=30):
            del limit
            if self._iter_once:
                return
            self._iter_once = True
            yield _IncomingMessage(200, reply_to=103)

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.delenv("TELETHON_STRING_SESSION", raising=False)
    monkeypatch.setenv("TELETHON_STRING_SESSIONS", "session-a,session-b")

    results: list[FallbackResult | None] = [None, None]

    def _run(idx: int) -> None:
        results[idx] = try_generate_via_syntax(
            prompt=f"lang:ru prompt {idx}",
            model="veo3_fast",
            duration_sec=6,
            start_image=_syntax_test_frame_bytes(),
            timeout_sec=5,
            acquire_timeout_sec=5,
        )

    threads = [threading.Thread(target=_run, args=(idx,)) for idx in range(2)]
    for thread in threads:
        thread.start()
    # The current Syntax navigation deliberately pauses after /video, Veo and
    # Generate so the bot can install each reply keyboard before the
    # next selection. Give both parallel flows enough wall time to complete
    # that real preflight while still bounding the test.
    for thread in threads:
        thread.join(timeout=12)

    assert all(result is not None and result.success for result in results)
    assert set(seen_sessions) == {"session-a", "session-b"}


@pytest.mark.parametrize("configure_error", [None, "Syntax Veo UI configure failed: upload counter timeout", "Syntax Veo not submitted: navigation failed"])
def test_try_generate_via_syntax_batch_sends_all_requests_before_waiting(monkeypatch, configure_error):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        def __init__(self, msg_id: int, reply_to: int, text: str):
            self.id = msg_id
            self.out = False
            self.reply_to = _ReplyTo(reply_to)
            self.raw_text = text
            self.message = text
            self.entities = []
            self.video = object()
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    class _ConfigureButton:
        text = "Настроить и запустить"

    class _ConfigureMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    events: list[str] = []
    configure_count = {"value": 0}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self._file_count = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_file(self, _target, file, caption=None, force_document=False):
            del caption, force_document
            self._file_count += 1
            events.append(f"send_file:{getattr(file, 'name', '')}")
            return _SentMessage(101 if self._file_count == 1 else 103)

        async def send_message(self, _target, text, reply_to=None):
            events.append(f"send_message:{text}")
            if text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90 + len(events))
            return _SentMessage(102 if "lang:ru" in text else 104)

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=40):
            del limit
            events.append("iter_messages")
            yield _IncomingMessage(201, reply_to=102, text="lang:ru ready")
            yield _IncomingMessage(202, reply_to=104, text="lang:en ready")

        async def download_media(self, _message, file):
            assert file is bytes
            return b"mp4-content"

    async def _fake_wait_for_configure(*_args, **_kwargs):
        configure_count["value"] += 1
        return _ConfigureMessage(150 + configure_count["value"]), _ConfigureButton()

    async def _fake_submit_webview(*_args, **kwargs):
        events.append(f"submit:{kwargs['prompt']}")
        return configure_error

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "_wait_for_latest_syntax_configure_button", _fake_wait_for_configure)
    monkeypatch.setattr(telethon_fallback, "_submit_syntax_veo_webview", _fake_submit_webview)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    sink = {}
    results = try_generate_via_syntax_batch(
        result_sink=sink,
        requests=[
            BatchFallbackRequest(key="ru", prompt="lang:ru prompt", start_image=_syntax_test_frame_bytes()),
            BatchFallbackRequest(key="en", prompt="lang:en prompt", start_image=_syntax_test_frame_bytes()),
        ],
        model="veo3_fast",
        duration_sec=6,
        timeout_sec=5,
        acquire_timeout_sec=5,
    )

    assert results is sink
    if configure_error and configure_error.startswith("Syntax Veo not submitted:"):
        assert all(not result.success for result in results.values())
        assert "iter_messages" not in events
        return
    assert sink["en"].video_bytes == b"mp4-content"
    assert results["ru"].success is True
    assert results["en"].success is True
    assert events[:12] == [
        "send_message:/video",
        "send_message:Veo",
        "send_message:Сгенерировать",
        "send_file:start_frame.png",
        "send_message:lang:ru prompt",
        "submit:lang:ru prompt",
        "send_message:/video",
        "send_message:Veo",
        "send_message:Сгенерировать",
        "send_file:start_frame.png",
        "send_message:lang:en prompt",
        "submit:lang:en prompt",
    ]
    assert events.index("iter_messages") > events.index("submit:lang:en prompt")


def test_batch_routes_edited_video_status_to_english_request():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        def __init__(self, token):
            self.expected_lang_token = token

    class _Message:
        id = 701
        out = False
        reply_to = None
        raw_text = ""
        message = ""
        video = object()
        document = None

    states = {
        "ru": {
            "request": _Request("lang:ru"),
            "sent_ids": {101, 102},
            "pending_refetch": {700},
        },
        "en": {
            "request": _Request("lang:en"),
            "sent_ids": {103, 104},
            "pending_refetch": {701},
        },
    }

    assert telethon_fallback._batch_message_candidate_keys(_Message(), states) == ["en"]


def test_batch_routes_bare_pending_statuses_in_request_order():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        def __init__(self, token):
            self.expected_lang_token = token

    class _Message:
        def __init__(self, msg_id: int):
            self.id = msg_id
            self.out = False
            self.reply_to = None
            self.raw_text = "Запрос ожидает выполнения в очереди."
            self.message = self.raw_text
            self.entities = []
            self.video = None
            self.document = None

    states = {
        "ru": {
            "request": _Request("lang:ru"),
            "reply_anchor": 102,
            "sent_ids": {101, 102},
            "pending_refetch": set(),
        },
        "en": {
            "request": _Request("lang:en"),
            "reply_anchor": 104,
            "sent_ids": {103, 104},
            "pending_refetch": set(),
        },
    }

    assert telethon_fallback._batch_message_candidate_keys(_Message(201), states) == ["ru"]
    states["ru"]["pending_refetch"].add(201)
    assert telethon_fallback._batch_message_candidate_keys(_Message(202), states) == ["en"]


def test_batch_does_not_guess_language_for_unmatched_bare_video():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        def __init__(self, token):
            self.expected_lang_token = token

    class _Message:
        id = 800
        out = False
        reply_to = None
        raw_text = ""
        message = ""
        video = object()
        document = None

    states = {
        "ru": {
            "request": _Request("lang:ru"),
            "sent_ids": {101, 102},
            "pending_refetch": set(),
        },
        "en": {
            "request": _Request("lang:en"),
            "sent_ids": {103, 104},
            "pending_refetch": set(),
        },
    }

    assert telethon_fallback._batch_message_candidate_keys(_Message(), states) == []


@pytest.mark.parametrize("initial_status", ["Запрос ожидает выполнения в очереди.", "Задание зарегистрировано."])
def test_batch_routes_edited_hard_reject_to_the_right_language(monkeypatch, initial_status):
    import app.syntax_telethon_fallback as telethon_fallback

    class _ReplyTo:
        def __init__(self, msg_id: int):
            self.reply_to_msg_id = msg_id

    class _IncomingMessage:
        def __init__(
            self,
            msg_id: int,
            *,
            text: str = "",
            reply_to: int | None = None,
            video: bool = False,
        ):
            self.id = msg_id
            self.out = False
            self.reply_to = _ReplyTo(reply_to) if reply_to is not None else None
            self.raw_text = text
            self.message = text
            self.entities = []
            self.video = object() if video else None
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self._sent_count = 0
            self._iter_count = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_file(self, _target, file, caption=None, force_document=False):
            del file, caption, force_document
            self._sent_count += 1
            return _SentMessage(101 if self._sent_count == 1 else 103)

        async def send_message(self, _target, text, reply_to=None):
            if text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90)
            self._sent_count += 1
            del reply_to
            return _SentMessage(102 if "lang:ru" in text else 104)

        async def get_messages(self, _target, limit=None, ids=None):
            del _target, limit
            if ids == 201 and self._iter_count >= 2:
                return _IncomingMessage(201, text="still waiting")
            if ids == 202 and self._iter_count >= 2:
                return _IncomingMessage(
                    202,
                    text="❌ Произошла ошибка. Пожалуйста, попробуйте еще раз.",
                )
            return None

        async def iter_messages(self, _target, limit=40):
            del _target, limit
            self._iter_count += 1
            if self._iter_count == 1:
                yield _IncomingMessage(202, text=initial_status, reply_to=104 if initial_status == "Задание зарегистрировано." else None)
                yield _IncomingMessage(201, text="Запрос ожидает выполнения в очереди.")
                return
            yield _IncomingMessage(203, reply_to=201, video=True)
            yield _IncomingMessage(
                202,
                text="❌ Произошла ошибка. Пожалуйста, попробуйте еще раз.",
            )

        async def download_media(self, _message, file):
            assert file is bytes
            return b"ru-mp4"

    async def _noop_sleep(_sec: float) -> None:
        return None

    async def _fake_wait_for_configure(*_args, **_kwargs):
        return _IncomingMessage(150), object()

    async def _fake_submit_webview(*_args, **_kwargs):
        return None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(telethon_fallback, "_wait_for_latest_syntax_configure_button", _fake_wait_for_configure)
    monkeypatch.setattr(telethon_fallback, "_submit_syntax_veo_webview", _fake_submit_webview)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    results = try_generate_via_syntax_batch(
        requests=[
            BatchFallbackRequest(key="ru", prompt="lang:ru prompt", start_image=_syntax_test_frame_bytes()),
            BatchFallbackRequest(key="en", prompt="lang:en prompt", start_image=_syntax_test_frame_bytes()),
        ],
        model="veo3_fast",
        duration_sec=6,
        timeout_sec=5,
        acquire_timeout_sec=5,
    )

    assert results["ru"].success is True
    assert results["ru"].video_bytes == b"ru-mp4"
    assert results["en"].success is False
    assert "Произошла ошибка" in results["en"].provider_message


def test_try_generate_via_syntax_batch_times_out_each_pending_request(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    class _IncomingMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id
            self.out = False
            self.reply_to = None
            self.raw_text = "Запрос ожидает выполнения в очереди."
            self.message = self.raw_text
            self.entities = []
            self.video = None
            self.document = None

    class _SentMessage:
        def __init__(self, msg_id: int):
            self.id = msg_id

    class _Entity:
        id = 999

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self._sent_count = 0
            self._iter_count = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_entity(self, _username):
            return _Entity()

        async def send_file(self, _target, file, caption=None, force_document=False):
            del file, caption, force_document
            self._sent_count += 1
            return _SentMessage(101 if self._sent_count == 1 else 103)

        async def send_message(self, _target, text, reply_to=None):
            if text in {"/start", "/video", "Veo", "Сгенерировать"}:
                assert reply_to is None
                return _SentMessage(90)
            self._sent_count += 1
            del reply_to
            return _SentMessage(102 if "lang:ru" in text else 104)

        async def get_messages(self, _target, limit=None, ids=None):
            del _target, limit, ids
            return None

        async def iter_messages(self, _target, limit=40):
            del _target, limit
            self._iter_count += 1
            if self._iter_count == 1:
                yield _IncomingMessage(202)
                yield _IncomingMessage(201)

    clock = {"now": 0.0}

    async def _advance_sleep(seconds: float) -> None:
        clock["now"] += max(seconds, 2.0)

    async def _fake_wait_for_configure(*_args, **_kwargs):
        return _IncomingMessage(150), object()

    async def _fake_submit_webview(*_args, **_kwargs):
        return None

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _advance_sleep)
    monkeypatch.setattr(telethon_fallback, "_poll_interval_sec", lambda: 2)
    monkeypatch.setattr(telethon_fallback, "_wait_for_latest_syntax_configure_button", _fake_wait_for_configure)
    monkeypatch.setattr(telethon_fallback, "_submit_syntax_veo_webview", _fake_submit_webview)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELETHON_STRING_SESSION", "session")

    results = try_generate_via_syntax_batch(
        requests=[
            BatchFallbackRequest(key="ru", prompt="lang:ru prompt", start_image=_syntax_test_frame_bytes()),
            BatchFallbackRequest(key="en", prompt="lang:en prompt", start_image=_syntax_test_frame_bytes()),
        ],
        model="veo3_fast",
        duration_sec=6,
        timeout_sec=1,
        acquire_timeout_sec=1,
    )

    assert results["ru"].success is False
    assert results["en"].success is False
    assert "Syntax fallback timed out after 1s" in results["ru"].provider_message
    assert "Syntax fallback timed out after 1s" in results["en"].provider_message
    assert "Запрос ожидает выполнения" in results["ru"].provider_message
    assert "Запрос ожидает выполнения" in results["en"].provider_message


def test_batch_routes_video_replying_to_english_status():
    import app.syntax_telethon_fallback as telethon_fallback

    class _Request:
        def __init__(self, token):
            self.expected_lang_token = token

    class _ReplyTo:
        reply_to_msg_id = 701

    class _Message:
        id = 702
        out = False
        reply_to = _ReplyTo()
        raw_text = ""
        message = ""
        video = object()
        document = None

    states = {
        "ru": {
            "request": _Request("lang:ru"),
            "sent_ids": {101, 102},
            "pending_refetch": {700},
        },
        "en": {
            "request": _Request("lang:en"),
            "sent_ids": {103, 104},
            "pending_refetch": {701},
        },
    }

    assert telethon_fallback._batch_message_candidate_keys(_Message(), states) == ["en"]
