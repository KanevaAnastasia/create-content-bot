from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import quiet_mode
from app.models import Base, BotUser


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            BotUser(
                telegram_id=305928305,
                normalized_username=quiet_mode.QUIET_MODE_OWNER_USERNAME,
                display_name="Owner",
                role="admin",
                is_active=True,
                quiet_mode_enabled=True,
            )
        )
        db.commit()
    return factory


def _owner(factory):
    with factory() as db:
        return db.execute(select(BotUser)).scalar_one()


def test_automation_starts_at_next_moscow_midnight_without_changing_current_state():
    factory = _factory()
    before_midnight = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    assert quiet_mode.enable_automatic_quiet_mode_from_next_midnight(
        now=before_midnight,
        session_factory=factory,
    )
    armed = _owner(factory)
    assert armed.quiet_mode_enabled is True
    assert armed.quiet_mode_auto_generation_target == 0

    result = quiet_mode.reset_automatic_quiet_mode_cycle(
        now=datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc),
        target=8,
        session_factory=factory,
    )
    reset = _owner(factory)
    assert result["changed"] is True
    assert reset.quiet_mode_enabled is False
    assert reset.quiet_mode_auto_cycle_date.isoformat() == "2026-11-01"
    assert reset.quiet_mode_auto_generation_target == 8
    assert reset.quiet_mode_auto_generation_count == 0


def test_successful_generations_close_quiet_mode_at_target_and_stop_counting():
    factory = _factory()
    midnight = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    quiet_mode.enable_automatic_quiet_mode_from_next_midnight(now=midnight, session_factory=factory)
    quiet_mode.reset_automatic_quiet_mode_cycle(now=midnight, target=8, session_factory=factory)

    for expected in range(1, 8):
        result = quiet_mode.record_automatic_quiet_mode_generation(now=midnight, session_factory=factory)
        assert result == {"counted": True, "count": expected, "target": 8, "closed": False}

    result = quiet_mode.record_automatic_quiet_mode_generation(now=midnight, session_factory=factory)
    assert result == {"counted": True, "count": 8, "target": 8, "closed": True}
    assert _owner(factory).quiet_mode_enabled is True

    ignored = quiet_mode.record_automatic_quiet_mode_generation(now=midnight, session_factory=factory)
    assert ignored == {"counted": False, "reason": "not_accepting_generations"}
    assert _owner(factory).quiet_mode_auto_generation_count == 8


def test_midnight_delay_uses_moscow_time():
    now = datetime(2026, 10, 31, 20, 59, 30, tzinfo=timezone.utc)
    assert quiet_mode.seconds_until_next_moscow_midnight(now) == 30.0
