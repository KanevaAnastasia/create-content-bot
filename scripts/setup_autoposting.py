"""Prepare DB tables and project account mappings for autoposting."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, create_tables, get_engine
from app.models import Project, ProjectPublishAccount
from app.publications import upsert_project_publish_accounts_from_env


def _ensure_legacy_columns() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS background_music_asset_uri TEXT"))
        conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS background_music_volume DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE video_results ADD COLUMN IF NOT EXISTS language VARCHAR(8)"))
        conn.execute(text("ALTER TABLE video_results ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE video_results ADD COLUMN IF NOT EXISTS cost_breakdown_json JSON"))


def main() -> None:
    _ensure_legacy_columns()
    create_tables()
    with SessionLocal() as session:
        projects = session.scalars(select(Project)).all()
        if not projects:
            raise RuntimeError("No projects found in DB. Run project seed first.")
        total_accounts = 0
        for project in projects:
            accounts = upsert_project_publish_accounts_from_env(session, project)
            total_accounts += len([row for row in accounts if isinstance(row, ProjectPublishAccount)])
        session.commit()
        print(f"Autoposting schema ready. Projects: {len(projects)}. Publish accounts visible: {total_accounts}.")


if __name__ == "__main__":
    main()
