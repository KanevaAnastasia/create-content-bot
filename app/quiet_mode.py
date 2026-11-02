"""Persistent owner-only switch that silently pauses other Telegram users."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
import logging
import random
from typing import Iterator
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import SessionLocal
from .models import BotUser


QUIET_MODE_OWNER_USERNAME = "a_kaneva"
BUTTON_OPEN_QUIET_MODE = "Открыть тихий режим"
BUTTON_CLOSE_QUIET_MODE = "Закрыть тихий режим"

_MENU_BUTTON: ContextVar[str | None] = ContextVar("admin_quiet_mode_menu_button", default=None)
QUIET_MODE_TIMEZONE = ZoneInfo("Europe/Moscow")
QUIET_MODE_GENERATION_MIN = 8
QUIET_MODE_GENERATION_MAX = 11

logger = logging.getLogger("bot.quiet_mode")


def _moscow_date(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(QUIET_MODE_TIMEZONE).date()


def seconds_until_next_moscow_midnight(now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(QUIET_MODE_TIMEZONE)
    tomorrow = local_now.date() + timedelta(days=1)
    next_midnight = datetime.combine(tomorrow, time.min, tzinfo=QUIET_MODE_TIMEZONE)
    return max(0.0, (next_midnight - local_now).total_seconds())


def _owner_row(db, *, lock: bool = False) -> BotUser | None:
    statement = select(BotUser).where(
        BotUser.normalized_username == QUIET_MODE_OWNER_USERNAME,
        BotUser.role == "admin",
        BotUser.is_active.is_(True),
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one_or_none()


def _reset_cycle_row(row: BotUser, *, cycle_date: date, target: int | None = None) -> int:
    generation_target = int(target or random.randint(QUIET_MODE_GENERATION_MIN, QUIET_MODE_GENERATION_MAX))
    if not QUIET_MODE_GENERATION_MIN <= generation_target <= QUIET_MODE_GENERATION_MAX:
        raise ValueError("Quiet-mode generation target must be between 8 and 11.")
    row.quiet_mode_enabled = False
    row.quiet_mode_auto_cycle_date = cycle_date
    row.quiet_mode_auto_generation_count = 0
    row.quiet_mode_auto_generation_target = generation_target
    return generation_target


def enable_automatic_quiet_mode_from_next_midnight(
    *,
    now: datetime | None = None,
    session_factory=None,
) -> bool:
    """Arm automation without changing the owner's current quiet-mode state."""
    factory = session_factory or SessionLocal
    with factory() as db:
        row = _owner_row(db, lock=True)
        if row is None:
            return False
        row.quiet_mode_auto_enabled = True
        row.quiet_mode_auto_cycle_date = _moscow_date(now)
        row.quiet_mode_auto_generation_count = 0
        # Zero means the automation is armed but does not count until midnight.
        row.quiet_mode_auto_generation_target = 0
        db.commit()
    return True


def reset_automatic_quiet_mode_cycle(
    *,
    now: datetime | None = None,
    target: int | None = None,
    session_factory=None,
) -> dict[str, object]:
    """Open quiet mode and start a fresh random 8-11 generation cycle."""
    factory = session_factory or SessionLocal
    cycle_date = _moscow_date(now)
    with factory() as db:
        row = _owner_row(db, lock=True)
        if row is None:
            return {"changed": False, "reason": "owner_not_found"}
        if not row.quiet_mode_auto_enabled:
            return {"changed": False, "reason": "automation_disabled"}
        generation_target = _reset_cycle_row(row, cycle_date=cycle_date, target=target)
        db.commit()
    logger.info(
        "quiet_mode.auto_cycle_reset date=%s generation_target=%s",
        cycle_date,
        generation_target,
    )
    return {
        "changed": True,
        "cycle_date": cycle_date.isoformat(),
        "generation_target": generation_target,
    }


