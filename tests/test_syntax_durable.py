import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from app import syntax_durable as durable
from app.syntax_video_fallback import BatchFallbackRequest, FallbackResult


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTAX_JOB_DIR', str(tmp_path))
    return tmp_path


def result(key='en'):
    return FallbackResult(True, key.encode(), None, 'ok', key)


def payload():
    return dict(requests=durable.encode_requests([
        BatchFallbackRequest('en', 'lang:en prompt', b'frame')]),
        options=dict(model='veo3_fast', duration_sec=8))


def job(store):
    path = store / 'job'
    path.mkdir()
    durable.write_json(path / 'request.json', payload())
    return path


def test_executor_persists_partial_results_before_completion(store):
    path = job(store)
    def runner(*, requests, result_sink, **options):
        assert requests[0].start_image == b'frame'
        result_sink['en'] = result()
        assert durable.decode_result(durable.read_json(path / 'results.json')['en']).video_bytes == b'en'
        raise RuntimeError('later Russian transport failure')
    durable.execute_job(path, runner=runner)
    assert durable.read_json(path / 'state.json')['state'] == 'complete'
    assert durable.decode_result(durable.read_json(path / 'results.json')['en']).success


def test_completed_or_interrupted_executor_never_dispatches_again(store):
    path = job(store)
    for state in ('complete', 'running'):
        durable.write_json(path / 'state.json', {'state': state})
        durable.execute_job(path, runner=lambda **kw: pytest.fail('duplicate generation'))
        assert durable.read_json(path / 'state.json')['state'] == state


def test_two_executor_processes_cannot_own_same_job(store):
    # flock is acquired by separate file descriptions, also between threads.
    path = job(store)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def runner(**kw):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {'en': result()}
    thread = threading.Thread(target=durable.execute_job, args=(path,), kwargs={'runner': runner})
    thread.start()
    try:
        assert entered.wait(3)
        assert durable.worker_alive(path)
        durable.execute_job(path, runner=lambda **kw: pytest.fail('second worker'))
    finally:
        release.set()
        thread.join(3)
    assert calls == [1]
    assert not durable.worker_alive(path)


def fake_scope(store, *, recovered):
    update = SimpleNamespace(update_id=123, effective_chat=SimpleNamespace(id=456))
    return durable.Dialogue(update, store / 'dialogue.json', recovered=recovered)


def test_restarted_frontend_uses_saved_job_even_when_replay_normalizes_prompt(store, monkeypatch):
    path = job(store)
    durable.write_json(path / 'results.json', {'en': durable.encode_result(result())})
    durable.write_json(path / 'state.json', {'state': 'complete'})
    scope = fake_scope(store, recovered=True)
    durable.write_json(scope.envelope, dict(pending=True, active_job='job', ordinal=2))
    monkeypatch.setattr(durable, 'checkpoint_dialogue', lambda scope: None)
    monkeypatch.setattr(durable, 'launch_worker', lambda path: pytest.fail('new launch'))
    token = durable._CURRENT.set(scope)
    try:
        got = asyncio.run(durable.run_batch(
            requests=[BatchFallbackRequest('en', 'normalized replay prompt', b'frame')],
            model='veo3_fast', duration_sec=8))
    finally:
        durable._CURRENT.reset(token)
    assert got['en'].video_bytes == b'en'
    assert scope.ordinal == 3
    assert not scope.recovered


def test_cancelled_frontend_does_not_cancel_executor_and_new_waiter_gets_result(store, monkeypatch):
    path = job(store)
    scope = fake_scope(store, recovered=True)
    durable.write_json(scope.envelope, dict(pending=True, active_job='job', ordinal=0))
    monkeypatch.setattr(durable, 'checkpoint_dialogue', lambda scope: None)
    monkeypatch.setattr(durable, 'launch_worker', lambda path: pytest.fail('duplicate launch'))
    entered, release = threading.Event(), threading.Event()
    def runner(**kw):
        entered.set()
        assert release.wait(5)
        return {'en': result()}
    worker = threading.Thread(target=durable.execute_job, args=(path,), kwargs={'runner': runner})
    worker.start()
    assert entered.wait(3)
    async def exercise():
        token = durable._CURRENT.set(scope)
        try:
            args = dict(requests=[BatchFallbackRequest('en', 'lang:en prompt', b'frame')], model='veo3_fast', duration_sec=8)
            waiting = asyncio.create_task(durable.run_batch(**args))
            await asyncio.sleep(0.02)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert durable.worker_alive(path)
            release.set()
            return await asyncio.wait_for(durable.run_batch(**args), timeout=3)
        finally:
            release.set()
            durable._CURRENT.reset(token)
    try:
        got = asyncio.run(exercise())
        assert got['en'].success
    finally:
        release.set()
        worker.join(3)


def test_dead_executor_is_not_restarted(store, monkeypatch):
    path = job(store)
    durable.write_json(path / 'state.json', {'state': 'running'})
    scope = fake_scope(store, recovered=True)
    durable.write_json(scope.envelope, dict(pending=True, active_job='job', ordinal=0))
    monkeypatch.setattr(durable, 'checkpoint_dialogue', lambda scope: None)
    monkeypatch.setattr(durable, 'launch_worker', lambda path: pytest.fail('duplicate paid request'))
    token = durable._CURRENT.set(scope)
    try:
        with pytest.raises(RuntimeError, match='автоматический повтор отключён'):
            asyncio.run(durable.run_batch(requests=[], model='veo3_fast', duration_sec=8))
    finally:
        durable._CURRENT.reset(token)


