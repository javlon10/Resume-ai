"""Intervyu javoblarini baholash quvuri.

Ishlash prinsipi (resurs nuqtai nazaridan muhim):
  Har bir savol ALOHIDA so'rov sifatida PARALLEL yuboriladi. Ya'ni 8 savolli
  intervyu 8 ta so'rovga bo'linadi va 4 GPU o'rtasida taqsimlanadi — natijada
  bitta intervyu ~15-25 sekundda baholanadi, ketma-ket yuborilsa 2 daqiqa
  ketardi. Har so'rov qisqa (~250 token chiqish), shuning uchun kechikish past.

  So'rovlar `priority="interactive"` bilan ketadi: katta resume batch ishlab
  turgan bo'lsa ham, intervyuga zaxira slotlar doim ochiq (app/llm.py Admission).
"""
from __future__ import annotations

import datetime as _dt
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .interviews import InterviewSpec, Question, compute_interview_score
from .llm import LlamaPool, LLMError
from .prompts import (ANSWER_SYSTEM_PROMPT, INTERVIEW_SUMMARY_SYSTEM,
                      build_answer_prompt, build_summary_prompt)
from .schemas import build_answer_schema, build_interview_summary_schema

# Javob matni chegarasi — juda uzun javob kontekstni to'ldirmasligi uchun
MAX_ANSWER_CHARS = 8000
# Bo'sh deb hisoblanadigan javob uzunligi
MIN_ANSWER_CHARS = 2


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _as_list(v: Any, limit: int = 8) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()][:limit]


def is_answered(text: str | None) -> bool:
    return bool(text and len(text.strip()) >= MIN_ANSWER_CHARS)


