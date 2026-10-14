#!/usr/bin/env bash
# resume_analize — umumiy sozlamalar (barcha skriptlar shu fayldan o'qiydi)

PROJECT_DIR="/root/resume-ai/resume_analize"
LLAMA_BIN="/root/qwen-q8-test/llama.cpp/build/bin/llama-server"
MODEL_PATH="/root/qwen-q8-test/models/Qwen3.5-9B-Q8_0.gguf"

# Har GPU uchun bitta instance. Model Q8_0 ≈ 9.1 GiB, T4 = 15 GiB → bitta kartaga sig'adi.
GPUS="${GPUS:-0 1 2 3}"
BASE_PORT="${BASE_PORT:-8080}"
HOST="${HOST:-127.0.0.1}"

# Kontekst: bitta instance uchun umumiy KV pool. Slotlar o'rtasida bo'linadi.
# O'lchov natijasi (bitta T4, 400 token generatsiya):
#   4 slot → 55 tok/s   6 slot → 58 tok/s   8 slot → 62 tok/s (umumiy)
# 8 slot eng yaxshi o'tkazuvchanlikni beradi, VRAM 11.3/15.3 GB — zaxira qoladi.
# Slot konteksti 16k: uzun resume (prompt ~9k) + to'liq tahlil (~4k) sig'adi.
CTX_SIZE="${CTX_SIZE:-131072}"     # 8 slot × 16k
N_PARALLEL="${N_PARALLEL:-8}"      # bitta GPU'dagi parallel slotlar
KV_TYPE="${KV_TYPE:-q8_0}"         # KV cache kvantlash → VRAM tejaydi
BATCH_SIZE="${BATCH_SIZE:-2048}"
UBATCH_SIZE="${UBATCH_SIZE:-512}"

LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/run"

# Loyihaning o'z virtual muhiti. Tizim python'i ishlatilmaydi.
VENV_DIR="$PROJECT_DIR/.venv"
PY="$VENV_DIR/bin/python3"

require_venv() {
  if [[ ! -x "$PY" ]]; then
    echo "Virtual muhit topilmadi: $VENV_DIR" >&2
    echo "Ishga tushiring: $PROJECT_DIR/scripts/setup.sh" >&2
    exit 1
  fi
}
