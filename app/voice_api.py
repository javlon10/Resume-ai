"""Ovozli intervyu uchun HTTP endpointlar.

Alohida router: api.py uni FAQAT `RA_VOICE=1` bo'lganda ulaydi. Shu sababli
ovoz qismini o'chirish yoki butunlay olib tashlash asosiy kodga tegmaydi
(scripts/remove_voice.sh).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import os

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import config, voice
from .interviews import InterviewError, SessionStore

router = APIRouter(prefix="/voice", tags=["ovoz"])
_sessions = SessionStore(config.SESSIONS_DIR)

# Kirish nazorati. Ikki xil endpoint bor:
#   * HR/umumiy (tts, stt) — API kalit talab qiladi, chunki tashqi xizmatlarga
#     xarajat tushiradi va matnni ixtiyoriy yuborish imkonini beradi;
#   * sessiyaga bog'liq (question, answer) — kalit talab qilinmaydi, chunki
#     nomzodda kalit bo'lmaydi. Ular sessiya id'si (48 bit tasodifiy) bilan
#     himoyalangan va faqat o'sha sessiya doirasida ishlaydi.
_API_KEY = os.getenv("RA_API_KEY", "").strip()


def require_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    if not _API_KEY:
        return
    if x_api_key != _API_KEY:
        raise HTTPException(401, "X-API-Key noto'g'ri yoki berilmagan")


def _has_key(x_api_key: str | None) -> bool:
    return not _API_KEY or x_api_key == _API_KEY


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    emotion: str | None = Field(None, description="calm, happy, sad, ... (/voice/emotions)")
    speed: float | None = Field(None, ge=0.5, le=2.0)
    seed: int | None = None


def _wav_response(wav: bytes, meta: dict[str, Any], cached: bool = False) -> Response:
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "x-audio-duration": str(meta.get("duration_s") or ""),
            "x-inference-time": str(meta.get("inference_s") or ""),
            "x-cached": "1" if cached else "0",
            "Cache-Control": "public, max-age=86400",
        },
    )


# --- kesh: bir xil savol qayta-qayta sintez qilinmasin ------------------------
def _cache_path(text: str, emotion: str, speed: float, seed: int) -> Path:
    key = f"{text}|{emotion}|{speed}|{seed}".encode("utf-8")
    name = hashlib.sha256(key).hexdigest()[:32] + ".wav"
    return Path(config.VOICE_CACHE_DIR) / name


def _synth_cached(text: str, emotion: str | None = None,
                  speed: float | None = None, seed: int | None = None):
    """TTS natijasini diskda keshlaydi. (wav, meta, cached) qaytaradi."""
    em = emotion or config.TTS_EMOTION
    sp = speed if speed is not None else config.TTS_SPEED
    sd = seed if seed is not None else config.TTS_SEED

    if not config.VOICE_CACHE_ENABLED:
        wav, meta = voice.tts_synthesize(text, em, sp, sd)
        return wav, meta, False

    p = _cache_path(text, em, sp, sd)
    if p.exists():
        return p.read_bytes(), {"bytes": p.stat().st_size, "chars": len(text)}, True

    wav, meta = voice.tts_synthesize(text, em, sp, sd)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(wav)
        tmp.replace(p)
    except OSError:
        pass                                   # kesh yozilmasa ham ishlayveradi
    return wav, meta, False


@router.get("/voice.js", include_in_schema=False)
def voice_js() -> FileResponse:
    """Interfeys qatlami. index.html shu manzildan yuklaydi."""
    p = Path(__file__).resolve().parent / "static" / "voice.js"
    if not p.exists():
        raise HTTPException(404, "voice.js topilmadi")
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


# --- holat --------------------------------------------------------------------
@router.get("/status", summary="STT va TTS xizmatlari holati")
def status(x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
    """Har bir xizmat alohida tekshiriladi — biri ishlamasa ikkinchisi ishlayveradi.

    Nomzodga ham kerak (interfeys qaysi tugmani ko'rsatishni shu yerdan biladi),
    shuning uchun kalitsiz ham ochiq. Lekin kalitsiz ICHKI MANZILLAR va xato
    tafsilotlari berilmaydi — ular tashqi odamga keraksiz ma'lumot.
    """
    full = voice.voice_status()
    if _has_key(x_api_key):
        return full
    return {
        "enabled": full["enabled"],
        "stt": {"up": full["stt"]["up"]},
        "tts": {"up": full["tts"]["up"]},
        "any_up": full["any_up"],
    }


@router.get("/emotions", summary="TTS ovoz emotsiyalari",
            dependencies=[Depends(require_key)])
def emotions() -> dict[str, Any]:
    try:
        return {"emotions": voice.tts_emotions()}
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))


# --- TTS ----------------------------------------------------------------------
@router.post("/tts", summary="Matnni ovozga o'girish (WAV qaytaradi)",
             response_class=Response, dependencies=[Depends(require_key)])
def tts(req: TTSRequest) -> Response:
    try:
        wav, meta, cached = _synth_cached(req.text, req.emotion, req.speed, req.seed)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))
    return _wav_response(wav, meta, cached)


@router.get("/question/{sid}/{qid}", summary="Sessiya savolini ovoz bilan o'qib berish",
            response_class=Response)
def speak_question(sid: str, qid: str) -> Response:
    """Savol matnini serverdan oladi — nomzodga matnni qayta yuborish shart emas."""
    try:
        sess = _sessions.load(sid)
    except InterviewError as e:
        raise HTTPException(422, str(e))
    if sess is None:
        raise HTTPException(404, f"Sessiya topilmadi: {sid}")

    q = next((x for x in sess.get("questions", []) if x["id"] == qid), None)
    if q is None:
        raise HTTPException(404, f"Savol topilmadi: {qid}")

    try:
        wav, meta, cached = _synth_cached(q["text"].strip())
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))
    return _wav_response(wav, meta, cached)


# --- STT ----------------------------------------------------------------------
@router.post("/stt", summary="Ovozni matnga o'girish",
             dependencies=[Depends(require_key)])
async def stt(voice_file: UploadFile = File(..., alias="voice")) -> dict[str, Any]:
    """Audio fayl → matn. Brauzer WebM yozsa ham bo'ladi — serverda WAV'ga o'giriladi."""
    raw = await voice_file.read()
    if not raw:
        raise HTTPException(422, "Audio fayl bo'sh")
    try:
        return voice.stt_transcribe(raw, voice_file.filename or "voice.webm")
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))


