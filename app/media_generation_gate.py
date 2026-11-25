"""Cross-process serialization for paid media generation.

Image and video providers use independent advisory locks, so a long-running
video request cannot delay image generation. Requests in the same media lane
remain serialized across chats. Waiter files let service management wait for
all active and queued media work before terminating a worker.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Iterator


logger = logging.getLogger("bot.media_gate")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLL_SEC = 0.25
DEFAULT_QUIET_SEC = 10.0


def _runtime_root() -> Path:
    root = Path(os.getenv("MEDIA_GENERATION_GATE_DIR") or ROOT / "runtime" / "media_generation_gate")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _lane_for_kind(kind: str) -> str:
    normalized = str(kind or "media").strip().lower() or "media"
    if normalized.startswith("image"):
        return "image"
    if normalized.startswith("video"):
        return "video"
    return "media"


def _lock_path(lane: str | None = None) -> Path:
    if lane is None:
        return _runtime_root() / "generation.lock"
    return _runtime_root() / f"generation.{lane}.lock"


def _state_path(lane: str | None = None) -> Path:
    if lane is None:
        return _runtime_root() / "state.json"
    return _runtime_root() / f"state.{lane}.json"


def _waiters_root() -> Path:
    root = _runtime_root() / "waiters"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_waiters(*, lane: str | None = None) -> list[dict]:
    live: list[dict] = []
    for path in _waiters_root().glob("*.json"):
        value = _read_json(path)
        value_lane = str(value.get("lane") or _lane_for_kind(value.get("kind") or "media"))
        if _pid_alive(value.get("pid")):
            if lane is not None and value_lane != lane:
                continue
            value["lane"] = value_lane
            live.append(value)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return sorted(
        live,
        key=lambda item: (float(item.get("queued_at") or 0.0), str(item.get("token") or "")),
    )


def _known_lanes() -> list[str | None]:
    """Return provider lanes plus the legacy global lane during migration."""
    lanes = {"image", "video", "media"}
    for path in _runtime_root().glob("generation.*.lock"):
        lanes.add(path.name.removeprefix("generation.").removesuffix(".lock"))
    for path in _runtime_root().glob("state.*.json"):
        lanes.add(path.name.removeprefix("state.").removesuffix(".json"))
    lanes.update(str(item["lane"]) for item in _live_waiters())
    # The old global file may still be locked by a bot started before this
    # change. Keep observing it until every running process uses lane locks.
    return [None, *sorted(lanes)]


def _try_lock(stream) -> bool:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def media_generation_slot(kind: str, *, request_id: str | None = None) -> Iterator[None]:
    """Wait for the matching media lane and hold it through provider work."""
    normalized_kind = str(kind or "media").strip().lower() or "media"
    lane = _lane_for_kind(normalized_kind)
    token = uuid.uuid4().hex
    waiter_path = _waiters_root() / f"{token}.json"
    waiter = {
        "token": token,
        "pid": os.getpid(),
        "kind": normalized_kind,
        "lane": lane,
        "request_id": str(request_id or ""),
        "queued_at": time.time(),
    }
    _write_json(waiter_path, waiter)
    lock_stream = None
    started_waiting = time.monotonic()
    logged_wait = False
    try:
        while True:
            waiters = _live_waiters(lane=lane)
            if waiters and waiters[0].get("token") != token:
                if not logged_wait:
                    logger.info(
                        "media_gate.wait kind=%s request_id=%s pid=%s",
                        normalized_kind,
                        request_id or "-",
                        os.getpid(),
                    )
                    logged_wait = True
                time.sleep(DEFAULT_POLL_SEC)
                continue
            candidate = _lock_path(lane).open("a+")
            if _try_lock(candidate):
                lock_stream = candidate
                break
            candidate.close()
            if not logged_wait:
                logger.info(
                    "media_gate.wait kind=%s request_id=%s pid=%s",
                    normalized_kind,
                    request_id or "-",
                    os.getpid(),
                )
                logged_wait = True
            time.sleep(DEFAULT_POLL_SEC)

        try:
            waiter_path.unlink()
        except FileNotFoundError:
            pass
        waited_sec = time.monotonic() - started_waiting
        _write_json(
            _state_path(lane),
            {
                "active": True,
                "token": token,
                "pid": os.getpid(),
                "kind": normalized_kind,
                "lane": lane,
                "request_id": str(request_id or ""),
                "started_at": time.time(),
            },
        )
        logger.info(
            "media_gate.acquired kind=%s request_id=%s pid=%s waited_sec=%.3f",
            normalized_kind,
            request_id or "-",
            os.getpid(),
            waited_sec,
        )
        try:
            yield
        finally:
            _write_json(
                _state_path(lane),
                {
                    "active": False,
                    "token": token,
                    "pid": os.getpid(),
                    "kind": normalized_kind,
                    "lane": lane,
                    "request_id": str(request_id or ""),
                    "finished_at": time.time(),
                },
            )
            logger.info(
                "media_gate.released kind=%s request_id=%s pid=%s",
                normalized_kind,
                request_id or "-",
                os.getpid(),
            )
    finally:
        try:
            waiter_path.unlink()
        except FileNotFoundError:
            pass
        if lock_stream is not None:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
            lock_stream.close()


def wait_until_media_idle(*, quiet_sec: float = DEFAULT_QUIET_SEC, poll_sec: float = DEFAULT_POLL_SEC) -> None:
    """Wait for active and queued media work plus a short post-provider quiet period."""
    quiet_sec = max(0.0, float(quiet_sec))
    poll_sec = max(0.05, float(poll_sec))
    announced = False
    while True:
        streams = []
        blocked = False
        lanes = _known_lanes()
        for lane in lanes:
            stream = _lock_path(lane).open("a+")
            if not _try_lock(stream):
                stream.close()
                blocked = True
                break
            streams.append(stream)
        if blocked:
            for stream in streams:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
            if not announced:
                print("Waiting for active media generation to finish...", flush=True)
                announced = True
            time.sleep(poll_sec)
            continue
        try:
            waiters = _live_waiters()
            finished_at = max(
                (float(_read_json(_state_path(lane)).get("finished_at") or 0.0) for lane in lanes),
                default=0.0,
            )
            quiet_remaining = max(0.0, quiet_sec - max(0.0, time.time() - finished_at)) if finished_at else 0.0
            if not waiters and quiet_remaining <= 0:
                return
            if not announced:
                print("Waiting for queued media generation to finish...", flush=True)
                announced = True
        finally:
            for stream in streams:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
        time.sleep(min(poll_sec, quiet_remaining) if quiet_remaining > 0 else poll_sec)


def media_generation_status() -> dict:
    active_states = []
    latest_state = {}
    for lane in _known_lanes():
        stream = _lock_path(lane).open("a+")
        idle = _try_lock(stream)
        if idle:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        else:
            state = _read_json(_state_path(lane))
            active_states.append(
                state
                or {
                    "active": True,
                    "lane": lane or "legacy",
                    "kind": "legacy" if lane is None else lane,
                }
            )
        stream.close()
        state = _read_json(_state_path(lane))
        if float(state.get("started_at") or state.get("finished_at") or 0.0) > float(
            latest_state.get("started_at") or latest_state.get("finished_at") or 0.0
        ):
            latest_state = state
    return {
        "active": bool(active_states),
        "waiters": len(_live_waiters()),
        "state": active_states[0] if active_states else latest_state,
        "active_states": active_states,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or wait for the media generation lanes.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    wait_parser = subparsers.add_parser("wait-idle")
    wait_parser.add_argument(
        "--quiet-sec",
        type=float,
        default=float(os.getenv("MEDIA_GENERATION_QUIET_SEC") or DEFAULT_QUIET_SEC),
    )
    args = parser.parse_args()
    if args.command == "wait-idle":
        wait_until_media_idle(quiet_sec=args.quiet_sec)
        print("Media generation gate is idle.")
        return 0
    status = media_generation_status()
    if status["active"]:
        active = ", ".join(
            f"kind={item.get('kind') or '?'} pid={item.get('pid') or '?'}"
            for item in status["active_states"]
        )
        print(f"Media generation: active {active} waiters={status['waiters']}")
    else:
        print(f"Media generation: idle waiters={status['waiters']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
