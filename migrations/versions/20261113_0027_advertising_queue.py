"""Durable advertising products and a Moscow 09:00 two-day cadence."""
from alembic import op
import sqlalchemy as sa

revision = "20261113_0027"
down_revision = "20261031_0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "advertising_queue_schedule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
    )
    schedule = sa.table("advertising_queue_schedule", sa.column("id", sa.Integer()),
                        sa.column("enabled", sa.Boolean()), sa.column("next_run_at", sa.DateTime(timezone=True)))
    from datetime import datetime, timezone
    op.bulk_insert(schedule, [{"id": 1, "enabled": True,
                              "next_run_at": datetime(2026, 11, 15, 6, tzinfo=timezone.utc)}])
    op.create_table(
        "advertising_products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("images", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("prepared", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("refill_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_advertising_products_status", "advertising_products", ["status"])


def downgrade():
    op.drop_table("advertising_products")
    op.drop_table("advertising_queue_schedule")
