"""Store scene candidate media by URI.

Revision ID: 20260629_0017
Revises: 20260629_0016
Create Date: 2026-06-29 20:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0017"
down_revision = "20260629_0016"
branch_labels = None
depends_on = None


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_candidate_uri_columns(table_name: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table_name):
        return
    existing = _columns(inspector, table_name)
    if "media_uri" not in existing:
        op.add_column(table_name, sa.Column("media_uri", sa.Text(), nullable=True))
    if "mime_type" not in existing:
        op.add_column(table_name, sa.Column("mime_type", sa.String(length=128), nullable=True))
    if "byte_size" not in existing:
        op.add_column(table_name, sa.Column("byte_size", sa.Integer(), nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    _add_candidate_uri_columns("scene_image_candidates")
    _add_candidate_uri_columns("scene_video_candidates")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("scene_image_candidates") as batch_op:
            batch_op.alter_column("image_blob", existing_type=sa.LargeBinary(), nullable=True)
        with op.batch_alter_table("scene_video_candidates") as batch_op:
            batch_op.alter_column("video_blob", existing_type=sa.LargeBinary(), nullable=True)
        return
    if inspector.has_table("scene_image_candidates"):
        op.alter_column("scene_image_candidates", "image_blob", existing_type=sa.LargeBinary(), nullable=True)
    if inspector.has_table("scene_video_candidates"):
        op.alter_column("scene_video_candidates", "video_blob", existing_type=sa.LargeBinary(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table_name in ("scene_image_candidates", "scene_video_candidates"):
        if not inspector.has_table(table_name):
            continue
        existing = _columns(inspector, table_name)
        if "byte_size" in existing:
            op.drop_column(table_name, "byte_size")
        if "mime_type" in existing:
            op.drop_column(table_name, "mime_type")
        if "media_uri" in existing:
            op.drop_column(table_name, "media_uri")
