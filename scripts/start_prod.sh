#!/usr/bin/env bash
# Production: HTTPS + API kalit bilan ishga tushiradi.
#
#   ./scripts/start_prod.sh            — oldinda (log ekranga)
#   ./scripts/start_prod.sh --daemon   — fonda, pidfile bilan
#
# Sozlamalar secrets/prod.env dan o'qiladi (API kalit, port, CORS).
# Sertifikat: certs/server.crt + certs/server.key
set -euo pipefail
source "$(dirname "$0")/env.sh"
require_venv
cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$RUN_DIR"

ENV_FILE="$PROJECT_DIR/secrets/prod.env"
CERT="$PROJECT_DIR/certs/server.crt"
KEY="$PROJECT_DIR/certs/server.key"

for f in "$ENV_FILE" "$CERT" "$KEY"; do
  [[ -f "$f" ]] || { echo "Topilmadi: $f" >&2; exit 1; }
done

set -a; source "$ENV_FILE"; set +a

if [[ -z "${RA_API_KEY:-}" ]]; then
  echo "XATO: RA_API_KEY bo'sh. Internetga ochiq xizmat kalitsiz ishlamasin." >&2
  exit 1
fi

API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8430}"
PIDFILE="$RUN_DIR/api.pid"
LOGFILE="$LOG_DIR/api-prod.log"

# Xotiradagi batch tasklar sababli bitta worker (README 11-bo'lim).
CMD=("$PY" -m uvicorn app.api:api
     --host "$API_HOST" --port "$API_PORT" --workers 1
     --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
     --proxy-headers --forwarded-allow-ips '*')

if [[ "${1:-}" == "--daemon" ]]; then
  # Pidfile tekshiruvi FAQAT shu yerda: systemd oldinda ishlatadi va o'zi
  # nazorat qiladi, u yerda pidfile tekshirilsa eskirgan fayl xizmatni
  # ishga tushirmay qo'yardi.
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "API allaqachon ishlayapti (PID $(cat "$PIDFILE"))"
    echo "To'xtatish: $PROJECT_DIR/scripts/stop_api.sh"
    exit 0
  fi
  nohup "${CMD[@]}" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Ishga tushmadi. Log: $LOGFILE" >&2
    tail -20 "$LOGFILE" >&2
    rm -f "$PIDFILE"
    exit 1
  fi
  echo "API (HTTPS) fonda ishga tushdi — PID $(cat "$PIDFILE")"
  echo "  ichki:    https://127.0.0.1:$API_PORT/"
  [[ -n "${PUBLIC_URL:-}" ]] && echo "  tashqi:   $PUBLIC_URL"
  echo "  log:      $LOGFILE"
  echo "  API kalit: secrets/prod.env"
else
  exec "${CMD[@]}"
fi
