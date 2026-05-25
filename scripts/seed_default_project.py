from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.project_seed import seed_default_project


def main() -> None:
    with SessionLocal() as session:
        stats = seed_default_project(session)
    print(
        "Seeded default project "
        f"{stats.project_slug}: {stats.characters} characters, "
        f"{stats.locations} locations, {stats.visual_props} visual props, "
        f"{stats.text_steps} text steps."
    )


if __name__ == "__main__":
    main()
