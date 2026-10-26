#!/usr/bin/env bash
# Run the new (pytincture) page host-side against a THROWAWAY library, with a pidfile.
#
#   PANTHEON_DATA_DIR=/tmp/pantheon-dev scripts/web-dev.sh start|stop|restart|log
#
# Never point PANTHEON_DATA_DIR at the real data/: the smoke test writes to it.
# Port 7871 by default (7861/7866 are the real container's). Needs .venv and the browser
# wheel in appcode/ — see CLAUDE.md, "The new page".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${PANTHEON_DATA_DIR:?set PANTHEON_DATA_DIR to a scratch library}"
PORT="${PANTHEON_WEB_PORT:-7871}"
STATE="${XDG_RUNTIME_DIR:-/tmp}/pantheon-web-dev"
PIDFILE="$STATE/pid"
LOG="$STATE/log"
mkdir -p "$STATE"

if [[ "$(readlink -f "$PANTHEON_DATA_DIR")" == "$(readlink -f "$ROOT/data")" ]]; then
  echo "refusing: PANTHEON_DATA_DIR is the real library" >&2
  exit 1
fi

stop() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")"
    for _ in $(seq 1 20); do kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break; sleep 0.25; done
    echo "stopped"
  fi
  rm -f "$PIDFILE"
}

start() {
  cd "$ROOT"
  PANTHEON_SERVE=web PANTHEON_WEB_PORT="$PORT" PANTHEON_DATA_DIR="$PANTHEON_DATA_DIR" \
    KOBOLD_URL="${KOBOLD_URL:-http://localhost:5001}" COMFYUI_URL="${COMFYUI_URL:-http://localhost:8188}" \
    setsid .venv/bin/python service.py >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 40); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/pantheon/login"; then
      echo "up: http://127.0.0.1:$PORT/pantheon/ (pid $(cat "$PIDFILE"), log $LOG)"; return 0
    fi
    sleep 0.5
  done
  echo "did not start; last lines:" >&2; tail -20 "$LOG" >&2; return 1
}

case "${1:-}" in
  start) stop >/dev/null; start ;;
  stop) stop ;;
  restart) stop >/dev/null; start ;;
  log) tail -f "$LOG" ;;
  *) echo "usage: $0 start|stop|restart|log" >&2; exit 2 ;;
esac
