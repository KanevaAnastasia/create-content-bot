"""Authorization and one-project provisioning for the guest bot interface."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from .db import SessionLocal
from .models import BotUser, Project


_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,64}$")


def normalize_telegram_username(value: object) -> str | None:
    """Return the canonical Telegram username used for pre-registration."""
    normalized = str(value or "").strip().lstrip("@").lower()
    if not normalized or not _USERNAME_RE.fullmatch(normalized):
        return None
    return normalized


@dataclass(frozen=True, slots=True)
class BotIdentity:
    user_id: uuid.UUID
    telegram_id: int
    normalized_username: str | None
    display_name: str
    role: str
    is_active: bool
    project_id: uuid.UUID | None = None


def _display_name(telegram_user: object) -> str:
    full_name = " ".join(str(getattr(telegram_user, "full_name", "") or "").split())
    if full_name:
        return full_name
    first = str(getattr(telegram_user, "first_name", "") or "").strip()
    last = str(getattr(telegram_user, "last_name", "") or "").strip()
    return " ".join(part for part in (first, last) if part).strip()


def _ensure_guest_project(db, user: BotUser) -> Project:
    project = db.execute(select(Project).where(Project.owner_user_id == user.id)).scalar_one_or_none()
    if project is not None:
        return project
    project = Project(
        owner_user_id=user.id,
        slug=f"guest-{user.id.hex}",
        title="Мой проект",
        description="Персональный проект гостевого режима",
        is_default=False,
        is_active=True,
    )
    db.add(project)
    db.flush()
    return project


def resolve_bot_identity(telegram_user: object, *, allow_username_claim: bool = False) -> BotIdentity | None:
    """
    Resolve access by immutable Telegram ID.

    Username lookup is deliberately enabled only for the first ``/start``.  A
    matching pre-registration is then permanently claimed by that Telegram ID.
    """
    raw_id = getattr(telegram_user, "id", None)
    try:
        telegram_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    username = normalize_telegram_username(getattr(telegram_user, "username", None))
    display_name = _display_name(telegram_user)
    with SessionLocal() as db:
        row = db.execute(select(BotUser).where(BotUser.telegram_id == telegram_id)).scalar_one_or_none()
        if row is None and allow_username_claim and username:
            stmt = select(BotUser).where(BotUser.normalized_username == username)
            if db.bind is not None and db.bind.dialect.name != "sqlite":
                stmt = stmt.with_for_update()
            row = db.execute(stmt).scalar_one_or_none()
            if row is not None:
                if row.telegram_id is not None and int(row.telegram_id) != telegram_id:
                    return None
                row.telegram_id = telegram_id
                if display_name:
                    row.display_name = display_name
                db.flush()
        if row is None or not bool(row.is_active):
            return None
        project_id: uuid.UUID | None = None
        if str(row.role) == "guest":
            project_id = _ensure_guest_project(db, row).id
        else:
            if username and not str(row.normalized_username or "").strip():
                row.normalized_username = username
            if display_name and not str(row.display_name or "").strip():
                row.display_name = display_name
        db.commit()
        return BotIdentity(
            user_id=row.id,
            telegram_id=telegram_id,
            normalized_username=row.normalized_username,
            display_name=row.display_name,
            role=str(row.role),
            is_active=bool(row.is_active),
            project_id=project_id,
        )


def preregister_bot_user(
    username: str,
    *,
    role: str = "guest",
    display_name: str = "",
    is_active: bool = True,
) -> BotIdentity:
    """Create or update an unclaimed username-only registration."""
    normalized = normalize_telegram_username(username)
    if normalized is None:
        raise ValueError("Некорректный Telegram username.")
    if role not in {"admin", "guest"}:
        raise ValueError("Роль должна быть admin или guest.")
    with SessionLocal() as db:
        row = db.execute(select(BotUser).where(BotUser.normalized_username == normalized)).scalar_one_or_none()
        if row is None:
            row = BotUser(
                telegram_id=None,
                normalized_username=normalized,
                display_name=display_name.strip() or f"@{username.strip().lstrip('@')}",
                role=role,
                is_active=is_active,
            )
            db.add(row)
            db.flush()
        else:
            row.role = role
            row.display_name = display_name.strip() or row.display_name
            row.is_active = is_active
        project_id = _ensure_guest_project(db, row).id if role == "guest" and row.telegram_id is not None else None
        db.commit()
        return BotIdentity(
            user_id=row.id,
            telegram_id=int(row.telegram_id or 0),
            normalized_username=row.normalized_username,
            display_name=row.display_name,
            role=str(row.role),
            is_active=bool(row.is_active),
            project_id=project_id,
        )


def guest_project_id_for_user(user_id: uuid.UUID | str) -> uuid.UUID | None:
    try:
        parsed = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return None
    with SessionLocal() as db:
        return db.execute(select(Project.id).where(Project.owner_user_id == parsed)).scalar_one_or_none()
