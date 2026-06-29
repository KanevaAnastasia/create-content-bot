"""Persist generation cursor and scene prompts.

Revision ID: 20260628_0011
Revises: 20260623_0010
Create Date: 2026-06-28 20:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260628_0011"
down_revision = "20260623_0010"
branch_labels = None
depends_on = None


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::json")
    return sa.text("'{}'")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("generation_runs"):
        cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "is_active" not in cols:
            op.add_column(
                "generation_runs",
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            )
            op.create_index(op.f("ix_generation_runs_is_active"), "generation_runs", ["is_active"], unique=False)
        if "current_scene_index" not in cols:
            op.add_column("generation_runs", sa.Column("current_scene_index", sa.Integer(), nullable=True))
        if "current_media_substep" not in cols:
            op.add_column("generation_runs", sa.Column("current_media_substep", sa.String(length=128), nullable=True))
        if "current_media_language_index" not in cols:
            op.add_column("generation_runs", sa.Column("current_media_language_index", sa.Integer(), nullable=True))

    if inspector.has_table("scenes"):
        cols = {col["name"] for col in inspector.get_columns("scenes")}
        if "start_frame_prompt" not in cols:
            op.add_column("scenes", sa.Column("start_frame_prompt", sa.Text(), nullable=True))
        if "start_frame_prompt_effective" not in cols:
            op.add_column("scenes", sa.Column("start_frame_prompt_effective", sa.Text(), nullable=True))
        if "end_frame_prompt" not in cols:
            op.add_column("scenes", sa.Column("end_frame_prompt", sa.Text(), nullable=True))
        if "video_prompt_by_lang" not in cols:
            op.add_column(
                "scenes",
                sa.Column("video_prompt_by_lang", sa.JSON(), nullable=False, server_default=_json_default()),
            )
        if "speech_by_lang" not in cols:
            op.add_column(
                "scenes",
                sa.Column("speech_by_lang", sa.JSON(), nullable=False, server_default=_json_default()),
            )
        if "syntax_prompt_by_lang" not in cols:
            op.add_column(
                "scenes",
                sa.Column("syntax_prompt_by_lang", sa.JSON(), nullable=False, server_default=_json_default()),
            )
        if "prompt_meta_json" not in cols:
            op.add_column(
                "scenes",
                sa.Column("prompt_meta_json", sa.JSON(), nullable=False, server_default=_json_default()),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("scenes"):
        cols = {col["name"] for col in inspector.get_columns("scenes")}
        for name in (
            "prompt_meta_json",
            "syntax_prompt_by_lang",
            "speech_by_lang",
            "video_prompt_by_lang",
            "end_frame_prompt",
            "start_frame_prompt_effective",
            "start_frame_prompt",
        ):
            if name in cols:
                op.drop_column("scenes", name)

    if inspector.has_table("generation_runs"):
        cols = {col["name"] for col in inspector.get_columns("generation_runs")}
        if "current_media_language_index" in cols:
            op.drop_column("generation_runs", "current_media_language_index")
        if "current_media_substep" in cols:
            op.drop_column("generation_runs", "current_media_substep")
        if "current_scene_index" in cols:
            op.drop_column("generation_runs", "current_scene_index")
        if "is_active" in cols:
            op.drop_index(op.f("ix_generation_runs_is_active"), table_name="generation_runs")
            op.drop_column("generation_runs", "is_active")
