"""Published video analytics feedback loop.

Revision ID: 20260525_0004
Revises: 20260520_0003
Create Date: 2026-05-25 14:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260525_0004"
down_revision = "20260520_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("published_video_analytics"):
        return
    op.create_table(
        "published_video_analytics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("recipe", sa.Text(), nullable=True),
        sa.Column("format", sa.String(length=128), nullable=True),
        sa.Column("hook", sa.Text(), nullable=True),
        sa.Column("first_frame_type", sa.String(length=128), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("reach", sa.Integer(), nullable=True),
        sa.Column("average_view_duration", sa.Float(), nullable=True),
        sa.Column("average_percentage_viewed", sa.Float(), nullable=True),
        sa.Column("retention_3s", sa.Float(), nullable=True),
        sa.Column("retention_5s", sa.Float(), nullable=True),
        sa.Column("retention_10s", sa.Float(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("saves", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("follows", sa.Integer(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("published_video_analytics")
