#!/usr/bin/env bash
# llama-server instance'larini ishga tushiradi: har GPU uchun bittasi.
#   ./start_servers.sh            → GPU 0,1,2,3 → port 8080..8083
#   GPUS="0" ./start_servers.sh   → faqat GPU 0 → port 8080
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$LOG_DIR" "$RUN_DIR"

idx=0
for gpu in $GPUS; do
  port=$((BASE_PORT + idx))
  pidfile="$RUN_DIR/llama-gpu${gpu}.pid"
  logfile="$LOG_DIR/llama-gpu${gpu}.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "[skip] GPU $gpu allaqachon ishlayapti (PID $(cat "$pidfile"), port $port)"
    idx=$((idx + 1)); continue
  fi

  echo "[start] GPU $gpu → http://$HOST:$port  (log: $logfile)"
  CUDA_VISIBLE_DEVICES="$gpu" nohup "$LLAMA_BIN" \
    --model "$MODEL_PATH" \
    --alias qwen3.5-9b-q8 \
    --host "$HOST" --port "$port" \
    --n-gpu-layers 999 \
    --split-mode none \
    --ctx-size "$CTX_SIZE" \
    --parallel "$N_PARALLEL" \
    --batch-size "$BATCH_SIZE" \
    --ubatch-size "$UBATCH_SIZE" \
    --flash-attn on \
    --cache-type-k "$KV_TYPE" \
    --cache-type-v "$KV_TYPE" \
    --jinja \
    --metrics \
    --cont-batching \
    > "$logfile" 2>&1 &

  echo $! > "$pidfile"
  idx=$((idx + 1))
done

echo
echo "Yuklanishni kuzatish:  tail -f $LOG_DIR/llama-gpu0.log"
echo "Holatni tekshirish:    $PROJECT_DIR/scripts/health.sh"
