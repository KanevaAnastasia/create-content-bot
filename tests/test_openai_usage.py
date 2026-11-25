import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app import openai_usage as ledger


def record(key, *, now=None, model='gpt-6.2-sol', usage=None):
    ledger.record_usage(event_id=key, model=model, kind='text', now=now,
                        usage=usage or {'input_tokens': 750000, 'output_tokens': 0},
                        prompt='secret approved speech')


def test_prices_include_cache_writes_and_do_not_double_count_reasoning():
    usage = {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 200, 'cache_write_tokens': 300},
             'output_tokens': 100, 'output_tokens_details': {'reasoning_tokens': 80}}
    assert ledger.estimate_cost_nano('gpt-6.2-sol', usage, 'text') == 2_770_000
    assert ledger.estimate_cost_nano('new-model', usage, 'text') is None
    assert ledger.estimate_cost_nano('gpt-6.2-sol', {}, 'text') is None
    image = {'input_tokens_details': {'text_tokens': 200, 'image_tokens': 300}, 'output_tokens': 1500}
    assert ledger.estimate_cost_nano('gpt-image-2.6-sunburst', image, 'image') == 48_400_000


def test_duplicate_response_ids_and_moscow_midnight():
    before = datetime(2026, 11, 25, 20, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
    record('one', now=before, usage={'input_tokens': 100, 'output_tokens': 100})
    record('one', now=before)
    record('two', now=after, usage={'input_tokens': 100, 'output_tokens': 100})
    s = ledger.usage_summary(after)
    assert s['today_nano'] == 1_200_000
    assert s['month_nano'] == 2_400_000
    with ledger._db() as db:
        rows = db.execute('select * from usage_events').fetchall()
    assert len(rows) == 2
    assert 'secret approved speech' not in str([dict(x) for x in rows])


def test_concurrent_recording_and_atomic_daily_claim():
    now = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: record('one', now=now), range(20)))
        claims = list(pool.map(lambda _: ledger._claim_alert('2026-11-26'), range(20)))
    assert sum(x is not None for x in claims) == 1
    assert ledger.usage_summary(now)['month_nano'] == 3_000_000_000


def test_threshold_exactly_150_and_next_day_reset():
    a = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
    b = datetime(2026, 11, 27, 10, tzinfo=timezone.utc)
    record('a', now=a, usage={'input_tokens': 0, 'output_tokens': 149999})
    assert ledger._claim_alert('2026-11-26') is None
    record('b', now=a, usage={'input_tokens': 0, 'output_tokens': 1})
    assert ledger._claim_alert('2026-11-26') == 1_500_000_000
    assert ledger._claim_alert('2026-11-26') is None
    assert ledger._claim_alert('2026-11-27') is None
    record('c', now=b, usage={'input_tokens': 0, 'output_tokens': 150000})
    assert ledger._claim_alert('2026-11-27') == 1_500_000_000


def test_alert_only_owner_and_persisted_after_success(monkeypatch):
    from app import bot_commands
    now = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
    record('a', now=now)
    monkeypatch.setattr(bot_commands, 'owner_chat_id', lambda: None)
    bot = AsyncMock()
    asyncio.run(ledger.deliver_daily_alert(bot, now))
    bot.send_message.assert_not_awaited()
    monkeypatch.setattr(bot_commands, 'owner_chat_id', lambda: 123)
    asyncio.run(ledger.deliver_daily_alert(bot, now))
    asyncio.run(ledger.deliver_daily_alert(bot, now))
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs['chat_id'] == 123
    assert 'Генерации продолжаются' in bot.send_message.call_args.kwargs['text']


def test_ambiguous_telegram_timeout_never_duplicates(monkeypatch):
    from app import bot_commands
    from telegram.error import TimedOut
    now = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
    record('a', now=now)
    monkeypatch.setattr(bot_commands, 'owner_chat_id', lambda: 123)
    bot = AsyncMock()
    bot.send_message.side_effect = TimedOut()
    asyncio.run(ledger.deliver_daily_alert(bot, now))
    asyncio.run(ledger.deliver_daily_alert(bot, now))
    bot.send_message.assert_awaited_once()
    with ledger._db() as db:
        assert db.execute('select status from alerts').fetchone()[0] == 'delivery_unknown'


def test_balance_estimate_expiry_and_missing_price():
    start = datetime(2026, 11, 26, 1, tzinfo=timezone.utc)
    later = datetime(2026, 11, 26, 2, tzinfo=timezone.utc)
    ledger.set_balance_anchor(balance_usd='13.54', observed_at=start, expires_on='2026-12-02')
    record('before', now=start)
    record('after', now=later, usage={'input_tokens': 0, 'output_tokens': 100000})
    assert ledger.usage_summary(later)['remaining_nano'] == 12_540_000_000
    assert 'Расчётный остаток: $12.54' in ledger.format_usage_summary(ledger.usage_summary(later))
    expired = datetime(2026, 12, 2, tzinfo=timezone.utc)
    assert ledger.usage_summary(expired)['remaining_nano'] is None
    record('unknown', now=later, model='unknown')
    assert ledger.usage_summary(later)['remaining_nano'] is None
    assert ledger.usage_summary(later)['unknown'] == 1


def test_accounting_failure_does_not_fail_generation(monkeypatch):
    monkeypatch.setattr(ledger, '_db_path', lambda: 1/0)
    record('ok')  # Deliberate best-effort accounting, never another paid request.
