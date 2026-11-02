"""Store scene video candidate text snapshots on candidate rows.

Revision ID: 20260629_0019
Revises: 20260629_0018
Create Date: 2026-07-14 18:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260629_0019"
down_revision = "20260629_0018"
branch_labels = None
depends_on = None


SNAPSHOT_COLUMNS = (
    "video_prompt_by_lang",
    "speech_by_lang",
    "speech_cues_by_lang",
    "speech_plan_by_lang",
    "video_speech_qa_by_lang",
    "generation_params_json",
)


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::json")
    return sa.text("'{}'")


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scene_video_candidates"):
        return
    existing = _columns(inspector, "scene_video_candidates")
    for column_name in SNAPSHOT_COLUMNS:
        if column_name not in existing:
            op.add_column(
                "scene_video_candidates",
                sa.Column(column_name, sa.JSON(), nullable=False, server_default=_json_default()),
            )

    if not inspector.has_table("generation_artifacts"):
        return
    video_candidates = sa.table(
        "scene_video_candidates",
        sa.column("candidate_id", sa.String()),
        *(sa.column(column_name, sa.JSON()) for column_name in SNAPSHOT_COLUMNS),
    )
    artifacts = sa.table(
        "generation_artifacts",
        sa.column("artifact_type", sa.String()),
        sa.column("uri", sa.Text()),
        sa.column("payload_json", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(artifacts.c.uri, artifacts.c.payload_json).where(
            artifacts.c.artifact_type == "scene_video_candidate"
        )
    ).fetchall()
    for uri, payload in rows:
        if not isinstance(payload, dict):
            continue
        candidate_id = str(payload.get("candidate_id") or "").strip()
        if not candidate_id:
            raw_uri = str(uri or "").strip()
            if raw_uri.startswith("db://scene_video_candidates/"):
                candidate_id = raw_uri.rsplit("/", 1)[-1]
        if not candidate_id:
            continue
        values = {
            "video_prompt_by_lang": payload.get("video_prompt_by_lang") if isinstance(payload.get("video_prompt_by_lang"), dict) else {},
            "speech_by_lang": payload.get("speech_by_lang") if isinstance(payload.get("speech_by_lang"), dict) else {},
            "speech_cues_by_lang": payload.get("speech_cues_by_lang") if isinstance(payload.get("speech_cues_by_lang"), dict) else {},
            "speech_plan_by_lang": payload.get("speech_plan_by_lang") if isinstance(payload.get("speech_plan_by_lang"), dict) else {},
            "video_speech_qa_by_lang": payload.get("video_speech_qa_by_lang") if isinstance(payload.get("video_speech_qa_by_lang"), dict) else {},
            "generation_params_json": payload.get("generation_params_json") if isinstance(payload.get("generation_params_json"), dict) else {},
        }
        if not any(values.values()):
            continue
        bind.execute(
            video_candidates.update()
            .where(video_candidates.c.candidate_id == candidate_id)
            .values(**values)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("scene_video_candidates"):
        return
    existing = _columns(inspector, "scene_video_candidates")
    for column_name in reversed(SNAPSHOT_COLUMNS):
        if column_name in existing:
            op.drop_column("scene_video_candidates", column_name)
