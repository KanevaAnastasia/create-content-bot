"""Visual props catalog.

Revision ID: 20260520_0003
Revises: 20260519_0002
Create Date: 2026-05-20 18:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260520_0003"
down_revision = "20260519_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("visual_props"):
        op.create_table(
            "visual_props",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("canonical_key", sa.String(length=128), nullable=False),
            sa.Column("name_ru", sa.Text(), nullable=False),
            sa.Column("description_ru", sa.Text(), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "canonical_key", name="uq_visual_props_project_key"),
        )


def downgrade() -> None:
    op.drop_table("visual_props")
