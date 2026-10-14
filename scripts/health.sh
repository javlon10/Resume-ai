#!/usr/bin/env bash
# Har bir instance holati + GPU xotira holati.
set -uo pipefail
source "$(dirname "$0")/env.sh"

idx=0
for gpu in $GPUS; do
  port=$((BASE_PORT + idx))
  body="$(curl -s --max-time 3 "http://$HOST:$port/health" || true)"
  if [[ -z "$body" ]]; then
    state="JAVOB YO'Q"
  else
    state="$body"
  fi
  printf 'GPU %s  port %s  →  %s\n' "$gpu" "$port" "$state"
  idx=$((idx + 1))
done

echo
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
           --format=csv,noheader,nounits 2>/dev/null \
  | awk -F', ' '{printf "GPU %s: %s/%s MiB, util %s%%, %s°C\n",$1,$2,$3,$4,$5}'
