"""Add persistent chat sessions and scene media candidate tables.

Revision ID: 20260608_0007
Revises: 20260608_0006
Create Date: 2026-06-08 16:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260608_0007"
down_revision = "20260608_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("chat_sessions"):
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("chat_id", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=128), nullable=True),
            sa.Column("session_json", sa.JSON(), nullable=False),
            sa.Column("generation_run_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_chat_sessions_chat_id"), "chat_sessions", ["chat_id"], unique=True)

    if not inspector.has_table("scene_image_candidates"):
        op.create_table(
            "scene_image_candidates",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("candidate_id", sa.String(length=64), nullable=False),
            sa.Column("scene_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("image_blob", sa.LargeBinary(), nullable=False),
            sa.Column("liked", sa.Boolean(), nullable=False),
            sa.Column("approved_for_video", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_scene_image_candidates_candidate_id"), "scene_image_candidates", ["candidate_id"], unique=True)
        op.create_index(op.f("ix_scene_image_candidates_scene_id"), "scene_image_candidates", ["scene_id"], unique=False)

    if not inspector.has_table("scene_video_candidates"):
        op.create_table(
            "scene_video_candidates",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("candidate_id", sa.String(length=64), nullable=False),
            sa.Column("scene_id", sa.Uuid(), nullable=False),
            sa.Column("language", sa.String(length=8), nullable=False),
            sa.Column("video_blob", sa.LargeBinary(), nullable=False),
            sa.Column("liked", sa.Boolean(), nullable=False),
            sa.Column("approved_for_production", sa.Boolean(), nullable=False),
            sa.Column("speed_coef", sa.Float(), nullable=False),
            sa.Column("source_image_candidate_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_scene_video_candidates_candidate_id"), "scene_video_candidates", ["candidate_id"], unique=True)
        op.create_index(op.f("ix_scene_video_candidates_language"), "scene_video_candidates", ["language"], unique=False)
        op.create_index(op.f("ix_scene_video_candidates_scene_id"), "scene_video_candidates", ["scene_id"], unique=False)

    if not inspector.has_table("scene_media_message_links"):
        op.create_table(
            "scene_media_message_links",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("chat_id", sa.Integer(), nullable=False),
            sa.Column("telegram_message_id", sa.Integer(), nullable=False),
            sa.Column("scene_id", sa.Uuid(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("candidate_id", sa.String(length=64), nullable=False),
            sa.Column("language", sa.String(length=8), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("chat_id", "telegram_message_id", name="uq_scene_media_message_links_chat_message"),
        )
        op.create_index(op.f("ix_scene_media_message_links_candidate_id"), "scene_media_message_links", ["candidate_id"], unique=False)
        op.create_index(op.f("ix_scene_media_message_links_chat_id"), "scene_media_message_links", ["chat_id"], unique=False)
        op.create_index(op.f("ix_scene_media_message_links_scene_id"), "scene_media_message_links", ["scene_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("scene_media_message_links"):
        op.drop_index(op.f("ix_scene_media_message_links_scene_id"), table_name="scene_media_message_links")
        op.drop_index(op.f("ix_scene_media_message_links_chat_id"), table_name="scene_media_message_links")
        op.drop_index(op.f("ix_scene_media_message_links_candidate_id"), table_name="scene_media_message_links")
        op.drop_table("scene_media_message_links")
    if inspector.has_table("scene_video_candidates"):
        op.drop_index(op.f("ix_scene_video_candidates_scene_id"), table_name="scene_video_candidates")
        op.drop_index(op.f("ix_scene_video_candidates_language"), table_name="scene_video_candidates")
        op.drop_index(op.f("ix_scene_video_candidates_candidate_id"), table_name="scene_video_candidates")
        op.drop_table("scene_video_candidates")
    if inspector.has_table("scene_image_candidates"):
        op.drop_index(op.f("ix_scene_image_candidates_scene_id"), table_name="scene_image_candidates")
        op.drop_index(op.f("ix_scene_image_candidates_candidate_id"), table_name="scene_image_candidates")
        op.drop_table("scene_image_candidates")
    if inspector.has_table("chat_sessions"):
        op.drop_index(op.f("ix_chat_sessions_chat_id"), table_name="chat_sessions")
        op.drop_table("chat_sessions")
