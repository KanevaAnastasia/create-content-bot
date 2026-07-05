#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.bot.pid"
RUNTIME_DIR="$ROOT_DIR/runtime"
STATUS_FILE="$RUNTIME_DIR/botctl.status"
LOG_DIR="$ROOT_DIR/logs"
SUPERVISOR_LOG="$LOG_DIR/bot.supervisor.log"
FINAL_WORKER_LOG="$LOG_DIR/final_build_worker.log"
FINAL_WORKER_PID_FILE="$RUNTIME_DIR/final_build_worker.pid"
SCHEDULED_WORKER_LOG="$LOG_DIR/scheduled_generation_worker.log"
SCHEDULED_WORKER_PID_FILE="$RUNTIME_DIR/scheduled_generation_worker.pid"
CELERY_WORKER_LOG="$LOG_DIR/celery_worker.log"
CELERY_WORKER_LAUNCHD_LOG="$LOG_DIR/celery_worker.launchd.log"
CELERY_WORKER_LAUNCHD_ERR_LOG="$LOG_DIR/celery_worker.launchd.err.log"
CELERY_WORKER_PID_FILE="$RUNTIME_DIR/celery_worker.pid"
CELERY_BEAT_LOG="$LOG_DIR/celery_beat.log"
CELERY_BEAT_LAUNCHD_LOG="$LOG_DIR/celery_beat.launchd.log"
CELERY_BEAT_LAUNCHD_ERR_LOG="$LOG_DIR/celery_beat.launchd.err.log"
CELERY_BEAT_PID_FILE="$RUNTIME_DIR/celery_beat.pid"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
MAIN_SCRIPT="$ROOT_DIR/main.py"
ADMIN_MANAGE="$ROOT_DIR/admin_panel/manage.py"
LOCK_FILE="/tmp/content_fabric_bot.lock"

usage() {
  echo "Usage: $0 {start [-f|--follow]|stop|restart [-f|--follow]|status|logs [-f|--follow]|adopt <pid>|final-worker <run_id>|final-worker-foreground <run_id>|final-worker-status|scheduled-worker [limit]|scheduled-worker-status|celery-worker|celery-worker-foreground|celery-worker-status|celery-beat|celery-beat-foreground|celery-beat-status}"
}

ensure_runtime() {
  mkdir -p "$RUNTIME_DIR"
  mkdir -p "$LOG_DIR"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python not found: $PYTHON_BIN" >&2
    exit 1
  fi
}

pid_is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && {
    [[ -n "$(ps -p "$pid" -o pid= 2>/dev/null)" ]] || lsof -p "$pid" >/dev/null 2>&1
  }
}

current_pid() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d '[:space:]' < "$PID_FILE"
  fi
}

pid_command() {
  local pid="${1:-}"
  local command open_files
  if [[ -z "$pid" ]]; then
    return 0
  fi
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ -n "$command" ]]; then
    echo "$command"
    return 0
  fi
  open_files="$(lsof -p "$pid" 2>/dev/null || true)"
  if [[ "$open_files" == *"$LOCK_FILE"* ]] && [[ "$open_files" == *"$ROOT_DIR"* ]]; then
    echo "<command unavailable; holds $LOCK_FILE in $ROOT_DIR>"
  fi
}

is_bot_pid() {
  local pid="${1:-}"
  local command open_files
  command="$(pid_command "$pid")"
  if [[ "$command" == *"main.py"* ]]; then
    return 0
  fi
  open_files="$(lsof -p "$pid" 2>/dev/null || true)"
  [[ "$open_files" == *"$LOCK_FILE"* ]] && [[ "$open_files" == *"$ROOT_DIR"* ]]
}

all_bot_pids() {
  local pids
  pids="$(pgrep -f "main.py" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids"
    return 0
  fi
  pids="$(lsof -t "$LOCK_FILE" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids"
    return 0
  fi
  ps -axo pid=,command= 2>/dev/null | awk '/[m]ain\.py/ {print $1}' || true
}

unmanaged_bot_pids() {
  local managed pid
  managed="$(current_pid || true)"
  for pid in $(all_bot_pids); do
    if [[ "$pid" != "$managed" ]] && pid_is_running "$pid" && is_bot_pid "$pid"; then
      echo "$pid"
    fi
  done
}

child_pids() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && pgrep -P "$pid" 2>/dev/null || true
}

terminate_bot_tree() {
  local pid="$1"
  local child
  for child in $(child_pids "$pid"); do
    kill "$child" 2>/dev/null || true
  done
  kill "$pid" 2>/dev/null || true
}

