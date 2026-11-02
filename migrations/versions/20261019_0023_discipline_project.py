"""Add owner multiproject profiles and seed Discipline.

Revision ID: 20261019_0023
Revises: 20260929_0022
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261019_0023"
down_revision = "20260929_0022"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    project_columns = _columns("projects")
    if "elevenlabs_voice_id" not in project_columns:
        op.add_column("projects", sa.Column("elevenlabs_voice_id", sa.String(length=128), nullable=True))
    if "default_generation_mode" not in project_columns:
        op.add_column("projects", sa.Column("default_generation_mode", sa.String(length=32), nullable=True))
    if "style_source_project_slug" not in project_columns:
        op.add_column("projects", sa.Column("style_source_project_slug", sa.String(length=128), nullable=True))
    if "is_primary" not in _columns("project_characters"):
        op.add_column(
            "project_characters",
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    indexes = {item["name"]: item for item in inspector.get_indexes("projects")}
    owner_index = indexes.get("ix_projects_owner_user_id")
    if owner_index and owner_index.get("unique"):
        op.drop_index("ix_projects_owner_user_id", table_name="projects")
        op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"], unique=False)

    pc_indexes = {item["name"] for item in inspect(bind).get_indexes("project_characters")}
    if "uq_project_characters_one_primary" not in pc_indexes:
        op.create_index(
            "uq_project_characters_one_primary",
            "project_characters",
            ["project_id"],
            unique=True,
            postgresql_where=sa.text("is_primary = true"),
            sqlite_where=sa.text("is_primary = 1"),
        )

    projects = sa.table(
        "projects",
        sa.column("id", sa.Uuid()),
        sa.column("owner_user_id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("title", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("is_default", sa.Boolean()),
        sa.column("is_active", sa.Boolean()),
        sa.column("hashtags_fixed_ru", sa.JSON()),
        sa.column("hashtags_fixed_en", sa.JSON()),
        sa.column("default_video_provider", sa.String()),
        sa.column("elevenlabs_voice_id", sa.String()),
        sa.column("default_generation_mode", sa.String()),
        sa.column("style_source_project_slug", sa.String()),
    )
    users = sa.table(
        "bot_users",
        sa.column("id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    admin_id = bind.execute(
        sa.select(users.c.id)
        .where(users.c.role == "admin", users.c.is_active.is_(True))
        .order_by(users.c.created_at)
    ).scalar()
    existing = bind.execute(sa.select(projects.c.id).where(projects.c.slug == "discipline")).scalar()
    values = {
        "owner_user_id": admin_id,
        "title": "Дисциплина",
        "description": "Короткие истории о дисциплине с проектными персонажами.",
        "is_default": False,
        "is_active": True,
        "hashtags_fixed_ru": [],
        "hashtags_fixed_en": [],
        "default_video_provider": "vertex",
        "elevenlabs_voice_id": "nkvv6waLzFah2jTNUZGT",
        "default_generation_mode": "story",
        "style_source_project_slug": "grandma_kitchen",
    }
    if existing is None:
        bind.execute(projects.insert().values(id=uuid.uuid4(), slug="discipline", **values))
    else:
        bind.execute(projects.update().where(projects.c.id == existing).values(**values))


def downgrade() -> None:
    bind = op.get_bind()
    if "uq_project_characters_one_primary" in {item["name"] for item in inspect(bind).get_indexes("project_characters")}:
        op.drop_index("uq_project_characters_one_primary", table_name="project_characters")
    if "is_primary" in _columns("project_characters"):
        op.drop_column("project_characters", "is_primary")
    for column in ("style_source_project_slug", "default_generation_mode", "elevenlabs_voice_id"):
        if column in _columns("projects"):
            op.drop_column("projects", column)
