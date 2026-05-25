import io

from app.syntax_video_fallback import FallbackResult, try_generate_via_syntax

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional in some dev shells
    Image = None


def _syntax_test_frame_bytes() -> bytes:
    if Image is None:
        raise RuntimeError("Pillow is required for Syntax Telethon tests")
    buf = io.BytesIO()
    Image.new("RGB", (320, 320), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


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
            assert len(text) <= 3900
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


def test_syntax_prompt_compaction_removes_visual_blocks_and_keeps_speech():
    import app.syntax_telethon_fallback as telethon_fallback

    prompt = (
        'lang:ru A 6-second clip with cartoon grandmother and cartoon cat. '
        'All visual elements, including: Stove: long description. Pot: long description. '
        'Cauldron: long description. '
        'veo3_framing: close-up to medium-close shot; grandmother full figure always visible; '
        'every other character in frame; accurate lip sync; camera angle evolves. '
        'veo3_cartoon_character_rule: visual style is explicitly cartoon; grandmother is a cartoon grandmother, '
        'the cat is a cartoon cat; never realistic. '
        'kitchen_physics_rule: cookware and electric appliances must be physically plausible and functional. '
        'If a pot is cooking, it must sit on a visible stovetop burner. '
        'voice_lock_full: female voice Russian language elderly grandmother age approximately seventy. '
        'CHARACTERS SPEAKS: точная реплика [пауза] не терять'
    )

    compact, changed = telethon_fallback._prepare_prompt_for_syntax(prompt, max_len=420)

    assert changed is True
    assert len(compact) <= 420
    assert "All visual elements" not in compact
    assert "Cauldron: long description" not in compact
    assert "CHARACTERS SPEAKS: точная реплика [пауза] не терять" in compact


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
