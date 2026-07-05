"""Add scheduled generation event log.

Revision ID: 20260629_0016
Revises: 20260629_0015
Create Date: 2026-06-29 17:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0016"
down_revision = "20260629_0015"
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
    if inspector.has_table("scheduled_generation_events"):
        return
    op.create_table(
        "scheduled_generation_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_item_id", sa.Uuid(), nullable=False),
        sa.Column("generation_run_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
        sa.ForeignKeyConstraint(["scheduled_item_id"], ["scheduled_generation_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scheduled_generation_events_scheduled_item_id"), "scheduled_generation_events", ["scheduled_item_id"], unique=False)
    op.create_index(op.f("ix_scheduled_generation_events_generation_run_id"), "scheduled_generation_events", ["generation_run_id"], unique=False)
    op.create_index(op.f("ix_scheduled_generation_events_event_type"), "scheduled_generation_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_scheduled_generation_events_stage"), "scheduled_generation_events", ["stage"], unique=False)
    op.create_index(op.f("ix_scheduled_generation_events_status"), "scheduled_generation_events", ["status"], unique=False)
    op.create_index(op.f("ix_scheduled_generation_events_created_at"), "scheduled_generation_events", ["created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scheduled_generation_events"):
        return
    op.drop_index(op.f("ix_scheduled_generation_events_created_at"), table_name="scheduled_generation_events")
    op.drop_index(op.f("ix_scheduled_generation_events_status"), table_name="scheduled_generation_events")
    op.drop_index(op.f("ix_scheduled_generation_events_stage"), table_name="scheduled_generation_events")
    op.drop_index(op.f("ix_scheduled_generation_events_event_type"), table_name="scheduled_generation_events")
    op.drop_index(op.f("ix_scheduled_generation_events_generation_run_id"), table_name="scheduled_generation_events")
    op.drop_index(op.f("ix_scheduled_generation_events_scheduled_item_id"), table_name="scheduled_generation_events")
    op.drop_table("scheduled_generation_events")
