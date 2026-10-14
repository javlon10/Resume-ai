"""Ovozli intervyu: STT (nutq → matn) va TTS (matn → nutq) klientlari.

Bu modul TASHQI xizmatlarga murojaat qiladi — ular boshqa jamoa tomonidan
yuritiladi va doim ishlamasligi mumkin. Shuning uchun:

  * har bir xizmat ALOHIDA tekshiriladi (biri ishlamasa, ikkinchisi ishlayveradi);
  * har qanday xato `VoiceError` bo'lib chiqadi va intervyuning asosiy oqimini
    (matn bilan javob berish) hech qachon to'xtatmaydi;
  * `RA_VOICE=0` bilan butun ovoz qismi o'chadi.

Butunlay olib tashlash: scripts/remove_voice.sh (README "Ovozli intervyu" bo'limi).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from . import config

# Self-signed sertifikat ogohlantirishini o'chiramiz (TTS serverida shunday).
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:                                     # urllib3 bo'lmasa ham ishlayveradi
    pass


class VoiceError(RuntimeError):
    """Ovoz xizmatlaridagi har qanday muammo. Asosiy oqimni to'xtatmaydi."""


# --- umumiy ------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "resume_analize/voice"
    return s


@dataclass
class ServiceState:
    name: str
    url: str
    up: bool
    detail: str = ""
    latency_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "up": self.up,
                "detail": self.detail, "latency_ms": self.latency_ms}


# =============================================================================
# STT — nutqni matnga o'girish
# =============================================================================
# Xizmat ikki bosqichli: POST bilan navbatga qo'yiladi (HTTP 202 + task_id),
# keyin GET bilan natija so'raladi. Query parametr nomi aynan `task-id`
# (defis bilan) — `task_id` qabul qilinmaydi.

def _ffmpeg_to_wav(raw: bytes, suffix: str) -> bytes:
    """Brauzerdan kelgan audioni STT kutadigan WAV formatiga o'giradi.

    Brauzerning MediaRecorder'i odatda WebM/Opus yozadi, STT esa WAV bilan
    sinalgan. 16 kHz mono — nutqni tanish uchun standart format.
    """
    if not shutil.which("ffmpeg"):
        raise VoiceError("ffmpeg topilmadi — audio formatini o'girib bo'lmaydi")

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in{suffix or '.bin'}"
        dst = Path(d) / "out.wav"
        src.write_bytes(raw)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
             str(dst)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not dst.exists():
            raise VoiceError(
                f"Audio formatini o'girib bo'lmadi: {proc.stderr.decode()[:200]}"
            )
        return dst.read_bytes()


