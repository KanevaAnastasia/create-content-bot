"""Multiproject schema v1.

Revision ID: 20260519_0002
Revises: 20260519_0001
Create Date: 2026-05-19 00:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260519_0002"
down_revision = "20260519_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("projects"):
        op.create_table(
            "projects",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("slug", sa.String(length=128), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("final_scene_prompt", sa.Text(), nullable=True),
            sa.Column("title_preset", sa.Text(), nullable=True),
            sa.Column("hashtags_fixed_ru", sa.JSON(), nullable=False),
            sa.Column("hashtags_fixed_en", sa.JSON(), nullable=False),
            sa.Column("default_video_provider", sa.String(length=64), nullable=True),
            sa.Column("allowed_video_models", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("slug"),
        )
    index_names = {idx["name"] for idx in inspector.get_indexes("projects")} if inspector.has_table("projects") else set()
    if "uq_projects_single_default" not in index_names:
        op.create_index(
            "uq_projects_single_default",
            "projects",
            ["is_default"],
            unique=True,
            postgresql_where=sa.text("is_default = true"),
        )

    if not inspector.has_table("characters"):
        op.create_table(
            "characters",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("canonical_key", sa.String(length=128), nullable=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("default_asset_uri", sa.Text(), nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("canonical_key"),
        )

    if not inspector.has_table("locations"):
        op.create_table(
            "locations",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("canonical_code", sa.String(length=128), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("default_asset_uri", sa.Text(), nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("canonical_code"),
        )

    if not inspector.has_table("project_characters"):
        op.create_table(
            "project_characters",
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("character_id", sa.Uuid(), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=True),
            sa.Column("asset_uri", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("project_id", "character_id"),
        )

    if not inspector.has_table("project_locations"):
        op.create_table(
            "project_locations",
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("location_id", sa.Uuid(), nullable=False),
            sa.Column("asset_uri", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("meta_json", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("project_id", "location_id"),
        )

    if not inspector.has_table("text_steps"):
        op.create_table(
            "text_steps",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("step_key", sa.String(length=128), nullable=False),
            sa.Column("step_order", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("base_system_prompt", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("current_revision_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "step_key", name="uq_text_steps_project_step_key"),
            sa.UniqueConstraint("project_id", "step_order", name="uq_text_steps_project_step_order"),
        )

    if not inspector.has_table("text_step_revisions"):
        op.create_table(
            "text_step_revisions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("text_step_id", sa.Uuid(), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("base_prompt", sa.Text(), nullable=False),
            sa.Column("context_snapshot", sa.JSON(), nullable=False),
            sa.Column("user_feedback", sa.Text(), nullable=True),
            sa.Column("model_response", sa.Text(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("completion_tokens", sa.Integer(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("error_text", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["text_step_id"], ["text_steps.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("text_step_id", "revision_no", name="uq_text_step_revisions_step_revision_no"),
        )

    fk_names = {
        fk["name"]
        for fk in inspector.get_foreign_keys("text_steps")
    } if inspector.has_table("text_steps") else set()
    if "fk_text_steps_current_revision_id" not in fk_names:
        op.create_foreign_key(
            "fk_text_steps_current_revision_id",
            "text_steps",
            "text_step_revisions",
            ["current_revision_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint("fk_text_steps_current_revision_id", "text_steps", type_="foreignkey")
    op.drop_table("text_step_revisions")
    op.drop_table("text_steps")
    op.drop_table("project_locations")
    op.drop_table("project_characters")
    op.drop_table("locations")
    op.drop_table("characters")
    op.drop_index("uq_projects_single_default", table_name="projects")
    op.drop_table("projects")
