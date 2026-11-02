"""
Seed текущего дефолтного проекта в PostgreSQL.

Этот модуль переносит уже существующие кодовые источники (ASSETS и
TEXT_PIPELINE_STEPS) в новые таблицы мультипроектности, не меняя runtime UX.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assets import ASSETS
from app.character_catalog import CHARACTER_CATALOG, GRANDMA_KITCHEN_PROJECT_SLUG, GRANDMA_KITCHEN_PROJECT_TITLE
from app.constants import DEFAULT_GEMINI_MODEL, GEMINI_MODEL_ENV_NAME, TEXT_PIPELINE_STEPS
from app.models import Character, Location, Project, ProjectCharacter, ProjectLocation, TextStep
from app.visual_props import seed_visual_props_for_project


DEFAULT_PROJECT_SLUG = GRANDMA_KITCHEN_PROJECT_SLUG
DEFAULT_PROJECT_TITLE = GRANDMA_KITCHEN_PROJECT_TITLE


@dataclass(frozen=True, slots=True)
class SeedStats:
    """Короткая сводка результата seed-операции."""

    project_slug: str
    characters: int
    locations: int
    visual_props: int
    text_steps: int


def _display_name_from_key(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _asset_base_uri(item: Any) -> str | None:
    if isinstance(item, dict):
        base = item.get("base")
        return str(base).strip() if isinstance(base, str) and base.strip() else None
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None


def _asset_meta(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    meta: dict[str, Any] = {}
    top_view = item.get("top_view")
    if isinstance(top_view, str) and top_view.strip():
        meta["top_view_uri"] = top_view.strip()
    return meta


def _get_or_create_project(session: Session) -> Project:
    project = session.scalar(select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG))
    if project is None:
        legacy = session.scalar(select(Project).where(Project.slug == "grandma_cat_cooking"))
        if legacy is not None:
            legacy.slug = DEFAULT_PROJECT_SLUG
            project = legacy
    if project is None:
        project = Project(
            slug=DEFAULT_PROJECT_SLUG,
            title=DEFAULT_PROJECT_TITLE,
            description="Cozy cartoon cooking Shorts project with grandmother, orange cat, white cat, black-and-white cat, and family characters.",
            is_default=True,
            is_active=True,
            hashtags_fixed_ru=["#убабушкивкуснее", "#бабушкинырецепты", "#рецепты"],
            hashtags_fixed_en=["#grandmatastesbetter", "#grandmasrecipes", "#recipes"],
            default_video_provider="syntax",
            background_music_volume=0.18,
        )
        session.add(project)
        session.flush()
        return project
    project.title = DEFAULT_PROJECT_TITLE
    project.is_default = True
    project.is_active = True
    project.hashtags_fixed_ru = ["#убабушкивкуснее", "#бабушкинырецепты", "#рецепты"]
    project.hashtags_fixed_en = ["#grandmatastesbetter", "#grandmasrecipes", "#recipes"]
    project.description = "Cozy cartoon cooking Shorts project with grandmother, orange cat, white cat, black-and-white cat, and family characters."
    if project.default_video_provider in (None, "vertex"):
        project.default_video_provider = "syntax"
    if project.background_music_volume is None:
        project.background_music_volume = 0.18
    return project


def _seed_characters(session: Session, project: Project, assets: dict[str, Any]) -> int:
    characters = assets.get("characters") if isinstance(assets, dict) else {}
    if not isinstance(characters, dict):
        return 0
    count = 0
    for idx, (key, item) in enumerate(sorted(characters.items())):
        canonical_key = str(key).strip()
        if not canonical_key:
            continue
        asset_uri = _asset_base_uri(item)
        character_meta = CHARACTER_CATALOG.get(canonical_key, {})
        character_name = str(character_meta.get("name") or _display_name_from_key(canonical_key))
        character_short_description = (
            str(character_meta.get("short_description") or _display_name_from_key(canonical_key)).strip()
            or canonical_key
        )
        character_role = str(character_meta.get("role") or "").strip() or None
        character_description = str(character_meta.get("description") or "").strip() or None
        character = session.scalar(select(Character).where(Character.canonical_key == canonical_key))
        if character is None:
            character = Character(
                canonical_key=canonical_key,
                name=character_name,
                role=character_role,
                description=character_description,
                default_asset_uri=asset_uri,
                meta_json={"short_description": character_short_description},
                is_active=True,
            )
            session.add(character)
            session.flush()
        else:
            character.name = character_name
            character.role = character_role
            character.description = character_description
            character.default_asset_uri = asset_uri or character.default_asset_uri
            character.meta_json = {
                **(character.meta_json or {}),
                "short_description": character_short_description,
            }
            character.is_active = True

        link_meta = {
            "name": character.name,
            "short_description": character_short_description,
            "role": character.role,
            "description": character.description,
        }
        link = session.get(ProjectCharacter, {"project_id": project.id, "character_id": character.id})
        if link is None:
            session.add(
                ProjectCharacter(
                    project_id=project.id,
                    character_id=character.id,
                    display_name=character.name,
                    asset_uri=asset_uri,
                    sort_order=idx,
                    meta_json=link_meta,
                )
            )
        else:
            link.display_name = character.name
            link.asset_uri = asset_uri or link.asset_uri
            link.sort_order = idx
            link.meta_json = {**(link.meta_json or {}), **link_meta}
        count += 1
    return count


def _seed_locations(session: Session, project: Project, assets: dict[str, Any]) -> int:
    locations = assets.get("locations") if isinstance(assets, dict) else {}
    if not isinstance(locations, dict):
        return 0
    count = 0
    for idx, (key, item) in enumerate(sorted(locations.items())):
        canonical_code = str(key).strip()
        if not canonical_code:
            continue
        asset_uri = _asset_base_uri(item)
        meta = _asset_meta(item)
        location = session.scalar(select(Location).where(Location.canonical_code == canonical_code))
        if location is None:
            location = Location(
                canonical_code=canonical_code,
                name=_display_name_from_key(canonical_code),
                default_asset_uri=asset_uri,
                meta_json=meta,
                is_active=True,
            )
            session.add(location)
            session.flush()
        else:
            location.default_asset_uri = asset_uri or location.default_asset_uri
            location.meta_json = {**(location.meta_json or {}), **meta}
            location.is_active = True

        link = session.get(ProjectLocation, {"project_id": project.id, "location_id": location.id})
        if link is None:
            session.add(
                ProjectLocation(
                    project_id=project.id,
                    location_id=location.id,
                    asset_uri=asset_uri,
                    sort_order=idx,
                    meta_json=meta,
                )
            )
        else:
            link.asset_uri = asset_uri or link.asset_uri
            link.sort_order = idx
            link.meta_json = {**(link.meta_json or {}), **meta}
        count += 1
    return count


def _seed_text_steps(session: Session, project: Project) -> int:
    model = (os.getenv(GEMINI_MODEL_ENV_NAME) or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    count = 0
    for idx, step in enumerate(TEXT_PIPELINE_STEPS):
        existing = session.scalar(
            select(TextStep).where(TextStep.project_id == project.id, TextStep.step_key == step.step_id)
        )
        base_prompt = f"Legacy code-defined text step: {step.title_ru}"
        if existing is None:
            session.add(
                TextStep(
                    project_id=project.id,
                    step_key=step.step_id,
                    step_order=idx,
                    provider="vertex_gemini",
                    model=model,
                    base_system_prompt=base_prompt,
                    is_active=True,
                )
            )
        else:
            existing.step_order = idx
            existing.provider = existing.provider or "vertex_gemini"
            existing.model = existing.model or model
            existing.base_system_prompt = existing.base_system_prompt or base_prompt
            existing.is_active = True
        count += 1
    return count


def seed_default_project(session: Session, assets: dict[str, Any] | None = None) -> SeedStats:
    """
    Создаёт или обновляет default project из текущих ассетов и текстовых шагов.

    Функция идемпотентна: повторный запуск обновляет существующие строки и не
    создаёт дубликаты по canonical keys / project links / step keys.
    """
    project = _get_or_create_project(session)
    source_assets = ASSETS if assets is None else assets
    characters_count = _seed_characters(session, project, source_assets)
    locations_count = _seed_locations(session, project, source_assets)
    visual_props_count = seed_visual_props_for_project(session, project)
    text_steps_count = _seed_text_steps(session, project)
    session.commit()
    return SeedStats(
        project_slug=project.slug,
        characters=characters_count,
        locations=locations_count,
        visual_props=visual_props_count,
        text_steps=text_steps_count,
    )
