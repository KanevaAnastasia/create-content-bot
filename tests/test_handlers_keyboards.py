import asyncio
import io

from telegram.error import NetworkError, TimedOut

from app.handlers import handlers_keyboards


def test_safe_reply_text_retries_and_preserves_reply_markup(monkeypatch):
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, reply_markup=None):
            self.calls.append((text, reply_markup))
            if len(self.calls) == 1:
                raise NetworkError("temporary send failure")
            return True

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_keyboards.asyncio, "sleep", fake_sleep)

    message = Message()
    markup = object()
    asyncio.run(handlers_keyboards.safe_reply_text(message, "hello", reply_markup=markup))

    assert len(message.calls) == 2
    assert message.calls[0] == ("hello", markup)
    assert message.calls[1] == ("hello", markup)


def test_reply_text_without_keyboard_retries(monkeypatch):
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, reply_markup=None):
            self.calls.append((text, reply_markup))
            if len(self.calls) == 1:
                raise NetworkError("temporary send failure")
            return True

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_keyboards.asyncio, "sleep", fake_sleep)

    message = Message()
    asyncio.run(handlers_keyboards.reply_text_without_keyboard(message, "loading"))

    assert len(message.calls) == 2
    assert message.calls[0][0] == "loading"
    assert message.calls[1][0] == "loading"
    assert message.calls[0][1].__class__.__name__ == "ReplyKeyboardRemove"
    assert message.calls[1][1].__class__.__name__ == "ReplyKeyboardRemove"


def test_safe_reply_text_falls_back_for_message_stubs_without_reply_markup():
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text):
            self.calls.append(text)
            return True

    message = Message()
    asyncio.run(handlers_keyboards.safe_reply_text(message, "hello", reply_markup=object()))

    assert message.calls == ["hello"]


def test_safe_send_message_retries_and_preserves_reply_markup(monkeypatch):
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.calls.append((chat_id, text, reply_markup))
            if len(self.calls) == 1:
                raise NetworkError("temporary send failure")
            return True

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_keyboards.asyncio, "sleep", fake_sleep)

    bot = Bot()
    markup = object()
    asyncio.run(
        handlers_keyboards.safe_send_message(
            bot,
            chat_id=123,
            text="hello",
            reply_markup=markup,
        )
    )

    assert bot.calls == [
        (123, "hello", markup),
        (123, "hello", markup),
    ]


def test_safe_reply_photo_retries_with_fresh_stream(monkeypatch):
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_photo(self, *, photo, caption):
            data = photo.read()
            self.calls.append((data, getattr(photo, "name", ""), caption, photo))
            if len(self.calls) == 1:
                raise NetworkError("temporary send failure")
            return True

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_BASE_DELAY_SEC", 0)
    monkeypatch.setattr(handlers_keyboards.asyncio, "sleep", fake_sleep)

    photo = io.BytesIO(b"fake png")
    photo.name = "preview.png"
    message = Message()

    asyncio.run(handlers_keyboards.safe_reply_photo(message, photo=photo, caption="preview"))

    assert [call[:3] for call in message.calls] == [
        (b"fake png", "preview.png", "preview"),
        (b"fake png", "preview.png", "preview"),
    ]
    assert message.calls[0][3] is not message.calls[1][3]


def test_safe_send_retries_timed_out_with_exponential_backoff(monkeypatch):
    calls = {"count": 0}
    sleeps = []

    async def flaky_send():
        calls["count"] += 1
        if calls["count"] < 4:
            raise TimedOut("temporary")
        return "sent"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_ATTEMPTS", 4)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_BASE_DELAY_SEC", 2)
    monkeypatch.setattr(handlers_keyboards, "_SEND_RETRY_MAX_DELAY_SEC", 30)
    monkeypatch.setattr(handlers_keyboards.asyncio, "sleep", fake_sleep)

    result = asyncio.run(handlers_keyboards.safe_send(flaky_send, label="test"))

    assert result == "sent"
    assert calls["count"] == 4
    assert sleeps == [2, 4, 8]
