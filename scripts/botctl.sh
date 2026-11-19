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
CELERY_WORKER_LAUNCHAGENT_PLIST="$ROOT_DIR/ops/launchagents/com.contentfabric.celery.worker.plist"
CELERY_BEAT_LOG="$LOG_DIR/celery_beat.log"
CELERY_BEAT_LAUNCHD_LOG="$LOG_DIR/celery_beat.launchd.log"
CELERY_BEAT_LAUNCHD_ERR_LOG="$LOG_DIR/celery_beat.launchd.err.log"
CELERY_BEAT_PID_FILE="$RUNTIME_DIR/celery_beat.pid"
LAUNCHAGENT_SOURCE_DIR="$ROOT_DIR/ops/launchagents"
LAUNCHAGENT_INSTALL_DIR="$HOME/Library/LaunchAgents"
CONTENT_FABRIC_SERVICE_LABELS=(
  "com.contentfabric.admin.runserver"
  "com.contentfabric.celery.worker"
  "com.contentfabric.celery.marketer"
  "com.contentfabric.celery.beat"
  "com.contentfabric.bot"
)
REDIS_SERVICE_LABEL="homebrew.mxcl.redis"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
MAIN_SCRIPT="$ROOT_DIR/main.py"
ADMIN_MANAGE="$ROOT_DIR/admin_panel/manage.py"
LOCK_FILE="/tmp/content_fabric_bot.lock"

usage() {
  echo "Usage: $0 {start [-f|--follow]|launchd-foreground|stop|restart [-f|--follow]|status|logs [-f|--follow]|adopt <pid>|services-up|services-restart|services-status|final-worker <run_id> [--no-delivery]|final-worker-foreground <run_id> [--no-delivery]|final-worker-status|scheduled-worker [limit]|scheduled-worker-status|celery-worker|celery-worker-foreground|celery-worker-reload|celery-worker-status|celery-beat|celery-beat-foreground|celery-beat-status}"
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
  if [[ "$command" == *"$MAIN_SCRIPT"* ]]; then
    return 0
  fi
  open_files="$(lsof -p "$pid" 2>/dev/null || true)"
  [[ "$open_files" == *"$LOCK_FILE"* ]] && [[ "$open_files" == *"$ROOT_DIR"* ]]
}