class InterviewScorer:
    """Bitta sessiyaning javoblarini baholaydi."""

    def __init__(self, spec: InterviewSpec, pool: LlamaPool | None = None,
                 job_title: str = "") -> None:
        self.spec = spec
        self.pool = pool or LlamaPool()
        self.job_title = job_title or spec.title

    # --- bitta javob ----------------------------------------------------------
    def score_answer(self, q: Question, answer: str) -> dict[str, Any]:
        text = answer.strip()[:MAX_ANSWER_CHARS]
        prompt = build_answer_prompt(
            question_text=q.text,
            expected=q.expected,
            answer_text=text,
            topic=q.topic,
            difficulty=q.difficulty,
            job_title=self.job_title,
            reference=q.answer,
        )
        res = self.pool.chat_json(
            ANSWER_SYSTEM_PROMPT,
            prompt,
            # Mezon faqat matn ko'rinishidagi javob bo'lsa, asosiy fikrlarni model
            # o'zi ajratadi — ro'yxatga biroz kengroq joy beramiz.
            build_answer_schema(len(q.expected) or 6),
            schema_name="intervyu_javob",
            max_tokens=900,
            priority="interactive",
        )
        d = res.data
        score = d.get("score")
        score = float(score) if isinstance(score, (int, float)) else 0.0

        relevance = str(d.get("relevance", "on_topic")).lower()
        if relevance not in {"on_topic", "partly", "off_topic", "empty"}:
            relevance = "on_topic"

        return {
            "question_id": q.id,
            "score": max(0.0, min(100.0, score)),
            "covered": _as_list(d.get("covered")),
            "missing": _as_list(d.get("missing")),
            "errors": _as_list(d.get("errors"), 3),
            "comment": str(d.get("comment", "")).strip(),
            "relevance": relevance,
            "manipulation_attempt": bool(d.get("manipulation_attempt", False)),
            "confidence": str(d.get("confidence", "low")).lower(),
            "latency_s": res.latency_s,
            "endpoint": res.endpoint,
            "usage": res.usage,
        }

    # --- butun sessiya --------------------------------------------------------
    def score_session(
        self,
        questions: list[Question],
        answers: dict[str, str],
        with_summary: bool = True,
    ) -> dict[str, Any]:
        t0 = time.monotonic()

        # Javob berilmagan savollar modelga umuman yuborilmaydi — resurs tejaladi
        # va ular baribir 0 ball oladi.
        unanswered = {q.id for q in questions if not is_answered(answers.get(q.id))}
        to_score = [q for q in questions if q.id not in unanswered]

        evals: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []

        if to_score:
            workers = max(1, min(len(to_score), 16))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.score_answer, q, answers.get(q.id, "")): q
                        for q in to_score}
                for fut, q in futs.items():
                    try:
                        evals[q.id] = fut.result()
                    except Exception as e:      # bitta savol yiqilsa qolgani davom etadi
                        errors.append({"question_id": q.id,
                                       "error": f"{type(e).__name__}: {e}"})
                        evals[q.id] = {
                            "question_id": q.id, "score": 0.0, "covered": [],
                            "missing": [], "errors": [f"baholab bo'lmadi: {e}"],
                            "comment": "Texnik sabab bilan baholanmadi.",
                            "relevance": "empty", "manipulation_attempt": False,
                            "confidence": "low",
                        }

        # --- yakuniy ball: Python hisoblaydi ---
        sc = compute_interview_score(
            questions,
            {qid: e["score"] for qid, e in evals.items()},
            pass_score=self.spec.pass_score,
            unanswered=unanswered,
        )

        # Savol matni va baho izohini bitta qatorga birlashtiramiz
        by_id = {q.id: q for q in questions}
        rows: list[dict[str, Any]] = []
        for row in sc.per_question:
            q = by_id[row["id"]]
            ev = evals.get(q.id, {})
            rows.append({
                **row,
                "question": q.text,
                # `answer` — NOMZOD javobi (API hujjatida shunday),
                # `reference_answer` — HR yozgan to'g'ri javob. Nomlar
                # chalkashmasligi uchun atayin ikki xil.
                "expected": q.expected,
                "reference_answer": q.answer,
                "answer": answers.get(q.id, ""),
                "covered": ev.get("covered", []),
                "missing": ev.get("missing", []),
                "errors": ev.get("errors", []),
                "comment": ev.get("comment", "" if row["answered"] else "Javob berilmagan."),
                "relevance": ev.get("relevance", "empty"),
                "manipulation_attempt": ev.get("manipulation_attempt", False),
                "confidence": ev.get("confidence", "low"),
            })

        flagged = [r["id"] for r in rows if r.get("manipulation_attempt")]

        summary: dict[str, Any] = {}
        if with_summary and to_score:
            try:
                summary = self._summarize(sc.final_score, rows)
            except LLMError as e:
                errors.append({"question_id": "_summary", "error": str(e)})

        usage_in = sum((e.get("usage") or {}).get("prompt_tokens", 0) for e in evals.values())
        usage_out = sum((e.get("usage") or {}).get("completion_tokens", 0) for e in evals.values())

        return {
            "final_score": sc.final_score,
            "verdict": sc.verdict,
            "verdict_label": sc.verdict_label,
            "passed": sc.passed,
            "pass_score": sc.pass_score,
            "answered": sc.answered,
            "total": sc.total,
            "unanswered": sc.unanswered,
            "questions": rows,
            "strengths": summary.get("strengths", []),
            "weaknesses": summary.get("weaknesses", []),
            "topics_to_probe": summary.get("topics_to_probe", []),
            "overall_comment": summary.get("overall_comment", ""),
            "recommendation": summary.get("recommendation", ""),
            "manipulation_flagged": flagged,
            "errors": errors,
            "meta": {
                "evaluated_at": _now(),
                "elapsed_s": round(time.monotonic() - t0, 1),
                "model_calls": len(to_score) + (1 if summary else 0),
                "prompt_tokens": usage_in,
                "completion_tokens": usage_out,
            },
        }

    def _summarize(self, final_score: float, rows: list[dict]) -> dict[str, Any]:
        res = self.pool.chat_json(
            INTERVIEW_SUMMARY_SYSTEM,
            build_summary_prompt(self.job_title, final_score, self.spec.pass_score, rows),
            build_interview_summary_schema(),
            schema_name="intervyu_xulosa",
            max_tokens=900,
            priority="interactive",
        )
        d = res.data
        rec = str(d.get("recommendation", "")).lower()
        return {
            "strengths": _as_list(d.get("strengths"), 4),
            "weaknesses": _as_list(d.get("weaknesses"), 4),
            "topics_to_probe": _as_list(d.get("topics_to_probe"), 4),
            "overall_comment": str(d.get("overall_comment", "")).strip(),
            "recommendation": rec if rec in {"hire", "next_round", "hold", "reject"} else "hold",
        }
