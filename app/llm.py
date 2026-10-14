"""llama-server klienti: round-robin, retry, majburiy JSON chiqish."""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import requests

from . import config

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_local = threading.local()


class LLMError(RuntimeError):
    pass


def _session() -> requests.Session:
    """Har thread uchun alohida Session (connection pooling, thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _local.session = s
    return s


@dataclass
class LLMResult:
    data: dict[str, Any]          # parse qilingan JSON
    raw: str
    endpoint: str
    usage: dict[str, Any]
    latency_s: float
    attempts: int
    # Qwen "thinking" yoqilgan bo'lsa (RA_ENABLE_THINKING=1), llama.cpp fikrlash
    # qismini alohida qaytaradi — grammar faqat `content`ga qo'llanadi.
    reasoning: str | None = None


class Admission:
    """Batch ishlarini cheklab, interaktiv so'rovlarga slot qoldiradi.

    Muammo: 50 ta resumeli batch barcha slotlarni egallab olsa, shu payt kelgan
    intervyu baholash so'rovi navbatda daqiqalab kutadi. Nomzod ekran oldida
    kutib turadi — bu qabul qilib bo'lmaydi.

    Yechim: batch so'rovlari soni `batch_limit` bilan cheklanadi, interaktiv
    so'rovlar esa cheklanmaydi. Shunda kamida (jami slot - batch_limit) ta slot
    doim interaktiv ish uchun bo'sh qoladi.
    """

    def __init__(self, batch_limit: int, hold_s: float | None = None) -> None:
        self.batch_limit = max(1, batch_limit)
        self.hold_s = config.RESERVE_HOLD_S if hold_s is None else hold_s
        self._sem = threading.BoundedSemaphore(self.batch_limit)
        self._inflight = {"batch": 0, "interactive": 0}
        self._last_interactive = 0.0
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, priority: str = "interactive"):
        is_batch = priority == "batch"
        key = "batch" if is_batch else "interactive"
        if is_batch:
            self._sem.acquire()
        with self._lock:
            self._inflight[key] += 1
            if not is_batch:
                self._last_interactive = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._inflight[key] -= 1
                if not is_batch:
                    self._last_interactive = time.monotonic()
            if is_batch:
                self._sem.release()

    def interactive_active(self) -> bool:
        """Hozir interaktiv ish bormi yoki yaqinda bo'lganmi.

        Vaqt oynasi kerak: intervyuda savollar ketma-ket emas, to'lqin-to'lqin
        keladi (savollar → xulosa). Oynasiz, ikki to'lqin orasida batch darhol
        zaxira GPU'ni egallab olardi.
        """
        with self._lock:
            if self._inflight["interactive"] > 0:
                return True
            return (time.monotonic() - self._last_interactive) < self.hold_s

    def stats(self) -> dict[str, Any]:
        with self._lock:
            d = dict(self._inflight)
            idle = round(time.monotonic() - self._last_interactive, 1) \
                if self._last_interactive else None
        d["batch_limit"] = self.batch_limit
        d["interactive_idle_s"] = idle
        d["reserve_active"] = self.interactive_active()
        return d


class LlamaPool:
    """Bir nechta llama-server instance'i ustida yuklamani taqsimlaydi."""

    def __init__(self, endpoints: list[str] | None = None,
                 batch_limit: int | None = None) -> None:
        eps = [e.rstrip("/") for e in (endpoints or config.ENDPOINTS)]
        if not eps:
            raise ValueError("Kamida bitta endpoint kerak")
        self.endpoints = eps
        self._cycle = itertools.cycle(eps)
        self._lock = threading.Lock()
        self._supports_thinking_kwarg = True   # birinchi xatodan keyin False bo'ladi
        # Har endpointda nechta so'rov ishlayotgani — eng bo'shiga yuborish uchun
        self._busy: dict[str, int] = {ep: 0 for ep in eps}
        self.admission = Admission(
            batch_limit if batch_limit is not None else config.BATCH_CONCURRENCY
        )

    def _allowed(self, priority: str) -> list[str]:
        """Shu ustuvorlik uchun ruxsat etilgan serverlar.

        Interaktiv ish — barcha serverlar (bo'sh paytda intervyu 4 GPU'ga
        yoyilib tez baholanadi).
        Batch — interaktiv ish bor paytda oxirgi RESERVED_GPUS ta server
        chetlatiladi, shunda intervyu deyarli bo'sh GPU'da ishlaydi.
        """
        if priority != "batch":
            return self.endpoints
        k = config.RESERVED_GPUS
        if k <= 0 or len(self.endpoints) <= k or not self.admission.interactive_active():
            return self.endpoints
        return self.endpoints[:-k]

    def _next(self, priority: str = "interactive") -> str:
        """Ruxsat etilganlar ichidan eng kam band serverni tanlaydi.

        Oddiy round-robin yetmaydi: resume tahlili ~100 sekund, intervyu javobi
        ~15 sekund ishlaydi. Faqat navbat bo'yicha tarqatsak bitta server uzun
        so'rovlar bilan to'lib qolardi.
        """
        allowed = self._allowed(priority)
        with self._lock:
            best, best_busy = allowed[0], self._busy.get(allowed[0], 0)
            for ep in allowed[1:]:
                b = self._busy.get(ep, 0)
                if b < best_busy:
                    best, best_busy = ep, b
            return best

    @contextmanager
    def _track(self, ep: str):
        with self._lock:
            self._busy[ep] = self._busy.get(ep, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._busy[ep] = max(0, self._busy.get(ep, 1) - 1)

    def load_stats(self) -> dict[str, Any]:
        with self._lock:
            busy = dict(self._busy)
        return {
            "in_flight_per_endpoint": busy,
            "batch_endpoints": self._allowed("batch"),
            **self.admission.stats(),
        }

    # --- holat tekshirish ------------------------------------------------------
    def healthy(self, timeout: float = 3.0) -> list[str]:
        ok = []
        for ep in self.endpoints:
            try:
                r = _session().get(f"{ep}/health", timeout=timeout)
                if r.status_code == 200:
                    ok.append(ep)
            except requests.RequestException:
                pass
        return ok

    def require_healthy(self) -> list[str]:
        ok = self.healthy()
        if not ok:
            raise LLMError(
                "Hech bir llama-server javob bermayapti. Ishga tushiring: "
                "./scripts/start_servers.sh  (tekshirish: ./scripts/health.sh)"
            )
        if len(ok) < len(self.endpoints):
            missing = [e for e in self.endpoints if e not in ok]
            print(f"[ogohlantirish] ishlamayotgan endpointlar: {missing}")
        with self._lock:
            self.endpoints = ok
            self._cycle = itertools.cycle(ok)
        return ok

    # --- asosiy chaqiruv -------------------------------------------------------
    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict,
        schema_name: str = "resume_baho",
        max_tokens: int | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        priority: str = "interactive",
    ) -> LLMResult:
        """Chat completion + JSON Schema grammar. Natija — parse qilingan dict.

        priority="batch"       — cheklangan slotlar (katta to'plamlarni qayta ishlash)
        priority="interactive" — cheklovsiz (intervyu, bitta resume; odam kutib turadi)
        """
        payload: dict[str, Any] = {
            "model": config.MODEL_ALIAS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": config.TEMPERATURE if temperature is None else temperature,
            "top_p": config.TOP_P,
            "top_k": config.TOP_K,
            "min_p": config.MIN_P,
            "repeat_penalty": config.REPEAT_PENALTY,
            "seed": config.SEED if seed is None else seed,
            "max_tokens": max_tokens or config.MAX_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        if self._supports_thinking_kwarg:
            payload["chat_template_kwargs"] = {"enable_thinking": config.ENABLE_THINKING}

        last_err: Exception | None = None
        attempt = 0
        length_bumps = 0          # max_tokens kesilishi normal retry hisobiga kirmaydi
        max_attempts = config.MAX_RETRIES
        while attempt < max_attempts:
            attempt += 1
            ep = self._next(priority)
            t0 = time.monotonic()
            try:
                with self.admission.slot(priority), self._track(ep):
                    r = _session().post(
                        f"{ep}/v1/chat/completions",
                        json=payload,
                        timeout=config.REQUEST_TIMEOUT,
                    )
                if r.status_code >= 400:
                    body = r.text[:400]
                    # Ba'zi modellar chat_template_kwargs'ni qabul qilmaydi — qayta urinamiz
                    if "chat_template_kwargs" in payload and "template" in body.lower():
                        self._supports_thinking_kwarg = False
                        payload.pop("chat_template_kwargs", None)
                        last_err = LLMError(f"{ep}: template kwargs qo'llanmadi, qaytadan")
                        continue
                    raise LLMError(f"{ep}: HTTP {r.status_code}: {body}")

                body = r.json()
                if "error" in body:
                    raise LLMError(f"{ep}: {json.dumps(body['error'])[:400]}")

                choice = body["choices"][0]
                msg = choice["message"]
                content = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or None
                finish = choice.get("finish_reason")
                if finish == "length":
                    cur = int(payload["max_tokens"])
                    if cur < config.MAX_TOKENS_CAP and length_bumps < 2:
                        # Bir xil limit bilan qayta urinish foydasiz — limitni oshiramiz
                        payload["max_tokens"] = min(config.MAX_TOKENS_CAP, int(cur * 1.6))
                        length_bumps += 1
                        max_attempts += 1
                        last_err = LLMError(f"{ep}: javob {cur} tokenda kesildi")
                        continue
                    raise LLMError(
                        f"{ep}: javob max_tokens ({cur}) ga yetib kesildi. "
                        "RA_MAX_TOKENS_CAP ni oshiring yoki resumeni qisqartiring "
                        "(RA_MAX_RESUME_CHARS)"
                    )

                data = _parse_json(content)
                return LLMResult(
                    data=data,
                    raw=content,
                    endpoint=ep,
                    usage=body.get("usage", {}),
                    latency_s=round(time.monotonic() - t0, 2),
                    attempts=attempt,
                    reasoning=reasoning,
                )
            except (requests.RequestException, LLMError, KeyError, ValueError) as e:
                last_err = e
                if attempt < max_attempts:
                    # Keyingi urinishda seedni o'zgartiramiz — xuddi shu xato qaytmasligi uchun
                    payload["seed"] = int(payload["seed"]) + 1000 * attempt
                    time.sleep(min(2.0 * attempt, 6.0))

        raise LLMError(f"{attempt} urinishdan keyin ham muvaffaqiyatsiz: {last_err}")


def _parse_json(content: str) -> dict[str, Any]:
    """Model chiqishini JSON'ga aylantiradi (<think> bloki bo'lsa tashlaydi)."""
    text = _THINK_RE.sub("", content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"Javobdan JSON topilmadi: {text[:200]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"JSON obyekt kutilgan edi, keldi: {type(data).__name__}")
    return data
