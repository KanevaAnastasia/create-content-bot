from pathlib import Path
import subprocess


def test_stop_and_force_stop_preserve_durable_syntx_worker():
    script = Path('scripts/botctl.sh').read_text()
    functions = script[script.index('is_durable_syntax_worker() {'):script.index('write_status() {')]
    harness = '''
child_pids() { echo '21 22'; }
ps() {
  if [[ "$2" == 21 ]]; then
    echo '/python -m app.syntax_durable /opt/content_fabric/runtime/syntax_jobs/job'
  else
    echo '/python unrelated-child.py'
  fi
}
kill() { echo "signal $*"; }
'''
    result = subprocess.run(['bash', '-c', functions + harness + '\nterminate_bot_tree 20\nforce_kill_bot_tree 20\n'], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['signal 22', 'signal 20', 'signal -KILL 22', 'signal -KILL 20']


def test_stop_waits_for_global_media_gate_before_terminating_bot():
    script = Path('scripts/botctl.sh').read_text()
    stop = script[script.index('stop_bot() {'):script.index('status_bot() {')]
    assert stop.index('wait_for_media_generation_idle') < stop.index('terminate_bot_tree "$pid"')


def test_worker_and_service_restarts_wait_for_global_media_gate():
    script = Path('scripts/botctl.sh').read_text()
    for start, end in (
        ('celery_worker_reload() {', 'celery_beat_start() {'),
        ('services_up() {', 'services_restart() {'),
        ('services_restart() {', 'wait_for_admin() {'),
    ):
        block = script[script.index(start):script.index(end)]
        assert 'wait_for_media_generation_idle' in block
