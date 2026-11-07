"""Durable copies of the exact publication text shown in Telegram."""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import SessionLocal
from .models import GenerationArtifact

COPY_HISTORY_DIR = Path(__file__).resolve().parents[1] / "runtime" / "publication_copy_history"


def save_bot_publication_copy(session: dict, pending: dict, *, approved: bool = False) -> str | None:
    """Append a revision before sending/accepting copy; never replace history.

    Standalone description mode has no trustworthy run association. Archive it
    independently instead of attaching another recipe to the active video.
    """
    copy = pending.get("copy")
    if not isinstance(copy, dict) or not any(copy.values()):
        return None
    run_id = (session.get("generation_run_id")
              if pending.get("origin") == "production_final" else None)
    run_id = uuid.UUID(str(run_id)) if run_id else None
    revision_id = str(uuid.uuid4())
    payload = {
        "revision_id": revision_id,
        "parent_revision_id": pending.get("copy_revision_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": str(run_id) if run_id else None,
        "project_slug": pending.get("project_slug") or session.get("selected_project_key", ""),
        "copy": dict(copy), "recipe": pending.get("recipe", ""),
        "feedback": pending.get("last_feedback", ""),
        "origin": "telegram", "source_mode": pending.get("origin", ""),
        "status": "approved" if approved else "draft",
        "instagram_ru_trial_description": pending.get("instagram_ru_trial_description", ""),
    }
    COPY_HISTORY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation, unique revision filename, private permissions.
    path = COPY_HISTORY_DIR / f"{revision_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    if run_id:
        with SessionLocal() as db:
            db.add(GenerationArtifact(
                run_id=run_id,
                artifact_type="publication_copy_approved" if approved else "publication_copy_draft",
                label="Telegram publication copy: " + payload["status"],
                uri=f"db://generation_runs/{run_id}/publication_copy/bot/{revision_id}",
                mime_type="application/json", payload_json=payload,
            ))
            if payload["instagram_ru_trial_description"]:
                db.add(GenerationArtifact(
                    run_id=run_id, artifact_type="publication_trial_copy_draft",
                    label="Telegram Instagram RU Trial copy",
                    uri=f"db://generation_runs/{run_id}/publication_copy/trial/{revision_id}",
                    language="ru", mime_type="application/json",
                    payload_json={"description": payload["instagram_ru_trial_description"],
                                  "revision_id": revision_id},
                ))
            db.commit()
    pending["copy_revision_id"] = revision_id
    return revision_id
