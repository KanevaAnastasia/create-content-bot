"""Cross-process serialization for paid image and video generation.

Every provider entrypoint uses the same advisory file lock.  This keeps media
generation global rather than per chat, and the waiter files let service
management wait for both the active request and already queued requests before
terminating a worker.
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


def _lock_path() -> Path:
    return _runtime_root() / "generation.lock"


def _state_path() -> Path:
    return _runtime_root() / "state.json"


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


def _live_waiters() -> list[dict]:
    live: list[dict] = []
    for path in _waiters_root().glob("*.json"):
        value = _read_json(path)
        if _pid_alive(value.get("pid")):
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


def _try_lock(stream) -> bool:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def media_generation_slot(kind: str, *, request_id: str | None = None) -> Iterator[None]:
    """Wait for the single global media slot and hold it through provider work."""
    normalized_kind = str(kind or "media").strip().lower() or "media"
    token = uuid.uuid4().hex
    waiter_path = _waiters_root() / f"{token}.json"
    waiter = {
        "token": token,
        "pid": os.getpid(),
        "kind": normalized_kind,
        "request_id": str(request_id or ""),
        "queued_at": time.time(),
    }
    _write_json(waiter_path, waiter)
    lock_stream = None
    started_waiting = time.monotonic()
    logged_wait = False
    try:
        while True:
            waiters = _live_waiters()
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
            candidate = _lock_path().open("a+")
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
            _state_path(),
            {
                "active": True,
                "token": token,
                "pid": os.getpid(),
                "kind": normalized_kind,
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
                _state_path(),
                {
                    "active": False,
                    "token": token,
                    "pid": os.getpid(),
                    "kind": normalized_kind,
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
        stream = _lock_path().open("a+")
        acquired = _try_lock(stream)
        if not acquired:
            stream.close()
            if not announced:
                print("Waiting for active media generation to finish...", flush=True)
                announced = True
            time.sleep(poll_sec)
            continue
        try:
            waiters = _live_waiters()
            state = _read_json(_state_path())
            finished_at = float(state.get("finished_at") or 0.0)
            quiet_remaining = max(0.0, quiet_sec - max(0.0, time.time() - finished_at)) if finished_at else 0.0
            if not waiters and quiet_remaining <= 0:
                return
            if not announced:
                print("Waiting for queued media generation to finish...", flush=True)
                announced = True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        time.sleep(min(poll_sec, quiet_remaining) if quiet_remaining > 0 else poll_sec)


def media_generation_status() -> dict:
    stream = _lock_path().open("a+")
    idle = _try_lock(stream)
    if idle:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()
    return {
        "active": not idle,
        "waiters": len(_live_waiters()),
        "state": _read_json(_state_path()),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or wait for the global media generation gate.")
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
    state = status["state"]
    if status["active"]:
        print(
            "Media generation: active "
            f"kind={state.get('kind') or '?'} pid={state.get('pid') or '?'} "
            f"waiters={status['waiters']}"
        )
    else:
        print(f"Media generation: idle waiters={status['waiters']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
