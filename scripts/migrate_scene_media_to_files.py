#!/usr/bin/env python3
"""Move legacy scene candidate blobs out of PostgreSQL and into artifact files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.media_file_store import store_scene_candidate_media
from app.models import Scene, SceneImageCandidate, SceneVideoCandidate


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _blob_bytes(value) -> bytes:
    return bytes(value or b"")


def migrate(*, dry_run: bool, limit: int | None) -> tuple[int, int]:
    moved_count = 0
    moved_bytes = 0
    with SessionLocal() as db:
        plans = (
            (
                db.query(SceneImageCandidate, Scene)
                .join(Scene, SceneImageCandidate.scene_id == Scene.id)
                .filter(SceneImageCandidate.media_uri.is_(None))
                .filter(SceneImageCandidate.image_blob.is_not(None))
                .order_by(SceneImageCandidate.created_at.asc(), SceneImageCandidate.id.asc()),
                "image_blob",
                "png",
                "image/png",
            ),
            (
                db.query(SceneVideoCandidate, Scene)
                .join(Scene, SceneVideoCandidate.scene_id == Scene.id)
                .filter(SceneVideoCandidate.media_uri.is_(None))
                .filter(SceneVideoCandidate.video_blob.is_not(None))
                .order_by(SceneVideoCandidate.created_at.asc(), SceneVideoCandidate.id.asc()),
                "video_blob",
                "mp4",
                "video/mp4",
            ),
        )
        for query, blob_attr, extension, mime_type in plans:
            for row, scene in query.yield_per(10):
                if limit is not None and moved_count >= limit:
                    break
                payload = _blob_bytes(getattr(row, blob_attr))
                if not payload:
                    continue
                moved_count += 1
                moved_bytes += len(payload)
                if dry_run:
                    continue
                row.media_uri = store_scene_candidate_media(
                    run_id=scene.run_id,
                    scene_id=scene.id,
                    candidate_id=row.candidate_id,
                    data=payload,
                    extension=extension,
                )
                row.mime_type = mime_type
                row.byte_size = len(payload)
                setattr(row, blob_attr, None)
                if moved_count % 25 == 0:
                    db.commit()
            if limit is not None and moved_count >= limit:
                break
        if not dry_run:
            db.commit()
    return moved_count, moved_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Only report how much media would be moved.")
    parser.add_argument("--limit", type=_positive_int, default=None, help="Move at most N candidates.")
    args = parser.parse_args()
    count, byte_count = migrate(dry_run=args.dry_run, limit=args.limit)
    mode = "would move" if args.dry_run else "moved"
    print(f"{mode}: {count} candidates, {byte_count / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
