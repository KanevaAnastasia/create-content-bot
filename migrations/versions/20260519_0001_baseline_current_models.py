"""Baseline current generation models.

Revision ID: 20260519_0001
Revises:
Create Date: 2026-05-19 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260519_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    generation_language_type = postgresql.ENUM("ru", "en", name="generation_language")
    video_provider_type = postgresql.ENUM("veo", "telegram", name="video_provider")
    video_result_status_type = postgresql.ENUM("pending", "success", "failed", name="video_result_status")
    generation_language = postgresql.ENUM("ru", "en", name="generation_language", create_type=False)
    video_provider = postgresql.ENUM("veo", "telegram", name="video_provider", create_type=False)
    video_result_status = postgresql.ENUM("pending", "success", "failed", name="video_result_status", create_type=False)

    generation_language_type.create(bind, checkfirst=True)
    video_provider_type.create(bind, checkfirst=True)
    video_result_status_type.create(bind, checkfirst=True)

    if not inspector.has_table("generation_runs"):
        op.create_table(
            "generation_runs",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("language", generation_language, nullable=False),
            sa.Column("script", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not inspector.has_table("scenes"):
        op.create_table(
            "scenes",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("scene_order", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("duration_sec", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["generation_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "scene_order", name="uq_scenes_run_scene_order"),
        )

    if not inspector.has_table("video_results"):
        op.create_table(
            "video_results",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("scene_id", sa.Uuid(), nullable=False),
            sa.Column("provider", video_provider, nullable=False),
            sa.Column("status", video_result_status, nullable=False),
            sa.Column("video_path", sa.Text(), nullable=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["generation_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table("video_results")
    op.drop_table("scenes")
    op.drop_table("generation_runs")

    video_result_status = sa.Enum("pending", "success", "failed", name="video_result_status")
    video_provider = sa.Enum("veo", "telegram", name="video_provider")
    generation_language = sa.Enum("ru", "en", name="generation_language")

    video_result_status.drop(op.get_bind(), checkfirst=True)
    video_provider.drop(op.get_bind(), checkfirst=True)
    generation_language.drop(op.get_bind(), checkfirst=True)
