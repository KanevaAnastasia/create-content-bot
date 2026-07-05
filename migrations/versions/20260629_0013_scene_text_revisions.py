"""Add auditable scene text revision history.

Revision ID: 20260629_0013
Revises: 20260629_0012
Create Date: 2026-06-29 15:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0013"
down_revision = "20260629_0012"
branch_labels = None
depends_on = None


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::json")
    return sa.text("'{}'")


def _now_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("scene_text_revisions"):
        return
    op.create_table(
        "scene_text_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("scene_id", sa.Uuid(), nullable=True),
        sa.Column("scene_order", sa.Integer(), nullable=True),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("old_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("new_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="admin"),
        sa.Column("actor", sa.String(length=256), nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=False, server_default=_json_default()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
        sa.ForeignKeyConstraint(["run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scene_text_revisions_run_id"), "scene_text_revisions", ["run_id"], unique=False)
    op.create_index(op.f("ix_scene_text_revisions_scene_id"), "scene_text_revisions", ["scene_id"], unique=False)
    op.create_index(op.f("ix_scene_text_revisions_field_name"), "scene_text_revisions", ["field_name"], unique=False)
    op.create_index(op.f("ix_scene_text_revisions_language"), "scene_text_revisions", ["language"], unique=False)
    op.create_index(op.f("ix_scene_text_revisions_created_at"), "scene_text_revisions", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scene_text_revisions"):
        return
    op.drop_index(op.f("ix_scene_text_revisions_created_at"), table_name="scene_text_revisions")
    op.drop_index(op.f("ix_scene_text_revisions_language"), table_name="scene_text_revisions")
    op.drop_index(op.f("ix_scene_text_revisions_field_name"), table_name="scene_text_revisions")
    op.drop_index(op.f("ix_scene_text_revisions_scene_id"), table_name="scene_text_revisions")
    op.drop_index(op.f("ix_scene_text_revisions_run_id"), table_name="scene_text_revisions")
    op.drop_table("scene_text_revisions")