def stt_submit(wav: bytes, filename: str = "voice.wav") -> str:
    """Audioni navbatga qo'yadi va task_id qaytaradi."""
    url = f"{config.STT_URL}/stt/voice-to-text"
    try:
        # DIQQAT: maydon nomi aynan `voice`, JSON yuborilmaydi.
        # Content-Type va boundary'ni requests o'zi qo'yadi.
        r = _session().post(
            url,
            files={"voice": (filename, wav, "audio/wav")},
            timeout=config.STT_TIMEOUT,
        )
    except requests.RequestException as e:
        raise VoiceError(f"STT xizmatiga ulanib bo'lmadi ({config.STT_URL}): {e}")

    if r.status_code not in (200, 202):
        raise VoiceError(f"STT HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json().get("data") or {}
    except json.JSONDecodeError:
        raise VoiceError(f"STT javobi JSON emas: {r.text[:200]}")

    task_id = data.get("task_id")
    if not task_id:
        raise VoiceError(f"STT javobida task_id yo'q: {r.text[:200]}")
    return str(task_id)


def stt_result(task_id: str) -> dict[str, Any]:
    """Bitta marta natija so'raydi (poll qilmaydi)."""
    url = f"{config.STT_URL}/stt/get-result"
    try:
        # Query parametr nomi defis bilan: task-id
        r = _session().get(url, params={"task-id": task_id},
                           timeout=config.STT_TIMEOUT)
    except requests.RequestException as e:
        raise VoiceError(f"STT natijasini olib bo'lmadi: {e}")
    if r.status_code >= 400:
        raise VoiceError(f"STT HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json().get("data") or {}
    except json.JSONDecodeError:
        raise VoiceError(f"STT javobi JSON emas: {r.text[:200]}")


def stt_transcribe(audio: bytes, filename: str = "voice.wav") -> dict[str, Any]:
    """Audio → matn. Navbatga qo'yadi va natija tayyor bo'lguncha kutadi."""
    if not audio:
        raise VoiceError("Audio bo'sh")
    if len(audio) > config.VOICE_MAX_AUDIO_MB * 1024 * 1024:
        raise VoiceError(f"Audio {config.VOICE_MAX_AUDIO_MB} MB dan katta")

    suffix = Path(filename).suffix.lower()
    wav = audio if suffix == ".wav" else _ffmpeg_to_wav(audio, suffix)

    task_id = stt_submit(wav, Path(filename).stem + ".wav")
    deadline = time.monotonic() + config.STT_WAIT_TIMEOUT
    delay = config.STT_POLL_INTERVAL

    while time.monotonic() < deadline:
        time.sleep(delay)
        data = stt_result(task_id)
        status = str(data.get("status", "")).lower()
        if status == "done":
            return {
                "text": str(data.get("result", "")).strip(),
                "task_id": task_id,
                "lang": data.get("lang"),
                "words_count": data.get("words_count"),
                "processing_seconds": data.get("processing_seconds"),
            }
        if status in {"failed", "error"}:
            raise VoiceError(f"STT xatolik bilan tugadi: {data}")
        # pending / processing — kutamiz, intervalni asta oshiramiz
        delay = min(delay * 1.4, 3.0)

    raise VoiceError(
        f"STT {config.STT_WAIT_TIMEOUT} sekundda tayyor bo'lmadi (task {task_id})"
    )


# =============================================================================
# TTS — matnni nutqqa o'girish
# =============================================================================
def tts_synthesize(
    text: str,
    emotion: str | None = None,
    speed: float | None = None,
    seed: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Matn → WAV baytlari. (audio, meta) qaytaradi."""
    text = (text or "").strip()
    if not text:
        raise VoiceError("Matn bo'sh")
    if len(text) > config.TTS_MAX_CHARS:
        text = text[: config.TTS_MAX_CHARS].rsplit(" ", 1)[0]

    payload = {
        "text": text,
        "emotion": emotion or config.TTS_EMOTION,
        "speed": speed if speed is not None else config.TTS_SPEED,
        "seed": seed if seed is not None else config.TTS_SEED,
    }
    try:
        r = _session().post(
            f"{config.TTS_URL}/api/v1/tts/full",
            json=payload,
            headers={"Accept": "audio/wav"},
            timeout=config.TTS_TIMEOUT,
            verify=config.TTS_VERIFY_SSL,      # sertifikat self-signed
        )
    except requests.RequestException as e:
        raise VoiceError(f"TTS xizmatiga ulanib bo'lmadi ({config.TTS_URL}): {e}")

    if r.status_code >= 400:
        raise VoiceError(f"TTS HTTP {r.status_code}: {r.text[:200]}")
    if not r.content:
        raise VoiceError("TTS bo'sh javob qaytardi")
    if r.content[:4] != b"RIFF":
        raise VoiceError(f"TTS WAV emas, boshqa narsa qaytardi: {r.content[:80]!r}")

    return r.content, {
        "duration_s": r.headers.get("x-audio-duration"),
        "inference_s": r.headers.get("x-inference-time"),
        "bytes": len(r.content),
        "chars": len(text),
    }


def tts_emotions() -> list[dict[str, Any]]:
    try:
        r = _session().get(f"{config.TTS_URL}/api/v1/tts/emotions",
                           timeout=config.VOICE_HEALTH_TIMEOUT,
                           verify=config.TTS_VERIFY_SSL)
        r.raise_for_status()
        return r.json().get("emotions", [])
    except (requests.RequestException, json.JSONDecodeError) as e:
        raise VoiceError(f"TTS emotsiyalar ro'yxatini olib bo'lmadi: {e}")


# =============================================================================
# Holat
# =============================================================================
def check_stt() -> ServiceState:
    t0 = time.monotonic()
    try:
        # Yengil tekshiruv: mavjud bo'lmagan task so'raymiz. Xizmat tirik bo'lsa
        # HTTP javob qaytaradi (404/400 ham bo'lishi mumkin) — muhimi, ulanish bor.
        r = _session().get(f"{config.STT_URL}/stt/get-result",
                           params={"task-id": "__healthcheck__"},
                           timeout=config.VOICE_HEALTH_TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        return ServiceState("stt", config.STT_URL, True, f"HTTP {r.status_code}", ms)
    except requests.RequestException as e:
        return ServiceState("stt", config.STT_URL, False,
                            f"{type(e).__name__}: {str(e)[:120]}")


def check_tts() -> ServiceState:
    t0 = time.monotonic()
    try:
        r = _session().get(f"{config.TTS_URL}/api/v1/tts/emotions",
                           timeout=config.VOICE_HEALTH_TIMEOUT,
                           verify=config.TTS_VERIFY_SSL)
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return ServiceState("tts", config.TTS_URL, False, f"HTTP {r.status_code}", ms)
        return ServiceState("tts", config.TTS_URL, True, f"HTTP {r.status_code}", ms)
    except requests.RequestException as e:
        return ServiceState("tts", config.TTS_URL, False,
                            f"{type(e).__name__}: {str(e)[:120]}")


def voice_status() -> dict[str, Any]:
    """Ikkala xizmat holati. Biri ishlamasa ham ikkinchisi ishlatiladi."""
    stt, tts = check_stt(), check_tts()
    return {
        "enabled": config.VOICE_ENABLED,
        "stt": stt.as_dict(),
        "tts": tts.as_dict(),
        "any_up": stt.up or tts.up,
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }
