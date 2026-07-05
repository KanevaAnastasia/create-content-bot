"""Store title color on generation runs.

Revision ID: 20260629_0018
Revises: 20260629_0017
Create Date: 2026-06-29 20:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0018"
down_revision = "20260629_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("generation_runs"):
        return
    cols = {col["name"] for col in inspector.get_columns("generation_runs")}
    if "title_color" not in cols:
        op.add_column(
            "generation_runs",
            sa.Column("title_color", sa.String(length=32), nullable=False, server_default="#c72638"),
        )
    if inspector.has_table("scheduled_generation_items"):
        op.execute(
            """
            UPDATE generation_runs AS gr
            SET title_color = COALESCE(NULLIF(sgi.title_color, ''), gr.title_color)
            FROM scheduled_generation_items AS sgi
            WHERE gr.scheduled_item_id = sgi.id
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("generation_runs"):
        return
    cols = {col["name"] for col in inspector.get_columns("generation_runs")}
    if "title_color" in cols:
        op.drop_column("generation_runs", "title_color")
