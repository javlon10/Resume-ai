#!/usr/bin/env bash
# Ovozli intervyu (STT/TTS) qismini loyihadan BUTUNLAY olib tashlaydi va
# loyihani ovoz qo'shilishidan oldingi holatiga qaytaradi.
#
#   ./scripts/remove_voice.sh --dry-run   — nima o'chirilishini ko'rsatadi
#   ./scripts/remove_voice.sh             — o'chiradi (avval zaxira nusxa oladi)
#
# Vaqtincha o'chirish uchun bu skript kerak emas — `RA_VOICE=0` yetarli:
#   RA_VOICE=0 ./scripts/start_api.sh --daemon
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$PROJECT_DIR"

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

FILES=(
  "app/voice.py"
  "app/voice_api.py"
  "app/static/voice.js"
  "data/voice_cache"
)
PATCHED=(
  "app/config.py"
  "app/api.py"
  "app/static/index.html"
)

echo "=== O'chiriladigan fayllar ==="
for f in "${FILES[@]}"; do
  [[ -e "$f" ]] && echo "  - $f" || echo "  - $f (yo'q)"
done

echo
echo "=== VOICE bloklari olib tashlanadigan fayllar ==="
for f in "${PATCHED[@]}"; do
  n=$(grep -c "VOICE:BEGIN" "$f" 2>/dev/null || true)
  echo "  - $f  ($n ta blok)"
done

if [[ $DRY -eq 1 ]]; then
  echo
  echo "--dry-run: hech narsa o'zgartirilmadi."
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$PROJECT_DIR/.voice-backup-$STAMP"
mkdir -p "$BACKUP"
echo
echo "Zaxira nusxa: $BACKUP"
for f in "${FILES[@]}" "${PATCHED[@]}"; do
  if [[ -e "$f" ]]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp -r "$f" "$BACKUP/$f"
  fi
done

# VOICE:BEGIN ... VOICE:END orasidagi satrlarni (belgilar bilan birga) olib tashlaymiz
for f in "${PATCHED[@]}"; do
  [[ -f "$f" ]] || continue
  "$PY" - "$f" <<'PYEOF'
import re, sys
from pathlib import Path

p = Path(sys.argv[1])
text = p.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

out, depth, removed = [], 0, 0
for line in lines:
    if "VOICE:BEGIN" in line:
        depth += 1
        removed += 1
        continue
    if "VOICE:END" in line:
        depth = max(0, depth - 1)
        removed += 1
        continue
    if depth == 0:
        out.append(line)
    else:
        removed += 1

if depth != 0:
    print(f"  ! {p}: VOICE:BEGIN/END juftlashmadi — qo'lda tekshiring", file=sys.stderr)
    sys.exit(1)

# Blok o'rnida qolgan ketma-ket bo'sh satrlarni siqamiz
joined = re.sub(r"\n{4,}", "\n\n\n", "".join(out))
p.write_text(joined, encoding="utf-8")
print(f"  {p}: {removed} satr olib tashlandi")
PYEOF
done

for f in "${FILES[@]}"; do
  [[ -e "$f" ]] && { rm -rf "$f"; echo "  $f o'chirildi"; }
done

# Eskirgan bytecode qolmasin (aks holda grep ularni topadi)
rm -f app/__pycache__/voice*.pyc
find . -path ./.venv -prune -o -name "__pycache__" -print0 2>/dev/null \
  | xargs -0 rm -rf 2>/dev/null || true

echo
echo "Tekshiruv:"
if grep -rn --binary-files=without-match "VOICE:BEGIN\|voice_api\|RA_VOICE_HOOK" app/ 2>/dev/null; then
  echo "  ! yuqoridagi qoldiqlarni qo'lda ko'rib chiqing"
else
  echo "  qoldiq yo'q"
fi
"$PY" -m compileall -q app >/dev/null && echo "  modullar kompilyatsiya bo'ldi"

echo
echo "Tayyor. API'ni qayta ishga tushiring:"
echo "  ./scripts/stop_api.sh && ./scripts/start_api.sh --daemon"
echo
echo "Qaytarish kerak bo'lsa: cp -r $BACKUP/* $PROJECT_DIR/"
