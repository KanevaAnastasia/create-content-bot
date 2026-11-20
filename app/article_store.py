"""Durable, owner-scoped article drafts, separate from active video sessions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import copy
import fcntl
import json
from pathlib import Path
import uuid

from .syntax_durable import write_json

ROOT = Path(__file__).resolve().parents[1] / "runtime" / "articles"
TERMINAL = {"published", "cancelled"}


def pending(row):
    return row["state"] not in TERMINAL or (row["state"] == "published" and len(row.get("delivery", {})) < 3)


@contextmanager
def transaction(*, write=True):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        path = ROOT / "state.json"
        data = json.loads(path.read_text()) if path.exists() else {"enabled": False, "issues": {}, "replies": {}}
        yield data
        if write:
            write_json(path, data)


def snapshot():
    with transaction(write=False) as data:
        return copy.deepcopy(data)


def configure(*, owner_id, publisher_id=None, channel_id, username, next_run_at, samples_path, reference_paths):
    if next_run_at.tzinfo is None:
        raise ValueError("Schedule must include timezone")
    with transaction() as data:
        if data.get("enabled"):
            raise ValueError("Article schedule already configured")
        data.update(enabled=True, owner_id=int(owner_id), publisher_id=int(publisher_id) if publisher_id is not None else None,
                    channel_id=int(channel_id),
                    username=username, next_run_at=next_run_at.isoformat(),
                    samples_path=str(samples_path), reference_paths=list(map(str, reference_paths)))


def claim_due(now=None):
    now = now or datetime.now(timezone.utc)
    with transaction() as data:
        if not data.get("enabled") or any(pending(i) for i in data["issues"].values()):
            return None
        due = datetime.fromisoformat(data["next_run_at"])
        if now < due:
            return None
        while due <= now:
            due += timedelta(days=3)
        data["next_run_at"] = due.isoformat()
        iid = uuid.uuid4().hex[:12]
        data["issues"][iid] = {"id": iid, "revision": 0, "state": "working", "operation": "topics",
                               "created_at": now.isoformat(), "versions": [], "images": {}, "delivery": {}}
        if data.get("prepared_topics"):
            data["issues"][iid].update(state="topics", topics=data.pop("prepared_topics"))
        return copy.deepcopy(data["issues"][iid])


def current():
    issues = list(snapshot()["issues"].values())
    active = [i for i in issues if pending(i)]
    return active[-1] if active else (issues[-1] if issues else None)


def blocks_advertising(chat_id):
    data = snapshot()
    return bool(data.get("enabled") and data.get("owner_id") == chat_id
                and any(pending(row) for row in data["issues"].values()))


def issue(iid):
    return snapshot()["issues"][iid]


def change(iid, revision, *, allowed, **fields):
    with transaction() as data:
        row = data["issues"][iid]
        if row["revision"] != revision or row["state"] not in allowed:
            raise ValueError("Этот шаг уже изменился. Откройте текущую статью командой /articles.")
        row.update(copy.deepcopy(fields))
        row["revision"] += 1
        return copy.deepcopy(row)


def checkpoint(iid, **fields):
    with transaction() as data:
        data["issues"][iid].update(copy.deepcopy(fields))


def remember_reply(message_id, row, kind):
    with transaction() as data:
        data["replies"][str(message_id)] = {"id": row["id"], "revision": row["revision"], "kind": kind}


def recover_interrupted():
    # Never repeat paid generation or a channel send just because polling restarted.
    with transaction() as data:
        for row in data["issues"].values():
            if row["state"] == "working":
                row.update(state="failed", error="Работа прервалась при перезапуске. Сохранённые результаты доступны.")
                row["revision"] += 1
            elif row["state"] == "publishing":
                row.update(state="publication_uncertain", error="Нужно проверить, появился ли пост в канале.")
                row["revision"] += 1
