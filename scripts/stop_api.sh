#!/usr/bin/env bash
# API'ni to'xtatadi (pidfile bo'yicha — pkill -f ishlatilmaydi, chunki u
# o'z buyruq satrini ham topib shellni o'ldirishi mumkin).
set -uo pipefail
source "$(dirname "$0")/env.sh"
PIDFILE="$RUN_DIR/api.pid"

if [[ ! -f "$PIDFILE" ]]; then
  echo "API pidfile topilmadi — ishlamayapti deb hisoblanadi."
  exit 0
fi
pid="$(cat "$PIDFILE")"
if kill -0 "$pid" 2>/dev/null; then
  echo "[stop] API PID $pid"
  kill "$pid"
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && { echo "  → SIGKILL"; kill -9 "$pid"; }
else
  echo "[gone] PID $pid ishlamayapti"
fi
rm -f "$PIDFILE"
echo "Tugadi."