all_bot_pids() {
  local pids
  pids="$(pgrep -f "$MAIN_SCRIPT" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids"
    return 0
  fi
  pids="$(lsof -t "$LOCK_FILE" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids"
    return 0
  fi
  ps -axo pid=,command= 2>/dev/null | awk -v main_script="$MAIN_SCRIPT" 'index($0, main_script) {print $1}' || true
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

is_durable_syntax_worker() {
  local command
  command="$(ps -p "$1" -o command= 2>/dev/null || true)"
  [[ "$command" == *" -m app.syntax_durable "* ]]
}

terminate_bot_tree() {
  local pid="$1"
  local child
  for child in $(child_pids "$pid"); do
    # Syntx owns an independent session and must survive frontend restarts.
    is_durable_syntax_worker "$child" && continue
    kill "$child" 2>/dev/null || true
  done
  kill "$pid" 2>/dev/null || true
}

force_kill_bot_tree() {
  local pid="$1"
  local child
  for child in $(child_pids "$pid"); do
    # Syntx owns an independent session and must survive frontend restarts.
    is_durable_syntax_worker "$child" && continue
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

launchd_foreground_bot() {
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

  cd "$ROOT_DIR"
  echo "$$" > "$PID_FILE"
  write_status "running" "$$"
  echo "Bot launchd foreground starting: pid=$$"
  exec "$PYTHON_BIN" "$MAIN_SCRIPT"
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
  local -a delivery_args=(--once)
  if [[ "${2:-}" == "--no-delivery" ]]; then
    delivery_args+=(--no-delivery)
  elif [[ -n "${2:-}" ]]; then
    echo "Unknown final worker option: $2" >&2
    exit 2
  fi
  local worker_id
  worker_id="botctl-$$"
  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" "$ADMIN_MANAGE" final_build_worker "--run-id=$run_id" "--worker-id=$worker_id" "${delivery_args[@]}" >> "$FINAL_WORKER_LOG" 2>&1 &
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
  local -a delivery_args=(--once)
  if [[ "${2:-}" == "--no-delivery" ]]; then
    delivery_args+=(--no-delivery)
  elif [[ -n "${2:-}" ]]; then
    echo "Unknown final worker option: $2" >&2
    exit 2
  fi
  local worker_id
  worker_id="botctl-foreground-$$"
  cd "$ROOT_DIR"
  "$PYTHON_BIN" "$ADMIN_MANAGE" final_build_worker "--run-id=$run_id" "--worker-id=$worker_id" "${delivery_args[@]}"
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
  local concurrency="${CELERY_WORKER_CONCURRENCY:-4}"
  nohup env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin worker --loglevel=INFO --concurrency="$concurrency" --pool=threads --queues=celery --hostname='default@%h' >> "$CELERY_WORKER_LOG" 2>&1 &
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
  local concurrency="${CELERY_WORKER_CONCURRENCY:-4}"
  env "PYTHONPATH=$(celery_pythonpath)" "$PYTHON_BIN" -m celery -A cf_admin worker --loglevel=INFO --concurrency="$concurrency" --pool=threads --queues=celery --hostname='default@%h'
}

celery_worker_reload() {
  ensure_runtime
  local domain="gui/$(id -u)"
  local service="$domain/com.contentfabric.celery.worker"
  # launchd can reject bootstrapping a plist directly from /opt. Prefer the
  # already installed user LaunchAgent, retaining its deployment settings.
  local reload_plist="$CELERY_WORKER_LAUNCHAGENT_PLIST"
  local installed_plist="$HOME/Library/LaunchAgents/com.contentfabric.celery.worker.plist"
  if [[ -f "$installed_plist" ]]; then
    reload_plist="$installed_plist"
  fi
  if [[ ! -f "$reload_plist" ]]; then
    echo "LaunchAgent plist not found: $reload_plist" >&2
    exit 1
  fi
  plutil -lint "$reload_plist" >/dev/null
  launchctl bootout "$service" >/dev/null 2>&1 || true
  sleep 1
  launchctl bootstrap "$domain" "$reload_plist"
  launchctl enable "$service"
  launchctl kickstart -k "$service"
  sleep 1
  echo "Celery worker LaunchAgent reloaded from $reload_plist"
  launchctl print "$service" | rg "state =|pid =|last exit status =" || true
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

launchd_domain() {
  echo "gui/$(id -u)"
}

launchd_service_loaded() {
  local label="$1"
  launchctl print "$(launchd_domain)/$label" >/dev/null 2>&1
}

launchd_service_running() {
  local label="$1"
  launchctl print "$(launchd_domain)/$label" 2>/dev/null | rg -q "state = running"
}

bootstrap_launchagent() {
  local plist="$1"
  if launchctl bootstrap "$(launchd_domain)" "$plist"; then
    return 0
  fi
  # launchd can briefly retain a service after bootout even though `print`
  # already says it is gone. One short retry makes plist migrations reliable.
  sleep 2
  launchctl bootstrap "$(launchd_domain)" "$plist"
}

install_content_fabric_launchagent() {
  local label="$1"
  local source_plist="$LAUNCHAGENT_SOURCE_DIR/$label.plist"
  local installed_plist="$LAUNCHAGENT_INSTALL_DIR/$label.plist"
  local service="$(launchd_domain)/$label"
  local changed=0

  if [[ ! -f "$source_plist" ]]; then
    echo "LaunchAgent source not found: $source_plist" >&2
    return 1
  fi
  plutil -lint "$source_plist" >/dev/null
  mkdir -p "$LAUNCHAGENT_INSTALL_DIR"
  if [[ ! -f "$installed_plist" ]] || ! cmp -s "$source_plist" "$installed_plist"; then
    install -m 0644 "$source_plist" "$installed_plist"
    changed=1
  fi

  if launchd_service_loaded "$label"; then
    if [[ "$changed" -eq 1 ]]; then
      launchctl bootout "$service" >/dev/null || true
      sleep 1
      bootstrap_launchagent "$installed_plist"
    fi
  else
    bootstrap_launchagent "$installed_plist"
  fi
  launchctl enable "$service"
  if ! launchd_service_running "$label"; then
    launchctl kickstart -k "$service"
  fi
}

ensure_redis_launchagent() {
  local installed_plist="$LAUNCHAGENT_INSTALL_DIR/$REDIS_SERVICE_LABEL.plist"
  local service="$(launchd_domain)/$REDIS_SERVICE_LABEL"
  if launchd_service_loaded "$REDIS_SERVICE_LABEL"; then
    if ! launchd_service_running "$REDIS_SERVICE_LABEL"; then
      launchctl kickstart -k "$service"
    fi
    return 0
  fi
  if [[ ! -f "$installed_plist" ]]; then
    echo "Redis LaunchAgent not found: $installed_plist" >&2
    echo "Install it once with: brew services start redis" >&2
    return 1
  fi
  plutil -lint "$installed_plist" >/dev/null
  bootstrap_launchagent "$installed_plist"
  launchctl enable "$service"
  launchctl kickstart -k "$service"
}

services_up() {
  ensure_runtime
  ensure_redis_launchagent
  local label
  for label in "${CONTENT_FABRIC_SERVICE_LABELS[@]}"; do
    install_content_fabric_launchagent "$label"
  done
  wait_for_admin
  services_status
}

services_restart() {
  ensure_runtime
  ensure_redis_launchagent
  local label service
  for label in "${CONTENT_FABRIC_SERVICE_LABELS[@]}"; do
    install_content_fabric_launchagent "$label"
    service="$(launchd_domain)/$label"
    launchctl kickstart -k "$service"
  done
  wait_for_admin
  services_status
}

wait_for_admin() {
  local attempt
  for attempt in {1..15}; do
    if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:8000/admin/login/"; then
      return 0
    fi
    sleep 1
  done
  return 0
}

services_status() {
  ensure_runtime
  local failed=0 label state
  echo "Content Fabric services:"
  for label in "$REDIS_SERVICE_LABEL" "${CONTENT_FABRIC_SERVICE_LABELS[@]}"; do
    if launchd_service_running "$label"; then
      state="running"
    elif launchd_service_loaded "$label"; then
      state="loaded, not running"
      failed=1
    else
      state="not loaded"
      failed=1
    fi
    printf '  %-42s %s\n' "$label" "$state"
  done

  if command -v redis-cli >/dev/null 2>&1 && redis-cli ping >/dev/null 2>&1; then
    echo "  Redis ping                                 PONG"
    printf '  Celery queues                              default=%s marketer=%s\n' \
      "$(redis-cli LLEN celery 2>/dev/null || echo '?')" \
      "$(redis-cli LLEN marketer 2>/dev/null || echo '?')"
  else
    echo "  Redis ping                                 failed"
    failed=1
  fi
  if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:8000/admin/login/"; then
    echo "  Admin HTTP                                 healthy"
  else
    echo "  Admin HTTP                                 unavailable"
    failed=1
  fi
  return "$failed"
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
  launchd-foreground)
    launchd_foreground_bot
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
  services-up)
    services_up
    ;;
  services-restart)
    services_restart
    ;;
  services-status)
    services_status
    ;;
  final-worker)
    final_worker_once "${2:-}" "${3:-}"
    ;;
  final-worker-foreground)
    final_worker_foreground "${2:-}" "${3:-}"
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
  celery-worker-reload)
    celery_worker_reload
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
