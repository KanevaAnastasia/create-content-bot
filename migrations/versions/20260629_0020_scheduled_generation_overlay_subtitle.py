"""Add optional scheduled overlay subtitle.

Revision ID: 20260629_0020
Revises: 20260629_0019
Create Date: 2026-06-29 22:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0020"
down_revision = "20260629_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scheduled_generation_items"):
        return
    cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
    if "overlay_subtitle" not in cols:
        op.add_column("scheduled_generation_items", sa.Column("overlay_subtitle", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scheduled_generation_items"):
        return
    cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
    if "overlay_subtitle" in cols:
        op.drop_column("scheduled_generation_items", "overlay_subtitle")
