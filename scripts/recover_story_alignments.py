"""Recover missing Story timestamps from existing ElevenLabs history, without TTS/STT.

Usage: .venv/bin/python scripts/recover_story_alignments.py [--run-id UUID] [--apply]
Saved timestamps are always preferred. API reads occur only for missing data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlalchemy import select

from app.db import SessionLocal
from app.env_utils import load_env_file
from app.models import GenerationArtifact
from app.post_production import _resolve_ffmpeg
from app.story_alignment import load_story_audio_metadata, save_story_audio_metadata, valid_alignment, response_alignment


def pcm_digest(data: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="story_alignment_pcm_") as directory:
        source = Path(directory) / "audio.mp3"
        source.write_bytes(data)
        result = subprocess.run(
            [_resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(source), "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
            capture_output=True, check=True, timeout=120,
        )
    if not result.stdout:
        raise RuntimeError("Audio contains no decoded samples")
    return hashlib.sha256(result.stdout).hexdigest()


def normalized_text(value: str) -> str:
    return " ".join(str(value or "").split())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--apply", action="store_true", help="Save verified alignments; otherwise report only")
    args = parser.parse_args()
    load_env_file()
    with SessionLocal() as db:
        query = select(GenerationArtifact).where(GenerationArtifact.artifact_type == "story_narration_audio")
        if args.run_id:
            import uuid
            query = query.where(GenerationArtifact.run_id == uuid.UUID(args.run_id))
        rows = list(db.scalars(query.order_by(GenerationArtifact.created_at.desc())))
        pending = []
        for row in rows:
            payload = dict(row.payload_json or {})
            if valid_alignment(payload.get("alignment")):
                continue
            if not row.uri.startswith("file://"):
                continue
            path = Path(row.uri.removeprefix("file://"))
            if path.is_file():
                pending.append((row, path, payload))
        if not pending:
            print("No missing timestamps. No API requests made.")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = Path("artifacts/story_alignment_recovery") / stamp
        directory.mkdir(parents=True)
        (directory / "before.json").write_text(json.dumps([
            {"id": str(row.id), "uri": row.uri, "payload_json": payload}
            for row, _path, payload in pending
        ], ensure_ascii=False), encoding="utf-8")
        api = requests.Session()
        api.headers["Accept"] = "application/json"
        history = None

        def get_json(url, **kwargs):
            api.headers["xi-api-key"] = os.environ["ELEVENLABS_API_KEY"]
            for attempt in range(3):
                response = api.get("https://api.elevenlabs.io/v1/" + url, timeout=60, **kwargs)
                if response.status_code == 401:
                    raise RuntimeError("ElevenLabs history access denied. Enable History → Read (speech_history_read) for this API key.")
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError:
                    if attempt == 2:
                        raise RuntimeError("ElevenLabs history returned invalid JSON after 3 read attempts") from None

        report = []
        for row, path, payload in pending:
            saved = load_story_audio_metadata(path)
            alignment = saved.get("alignment")
            recovered = saved if valid_alignment(alignment) else None
            if recovered is None:
                known_id = saved.get("history_item_id") or payload.get("history_item_id")
                if history is None and not known_id:
                    history = []
                    cursor = None
                    seen = set()
                    while True:
                        params = {"page_size": 1000}
                        if cursor:
                            params["start_after_history_item_id"] = cursor
                        page = get_json("history", params=params)
                        history.extend(page.get("history") or [])
                        if not page.get("has_more"):
                            break
                        cursor = page.get("last_history_item_id")
                        if not cursor or cursor in seen:
                            raise RuntimeError("ElevenLabs history pagination did not advance")
                        seen.add(cursor)
                target = normalized_text(payload.get("voiceover_text"))
                candidates = [{"history_item_id": known_id}] if known_id else [item for item in history if target and normalized_text(
                    item.get("text") or " ".join(turn.get("text", "") for turn in item.get("dialogue") or [])
                ) == target]
                local_digest = None
                for candidate in candidates:
                    hid = candidate["history_item_id"]
                    detail = get_json("history/" + hid)
                    alignment = response_alignment(detail)
                    if not alignment:
                        continue
                    for attempt in range(3):
                        audio = api.get("https://api.elevenlabs.io/v1/history/" + hid + "/audio", timeout=90)
                        audio.raise_for_status()
                        if audio.headers.get("content-type", "").lower().startswith("audio/"):
                            break
                    else:
                        raise RuntimeError("ElevenLabs history returned non-audio content after 3 read attempts")
                    local_audio = path.read_bytes()
                    if audio.content != local_audio:
                        local_digest = local_digest or pcm_digest(local_audio)
                        if pcm_digest(audio.content) != local_digest:
                            continue
                    recovered = {
                        **saved, "alignment": alignment, "history_item_id": hid,
                        "request_id": detail.get("request_id"),
                        "alignment_recovery_source": "elevenlabs_history_pcm_sha256",
                    }
                    break
            status = "not_found"
            if recovered:
                status = "recovered" if args.apply else "verified"
                if args.apply:
                    updated = {**payload, **{key: value for key, value in recovered.items() if key != "audio_sha256"}}
                    save_story_audio_metadata(path, updated)
                    row.payload_json = updated
                    db.commit()
            result = {"run_id": str(row.run_id), "artifact_id": str(row.id), "language": row.language, "status": status}
            report.append(result)
            print(json.dumps(result), flush=True)
            (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
