"""Asosiy quvur: resume + vakansiya → ball, verdict, izoh."""
from __future__ import annotations

import datetime as _dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import config
from .extract import extract_text, truncate
from .jobs import JobSpec
from .llm import LlamaPool, LLMError
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .rubric import Criterion, compute_score, resolve_criteria
from .schemas import build_schema, must_have_keys


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _as_list(v: Any, limit: int = 10) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out = [str(x).strip() for x in v if str(x).strip()]
    return out[:limit]


class ResumeScorer:
    """Bitta vakansiya uchun resumelarni baholaydi."""

    def __init__(
        self,
        job: JobSpec,
        pool: LlamaPool | None = None,
        mode: str = "full",
    ) -> None:
        self.job = job
        self.mode = mode
        self.criteria: list[Criterion] = resolve_criteria(job.weights, job.criteria_guidance)
        self.pool = pool or LlamaPool()
        self.schema = build_schema(self.criteria, len(job.must_have), mode)

    # --- bitta resume ----------------------------------------------------------
    def score_text(
        self,
        resume_text: str,
        resume_id: str = "resume",
        source: str | None = None,
        priority: str = "interactive",
    ) -> dict[str, Any]:
        text, truncated = truncate(resume_text, config.MAX_RESUME_CHARS)
        user = build_user_prompt(self.job, self.criteria, text, self.mode)

        res = self.pool.chat_json(
            SYSTEM_PROMPT, user, self.schema,
            schema_name=f"resume_baho_{self.mode}", priority=priority,
        )
        return self._assemble(res, resume_id, source, truncated)

    def score_file(self, path: str | Path, priority: str = "interactive") -> dict[str, Any]:
        p = Path(path)
        text = extract_text(p)
        return self.score_text(text, resume_id=p.stem, source=str(p), priority=priority)

    # --- model chiqishini natijaga aylantirish ---------------------------------
    def _assemble(self, res, resume_id: str, source: str | None, truncated: bool) -> dict[str, Any]:
        d = res.data
        model_crit: dict[str, Any] = d.get("criteria") or {}

        # 1) Har mezon bo'yicha modeldan kelgan ballarni ajratamiz
        model_scores: dict[str, float] = {}
        for c in self.criteria:
            item = model_crit.get(c.id) or {}
            sc = item.get("score")
            if isinstance(sc, (int, float)):
                model_scores[c.id] = float(sc)

        # 2) Majburiy talablar holati (og'irlikli ballga cap qo'yadi)
        mh_rows: list[dict[str, Any]] = []
        mh_raw = d.get("must_have_checks") or {}
        for key, requirement in zip(must_have_keys(len(self.job.must_have)), self.job.must_have):
            item = mh_raw.get(key) or {}
            status = str(item.get("status", "unknown")).strip().lower()
            if status not in {"met", "partial", "not_met", "unknown"}:
                status = "unknown"
            mh_rows.append(
                {
                    "key": key,
                    "requirement": requirement,     # manba — vakansiya fayli, modelning nusxasi emas
                    "status": status,
                    "evidence": str(item.get("evidence", "")).strip(),
                }
            )

        # 3) Yakuniy ball — Python hisoblaydi
        bd = compute_score(self.criteria, model_scores, [r["status"] for r in mh_rows])

        # 4) Mezon satrlarini model izohlari bilan boyitamiz
        by_id = {row["id"]: row for row in bd.per_criterion}
        criteria_out: list[dict[str, Any]] = []
        for c in self.criteria:
            row = dict(by_id[c.id])
            item = model_crit.get(c.id) or {}
            row.update(
                {
                    "comment": str(item.get("comment", "")).strip(),
                    "evidence": _as_list(item.get("evidence"), 3),
                    "matched": _as_list(item.get("matched"), 8),
                    "missing": _as_list(item.get("missing"), 8),
                    "confidence": str(item.get("confidence", "low")).lower(),
                    "score_from_model": c.id in model_scores,
                }
            )
            criteria_out.append(row)

        cand = d.get("candidate") or {}
        return {
            "resume_id": resume_id,
            "resume_file": source,
            "job_id": self.job.id,
            "job_title": self.job.title,
            "final_score": bd.final_score,
            "raw_score": bd.raw_score,
            "verdict": bd.verdict,
            "verdict_label": bd.verdict_label,
            "penalty_factor": bd.penalty_factor,
            "penalty_reason": bd.penalty_reason,
            "candidate": {
                "name": str(cand.get("name", "")).strip(),
                "current_role": str(cand.get("current_role", "")).strip(),
                "total_experience_years": cand.get("total_experience_years"),
                "relevant_experience_years": cand.get("relevant_experience_years"),
                "location": str(cand.get("location", "")).strip(),
            },
            "resume_summary": str(d.get("resume_summary", "")).strip(),
            "must_have": mh_rows,
            "criteria": criteria_out,
            "strengths": _as_list(d.get("strengths"), 5),
            "gaps": _as_list(d.get("gaps"), 5),
            "red_flags": _as_list(d.get("red_flags"), 4),
            "interview_questions": _as_list(d.get("interview_questions"), 5),
            "overall_comment": str(d.get("overall_comment", "")).strip(),
            "meta": {
                "created_at": _now(),
                "model": config.MODEL_ALIAS,
                "mode": self.mode,
                "endpoint": res.endpoint,
                "latency_s": res.latency_s,
                "attempts": res.attempts,
                "usage": res.usage,
                "resume_truncated": truncated,
                "reasoning": res.reasoning,        # thinking o'chiq bo'lsa None
                "criteria_missing_from_model": bd.missing_criteria,
                "temperature": config.TEMPERATURE,
                "seed": config.SEED,
            },
        }

    # --- ko'p resume (parallel) -------------------------------------------------
    def score_many(
        self,
        paths: list[Path],
        max_workers: int | None = None,
        on_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Resumelarni parallel baholaydi. (natijalar, xatolar) qaytaradi."""
        workers = max_workers or max(1, min(config.CONCURRENCY, len(paths)))
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.score_file, p, "batch"): p for p in paths}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    r = fut.result()
                    results.append(r)
                    if on_done:
                        on_done(r)
                except Exception as e:                  # bitta resume yiqilsa, qolgani davom etadi
                    errors.append({"resume_file": str(p), "error": f"{type(e).__name__}: {e}"})
                    if on_done:
                        on_done({"resume_id": p.stem, "resume_file": str(p), "error": str(e)})

        results.sort(key=lambda r: r["final_score"], reverse=True)
        return results, errors
