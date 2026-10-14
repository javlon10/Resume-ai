#!/usr/bin/env bash
# systemd xizmatlarini o'rnatadi: server qayta yuklansa ham o'zi ko'tariladi.
#
#   ./scripts/install_systemd.sh          — o'rnatadi va yoqadi
#   ./scripts/install_systemd.sh --remove — o'chiradi
set -euo pipefail
source "$(dirname "$0")/env.sh"

LLAMA_UNIT=/etc/systemd/system/resume-analize-llama.service
API_UNIT=/etc/systemd/system/resume-analize-api.service

if [[ "${1:-}" == "--remove" ]]; then
  systemctl disable --now resume-analize-api.service resume-analize-llama.service 2>/dev/null || true
  rm -f "$API_UNIT" "$LLAMA_UNIT"
  systemctl daemon-reload
  echo "systemd xizmatlari o'chirildi."
  exit 0
fi

cat > "$LLAMA_UNIT" <<UNIT
[Unit]
Description=resume_analize — llama-server x4 GPU
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/start_servers.sh
ExecStop=$PROJECT_DIR/scripts/stop_servers.sh
TimeoutStartSec=600
Restart=no

[Install]
WantedBy=multi-user.target
UNIT

cat > "$API_UNIT" <<UNIT
[Unit]
Description=resume_analize — HTTPS API va veb interfeys
After=resume-analize-llama.service
Requires=resume-analize-llama.service

[Service]
Type=exec
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/secrets/prod.env
ExecStart=$PROJECT_DIR/scripts/start_prod.sh
Restart=always
RestartSec=5
# Modellar yuklanishini kutadi
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 120); do curl -sf http://127.0.0.1:8080/health >/dev/null && exit 0; sleep 5; done; exit 1'

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable resume-analize-llama.service resume-analize-api.service
echo "O'rnatildi va yoqildi:"
echo "  systemctl status resume-analize-llama"
echo "  systemctl status resume-analize-api"
echo
echo "DIQQAT: hozir xizmatlar qo'lda ishlab turgan bo'lsa, avval ularni to'xtating,"
echo "keyin: systemctl start resume-analize-llama resume-analize-api"
