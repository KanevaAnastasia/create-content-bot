"""Owner-only /stat Instagram report and inline date picker."""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..instagram_stats import InstagramReport, default_start, fetch_instagram_report, moscow_now

logger = logging.getLogger("bot")
PREFIX = "igstat:"
MONTHS = ("", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def date_markup(start: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "Другая дата", callback_data=f"{PREFIX}month:{start.replace(day=1).isoformat()}",
    )]])


def calendar_markup(month: date, today: date) -> InlineKeyboardMarkup:
    month = month.replace(day=1)
    noop = f"{PREFIX}noop"
    rows = [[InlineKeyboardButton(f"{MONTHS[month.month]} {month.year}", callback_data=noop)]]
    rows.append([InlineKeyboardButton(day, callback_data=noop) for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")])
    for week in calendar.monthcalendar(month.year, month.month):
        rows.append([
            InlineKeyboardButton(
                str(day) if day else " ",
                callback_data=f"{PREFIX}date:{month.replace(day=day).isoformat()}"
                if day and month.replace(day=day) <= today else noop,
            ) for day in week
        ])
    nav = []
    if month.year > 1 or month.month > 1:
        previous = (month - timedelta(days=1)).replace(day=1)
        nav.append(InlineKeyboardButton("‹ Месяц", callback_data=f"{PREFIX}month:{previous.isoformat()}"))
    if month < today.replace(day=1):
        following = (month + timedelta(days=32)).replace(day=1)
        nav.append(InlineKeyboardButton("Месяц ›", callback_data=f"{PREFIX}month:{following.isoformat()}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("С последнего 10-го", callback_data=f"{PREFIX}default")])
    return InlineKeyboardMarkup(rows)


def report_chunks(report: InstagramReport) -> list[str]:
    count = len(report.videos)
    known = [video.views for video in report.videos if video.views is not None]
    total = f"{sum(known):,}".replace(",", " ")
    lines = [
        "Instagram · Бабушка (русская версия) · @syntx_ai в описании",
        f"Публикации: {report.start:%d.%m.%Y} — {report.as_of:%d.%m.%Y} включительно (МСК).",
        f"Подключённых аккаунтов: {report.accounts}.",
        f"{'Всего роликов' if report.listing_complete else 'Найдено роликов (список неполный)'}: {count}.",
    ]
    if not report.accounts:
        lines.append("Instagram русской Бабушки не подключён.")
    elif len(known) == count and report.listing_complete:
        lines.append(f"Всего просмотров: {total}.")
    elif known:
        lines.append(f"Известная сумма просмотров: {total} (итог неполный).")
    else:
        lines.append("Сумма просмотров недоступна.")
    if len(known) < count:
        lines.append(f"Без данных о просмотрах: {count - len(known)} из {count} роликов.")
    lines.append(f"Просмотры за всё время на момент запроса: {report.as_of:%d.%m.%Y %H:%M} МСК.")
    lines.extend(report.warnings)
    if not count and report.accounts and report.listing_complete:
        lines.append("За этот период роликов с @syntx_ai в описании нет.")
    for index, video in enumerate(report.videos, 1):
        views = f"{video.views:,}".replace(",", " ") + " просмотров" if video.views is not None else "просмотры недоступны"
        lines.append(f"\n{index}. {video.published_at:%d.%m.%Y} · {video.account}\n{video.permalink or 'Ссылка недоступна'} — {views}")
    chunks = []
    chunk = ""
    for line in lines:
        # Leave margin below Telegram's 4096-character limit, including emoji.
        for offset in range(0, max(len(line), 1), 3500):
            part = line[offset:offset + 3500]
            if len(chunk) + len(part) + 1 > 3500:
                chunks.append(chunk)
                chunk = ""
            chunk = f"{chunk}\n{part}" if chunk else part
    if chunk:
        chunks.append(chunk)
    return chunks


async def send_instagram_stats(message, start: date | None = None) -> None:
    now = moscow_now()
    start = start or default_start(now.date())
    loading = await message.reply_text("Собираю статистику Instagram…")
    try:
        report = await asyncio.to_thread(fetch_instagram_report, start, now=now)
        chunks = report_chunks(report)
    except Exception as exc:
        # Network exceptions may contain credentials; log only their type.
        logger.warning("instagram.stats_failed type=%s", type(exc).__name__)
        chunks = ["Не удалось загрузить статистику Instagram. Попробуйте /stat ещё раз."]
    for index, chunk in enumerate(chunks):
        kwargs = {
            "reply_markup": date_markup(start) if index == len(chunks) - 1 else None,
            "disable_web_page_preview": True,
            "parse_mode": None,
        }
        if index == 0:
            await loading.edit_text(chunk, **kwargs)
        else:
            await message.reply_text(chunk, **kwargs)


async def handle_calendar(update, context) -> None:
    _ = context
    query = update.callback_query
    data = str(query.data or "").removeprefix(PREFIX)
    today = moscow_now().date()
    if data == "noop":
        await query.answer()
        return
    try:
        if data == "default":
            action, chosen = "date", default_start(today)
        else:
            action, raw_date = data.split(":", 1)
            chosen = date.fromisoformat(raw_date)
        if action not in {"month", "date"} or chosen > today:
            raise ValueError("Invalid selection")
    except ValueError:
        await query.answer("Выберите дату не позже сегодняшней.", show_alert=True)
        return
    await query.answer()
    if action == "month":
        try:
            await query.edit_message_reply_markup(reply_markup=calendar_markup(chosen, today))
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif query.message is not None:
        await send_instagram_stats(query.message, chosen)
