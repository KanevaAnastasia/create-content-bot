"""Update the Discipline project voice.

Revision ID: 20261020_0024
Revises: 20261019_0023
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261020_0024"
down_revision = "20261019_0023"
branch_labels = None
depends_on = None


_NEW_VOICE_ID = "EvnFqTSGPvDU3JBwJbCC"
_OLD_VOICE_ID = "nkvv6waLzFah2jTNUZGT"


def upgrade() -> None:
    projects = sa.table(
        "projects",
        sa.column("slug", sa.String()),
        sa.column("elevenlabs_voice_id", sa.String()),
    )
    op.get_bind().execute(
        projects.update()
        .where(projects.c.slug == "discipline")
        .values(elevenlabs_voice_id=_NEW_VOICE_ID)
    )


def downgrade() -> None:
    projects = sa.table(
        "projects",
        sa.column("slug", sa.String()),
        sa.column("elevenlabs_voice_id", sa.String()),
    )
    op.get_bind().execute(
        projects.update()
        .where(projects.c.slug == "discipline")
        .values(elevenlabs_voice_id=_OLD_VOICE_ID)
    )
