#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.bot.pid"
LOG_DIR="$ROOT_DIR/logs"
SUPERVISOR_LOG="$LOG_DIR/bot.supervisor.log"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

usage() {
  echo "Usage: $0 {start|stop|restart|status|logs}"
}

ensure_runtime() {
  mkdir -p "$LOG_DIR"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python not found: $PYTHON_BIN" >&2
    exit 1
  fi
}

pid_is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

current_pid() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d '[:space:]' < "$PID_FILE"
  fi
}

start_bot() {
  ensure_runtime
  local pid
  pid="$(current_pid || true)"
  if pid_is_running "$pid"; then
    echo "Bot is already running: pid=$pid"
    return 0
  fi

  rm -f "$PID_FILE"
  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" main.py >> "$SUPERVISOR_LOG" 2>&1 &
  pid="$!"
  echo "$pid" > "$PID_FILE"
  sleep 1

  if pid_is_running "$pid"; then
    echo "Bot started: pid=$pid"
    echo "Supervisor log: $SUPERVISOR_LOG"
  else
    rm -f "$PID_FILE"
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
    return 0
  fi

  kill "$pid"
  for _ in {1..20}; do
    if ! pid_is_running "$pid"; then
      rm -f "$PID_FILE"
      echo "Bot stopped: pid=$pid"
      return 0
    fi
    sleep 0.25
  done

  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Bot force-stopped: pid=$pid"
}

status_bot() {
  local pid
  pid="$(current_pid || true)"
  if pid_is_running "$pid"; then
    echo "Bot is running: pid=$pid"
  else
    rm -f "$PID_FILE"
    echo "Bot is not running"
  fi
}

case "${1:-}" in
  start)
    start_bot
    ;;
  stop)
    stop_bot
    ;;
  restart)
    stop_bot
    start_bot
    ;;
  status)
    status_bot
    ;;
  logs)
    ensure_runtime
    touch "$SUPERVISOR_LOG"
    tail -80 "$SUPERVISOR_LOG"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
