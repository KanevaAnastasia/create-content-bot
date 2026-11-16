"""Durable admin-dialogue Syntx jobs, independent of the polling process.

The executor owns Telethon/Playwright. Restarting the Telegram frontend only
replaces its waiter. A lost executor is never automatically replayed.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
MARKER = '_syntax_durable_update_id'


def storage_root() -> Path:
    path = Path(os.getenv('SYNTAX_JOB_DIR') or ROOT / 'runtime' / 'syntax_jobs')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.checkpoint-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


@dataclass
class Dialogue:
    update: Any
    envelope: Path
    recovered: bool = False
    used: bool = False
    ordinal: int = 0


_CURRENT: ContextVar[Dialogue | None] = ContextVar('syntax_dialogue', default=None)


def enabled_for_scene() -> bool:
    scope = _CURRENT.get()
    if scope is None:
        return False
    from .state import SESSIONS
    session = SESSIONS.get(scope.update.effective_chat.id) or {}
    return session.get("state") == "waiting_scene_media" and session.get("scene_media_substep") == "video_prompt"


def current_dialogue() -> Dialogue | None:
    return _CURRENT.get()


@contextmanager
def dialogue_scope(update: Any, *, recovered: bool = False):
    path = storage_root() / f'dialogue-{int(update.effective_chat.id)}-{int(update.update_id)}.json'
    value = Dialogue(update, path, recovered)
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        _CURRENT.reset(token)


def checkpoint_dialogue(scope: Dialogue) -> None:
    from .state import SESSIONS
    from .session_store import save_session
    chat_id = int(scope.update.effective_chat.id)
    session = SESSIONS.get(chat_id)
    if not isinstance(session, dict):
        raise RuntimeError('Cannot checkpoint Syntx without an active dialogue')
    # Keep the original snapshot on replay: it is the point before Syntx.
    if not scope.recovered:
        from .session_store import _serialize_value
        snapshot = _serialize_value(session)
        snapshot[MARKER] = int(scope.update.update_id)
        write_json(scope.envelope, dict(pending=True, chat_id=chat_id,
            update_id=int(scope.update.update_id), update=json.loads(scope.update.to_json()),
            session=snapshot))
    session[MARKER] = int(scope.update.update_id)
    save_session(chat_id, session)
    scope.used = True


def finish_dialogue(scope: Dialogue) -> None:
    if not scope.used:
        return
    from .state import SESSIONS
    from .session_store import save_session
    session = SESSIONS.get(scope.update.effective_chat.id)
    if isinstance(session, dict):
        session.pop(MARKER, None)
        save_session(scope.update.effective_chat.id, session)
    envelope = read_json(scope.envelope)
    envelope['pending'] = False
    write_json(scope.envelope, envelope)


def encode_result(result: Any) -> dict:
    value = asdict(result)
    value['video_bytes'] = base64.b64encode(result.video_bytes).decode() if result.video_bytes else None
    return value


def decode_result(value: dict):
    from .syntax_video_fallback import FallbackResult
    value = dict(value)
    if value.get('video_bytes'):
        value['video_bytes'] = base64.b64decode(value['video_bytes'])
    return FallbackResult(**value)


def encode_requests(requests: list[Any]) -> list[dict]:
    return [dict(key=r.key, prompt=r.prompt,
                 start_image=base64.b64encode(r.start_image).decode(),
                 end_image=base64.b64encode(r.end_image).decode() if r.end_image else None)
            for r in requests]


def worker_alive(path: Path) -> bool:
    with (path / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def launch_worker(path: Path) -> None:
    with (path / 'worker.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-m', 'app.syntax_durable', str(path)],
                         cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


async def run_batch(*, requests: list[Any], **options) -> dict:
    scope = current_dialogue()
    if scope is None:
        raise RuntimeError('Durable Syntx requires a dialogue scope')
    checkpoint_dialogue(scope)
    payload = dict(requests=encode_requests(requests), options=options)
    envelope = read_json(scope.envelope)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if scope.recovered and envelope.get('active_job'):
        path = storage_root() / envelope['active_job']
        scope.ordinal = int(envelope.get('ordinal', 0)) + 1
    else:
        path = storage_root() / f'{scope.update.effective_chat.id}-{scope.update.update_id}-{scope.ordinal}-{fingerprint}'
        envelope.update(active_job=path.name, ordinal=scope.ordinal)
        scope.ordinal += 1
    path.mkdir(mode=0o700, exist_ok=True)
    if not (path / 'request.json').exists():
        write_json(path / 'request.json', payload)
    write_json(scope.envelope, envelope)
    state = read_json(path / 'state.json')
    if state.get('state') != 'complete' and not worker_alive(path):
        state = read_json(path / 'state.json')
        if state.get('state') == 'running':
            raise RuntimeError('Исполнитель Syntx остановился. Запрос мог быть отправлен; автоматический повтор отключён.')
        if state.get('state') != 'complete':
            launch_worker(path)
    # No frontend timeout: the executor applies the transport timeout and
    # persists all partial results. Cancellation does not terminate it.
    startup_checks = 0
    while True:
        state = read_json(path / 'state.json')
        if state.get('state') == 'complete':
            results = {key: decode_result(value) for key, value in read_json(path / 'results.json').items()}
            if state.get('error') and not results:
                raise RuntimeError(state['error'])
            scope.recovered = False
            return results
        if not worker_alive(path):
            if read_json(path / 'state.json').get('state') == 'complete':
                continue
            startup_checks += 1
            if startup_checks > 10 or state.get('state') == 'running':
                raise RuntimeError('Исполнитель Syntx недоступен; сохранённое задание не будет отправлено повторно.')
        else:
            startup_checks = 0
        await asyncio.sleep(1)


def execute_job(path: Path, *, runner=None) -> None:
    from .env_utils import load_env_file
    from .syntax_video_fallback import BatchFallbackRequest, try_generate_via_syntax_batch
    load_env_file()
    with (path / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if read_json(path / 'state.json').get('state') in {'running', 'complete'}:
            return  # A previous executor may already have dispatched a paid job.
        write_json(path / 'state.json', {'state': 'running', 'pid': os.getpid()})
        payload = read_json(path / 'request.json')
        requests = [BatchFallbackRequest(key=r['key'], prompt=r['prompt'],
            start_image=base64.b64decode(r['start_image']),
            end_image=base64.b64decode(r['end_image']) if r['end_image'] else None)
            for r in payload['requests']]
        class Results(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                write_json(path / 'results.json', {k: encode_result(v) for k, v in self.items()})
        results = Results()
        error = None
        try:
            output = (runner or try_generate_via_syntax_batch)(requests=requests, result_sink=results, **payload['options'])
            for key, value in output.items():
                results[key] = value
        except Exception as exc:
            logger.exception('syntax_durable.executor_failed')
            error = f'Syntx executor failed: {type(exc).__name__}'
        write_json(path / 'state.json', {'state': 'complete', 'error': error})


async def enqueue_recovery(application) -> None:
    from telegram import Update
    from .state import SESSIONS
    for path in sorted(storage_root().glob('dialogue-*.json')):
        try:
            envelope = read_json(path)
            if not envelope.get('pending'):
                continue
            session = SESSIONS.get(envelope['chat_id'])
            if not isinstance(session, dict) or session.get(MARKER) != envelope['update_id']:
                logger.warning('syntax_durable.stale_dialogue_skipped chat_id=%s', envelope['chat_id'])
                continue
            application.bot_data.setdefault('syntax_recovery_updates', set()).add(envelope['update_id'])
            await application.update_queue.put(Update.de_json(envelope['update'], application.bot))
            logger.info('syntax_durable.resume_queued chat_id=%s update_id=%s', envelope['chat_id'], envelope['update_id'])
        except Exception:
            logger.exception('syntax_durable.recovery_enqueue_failed')


async def resume_scene_job(update, context, scope: Dialogue) -> None:
    """Resume at video delivery, never replay an approval or prompt-generation command."""
    from .state import SESSIONS
    from .session_store import save_session, _serialize_value
    from .syntax_video_fallback import BatchFallbackRequest
    from .syntax_fallback_output import video_bytes_from_fallback_result
    from .handlers import handlers_state_media as media
    envelope = read_json(scope.envelope)
    if not envelope.get("active_job"):
        raise RuntimeError("Syntx checkpoint has no submitted job; refusing to replay an approval")
    payload = read_json(storage_root() / envelope["active_job"] / "request.json")
    requests = [BatchFallbackRequest(key=r["key"], prompt=r["prompt"],
        start_image=base64.b64decode(r["start_image"]),
        end_image=base64.b64decode(r["end_image"]) if r["end_image"] else None)
        for r in payload["requests"]]
    session = SESSIONS[update.effective_chat.id]
    scenes, idx = media._scene_media_context(session)
    if not scenes or idx >= len(scenes):
        raise RuntimeError("Cannot restore Syntx: saved scene is unavailable")
    scene = scenes[idx]
    results = await run_batch(requests=requests, **payload["options"])
    envelope = read_json(scope.envelope)
    delivered = set(envelope.get("delivered", []))
    for request in requests:
        result = results.get(request.key)
        if request.key in delivered or result is None or not result.success:
            continue
        video = await asyncio.to_thread(video_bytes_from_fallback_result, result)
        await media._deliver_scene_video_result(update, chat_id=update.effective_chat.id,
            session=session, scene=scene, idx=idx, total=len(scenes), language=request.key,
            video_bytes=video, provider="telegram", caption_suffix=". Источник: Syntax.")
        delivered.add(request.key)
        save_session(update.effective_chat.id, session)
        envelope.update(session=_serialize_value(session), delivered=sorted(delivered))
        write_json(scope.envelope, envelope)
    missing = [request for request in requests if request.key not in delivered]
    if missing:
        options = payload["options"]
        scene["video_provider_choice_pending"] = dict(source="scene_batch",
            languages=[r.key for r in missing], missing_languages=[r.key for r in missing],
            entries_by_lang={r.key: media._scene_video_provider_choice_entry(
                prompt=r.prompt, model=options["model"], duration_sec=options["duration_sec"],
                start_image=r.start_image, end_image=r.end_image, quality="720",
                aspect_ratio=options.get("aspect_ratio", "9:16"),
                audio_enabled=options.get("audio_enabled", True), syntax_provider=options.get("syntax_provider"),
                syntax_error_message=str(getattr(results.get(r.key), "provider_message", "Результат ещё не получен")),
            ) for r in missing})
        await media._reply_current_scene_media_keyboard(update, session,
            "Ожидание Syntax восстановлено после перезапуска. Получено: "
            + (", ".join(sorted(delivered)) or "пока ничего")
            + ". Без готового видео: " + ", ".join(r.key for r in missing) + ".")
    else:
        scene.pop("video_provider_choice_pending", None)
        await media._continue_after_scene_video_provider_result(update, context,
            chat_id=update.effective_chat.id, session=session, scene=scene, idx=idx, total=len(scenes))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    execute_job(Path(sys.argv[1]))
