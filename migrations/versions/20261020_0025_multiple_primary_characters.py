"""Allow several primary recurring characters in a project.

Revision ID: 20261020_0025
Revises: 20261020_0024
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261020_0025"
down_revision = "20261020_0024"
branch_labels = None
depends_on = None


_INDEX_NAME = "uq_project_characters_one_primary"


def upgrade() -> None:
    indexes = {item["name"] for item in inspect(op.get_bind()).get_indexes("project_characters")}
    if _INDEX_NAME in indexes:
        op.drop_index(_INDEX_NAME, table_name="project_characters")


def downgrade() -> None:
    bind = op.get_bind()
    links = sa.table(
        "project_characters",
        sa.column("project_id", sa.Uuid()),
        sa.column("character_id", sa.Uuid()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_primary", sa.Boolean()),
    )
    rows = bind.execute(
        sa.select(links.c.project_id, links.c.character_id)
        .where(links.c.is_primary.is_(True))
        .order_by(links.c.project_id, links.c.sort_order, links.c.character_id)
    ).all()
    seen_projects: set[object] = set()
    for project_id, character_id in rows:
        if project_id in seen_projects:
            bind.execute(
                links.update()
                .where(links.c.project_id == project_id, links.c.character_id == character_id)
                .values(is_primary=False)
            )
        else:
            seen_projects.add(project_id)
    op.create_index(
        _INDEX_NAME,
        "project_characters",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
        sqlite_where=sa.text("is_primary = 1"),
    )