def test_real_executor_survives_frontend_process_exit(store):
    import subprocess
    import sys
    import time
    path = job(store)
    script = '''import sys, time
from pathlib import Path
from app.syntax_durable import execute_job
from app.syntax_video_fallback import FallbackResult
def runner(**kw):
    time.sleep(0.5)
    return {'en': FallbackResult(True, b'persisted', None, 'ok', 'job')}
execute_job(Path(sys.argv[1]), runner=runner)
'''
    launcher = '''import subprocess, sys
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True, close_fds=True)
'''
    subprocess.run([sys.executable, '-c', launcher, script, str(path)], cwd=durable.ROOT, check=True)
    # The frontend has exited. Only the detached executor can produce this result.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and durable.read_json(path / 'state.json').get('state') != 'complete':
        time.sleep(0.05)
    assert durable.read_json(path / 'state.json')['state'] == 'complete'
    assert durable.decode_result(durable.read_json(path / 'results.json')['en']).video_bytes == b'persisted'


def test_recovery_continues_delivery_without_replaying_approved_phrase(store, monkeypatch):
    from datetime import datetime, timezone
    from telegram import Update, Message, Chat, User
    from app import bot_app, state
    update = Update(123, message=Message(12, datetime.now(timezone.utc),
        Chat(456, 'private'), from_user=User(456, 'User', False), text='ОК'))
    session = {'state': 'waiting_scene_media', durable.MARKER: 123,
               'approved_phrase': 'Leave exactly this phrase'}
    monkeypatch.setattr(state, 'SESSIONS', {456: session.copy()})
    monkeypatch.setattr(bot_app, 'SESSIONS', state.SESSIONS)
    path = store / 'dialogue-456-123.json'
    durable.write_json(path, dict(pending=True, session=session))
    calls = []
    async def resume(u, c, scope):
        calls.append('resume_delivery')
        assert state.SESSIONS[456]['approved_phrase'] == session['approved_phrase']
    async def original_command(*args):
        pytest.fail('Approval command must never be replayed')
    monkeypatch.setattr(durable, 'resume_scene_job', resume)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={'syntax_recovery_updates': {123}}))
    asyncio.run(bot_app._run_admin(original_command, update, context,
        SimpleNamespace(role='admin', normalized_username='other'), quiet_enabled=False))
    assert calls == ['resume_delivery']


def test_startup_queues_only_current_unfinished_dialogue(store, monkeypatch):
    from datetime import datetime, timezone
    from telegram import Update, Message, Chat, User
    from app import state
    update = Update(123, message=Message(12, datetime.now(timezone.utc),
        Chat(456, 'private'), from_user=User(456, 'User', False), text='ОК'))
    durable.write_json(store / 'dialogue-456-123.json', dict(pending=True,
        chat_id=456, update_id=123, update=__import__('json').loads(update.to_json())))
    monkeypatch.setattr(state, 'SESSIONS', {456: {durable.MARKER: 123}})
    app = SimpleNamespace(bot=None, bot_data={}, update_queue=asyncio.Queue())
    asyncio.run(durable.enqueue_recovery(app))
    assert app.update_queue.get_nowait().update_id == 123
    state.SESSIONS[456] = {'state': 'new_dialogue'}
    asyncio.run(durable.enqueue_recovery(app))
    assert app.update_queue.empty()


def test_resume_delivers_video_without_rewriting_phrase_or_prompt(store, monkeypatch):
    from app import state, session_store
    from app.handlers import handlers_state_media as media
    scope = fake_scope(store, recovered=True)
    path = job(store)
    durable.write_json(scope.envelope, dict(pending=True, active_job='job', ordinal=0))
    scene = dict(text='Approved phrase', speech_budget_override_by_lang={'en': True},
                 video_prompt_by_lang={'en': 'Approved prompt'})
    session = dict(production={'json_plan': [scene]}, image_scene_index=0, target_languages=['en'])
    monkeypatch.setattr(state, 'SESSIONS', {456: session})
    monkeypatch.setattr(session_store, 'save_session', lambda *a: None)
    calls = []
    async def batch(**kw):
        assert kw['requests'][0].prompt == 'lang:en prompt'  # stored job, not reconstructed prompt
        return {'en': result()}
    async def deliver(update, **kw):
        calls.append(('deliver', kw['language']))
        assert kw['scene']['text'] == 'Approved phrase'
        assert kw['scene']['video_prompt_by_lang']['en'] == 'Approved prompt'
    async def advance(*a, **kw):
        calls.append(('advance', kw['idx']))
    monkeypatch.setattr(durable, 'run_batch', batch)
    monkeypatch.setattr(media, '_deliver_scene_video_result', deliver)
    monkeypatch.setattr(media, '_continue_after_scene_video_provider_result', advance)
    asyncio.run(durable.resume_scene_job(scope.update, None, scope))
    assert calls == [('deliver', 'en'), ('advance', 0)]
    assert durable.read_json(scope.envelope)['delivered'] == ['en']