force_kill_bot_tree() {
  local pid="$1"
  local child
  for child in $(child_pids "$pid"); do
    kill -KILL "$child" 2>/dev/null || true
  done
  kill -KILL "$pid" 2>/dev/null || true
}

write_status() {
  ensure_runtime
  local state="$1"
  local pid="${2:-}"
  {
    echo "state=$state"
    echo "pid=$pid"
    echo "updated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "root=$ROOT_DIR"
    echo "log=$SUPERVISOR_LOG"
    echo "lock=$LOCK_FILE"
  } > "$STATUS_FILE"
}

print_processes() {
  local pid
  for pid in $(all_bot_pids); do
    if pid_is_running "$pid" && is_bot_pid "$pid"; then
      echo "  pid=$pid command=$(pid_command "$pid")"
    fi
  done
}

print_unmanaged_processes() {
  local pid
  for pid in $(unmanaged_bot_pids); do
    echo "  pid=$pid command=$(pid_command "$pid")"
  done
}

start_bot() {
  ensure_runtime
  local pid unmanaged
  pid="$(current_pid || true)"
  if pid_is_running "$pid"; then
    echo "Bot is already running: pid=$pid"
    write_status "running" "$pid"
    return 0
  fi

  unmanaged="$(unmanaged_bot_pids || true)"
  if [[ -n "$unmanaged" ]]; then
    echo "Refusing to start: bot-like process already exists but is not managed by $PID_FILE." >&2
    print_unmanaged_processes >&2
    echo "If this is the intended process, run: $0 adopt <pid>" >&2
    exit 1
  fi

  rm -f "$PID_FILE"
  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" "$MAIN_SCRIPT" >> "$SUPERVISOR_LOG" 2>&1 &
  pid="$!"
  echo "$pid" > "$PID_FILE"
  sleep 1

  if pid_is_running "$pid"; then
    echo "Bot started: pid=$pid"
    echo "Supervisor log: $SUPERVISOR_LOG"
    write_status "running" "$pid"
  else
    rm -f "$PID_FILE"
    write_status "failed" ""
    echo "Bot failed to stay running. Last log lines:" >&2
    tail -40 "$SUPERVISOR_LOG" >&2 || true
    exit 1
  fi
}

stop_bot() {
  local pid
  pid="$(current_pid || true)"
  if ! pid_is_running "$pid"; then
    rm -f "$PID_FILE"
    echo "Bot is not running"
    write_status "stopped" ""
    return 0
  fi

  if ! is_bot_pid "$pid"; then
    echo "Refusing to stop pid=$pid: it does not look like main.py." >&2
    echo "Command: $(pid_command "$pid")" >&2
    exit 1
  fi

  terminate_bot_tree "$pid"
  for _ in {1..20}; do
    if ! pid_is_running "$pid"; then
      rm -f "$PID_FILE"
      echo "Bot stopped: pid=$pid"
      write_status "stopped" ""
      return 0
    fi
    sleep 0.25
  done

  force_kill_bot_tree "$pid"
  rm -f "$PID_FILE"
  write_status "force-stopped" "$pid"
  echo "Bot force-stopped: pid=$pid"
}

status_bot() {
  ensure_runtime
  local pid unmanaged
  pid="$(current_pid || true)"
  if pid_is_running "$pid"; then
    echo "Bot is running: pid=$pid"
    echo "Command: $(pid_command "$pid")"
    write_status "running" "$pid"
  else
    rm -f "$PID_FILE"
    echo "Bot is not running"
    write_status "stopped" ""
  fi
  unmanaged="$(unmanaged_bot_pids || true)"
  if [[ -n "$unmanaged" ]]; then
    echo "Unmanaged bot-like processes:"
    print_unmanaged_processes
  fi
  echo "Status file: $STATUS_FILE"
}

final_worker_pids() {
  pgrep -f "final_build_worker" 2>/dev/null || true
}

final_worker_once() {
  ensure_runtime
  local run_id="${1:-}"
  if [[ -z "$run_id" ]]; then
    echo "Usage: $0 final-worker <run_id>" >&2
    exit 2
  fi
  if [[ ! -f "$ADMIN_MANAGE" ]]; then
    echo "Django manage.py not found: $ADMIN_MANAGE" >&2
    exit 1
  fi
  local worker_id
  worker_id="botctl-$$"
  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" "$ADMIN_MANAGE" final_build_worker --once "--run-id=$run_id" "--worker-id=$worker_id" >> "$FINAL_WORKER_LOG" 2>&1 &
  local pid="$!"
  echo "$pid" > "$FINAL_WORKER_PID_FILE"
  echo "Final build worker started: pid=$pid run_id=$run_id worker_id=$worker_id"
  echo "Worker log: $FINAL_WORKER_LOG"
}

