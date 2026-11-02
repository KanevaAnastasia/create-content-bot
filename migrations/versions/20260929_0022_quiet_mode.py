"""Add persistent owner-controlled quiet mode.

Revision ID: 20260929_0022
Revises: 20260928_0021
Create Date: 2026-09-29 23:40:00.000000
"""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260929_0022"
down_revision = "20260928_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("bot_users"):
        return
    columns = {column["name"] for column in inspector.get_columns("bot_users")}
    if "quiet_mode_enabled" not in columns:
        op.add_column(
            "bot_users",
            sa.Column("quiet_mode_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    users = sa.table(
        "bot_users",
        sa.column("telegram_id", sa.BigInteger()),
        sa.column("normalized_username", sa.String()),
        sa.column("role", sa.String()),
    )
    admin_id_raw = str(os.getenv("SCHEDULED_GENERATION_ADMIN_CHAT_ID") or "305928305").strip()
    if admin_id_raw.isdigit():
        bind.execute(
            users.update()
            .where(
                users.c.telegram_id == int(admin_id_raw),
                users.c.role == "admin",
                users.c.normalized_username.is_(None),
            )
            .values(normalized_username="a_kaneva")
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("bot_users"):
        columns = {column["name"] for column in inspector.get_columns("bot_users")}
        if "quiet_mode_enabled" in columns:
            op.drop_column("bot_users", "quiet_mode_enabled")
