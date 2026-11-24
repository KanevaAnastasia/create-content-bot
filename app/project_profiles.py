"""Runtime profiles for the owner's selectable content projects."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from .character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG, GRANDMA_KITCHEN_PROJECT_TITLE
from .db import SessionLocal
from .models import BotUser, Character, Project, ProjectCharacter


DISCIPLINE_PROJECT_SLUG = "discipline"
DISCIPLINE_PROJECT_TITLE = "Дисциплина"
DISCIPLINE_VOICE_ID = "EvnFqTSGPvDU3JBwJbCC"
DISCIPLINE_HASHTAGS_FIXED_RU = ["#тихаядисциплина"]
DISCIPLINE_HASHTAGS_FIXED_EN = ["#quietdiscipline"]


def resolve_project_final_scene_prompt(project_slug: str | None) -> str:
    slug = str(project_slug or "").strip()
    if not slug:
        return ""
    with SessionLocal() as db:
        value = db.execute(
            select(Project.final_scene_prompt).where(Project.slug == slug, Project.is_active.is_(True))
        ).scalar_one_or_none()
    return " ".join(str(value or "").split())


def resolve_project_title_preset(project_slug: str | None) -> dict[str, Any]:
    """Return the structured project title/output policy stored in title_preset."""
    slug = str(project_slug or "").strip()
    if not slug:
        return {}
    with SessionLocal() as db:
        value = db.execute(
            select(Project.title_preset).where(Project.slug == slug, Project.is_active.is_(True))
        ).scalar_one_or_none()
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"description": raw}
    return parsed if isinstance(parsed, dict) else {}


def project_title(project_slug: str | None) -> str:
    slug = str(project_slug or "").strip()
    if slug == DISCIPLINE_PROJECT_SLUG:
        return DISCIPLINE_PROJECT_TITLE
    if slug in {GRANDMA_KITCHEN_PROJECT_SLUG, "grandma_cat_cooking"}:
        return GRANDMA_KITCHEN_PROJECT_TITLE
    with SessionLocal() as db:
        value = db.execute(select(Project.title).where(Project.slug == slug)).scalar_one_or_none()
    return str(value or GRANDMA_KITCHEN_PROJECT_TITLE)


def discipline_project() -> Project | None:
    with SessionLocal() as db:
        return db.execute(
            select(Project).where(Project.slug == DISCIPLINE_PROJECT_SLUG, Project.is_active.is_(True))
        ).scalar_one_or_none()


def discipline_workspace_session() -> dict[str, Any] | None:
    """Build the owner's project-management session for Discipline."""
    with SessionLocal() as db:
        project = db.execute(
            select(Project).where(Project.slug == DISCIPLINE_PROJECT_SLUG, Project.is_active.is_(True))
        ).scalar_one_or_none()
        if project is None:
            return None
        owner_id = project.owner_user_id
        if owner_id is None:
            owner_id = db.execute(
                select(BotUser.id).where(BotUser.role == "admin", BotUser.is_active.is_(True)).order_by(BotUser.created_at)
            ).scalars().first()
            if owner_id is not None:
                project.owner_user_id = owner_id
                db.commit()
        if owner_id is None:
            return None
        return {
            "state": "guest_menu",
            "guest_mode": True,
            "project_workspace_mode": True,
            "bot_user_id": str(owner_id),
            "project_id": str(project.id),
            "user_role": "admin",
            "guest_data": {},
            "selected_project_key": project.slug,
            "force_story_mode": True,
            "project_voice_id": str(project.elevenlabs_voice_id or DISCIPLINE_VOICE_ID),
        }


def resolve_project_voice_id(project_slug: str | None, fallback: str | None = None) -> str:
    """Resolve the current DB-backed voice so restored sessions cannot retain an obsolete project voice."""
    slug = str(project_slug or "").strip()
    if slug:
        with SessionLocal() as db:
            value = db.execute(
                select(Project.elevenlabs_voice_id).where(Project.slug == slug, Project.is_active.is_(True))
            ).scalar_one_or_none()
        if str(value or "").strip():
            return str(value).strip()
    if slug == DISCIPLINE_PROJECT_SLUG:
        return DISCIPLINE_VOICE_ID
    return str(fallback or "").strip()


def project_story_characters(project_slug: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Return approved project characters in the shape expected by Story mode."""
    slug = str(project_slug or "").strip()
    if not slug:
        return [], None
    with SessionLocal() as db:
        rows = db.execute(
            select(Character, ProjectCharacter)
            .join(ProjectCharacter, ProjectCharacter.character_id == Character.id)
            .join(Project, Project.id == ProjectCharacter.project_id)
            .where(Project.slug == slug, Character.is_active.is_(True))
            .order_by(ProjectCharacter.sort_order, Character.created_at)
        ).all()
    items: list[dict[str, Any]] = []
    primary_id: str | None = None
    for character, link in rows:
        character_meta = character.meta_json if isinstance(character.meta_json, dict) else {}
        description = " ".join(
            str(character.description or character_meta.get("short_description") or character.name or "").split()
        )
        asset_uri = str(link.asset_uri or character.default_asset_uri or "").strip()
        if not description or not asset_uri:
            continue
        character_id = str(character.canonical_key or f"character_{character.id.hex}")
        item = {
            "id": character_id,
            "display_name": str(link.display_name or character.name),
            "role": str(character.role or "персонаж проекта"),
            "age_stage": "adult",
            "same_person_group": character_id,
            "description": description,
            "reference_prompt": description,
            "requires_reference": False,
            "asset_uri": asset_uri,
            "is_primary": bool(link.is_primary),
        }
        items.append(item)
        if link.is_primary and primary_id is None:
            primary_id = character_id
    return items, primary_id


def resolve_project_id(project_slug: str | None) -> uuid.UUID | None:
    slug = str(project_slug or "").strip()
    if not slug:
        return None
    with SessionLocal() as db:
        return db.execute(select(Project.id).where(Project.slug == slug)).scalar_one_or_none()


def resolve_story_voice_id(session: dict) -> str:
    """An explicitly selected series voice wins over format and project defaults."""
    voice = str(session.get("series_voice_id") or "").strip()
    if voice:
        return voice
    from .discipline_modes import active_mode, MALE_VOICE_ID
    if active_mode(session):
        return MALE_VOICE_ID
    return resolve_project_voice_id(
        str(session.get("selected_project_key") or ""),
        str(session.get("project_voice_id") or ""),
    )