final_worker_foreground() {
  ensure_runtime
  local run_id="${1:-}"
  if [[ -z "$run_id" ]]; then
    echo "Usage: $0 final-worker-foreground <run_id>" >&2
    exit 2
  fi
  local worker_id
  worker_id="botctl-foreground-$$"
  cd "$ROOT_DIR"
  "$PYTHON_BIN" "$ADMIN_MANAGE" final_build_worker --once "--run-id=$run_id" "--worker-id=$worker_id"
}

final_worker_status() {
  ensure_runtime
  local pid
  pid=""
  if [[ -f "$FINAL_WORKER_PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$FINAL_WORKER_PID_FILE")"
  fi
  if pid_is_running "$pid"; then
    echo "Final build worker is running: pid=$pid"
    echo "Command: $(pid_command "$pid")"
  else
    echo "Final build worker pid file is not active"
  fi
  local running
  running="$(final_worker_pids || true)"
  if [[ -n "$running" ]]; then
    echo "Detected final_build_worker processes:"
    for pid in $running; do
      if pid_is_running "$pid"; then
        echo "  pid=$pid command=$(pid_command "$pid")"
      fi
    done
  fi
  echo "Worker log: $FINAL_WORKER_LOG"
}

scheduled_worker_pids() {
  pgrep -f "process_scheduled_generations" 2>/dev/null || true
}

scheduled_worker_once() {
  ensure_runtime
  local limit="${1:-0}"
  if [[ ! -f "$ADMIN_MANAGE" ]]; then
    echo "Django manage.py not found: $ADMIN_MANAGE" >&2
    exit 1
  fi
  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" "$ADMIN_MANAGE" process_scheduled_generations --due --once "--limit=$limit" >> "$SCHEDULED_WORKER_LOG" 2>&1 &
  local pid="$!"
  echo "$pid" > "$SCHEDULED_WORKER_PID_FILE"
  echo "Scheduled generation worker started: pid=$pid limit=$limit"
  echo "Worker log: $SCHEDULED_WORKER_LOG"
}

scheduled_worker_status() {
  ensure_runtime
  local pid
  pid=""
  if [[ -f "$SCHEDULED_WORKER_PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$SCHEDULED_WORKER_PID_FILE")"
  fi
  if pid_is_running "$pid"; then
    echo "Scheduled generation worker is running: pid=$pid"
    echo "Command: $(pid_command "$pid")"
  else
    echo "Scheduled generation worker pid file is not active"
  fi
  local running
  running="$(scheduled_worker_pids || true)"
  if [[ -n "$running" ]]; then
    echo "Detected process_scheduled_generations processes:"
    for pid in $running; do
      if pid_is_running "$pid"; then
        echo "  pid=$pid command=$(pid_command "$pid")"
      fi
    done
  fi
  echo "Worker log: $SCHEDULED_WORKER_LOG"
}

celery_worker_pids() {
  ps ax -o pid=,command= 2>/dev/null | awk '/[c]elery -A cf_admin worker/ {print $1}' || true
}

celery_beat_pids() {
  ps ax -o pid=,command= 2>/dev/null | awk '/[c]elery -A cf_admin beat/ {print $1}' || true
}

celery_pythonpath() {
  if [[ -n "${PYTHONPATH:-}" ]]; then
    echo "$ROOT_DIR:$ROOT_DIR/admin_panel:$PYTHONPATH"
  else
    echo "$ROOT_DIR:$ROOT_DIR/admin_panel"
  fi
}

celery_worker_start() {
  ensure_runtime
  cd "$ROOT_DIR"
  nohup env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin worker --loglevel=INFO --concurrency=1 --pool=solo >> "$CELERY_WORKER_LOG" 2>&1 &
  local pid="$!"
  echo "$pid" > "$CELERY_WORKER_PID_FILE"
  echo "Celery worker started: pid=$pid"
  echo "Worker log: $CELERY_WORKER_LOG"
}

celery_worker_status() {
  ensure_runtime
  local pid
  pid=""
  if [[ -f "$CELERY_WORKER_PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$CELERY_WORKER_PID_FILE")"
  fi
  if pid_is_running "$pid"; then
    echo "Celery worker is running: pid=$pid"
    echo "Command: $(pid_command "$pid")"
  else
    echo "Celery worker pid file is not active"
  fi
  local running
  running="$(celery_worker_pids || true)"
  if [[ -n "$running" ]]; then
    echo "Detected Celery worker processes:"
    for pid in $running; do
      if pid_is_running "$pid"; then
        echo "  pid=$pid command=$(pid_command "$pid")"
      fi
    done
  fi
  echo "Worker log: $CELERY_WORKER_LOG"
  echo "LaunchAgent logs: $CELERY_WORKER_LAUNCHD_LOG $CELERY_WORKER_LAUNCHD_ERR_LOG"
}

celery_worker_foreground() {
  ensure_runtime
  cd "$ROOT_DIR"
  env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin worker --loglevel=INFO --concurrency=1 --pool=solo
}

celery_beat_start() {
  ensure_runtime
  cd "$ROOT_DIR"
  nohup env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin beat --loglevel=INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler >> "$CELERY_BEAT_LOG" 2>&1 &
  local pid="$!"
  echo "$pid" > "$CELERY_BEAT_PID_FILE"
  echo "Celery beat started: pid=$pid"
  echo "Beat log: $CELERY_BEAT_LOG"
}

celery_beat_status() {
  ensure_runtime
  local pid
  pid=""
  if [[ -f "$CELERY_BEAT_PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$CELERY_BEAT_PID_FILE")"
  fi
  if pid_is_running "$pid"; then
    echo "Celery beat is running: pid=$pid"
    echo "Command: $(pid_command "$pid")"
  else
    echo "Celery beat pid file is not active"
  fi
  local running
  running="$(celery_beat_pids || true)"
  if [[ -n "$running" ]]; then
    echo "Detected Celery beat processes:"
    for pid in $running; do
      if pid_is_running "$pid"; then
        echo "  pid=$pid command=$(pid_command "$pid")"
      fi
    done
  fi
  echo "Beat log: $CELERY_BEAT_LOG"
  echo "LaunchAgent logs: $CELERY_BEAT_LAUNCHD_LOG $CELERY_BEAT_LAUNCHD_ERR_LOG"
}

celery_beat_foreground() {
  ensure_runtime
  cd "$ROOT_DIR"
  env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin beat --loglevel=INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
}

adopt_bot() {
  ensure_runtime
  local pid="${1:-}"
  if [[ -z "$pid" ]]; then
    echo "Usage: $0 adopt <pid>" >&2
    exit 2
  fi
  if ! pid_is_running "$pid"; then
    echo "Cannot adopt: pid=$pid is not running" >&2
    exit 1
  fi
  if ! is_bot_pid "$pid"; then
    echo "Cannot adopt: pid=$pid does not look like main.py" >&2
    echo "Command: $(pid_command "$pid")" >&2
    exit 1
  fi
  echo "$pid" > "$PID_FILE"
  write_status "running" "$pid"
  echo "Bot process adopted: pid=$pid"
}

follow_requested() {
  case "${1:-}" in
    -f|--follow|follow)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

show_logs() {
  ensure_runtime
  touch "$SUPERVISOR_LOG"
  if follow_requested "${1:-}"; then
    tail -f "$SUPERVISOR_LOG"
  else
    tail -80 "$SUPERVISOR_LOG"
  fi
}

case "${1:-}" in
  start)
    start_bot
    if follow_requested "${2:-}"; then
      show_logs --follow
    fi
    ;;
  stop)
    stop_bot
    ;;
  restart)
    stop_bot
    start_bot
    if follow_requested "${2:-}"; then
      show_logs --follow
    fi
    ;;
  status)
    status_bot
    ;;
  adopt)
    adopt_bot "${2:-}"
    ;;
  final-worker)
    final_worker_once "${2:-}"
    ;;
  final-worker-foreground)
    final_worker_foreground "${2:-}"
    ;;
  final-worker-status)
    final_worker_status
    ;;
  scheduled-worker)
    scheduled_worker_once "${2:-0}"
    ;;
  scheduled-worker-status)
    scheduled_worker_status
    ;;
  celery-worker)
    celery_worker_start
    ;;
  celery-worker-foreground)
    celery_worker_foreground
    ;;
  celery-worker-status)
    celery_worker_status
    ;;
  celery-beat)
    celery_beat_start
    ;;
  celery-beat-foreground)
    celery_beat_foreground
    ;;
  celery-beat-status)
    celery_beat_status
    ;;
  logs)
    show_logs "${2:-}"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
