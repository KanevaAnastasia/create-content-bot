"""Add series to scheduled generation items.

Revision ID: 20260629_0015
Revises: 20260629_0014
Create Date: 2026-06-29 16:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0015"
down_revision = "20260629_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scheduled_generation_items"):
        return
    cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
    if "series_id" not in cols:
        op.add_column("scheduled_generation_items", sa.Column("series_id", sa.Uuid(), nullable=True))
        op.create_index(
            op.f("ix_scheduled_generation_items_series_id"),
            "scheduled_generation_items",
            ["series_id"],
            unique=False,
        )
        if inspector.has_table("content_series"):
            op.create_foreign_key(
                "fk_scheduled_generation_items_series_id",
                "scheduled_generation_items",
                "content_series",
                ["series_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scheduled_generation_items"):
        return
    cols = {col["name"] for col in inspector.get_columns("scheduled_generation_items")}
    if "series_id" in cols:
        if inspector.has_table("content_series"):
            op.drop_constraint("fk_scheduled_generation_items_series_id", "scheduled_generation_items", type_="foreignkey")
        op.drop_index(op.f("ix_scheduled_generation_items_series_id"), table_name="scheduled_generation_items")
        op.drop_column("scheduled_generation_items", "series_id")
