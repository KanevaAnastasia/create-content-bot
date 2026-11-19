import asyncio
from types import SimpleNamespace
from app import syntax_telethon_fallback as syntax


def test_settings_can_arrive_after_forty_seconds_with_unrelated_replies(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(syntax.time, 'monotonic', lambda: clock[0])
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(syntax.asyncio, 'sleep', sleep)
    button = SimpleNamespace(text='Настройки')
    def message(mid, text, reply):
        return SimpleNamespace(id=mid, out=False, raw_text=text, buttons=[[button]], reply_to=SimpleNamespace(reply_to_msg_id=reply))
    stale = message(205, 'Промпт сохранён', 100)
    uploaded = message(206, 'Файл сохранён', 200)
    current = message(207, 'Промпт сохранён', 201)
    class Client:
        calls = 0
        async def get_messages(self, target, limit):
            self.calls += 1
            assert limit == 20
            return [stale, uploaded] + ([current] if clock[0] >= 40 else [])
    client = Client()
    msg, found = asyncio.run(syntax._wait_for_latest_syntax_configure_button(
        client, None, reply_anchor=201, poll=2, require_prompt_saved=True, deadline=180))
    assert msg is current and found is button
    assert clock[0] == 40 and client.calls == 21


def test_missing_settings_wait_until_deadline_without_resending(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(syntax.time, 'monotonic', lambda: clock[0])
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(syntax.asyncio, 'sleep', sleep)
    class Client:
        async def get_messages(self, target, limit):
            return []
    assert asyncio.run(syntax._wait_for_latest_syntax_configure_button(
        Client(), None, reply_anchor=201, poll=2, require_prompt_saved=True, deadline=180)) == (None, None)
    assert clock[0] == 180


def test_settings_timeout_reports_not_started():
    from app.handlers.handlers_media_core import _syntax_dispatch_error_user_message, _syntax_failure_happened_before_dispatch
    error = 'Syntax Veo not submitted: prompt acknowledgement and settings did not arrive before the configure deadline.'
    assert _syntax_failure_happened_before_dispatch(error)
    assert 'ещё не запущена' in _syntax_dispatch_error_user_message(error)
