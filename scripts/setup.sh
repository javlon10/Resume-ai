#!/usr/bin/env bash
# Loyiha uchun alohida virtual muhit yaratadi va talablarni o'rnatadi.
# Tizim python'iga hech narsa o'rnatilmaydi.
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$PROJECT_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[1/3] Virtual muhit yaratilmoqda: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "[1/3] Virtual muhit mavjud: $VENV_DIR"
fi

echo "[2/3] pip yangilanmoqda"
"$PY" -m pip install --quiet --upgrade pip

echo "[3/3] Talablar o'rnatilmoqda (requirements.txt)"
"$PY" -m pip install --quiet -r requirements.txt

echo
echo "Tayyor. Tekshirish:"
echo "  $PROJECT_DIR/scripts/ra health"
"$PY" -c "import fastapi, uvicorn, pdfplumber, docx, yaml, requests; print('  barcha paketlar joyida')"
