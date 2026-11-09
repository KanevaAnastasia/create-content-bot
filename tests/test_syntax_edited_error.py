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


def test_batch_binds_english_status_before_it_is_edited_without_reply_or_language():
    def msg(mid, text):
        return SimpleNamespace(id=mid, out=False, reply_to=None, raw_text=text,
                               entities=[], video=None, document=None)

    client = SimpleNamespace(get_messages=AsyncMock(return_value=msg(808003, 'Ожидайте, видео в очереди.')))
    status_ids = asyncio.run(syntax._capture_syntax_submission_status(client, None, after_id=808002))
    assert status_ids == {808003}
    states = {
        'ru': {'request': SimpleNamespace(expected_lang_token='lang:ru'),
               'sent_ids': {807988, 807990}, 'reply_anchor': 807987,
               'pending_refetch': {807990}},
        'en': {'request': SimpleNamespace(expected_lang_token='lang:en'),
               'sent_ids': {808000, 808002}, 'reply_anchor': 807999,
               'pending_refetch': {808002}, 'submission_status_ids': status_ids},
    }
    error = msg(808003, '❌ Произошла ошибка. Попробуйте ещё раз.')
    assert syntax._batch_message_candidate_keys(error, states) == ['en']
    states['en']['pending_refetch'].update(status_ids)
    result = asyncio.run(syntax._process_bot_message(
        client, message=error,
        sent_ids=states['en']['sent_ids'] | states['en']['pending_refetch'],
        expected_lang_token='lang:en', require_lang_token_for_loose=True,
    ))
    assert result[-1] is True
    assert 'ru' in states  # English failure does not depend on Russian completion.
