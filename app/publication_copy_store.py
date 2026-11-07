"""Durable copies of the exact publication text shown in Telegram."""
import uuid

from .db import SessionLocal
from .models import GenerationArtifact


def save_bot_publication_copy(session: dict, pending: dict) -> None:
    run_id = session.get("generation_run_id")
    copy = pending.get("copy")
    if (pending.get("origin") != "production_final" or not run_id
            or not isinstance(copy, dict) or not any(copy.values())):
        return
    run_id = uuid.UUID(str(run_id))
    with SessionLocal() as db:
        db.add(GenerationArtifact(
            run_id=run_id,
            artifact_type="publication_copy_draft",
            label="Publication copy shown in Telegram",
            uri=f"db://generation_runs/{run_id}/publication_copy/bot/{uuid.uuid4()}",
            mime_type="application/json",
            payload_json={"copy": dict(copy), "recipe": pending.get("recipe", ""),
                          "origin": "telegram"},
        ))
        db.commit()
