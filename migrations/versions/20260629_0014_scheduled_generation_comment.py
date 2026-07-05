"""Add scheduled generation comment.

Revision ID: 20260629_0014
Revises: 20260629_0013
Create Date: 2026-06-29 16:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0014"
down_revision = "20260629_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("scheduled_generation_items"):
        cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
        if "comment" not in cols:
            op.add_column("scheduled_generation_items", sa.Column("comment", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("scheduled_generation_items"):
        cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
        if "comment" in cols:
            op.drop_column("scheduled_generation_items", "comment")
