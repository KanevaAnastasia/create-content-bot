"""Add content series catalog and generation run link.

Revision ID: 20260623_0010
Revises: 20260622_0009
Create Date: 2026-06-23 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260623_0010"
down_revision = "20260622_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("content_series"):
        op.create_table(
            "content_series",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=True),
            sa.Column("slug", sa.String(length=128), nullable=False),
            sa.Column("title_ru", sa.Text(), nullable=False),
            sa.Column("title_en", sa.Text(), nullable=False),
            sa.Column("current_video_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("total_videos", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "slug", name="uq_content_series_project_slug"),
        )
        op.create_index(op.f("ix_content_series_project_id"), "content_series", ["project_id"], unique=False)
        op.create_index(op.f("ix_content_series_is_active"), "content_series", ["is_active"], unique=False)

    if inspector.has_table("generation_runs"):
        run_cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "series_id" not in run_cols:
            op.add_column("generation_runs", sa.Column("series_id", sa.Uuid(), nullable=True))
            op.create_index(op.f("ix_generation_runs_series_id"), "generation_runs", ["series_id"], unique=False)
            op.create_foreign_key(
                "fk_generation_runs_series_id_content_series",
                "generation_runs",
                "content_series",
                ["series_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "series_video_index" not in run_cols:
            op.add_column("generation_runs", sa.Column("series_video_index", sa.Integer(), nullable=True))
        if "series_total_videos" not in run_cols:
            op.add_column("generation_runs", sa.Column("series_total_videos", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("generation_runs"):
        run_cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "series_id" in run_cols:
            op.drop_constraint("fk_generation_runs_series_id_content_series", "generation_runs", type_="foreignkey")
            op.drop_index(op.f("ix_generation_runs_series_id"), table_name="generation_runs")
            op.drop_column("generation_runs", "series_id")
        if "series_total_videos" in run_cols:
            op.drop_column("generation_runs", "series_total_videos")
        if "series_video_index" in run_cols:
            op.drop_column("generation_runs", "series_video_index")

    if inspector.has_table("content_series"):
        op.drop_index(op.f("ix_content_series_is_active"), table_name="content_series")
        op.drop_index(op.f("ix_content_series_project_id"), table_name="content_series")
        op.drop_table("content_series")
