"""Add isolated guest users, owned projects and guest workflow metadata.

Revision ID: 20260928_0021
Revises: 20260629_0020
Create Date: 2026-09-28 12:00:00.000000
"""

from __future__ import annotations

import os
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260928_0021"
down_revision = "20260629_0020"
branch_labels = None
depends_on = None


bot_user_role = sa.Enum("admin", "guest", name="bot_user_role")
generation_workflow_type = sa.Enum("image", "video", "cartoon", name="generation_workflow_type")


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("bot_users"):
        op.create_table(
            "bot_users",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("telegram_id", sa.BigInteger(), nullable=True),
            sa.Column("normalized_username", sa.String(length=128), nullable=True),
            sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("role", bot_user_role, nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("telegram_id", name="uq_bot_users_telegram_id"),
            sa.UniqueConstraint("normalized_username", name="uq_bot_users_normalized_username"),
        )
        op.create_index("ix_bot_users_telegram_id", "bot_users", ["telegram_id"], unique=False)
        op.create_index("ix_bot_users_normalized_username", "bot_users", ["normalized_username"], unique=False)

    project_columns = _columns("projects")
    if project_columns and "owner_user_id" not in project_columns:
        op.add_column("projects", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "fk_projects_owner_user_id",
            "projects",
            "bot_users",
            ["owner_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"], unique=True)

    character_columns = _columns("characters")
    if character_columns and "voice_description" not in character_columns:
        op.add_column("characters", sa.Column("voice_description", sa.Text(), nullable=True))

    run_columns = _columns("generation_runs")
    if run_columns and "project_id" not in run_columns:
        op.add_column("generation_runs", sa.Column("project_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "fk_generation_runs_project_id",
            "generation_runs",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_generation_runs_project_id", "generation_runs", ["project_id"], unique=False)
    run_columns = _columns("generation_runs")
    if run_columns and "workflow_type" not in run_columns:
        generation_workflow_type.create(bind, checkfirst=True)
        op.add_column("generation_runs", sa.Column("workflow_type", generation_workflow_type, nullable=True))
        op.create_index("ix_generation_runs_workflow_type", "generation_runs", ["workflow_type"], unique=False)

    users = sa.table(
        "bot_users",
        sa.column("id", sa.Uuid()),
        sa.column("telegram_id", sa.BigInteger()),
        sa.column("normalized_username", sa.String()),
        sa.column("display_name", sa.Text()),
        sa.column("role", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    admin_id_raw = str(os.getenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID") or "305928305").strip()
    admin_telegram_id = int(admin_id_raw) if admin_id_raw.isdigit() else 305928305
    existing_admin = bind.execute(
        sa.select(users.c.id).where(users.c.telegram_id == admin_telegram_id)
    ).first()
    if existing_admin is None:
        bind.execute(
            users.insert().values(
                id=uuid.uuid4(),
                telegram_id=admin_telegram_id,
                normalized_username=None,
                display_name="Владелец",
                role="admin",
                is_active=True,
            )
        )
    existing_guest = bind.execute(
        sa.select(users.c.id).where(users.c.normalized_username == "anyasovet")
    ).first()
    if existing_guest is None:
        bind.execute(
            users.insert().values(
                id=uuid.uuid4(),
                telegram_id=None,
                normalized_username="anyasovet",
                display_name="@AnyaSovet",
                role="guest",
                is_active=True,
            )
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("generation_runs"):
        columns = _columns("generation_runs")
        if "workflow_type" in columns:
            op.drop_index("ix_generation_runs_workflow_type", table_name="generation_runs")
            op.drop_column("generation_runs", "workflow_type")
        if "project_id" in columns:
            op.drop_index("ix_generation_runs_project_id", table_name="generation_runs")
            op.drop_constraint("fk_generation_runs_project_id", "generation_runs", type_="foreignkey")
            op.drop_column("generation_runs", "project_id")
    if inspector.has_table("characters") and "voice_description" in _columns("characters"):
        op.drop_column("characters", "voice_description")
    if inspector.has_table("projects") and "owner_user_id" in _columns("projects"):
        op.drop_index("ix_projects_owner_user_id", table_name="projects")
        op.drop_constraint("fk_projects_owner_user_id", "projects", type_="foreignkey")
        op.drop_column("projects", "owner_user_id")
    if inspector.has_table("bot_users"):
        op.drop_table("bot_users")
    generation_workflow_type.drop(op.get_bind(), checkfirst=True)
    bot_user_role.drop(op.get_bind(), checkfirst=True)
