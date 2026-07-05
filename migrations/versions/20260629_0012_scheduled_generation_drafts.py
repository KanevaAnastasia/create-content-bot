"""Add scheduled generation queue and draft snapshots.

Revision ID: 20260629_0012
Revises: 20260628_0011
Create Date: 2026-06-29 02:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0012"
down_revision = "20260628_0011"
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


def _true_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("true")
    return sa.text("1")


def _false_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("false")
    return sa.text("0")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("scheduled_generation_items"):
        op.create_table(
            "scheduled_generation_items",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("recipe", sa.Text(), nullable=False),
            sa.Column("title_color", sa.String(length=32), nullable=False, server_default="#c72638"),
            sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=_true_default()),
            sa.Column("status", sa.String(length=64), nullable=False, server_default="pending"),
            sa.Column("generation_run_id", sa.Uuid(), nullable=True),
            sa.Column("source_row_key", sa.String(length=128), nullable=True),
            sa.Column("source_file_path", sa.Text(), nullable=True),
            sa.Column("error_text", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default()),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
            sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_row_key", name="uq_scheduled_generation_items_source_row_key"),
        )
        op.create_index(
            op.f("ix_scheduled_generation_items_run_at"),
            "scheduled_generation_items",
            ["run_at"],
            unique=False,
        )
        op.create_index(
            op.f("ix_scheduled_generation_items_status"),
            "scheduled_generation_items",
            ["status"],
            unique=False,
        )
        op.create_index(
            op.f("ix_scheduled_generation_items_generation_run_id"),
            "scheduled_generation_items",
            ["generation_run_id"],
            unique=False,
        )

    if not inspector.has_table("generation_drafts"):
        op.create_table(
            "generation_drafts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("chat_id", sa.Integer(), nullable=True),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("stage", sa.String(length=128), nullable=True),
            sa.Column("scene_index", sa.Integer(), nullable=True),
            sa.Column("substep", sa.String(length=128), nullable=True),
            sa.Column("language", sa.String(length=8), nullable=True),
            sa.Column("session_snapshot_json", sa.JSON(), nullable=False, server_default=_json_default()),
            sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
            sa.ForeignKeyConstraint(["run_id"], ["generation_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_generation_drafts_chat_id"), "generation_drafts", ["chat_id"], unique=False)
        op.create_index(op.f("ix_generation_drafts_run_id"), "generation_drafts", ["run_id"], unique=False)
        op.create_index(op.f("ix_generation_drafts_created_at"), "generation_drafts", ["created_at"], unique=False)

    if inspector.has_table("generation_runs"):
        cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "is_draft" not in cols:
            op.add_column(
                "generation_runs",
                sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=_false_default()),
            )
            op.create_index(op.f("ix_generation_runs_is_draft"), "generation_runs", ["is_draft"], unique=False)
        if "scheduled_item_id" not in cols:
            op.add_column("generation_runs", sa.Column("scheduled_item_id", sa.Uuid(), nullable=True))
            op.create_index(
                op.f("ix_generation_runs_scheduled_item_id"),
                "generation_runs",
                ["scheduled_item_id"],
                unique=False,
            )
            op.create_foreign_key(
                "fk_generation_runs_scheduled_item_id",
                "generation_runs",
                "scheduled_generation_items",
                ["scheduled_item_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("generation_runs"):
        cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "scheduled_item_id" in cols:
            op.drop_constraint("fk_generation_runs_scheduled_item_id", "generation_runs", type_="foreignkey")
            op.drop_index(op.f("ix_generation_runs_scheduled_item_id"), table_name="generation_runs")
            op.drop_column("generation_runs", "scheduled_item_id")
        if "is_draft" in cols:
            op.drop_index(op.f("ix_generation_runs_is_draft"), table_name="generation_runs")
            op.drop_column("generation_runs", "is_draft")

    if inspector.has_table("generation_drafts"):
        op.drop_index(op.f("ix_generation_drafts_created_at"), table_name="generation_drafts")
        op.drop_index(op.f("ix_generation_drafts_run_id"), table_name="generation_drafts")
        op.drop_index(op.f("ix_generation_drafts_chat_id"), table_name="generation_drafts")
        op.drop_table("generation_drafts")

    if inspector.has_table("scheduled_generation_items"):
        op.drop_index(op.f("ix_scheduled_generation_items_generation_run_id"), table_name="scheduled_generation_items")
        op.drop_index(op.f("ix_scheduled_generation_items_status"), table_name="scheduled_generation_items")
        op.drop_index(op.f("ix_scheduled_generation_items_run_at"), table_name="scheduled_generation_items")
        op.drop_table("scheduled_generation_items")
