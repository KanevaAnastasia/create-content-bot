import base64
import threading

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


def _syntax_test_frame_bytes() -> bytes:
    return _ONE_PIXEL_PNG


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
            call_order.append("send_message")
            assert len(_text) <= 2000
            assert reply_to is not None
            return _SentMessage(103)

        async def send_file(self, _target, file, caption=None, force_document=False):
            call_order.append("send_file")
            assert getattr(file, "name", "") in ("start_frame.png", "end_frame.png")
            assert caption is None
            assert force_document is False
            if len(call_order) == 1:
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
    )
    assert result.success is True
    assert result.video_bytes == b"mp4-content"
    assert result.video_uri is None
    assert result.attempt_id == "fixed-attempt"
    assert call_order[:3] == ["send_file", "send_file", "send_message"]


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
            assert reply_to == 101
            return _SentMessage(102)

        async def get_messages(self, _target, limit=None, ids=None):
            del limit, ids
            return None

        async def iter_messages(self, _target, limit=30):
            del limit
            yield _IncomingMessage()

    async def _sleep_must_not_happen(_sec: float) -> None:
        raise AssertionError("Veo update message should stop polling immediately")

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
    monkeypatch.setattr(telethon_fallback, "uuid4", lambda: type("_UUID", (), {"hex": "veo-update"})())
    monkeypatch.setattr(telethon_fallback.asyncio, "sleep", _sleep_must_not_happen)
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
        lambda _prompt: '{"prompt":"short Gemini prompt"}',
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

    def _fake_gemini(prompt: str) -> str:
        seen_prompt["prompt"] = prompt
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


def test_syntax_prompt_does_not_cut_when_gemini_returns_too_long(monkeypatch):
    import app.syntax_telethon_fallback as telethon_fallback

    monkeypatch.setattr(
        telethon_fallback,
        "gemini_generate_sync",
        lambda _prompt: '{"prompt":"' + ("x" * 121) + '"}',
    )

    assert telethon_fallback._prepare_prompt_for_syntax("y" * 200, max_len=120) is None


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
    for thread in threads:
        thread.join(timeout=2)

    assert all(result is not None and result.success for result in results)
    assert set(seen_sessions) == {"session-a", "session-b"}


def test_try_generate_via_syntax_batch_sends_all_requests_before_waiting(monkeypatch):
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

    events: list[str] = []

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
            del caption, force_document
            events.append(f"send_file:{getattr(file, 'name', '')}")
            return _SentMessage(101 if len(events) == 1 else 103)

        async def send_message(self, _target, text, reply_to=None):
            events.append(f"send_message:{text}")
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

    monkeypatch.setattr(telethon_fallback, "TelegramClient", _FakeClient)
    monkeypatch.setattr(telethon_fallback, "StringSession", lambda value: value)
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
    assert results["en"].success is True
    assert events[:4] == [
        "send_file:start_frame.png",
        "send_message:lang:ru prompt",
        "send_file:start_frame.png",
        "send_message:lang:en prompt",
    ]
