"""Add the daily automatic quiet-mode cycle.

Revision ID: 20261031_0026
Revises: 20261020_0025
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261031_0026"
down_revision = "20261020_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("bot_users"):
        return
    columns = {column["name"] for column in inspector.get_columns("bot_users")}
    if "quiet_mode_auto_enabled" not in columns:
        op.add_column(
            "bot_users",
            sa.Column("quiet_mode_auto_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "quiet_mode_auto_cycle_date" not in columns:
        op.add_column("bot_users", sa.Column("quiet_mode_auto_cycle_date", sa.Date(), nullable=True))
    if "quiet_mode_auto_generation_count" not in columns:
        op.add_column(
            "bot_users",
            sa.Column("quiet_mode_auto_generation_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "quiet_mode_auto_generation_target" not in columns:
        op.add_column("bot_users", sa.Column("quiet_mode_auto_generation_target", sa.Integer(), nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("bot_users"):
        return
    columns = {column["name"] for column in inspector.get_columns("bot_users")}
    for name in (
        "quiet_mode_auto_generation_target",
        "quiet_mode_auto_generation_count",
        "quiet_mode_auto_cycle_date",
        "quiet_mode_auto_enabled",
    ):
        if name in columns:
            op.drop_column("bot_users", name)
