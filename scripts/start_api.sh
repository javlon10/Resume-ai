#!/usr/bin/env bash
# HTTP API ni ishga tushiradi (llama-server'lar allaqachon ishlab turishi kerak).
#   ./scripts/start_api.sh            — oldinda (foreground)
#   ./scripts/start_api.sh --daemon   — fonda, pidfile bilan
set -euo pipefail
source "$(dirname "$0")/env.sh"
require_venv
cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$RUN_DIR"

API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-9000}"
PIDFILE="$RUN_DIR/api.pid"
LOGFILE="$LOG_DIR/api.log"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "API allaqachon ishlayapti (PID $(cat "$PIDFILE"), port $API_PORT)"
  echo "To'xtatish: $PROJECT_DIR/scripts/stop_api.sh"
  exit 0
fi

# Xotirada saqlanadigan batch tasklar sababli bitta worker ishlatiladi.
# Ko'p worker kerak bo'lsa tasklarni Redis kabi umumiy omborga ko'chirish kerak.
CMD=("$PY" -m uvicorn app.api:api --host "$API_HOST" --port "$API_PORT" --workers 1)

if [[ "${1:-}" == "--daemon" ]]; then
  nohup "${CMD[@]}" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "API fonda ishga tushdi: http://$API_HOST:$API_PORT  (PID $!, log: $LOGFILE)"
  echo "  interfeys: http://localhost:$API_PORT/"
  echo "  hujjat:    http://localhost:$API_PORT/docs"
else
  exec "${CMD[@]}"
fi
