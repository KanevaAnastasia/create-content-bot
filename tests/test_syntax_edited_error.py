import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import syntax_telethon_fallback as syntax


def test_single_poll_rechecks_unknown_status_edited_into_error(monkeypatch):
    error = ('❌ Произошла ошибка\n\nПопробуйте ещё раз. Если ошибка повторяется, проверьте:'
             '\n\n❓ Правила фильтрации\n\n📊 Статус системы\n\n📖 База знаний')

    def message(text, edited=None):
        return SimpleNamespace(id=500, out=False, reply_to=None, raw_text=text,
                               entities=[], video=None, document=None, edit_date=edited)

    initial = message('Задание зарегистрировано.')
    final = message(error, '2026-11-09T16:33:09Z')

    class Client:
        reads = 0

        async def get_messages(self, target, limit=None, ids=None):
            self.reads += 1
            return initial if self.reads == 1 else final

        async def iter_messages(self, target, limit=40):
            yield initial

    monkeypatch.delenv('SYNTAX_STRICT_REPLY_MATCH', raising=False)
    monkeypatch.setattr(syntax.asyncio, 'sleep', AsyncMock())
    client = Client()
    result = asyncio.run(syntax._poll_syntax_bot_until_video(
        client, object(), reply_anchor=100, sent_ids={100, 200}, attempt_id='edited-error',
        poll=10, deadline=time.monotonic()+2, initial_last_info='', timeout=2,
        expected_lang_token='lang:en',
    ))
    assert result.success is False
    assert error in result.provider_message
    assert 'timed out' not in result.provider_message
    assert client.reads == 2
