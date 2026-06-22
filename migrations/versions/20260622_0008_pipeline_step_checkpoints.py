"""Add pipeline step checkpoints.

Revision ID: 20260622_0008
Revises: 20260608_0007
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260622_0008"
down_revision = "20260608_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("pipeline_step_checkpoints"):
        return

    op.create_table(
        "pipeline_step_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("generation_run_id", sa.Uuid(), nullable=True),
        sa.Column("step_key", sa.String(length=128), nullable=False),
        sa.Column("scene_index", sa.Integer(), nullable=True),
        sa.Column("substep", sa.String(length=128), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("session_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pipeline_step_checkpoints_chat_id"),
        "pipeline_step_checkpoints",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pipeline_step_checkpoints_generation_run_id"),
        "pipeline_step_checkpoints",
        ["generation_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pipeline_step_checkpoints_step_key"),
        "pipeline_step_checkpoints",
        ["step_key"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("pipeline_step_checkpoints"):
        return

    op.drop_index(op.f("ix_pipeline_step_checkpoints_step_key"), table_name="pipeline_step_checkpoints")
    op.drop_index(op.f("ix_pipeline_step_checkpoints_generation_run_id"), table_name="pipeline_step_checkpoints")
    op.drop_index(op.f("ix_pipeline_step_checkpoints_chat_id"), table_name="pipeline_step_checkpoints")
    op.drop_table("pipeline_step_checkpoints")
