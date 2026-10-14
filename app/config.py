"""Loyiha sozlamalari. Hammasi ENV orqali ham o'zgartiriladi."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("RA_DATA_DIR", PROJECT_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
RESUMES_DIR = DATA_DIR / "resumes"
OUT_DIR = DATA_DIR / "out"
# Intervyu savol banklari (HR yozadi) va sessiyalar (diskda saqlanadi)
INTERVIEWS_DIR = DATA_DIR / "interviews"
SESSIONS_DIR = Path(os.getenv("RA_SESSIONS_DIR", DATA_DIR / "sessions"))
SESSION_TTL_S = int(os.getenv("RA_SESSION_TTL", str(30 * 24 * 3600)))

# --- llama-server endpointlari -------------------------------------------------
# Har GPU uchun bitta instance: 8080..8083. Klient round-robin bilan taqsimlaydi.
def _default_endpoints() -> list[str]:
    raw = os.getenv("RA_ENDPOINTS")
    if raw:
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    host = os.getenv("RA_HOST", "127.0.0.1")
    base = int(os.getenv("RA_BASE_PORT", "8080"))
    count = int(os.getenv("RA_N_SERVERS", "4"))
    return [f"http://{host}:{base + i}" for i in range(count)]


ENDPOINTS = _default_endpoints()

MODEL_ALIAS = os.getenv("RA_MODEL", "qwen3.5-9b-q8")

# --- generatsiya parametrlari --------------------------------------------------
# Baholash uchun GREEDY dekodlash (temperature=0): o'lchov ko'rsatdi — bir xil
# resumeni 4 marta baholaganda ballar tarqalishi o'rtacha 2.9 dan 2.0 ga tushdi
# (temperature 0.15 bilan solishtirganda).
#
# MUHIM: bu ham to'liq takrorlanuvchanlikni bermaydi. llama.cpp continuous
# batching ishlatadi — partiya tarkibi har safar boshqacha bo'lgani uchun
# suzuvchi nuqta yig'indilari tartibi o'zgaradi va ayrim tokenlar almashadi.
# Seed buni qoplamaydi. Amalda bir xil resume uchun ±2-3 ball farq bo'lishi
# normal; verdict chegaralari (82/68/52) shu farqni hisobga olib tanlangan.
TEMPERATURE = float(os.getenv("RA_TEMPERATURE", "0.0"))
TOP_P = float(os.getenv("RA_TOP_P", "1.0"))
TOP_K = int(os.getenv("RA_TOP_K", "1"))
MIN_P = float(os.getenv("RA_MIN_P", "0.0"))
# Yengil takrorlanish jazosi. Sinovda ko'rindi: o'zbek tilida uzun matn
# generatsiyasida model ba'zan siklga tushib qoladi ("Oliy sudi o'rinbosari
# o'rinbosari o'rinbosari..."). Grammar va maxLength buni cheklaydi, lekin
# maydon ichida axlat matn qolib ketishi mumkin. 1.05 — strukturali chiqishni
# buzmaydigan darajada yengil.
REPEAT_PENALTY = float(os.getenv("RA_REPEAT_PENALTY", "1.05"))
SEED = int(os.getenv("RA_SEED", "42"))
MAX_TOKENS = int(os.getenv("RA_MAX_TOKENS", "4096"))
# Javob kesilib qolsa, klient max_tokens ni shu chegaragacha avtomatik oshiradi.
MAX_TOKENS_CAP = int(os.getenv("RA_MAX_TOKENS_CAP", "8192"))

# Qwen3.x "thinking" rejimi. Structured JSON uchun o'chirib qo'yamiz —
# grammar bilan birga <think> bloki chiqsa JSON buziladi.
ENABLE_THINKING = os.getenv("RA_ENABLE_THINKING", "0") == "1"

REQUEST_TIMEOUT = float(os.getenv("RA_TIMEOUT", "600"))
MAX_RETRIES = int(os.getenv("RA_MAX_RETRIES", "3"))

# Resume matnini cheklash (kontekstga sig'ishi uchun), belgilar soni.
MAX_RESUME_CHARS = int(os.getenv("RA_MAX_RESUME_CHARS", "24000"))
MAX_JOB_CHARS = int(os.getenv("RA_MAX_JOB_CHARS", "8000"))

# --- sig'im va ustuvorlik -------------------------------------------------------
# Har bir llama-server 8 slot bilan ishga tushadi (scripts/env.sh: N_PARALLEL).
SLOTS_PER_SERVER = int(os.getenv("RA_SLOTS_PER_SERVER", "8"))
TOTAL_SLOTS = len(ENDPOINTS) * SLOTS_PER_SERVER

# Batch (resume to'plami) uchun chegara. Qolgan slotlar interaktiv ishlarga —
# intervyu baholash va bitta resume — doim bo'sh turadi, shunda ular katta
# batch orqasida navbatda kutmaydi.
#
# Nega GPU'larni statik bo'lmaymiz (2 ta resume + 2 ta intervyu):
# o'lchov ko'rsatdi, bitta GPU maksimum ~62 tok/s beradi. Statik bo'linishda
# resume batch faqat 2 GPU'ni ko'radi (~125 tok/s) va intervyu bo'lmaganda
# 2 GPU bekor turadi. Umumiy pool + zaxira slot esa batchga 4 GPU'ni beradi
# (~250 tok/s) va intervyu so'rovi kelganda unga darhol bo'sh slot topiladi.
RESERVED_SLOTS = int(os.getenv("RA_RESERVED_SLOTS", str(len(ENDPOINTS) * 2)))
BATCH_CONCURRENCY = int(os.getenv("RA_BATCH_CONCURRENCY",
                                  str(max(1, TOTAL_SLOTS - RESERVED_SLOTS))))

# Faqat slot zaxirasi yetarli emas: bo'sh slot topilsa ham, GPU batch bilan
# BO'LISHILADI va interaktiv so'rov sekinlashadi (o'lchovda 48s → 120s).
# Shuning uchun interaktiv ish bor paytda batch oxirgi N ta serverni butunlay
# bo'shatib beradi — shunda intervyu deyarli bo'sh GPU'da ishlaydi.
# Interaktiv ish bo'lmasa batch hamma GPU'ni ishlatadi (resurs bekor turmaydi).
RESERVED_GPUS = int(os.getenv("RA_RESERVED_GPUS", "1"))
# Oxirgi interaktiv so'rovdan keyin necha sekund zaxira saqlanadi
RESERVE_HOLD_S = float(os.getenv("RA_RESERVE_HOLD", "45"))

# Batch'da bir vaqtda yuboriladigan so'rovlar soni.
CONCURRENCY = int(os.getenv("RA_CONCURRENCY", str(BATCH_CONCURRENCY)))

# --- VOICE:BEGIN -------------------------------------------------------------
# Ovozli intervyu (STT/TTS). Butun blok ixtiyoriy — `RA_VOICE=0` bilan o'chadi,
# yoki scripts/remove_voice.sh bilan loyihadan butunlay chiqariladi.
VOICE_ENABLED = os.getenv("RA_VOICE", "1") == "1"

# STT — nutqni matnga o'girish (tashqi xizmat).
# Manzil ENV orqali beriladi: RA_STT_URL=http://<host>:<port>
STT_URL = os.getenv("RA_STT_URL", "http://127.0.0.1:3020").rstrip("/")
STT_TIMEOUT = float(os.getenv("RA_STT_TIMEOUT", "60"))        # bitta HTTP so'rov
STT_WAIT_TIMEOUT = float(os.getenv("RA_STT_WAIT", "180"))     # natijani kutish
STT_POLL_INTERVAL = float(os.getenv("RA_STT_POLL", "1.0"))

# TTS — matnni nutqqa o'girish (tashqi xizmat, odatda self-signed sertifikat).
# Manzil ENV orqali beriladi: RA_TTS_URL=https://<host>:<port>
TTS_URL = os.getenv("RA_TTS_URL", "https://127.0.0.1:8429").rstrip("/")
TTS_TIMEOUT = float(os.getenv("RA_TTS_TIMEOUT", "180"))
TTS_VERIFY_SSL = os.getenv("RA_TTS_VERIFY_SSL", "0") == "1"
TTS_EMOTION = os.getenv("RA_TTS_EMOTION", "calm")
TTS_SPEED = float(os.getenv("RA_TTS_SPEED", "1"))
TTS_SEED = int(os.getenv("RA_TTS_SEED", "1986"))
TTS_MAX_CHARS = int(os.getenv("RA_TTS_MAX_CHARS", "1200"))

VOICE_HEALTH_TIMEOUT = float(os.getenv("RA_VOICE_HEALTH_TIMEOUT", "4"))
VOICE_MAX_AUDIO_MB = float(os.getenv("RA_VOICE_MAX_AUDIO_MB", "25"))
# Savol audiosi keshi (bir xil savol qayta-qayta sintez qilinmasligi uchun)
VOICE_CACHE_DIR = Path(os.getenv("RA_VOICE_CACHE_DIR", DATA_DIR / "voice_cache"))
VOICE_CACHE_ENABLED = os.getenv("RA_VOICE_CACHE", "1") == "1"
# --- VOICE:END ---------------------------------------------------------------
