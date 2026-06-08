"""Add publication tables and project media settings.

Revision ID: 20260608_0006
Revises: 20260603_0005
Create Date: 2026-06-08 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260608_0006"
down_revision = "20260603_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("projects")}
    if "background_music_asset_uri" not in columns:
        op.add_column("projects", sa.Column("background_music_asset_uri", sa.Text(), nullable=True))
    if "background_music_volume" not in columns:
        op.add_column("projects", sa.Column("background_music_volume", sa.Float(), nullable=True))
    if not inspector.has_table("project_publish_accounts"):
        op.create_table(
            "project_publish_accounts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("platform", sa.String(length=64), nullable=False),
            sa.Column("language", sa.String(length=8), nullable=False),
            sa.Column("account_key", sa.String(length=128), nullable=False),
            sa.Column("external_account_id", sa.Text(), nullable=True),
            sa.Column("publish_policy", sa.String(length=64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "project_id",
                "platform",
                "language",
                "account_key",
                name="uq_project_publish_accounts_project_platform_language_key",
            ),
        )
    if not inspector.has_table("publication_jobs"):
        op.create_table(
            "publication_jobs",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("bundle_key", sa.String(length=128), nullable=False),
            sa.Column("origin", sa.String(length=64), nullable=False),
            sa.Column("platform", sa.String(length=64), nullable=False),
            sa.Column("language", sa.String(length=8), nullable=False),
            sa.Column("account_key", sa.String(length=128), nullable=False),
            sa.Column("external_account_id", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("video_path", sa.Text(), nullable=False),
            sa.Column("video_label", sa.String(length=128), nullable=True),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("hashtags_json", sa.JSON(), nullable=False),
            sa.Column("recipe", sa.Text(), nullable=True),
            sa.Column("publish_policy", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("external_post_id", sa.Text(), nullable=True),
            sa.Column("publish_error", sa.Text(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "bundle_key",
                "platform",
                "language",
                "account_key",
                name="uq_publication_jobs_bundle_platform_language_account",
            ),
        )
        op.create_index(op.f("ix_publication_jobs_bundle_key"), "publication_jobs", ["bundle_key"], unique=False)
        op.create_index(op.f("ix_publication_jobs_status"), "publication_jobs", ["status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("projects")}
    if inspector.has_table("publication_jobs"):
        op.drop_index(op.f("ix_publication_jobs_status"), table_name="publication_jobs")
        op.drop_index(op.f("ix_publication_jobs_bundle_key"), table_name="publication_jobs")
        op.drop_table("publication_jobs")
    if inspector.has_table("project_publish_accounts"):
        op.drop_table("project_publish_accounts")
    if "background_music_volume" in columns:
        op.drop_column("projects", "background_music_volume")
    if "background_music_asset_uri" in columns:
        op.drop_column("projects", "background_music_asset_uri")
