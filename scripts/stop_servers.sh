#!/usr/bin/env bash
# Barcha llama-server instance'larini to'xtatadi.
set -uo pipefail
source "$(dirname "$0")/env.sh"

shopt -s nullglob
for pidfile in "$RUN_DIR"/llama-gpu*.pid; do
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop] PID $pid ($(basename "$pidfile" .pid))"
    kill "$pid"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && { echo "  → SIGKILL"; kill -9 "$pid"; }
  else
    echo "[gone] $(basename "$pidfile" .pid) ishlamayapti"
  fi
  rm -f "$pidfile"
done
echo "Tugadi."