@router.post("/answer/{sid}/{qid}", summary="Ovozli javobni yozib, sessiyaga saqlash")
async def voice_answer(sid: str, qid: str,
                       voice_file: UploadFile = File(..., alias="voice")) -> dict[str, Any]:
    """Nomzod ovozini matnga o'giradi va sessiyadagi javobga qo'shadi.

    Qo'shib qo'yadi (almashtirmaydi): nomzod bir savolga bir necha marta
    gapirishi yoki matnni qo'lda to'ldirishi mumkin.
    """
    try:
        sess = _sessions.load(sid)
    except InterviewError as e:
        raise HTTPException(422, str(e))
    if sess is None:
        raise HTTPException(404, f"Sessiya topilmadi: {sid}")
    if sess.get("status") != "open":
        raise HTTPException(409, f"Sessiya yopilgan (status: {sess['status']})")
    if not any(q["id"] == qid for q in sess.get("questions", [])):
        raise HTTPException(404, f"Savol topilmadi: {qid}")

    raw = await voice_file.read()
    if not raw:
        raise HTTPException(422, "Audio fayl bo'sh")
    try:
        res = voice.stt_transcribe(raw, voice_file.filename or "voice.webm")
    except voice.VoiceError as e:
        raise HTTPException(503, str(e))

    text = res.get("text", "").strip()
    prev = (sess.get("answers") or {}).get(qid, "").strip()
    sess.setdefault("answers", {})[qid] = f"{prev} {text}".strip() if prev else text
    _sessions.save(sess)

    return {**res, "session_id": sid, "question_id": qid, "answer": sess["answers"][qid]}