def ensure_automatic_quiet_mode_cycle(
    *,
    now: datetime | None = None,
    session_factory=None,
) -> bool:
    """Reset a stale automatic cycle after a restart or a missed midnight tick."""
    factory = session_factory or SessionLocal
    cycle_date = _moscow_date(now)
    with factory() as db:
        row = _owner_row(db, lock=True)
        if row is None or not row.quiet_mode_auto_enabled:
            return False
        if row.quiet_mode_auto_cycle_date == cycle_date:
            return False
        generation_target = _reset_cycle_row(row, cycle_date=cycle_date)
        db.commit()
    logger.info(
        "quiet_mode.auto_cycle_caught_up date=%s generation_target=%s",
        cycle_date,
        generation_target,
    )
    return True


def record_automatic_quiet_mode_generation(
    *,
    now: datetime | None = None,
    session_factory=None,
) -> dict[str, object]:
    """Count one successful guest media generation and close at the daily target."""
    factory = session_factory or SessionLocal
    cycle_date = _moscow_date(now)
    with factory() as db:
        row = _owner_row(db, lock=True)
        if row is None or not row.quiet_mode_auto_enabled:
            return {"counted": False, "reason": "automation_disabled"}
        if row.quiet_mode_auto_cycle_date != cycle_date:
            _reset_cycle_row(row, cycle_date=cycle_date)
        target = int(row.quiet_mode_auto_generation_target or 0)
        if target <= 0 or row.quiet_mode_enabled:
            return {"counted": False, "reason": "not_accepting_generations"}
        count = int(row.quiet_mode_auto_generation_count or 0) + 1
        row.quiet_mode_auto_generation_count = count
        closed = count >= target
        if closed:
            row.quiet_mode_enabled = True
        db.commit()
    logger.info(
        "quiet_mode.auto_generation_counted count=%s target=%s closed=%s",
        count,
        target,
        closed,
    )
    return {"counted": True, "count": count, "target": target, "closed": closed}


def is_quiet_mode_owner(identity: object | None) -> bool:
    return bool(
        identity is not None
        and str(getattr(identity, "role", "") or "") == "admin"
        and str(getattr(identity, "normalized_username", "") or "").lower() == QUIET_MODE_OWNER_USERNAME
    )


def is_quiet_mode_enabled() -> bool:
    ensure_automatic_quiet_mode_cycle()
    with SessionLocal() as db:
        value = db.execute(
            select(BotUser.quiet_mode_enabled).where(
                BotUser.normalized_username == QUIET_MODE_OWNER_USERNAME,
                BotUser.role == "admin",
                BotUser.is_active.is_(True),
            )
        ).scalar_one_or_none()
    return bool(value)


def set_quiet_mode_enabled(identity: object, enabled: bool) -> bool:
    if not is_quiet_mode_owner(identity):
        raise PermissionError("Управлять тихим режимом может только его владелец.")
    with SessionLocal() as db:
        row = db.execute(
            select(BotUser).where(
                BotUser.id == getattr(identity, "user_id", None),
                BotUser.normalized_username == QUIET_MODE_OWNER_USERNAME,
                BotUser.role == "admin",
                BotUser.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            raise PermissionError("Владелец тихого режима не найден.")
        row.quiet_mode_enabled = bool(enabled)
        db.commit()
    return bool(enabled)


def quiet_mode_button_label(enabled: bool) -> str:
    return BUTTON_CLOSE_QUIET_MODE if enabled else BUTTON_OPEN_QUIET_MODE


def current_quiet_mode_menu_button() -> str | None:
    return _MENU_BUTTON.get()


@contextmanager
def quiet_mode_menu_scope(identity: object | None, enabled: bool) -> Iterator[None]:
    label = quiet_mode_button_label(enabled) if is_quiet_mode_owner(identity) else None
    token = _MENU_BUTTON.set(label)
    try:
        yield
    finally:
        _MENU_BUTTON.reset(token)
