"""Video result estimated cost fields.

Revision ID: 20260603_0005
Revises: 20260525_0004
Create Date: 2026-06-03 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260603_0005"
down_revision = "20260525_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {col["name"] for col in inspector.get_columns("video_results")} if inspector.has_table("video_results") else set()
    if "language" not in cols:
        op.add_column("video_results", sa.Column("language", sa.String(length=8), nullable=True))
    if "estimated_cost_usd" not in cols:
        op.add_column("video_results", sa.Column("estimated_cost_usd", sa.Float(), nullable=True))
    if "cost_breakdown_json" not in cols:
        op.add_column("video_results", sa.Column("cost_breakdown_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_results", "cost_breakdown_json")
    op.drop_column("video_results", "estimated_cost_usd")
    op.drop_column("video_results", "language")
