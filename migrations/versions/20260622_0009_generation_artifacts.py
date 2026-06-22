"""Add generation artifact index.

Revision ID: 20260622_0009
Revises: 20260622_0008
Create Date: 2026-06-22 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260622_0009"
down_revision = "20260622_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("generation_runs"):
        run_cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "human_title" not in run_cols:
            op.add_column("generation_runs", sa.Column("human_title", sa.Text(), nullable=True))

    if not inspector.has_table("generation_artifacts"):
        op.create_table(
            "generation_artifacts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("scene_id", sa.Uuid(), nullable=True),
            sa.Column("artifact_type", sa.String(length=64), nullable=False),
            sa.Column("label", sa.Text(), nullable=False),
            sa.Column("uri", sa.Text(), nullable=False),
            sa.Column("language", sa.String(length=8), nullable=True),
            sa.Column("mime_type", sa.String(length=128), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["generation_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_generation_artifacts_run_id"), "generation_artifacts", ["run_id"], unique=False)
        op.create_index(op.f("ix_generation_artifacts_scene_id"), "generation_artifacts", ["scene_id"], unique=False)
        op.create_index(op.f("ix_generation_artifacts_artifact_type"), "generation_artifacts", ["artifact_type"], unique=False)
        op.create_index(op.f("ix_generation_artifacts_language"), "generation_artifacts", ["language"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("generation_artifacts"):
        op.drop_index(op.f("ix_generation_artifacts_language"), table_name="generation_artifacts")
        op.drop_index(op.f("ix_generation_artifacts_artifact_type"), table_name="generation_artifacts")
        op.drop_index(op.f("ix_generation_artifacts_scene_id"), table_name="generation_artifacts")
        op.drop_index(op.f("ix_generation_artifacts_run_id"), table_name="generation_artifacts")
        op.drop_table("generation_artifacts")

    if inspector.has_table("generation_runs"):
        run_cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "human_title" in run_cols:
            op.drop_column("generation_runs", "human_title")
