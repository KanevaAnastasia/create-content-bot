"""Durable local OpenAI usage ledger and private owner spending alerts.

Rates: https://developers.openai.com/api/docs/pricing (2026-11-26).
Costs are estimates from provider usage, not the organization's billing balance.
No prompts, responses, API credentials or Telegram IDs are stored here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

logger = logging.getLogger("bot.openai_usage")
MOSCOW = ZoneInfo("Europe/Moscow")
NANO = 1_000_000_000
DAILY_ALERT_NANO = 1_500_000_000
# USD per million tokens: input, cached input, output. Cache writes use 30m.
TEXT_RATES = {
    "gpt-6.2-sol": (2, .1, 10), "gpt-6.1-sol": (2, .1, 10),
    "gpt-6.2-astra": (8, .4, 40), "gpt-6-astra": (10, 1, 50),
    "gpt-6-sol": (2, .2, 10), "gpt-6-luna": (.1, .01, .5),
    "gpt-6.1-terra": (1, .05, 5),
}
IMAGE_MODELS = {"gpt-image-2", "gpt-image-2.6-sunburst", "gpt-image-2.6-flare"}


def utc_now():
    return datetime.now(timezone.utc)


def _aware(now):
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def _count(value):
    return max(0, int(value or 0))


def estimate_cost_nano(model: str, usage: dict, kind: str) -> int | None:
    """Do not turn missing usage/unknown prices into free requests."""
    if not usage or "output_tokens" not in usage:
        return None
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    details = usage.get("input_tokens_details") or {}
    output = _count(usage["output_tokens"])
    if kind == "image":
        if model not in IMAGE_MODELS or not {"text_tokens", "image_tokens"} <= details.keys():
            return None
        # Image caching may include a modality breakdown. Without it use the
        # full input rate, a conservative estimate rather than a false discount.
        text_tokens, image_tokens = _count(details["text_tokens"]), _count(details["image_tokens"])
        cached = details.get("cached_tokens_details") or {}
        ct = min(text_tokens, _count(cached.get("text_tokens")))
        ci = min(image_tokens, _count(cached.get("image_tokens")))
        usd_million = (Decimal(text_tokens - ct) * 5 + Decimal(ct) * Decimal('1.25')
                       + Decimal(image_tokens - ci) * 8 + Decimal(ci) * 2 + output * 30)
    else:
        rates = TEXT_RATES.get(model)
        if rates is None or "input_tokens" not in usage:
            return None
        inp = _count(usage["input_tokens"])
        cached = min(inp, _count(details.get("cached_tokens")))
        writes = min(inp - cached, _count(details.get("cache_write_tokens")))
        a, b, c = map(lambda v: Decimal(str(v)), rates)
        # Current integration uses Standard speed and 30m explicit cache writes.
        multiplier_in = Decimal(2) if inp > 272_000 else Decimal(1)
        multiplier_out = Decimal('1.5') if inp > 272_000 else Decimal(1)
        usd_million = ((inp - cached - writes) * a + cached * b + writes * a * Decimal('1.25')) * multiplier_in + output * c * multiplier_out
    return int(usd_million * 1000)


def _db_path():
    configured = os.getenv("OPENAI_USAGE_DB")
    if configured:
        return Path(configured)
    if os.getenv("PYTEST_CURRENT_TEST"):
        raise RuntimeError("Tests must configure an isolated OPENAI_USAGE_DB")
    return Path(__file__).resolve().parents[1] / "runtime" / "openai_usage.sqlite3"


@contextmanager
def _db():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS usage_events (
                event_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, day TEXT NOT NULL,
                model TEXT NOT NULL, kind TEXT NOT NULL, profile TEXT, operation TEXT,
                status TEXT, usage_json TEXT NOT NULL, cost_nano INTEGER,
                prompt_chars INTEGER NOT NULL, prompt_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS usage_day ON usage_events(day);
            CREATE TABLE IF NOT EXISTS alerts (
                day TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        with conn:
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('started_at', ?)", (utc_now().isoformat(),))
        yield conn
    finally:
        conn.close()


def operation_name():
    """Small code label, captured before moving work into the LLM queue."""
    frame = inspect.currentframe()
    try:
        while frame:
            name = frame.f_globals.get("__name__", "")
            if name.startswith("app.") and name not in {__name__, "app.llm_utils", "app.openai_images", "app.vertex_images", "app.pipeline_llm_steps"}:
                return f"{name}.{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def record_usage(*, event_id, model, kind, usage, profile=None, operation=None,
                 status="completed", prompt="", now=None):
    """Accounting failure must never trigger another paid generation."""
    try:
        usage = usage if isinstance(usage, dict) else {}
        event_id = event_id if isinstance(event_id, str) and event_id else str(uuid.uuid4())
        current = _aware(now or utc_now()).astimezone(timezone.utc)
        cost = estimate_cost_nano(model, usage, kind)
        with _db() as db, db:
            db.execute("INSERT OR IGNORE INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                event_id, current.isoformat(), current.astimezone(MOSCOW).date().isoformat(),
                model, kind, profile, operation, status, json.dumps(usage, separators=(',', ':')), cost,
                len(prompt), hashlib.sha256(prompt.encode()).hexdigest(),
            ))
        logger.info("openai.usage model=%s kind=%s profile=%s cost_usd=%s status=%s",
                    model, kind, profile, cost / NANO if cost is not None else 'unknown', status)
    except Exception as exc:
        logger.error("openai.usage_record_failed type=%s", type(exc).__name__)


def usage_summary(now=None):
    day = _aware(now or utc_now()).astimezone(MOSCOW).date().isoformat()
    with _db() as db:
        settings = dict(db.execute("SELECT key,value FROM settings"))
        rows = db.execute('''SELECT kind, SUM(cost_nano) cost,
            SUM(CASE WHEN day=? THEN cost_nano ELSE 0 END) today,
            SUM(CASE WHEN cost_nano IS NULL THEN 1 ELSE 0 END) unknown,
            COUNT(*) requests FROM usage_events WHERE day>=? AND day<=? GROUP BY kind''',
            (day, day[:7] + '-01', day)).fetchall()
        result = {"day": day, "started_at": settings['started_at'], "today_nano": 0,
                  "month_nano": 0, "unknown": 0, "by_kind": {}}
        for row in rows:
            result['today_nano'] += row['today'] or 0
            result['month_nano'] += row['cost'] or 0
            result['unknown'] += row['unknown']
            result['by_kind'][row['kind']] = row['cost'] or 0
        anchor = json.loads(settings.get('balance_anchor', 'null'))
        result['balance_anchor'] = anchor
        result['remaining_nano'] = None
        if anchor and day < anchor['expires_on']:
            after = db.execute("SELECT SUM(cost_nano), SUM(CASE WHEN cost_nano IS NULL THEN 1 ELSE 0 END) FROM usage_events WHERE recorded_at>?", (anchor['observed_at'],)).fetchone()
            if not after[1]:
                result['remaining_nano'] = max(0, anchor['balance_nano'] - (after[0] or 0))
    return result


def set_balance_anchor(*, balance_usd, observed_at, expires_on):
    """Seed only from an actually observed billing balance, never a guessed budget."""
    anchor = dict(balance_nano=int(Decimal(str(balance_usd)) * NANO),
                  observed_at=_aware(observed_at).astimezone(timezone.utc).isoformat(), expires_on=expires_on)
    with _db() as db, db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('balance_anchor',?)", (json.dumps(anchor),))


def format_usage_summary(summary):
    started = datetime.fromisoformat(summary['started_at']).astimezone(MOSCOW).strftime('%d.%m %H:%M')
    kinds = summary['by_kind']
    lines = ["", "OpenAI API (по учёту бота, дни по Москве):",
             f"Сегодня: ${summary['today_nano'] / NANO:.2f}. В этом месяце: ${summary['month_nano'] / NANO:.2f}.",
             f"За месяц: тексты ${kinds.get('text', 0) / NANO:.2f}, изображения ${kinds.get('image', 0) / NANO:.2f}.",
             f"Учёт включён {started}; более ранние расходы не включены.",
             "Предупреждение при $1,50 за день; генерации продолжаются."]
    if summary['unknown']:
        lines.append(f"Есть запросы без рассчитанной стоимости: {summary['unknown']}. Итог неполный.")
    anchor = summary['balance_anchor']
    if summary['remaining_nano'] is not None:
        observed = datetime.fromisoformat(anchor['observed_at']).astimezone(MOSCOW).strftime('%d.%m %H:%M')
        lines.append(f"Расчётный остаток: ${summary['remaining_nano'] / NANO:.2f} (баланс проверен {observed}).")
        lines.append(f"Срок кредитов: {anchor['expires_on']}. Пополнения и расходы вне этого бота автоматически не учитываются.")
    else:
        lines.append("Остаток OpenAI сейчас не подтверждён; проверьте Billing.")
    return '\n'.join(lines)


def _claim_alert(day):
    with _db() as db, db:
        db.execute('BEGIN IMMEDIATE')
        total = db.execute('SELECT SUM(cost_nano) FROM usage_events WHERE day=?', (day,)).fetchone()[0] or 0
        if total < DAILY_ALERT_NANO:
            return None
        db.execute("INSERT OR IGNORE INTO alerts(day,status) VALUES (?,'pending')", (day,))
        updated = db.execute("UPDATE alerts SET status='sending' WHERE day=? AND status='pending' AND retry_at<=?", (day, time.time()))
        return total if updated.rowcount else None


def _finish_alert(day, status, retry_at=0):
    with _db() as db, db:
        db.execute('UPDATE alerts SET status=?,retry_at=? WHERE day=?', (status, retry_at, day))


async def deliver_daily_alert(bot, now=None):
    from .bot_commands import owner_chat_id
    from telegram.error import NetworkError
    day = _aware(now or utc_now()).astimezone(MOSCOW).date().isoformat()
    # No fallback to current user, project owner, a group, or other admins.
    recipient = await asyncio.to_thread(owner_chat_id)
    if recipient is None:
        return
    total = await asyncio.to_thread(_claim_alert, day)
    if total is None:
        return
    try:
        await bot.send_message(chat_id=recipient, text=(
            f"Расход OpenAI API по учёту бота за {day} по Москве достиг ${total / NANO:.2f}. "
            "Порог предупреждения: $1,50. Генерации продолжаются. Подробности: /stat."
        ), connect_timeout=10, read_timeout=20, write_timeout=20, pool_timeout=10)
    except NetworkError:
        # Telegram might have accepted the message. Do not duplicate on timeout.
        await asyncio.to_thread(_finish_alert, day, 'delivery_unknown')
        logger.warning('openai.daily_alert_delivery_unknown day=%s', day)
    except Exception as exc:
        await asyncio.to_thread(_finish_alert, day, 'pending', time.time() + 300)
        logger.warning('openai.daily_alert_failed type=%s', type(exc).__name__)
    else:
        await asyncio.to_thread(_finish_alert, day, 'sent')
        logger.info('openai.daily_alert_sent day=%s', day)


async def run_alerts(bot):
    while True:
        try:
            await deliver_daily_alert(bot)
        except Exception as exc:
            logger.warning('openai.daily_alert_check_failed type=%s', type(exc).__name__)
        await asyncio.sleep(60)
