#!/usr/bin/env python3
"""Barqarorlik tekshiruvi: bir xil resumeni N marta baholab, tarqoqlikni o'lchaydi.

LLM bilan baholashda asosiy xavf — bir xil kiruvchi ma'lumotga har xil ball.
Bu skript shuni raqamda ko'rsatadi. Har safar boshqa seed ishlatiladi, ya'ni
"eng yomon holat" o'lchanadi (RA_SEED fiksatsiyasi bilan tarqoqlik kamroq bo'ladi).

    python3 eval/consistency.py --job data/jobs/backend-python-senior.yaml \
        --resume data/resumes/dilshod-karimov.txt --runs 5 --mode screen
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extract import extract_text          # noqa: E402
from app.jobs import load_job                 # noqa: E402
from app.llm import LlamaPool                 # noqa: E402
from app.prompts import SYSTEM_PROMPT, build_user_prompt   # noqa: E402
from app.scorer import ResumeScorer           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Baholash barqarorligini tekshirish")
    ap.add_argument("--job", required=True)
    ap.add_argument("--resume", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--mode", choices=["screen", "full"], default="screen")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    job = load_job(args.job)
    text = extract_text(args.resume)
    pool = LlamaPool()
    pool.require_healthy()
    scorer = ResumeScorer(job, pool, mode=args.mode)

    print(f"Vakansiya : {job.title}")
    print(f"Resume    : {Path(args.resume).name}")
    print(f"Urinishlar: {args.runs} ({args.mode} rejim, har safar boshqa seed)\n")

    def run_once(i: int) -> dict:
        # Seed'ni atayin o'zgartiramiz — tarqoqlikning yuqori chegarasini ko'rish uchun
        res = scorer.pool.chat_json(
            SYSTEM_PROMPT,
            build_user_prompt(job, scorer.criteria, text, scorer.mode),
            scorer.schema,
            seed=1000 + i,
        )
        return scorer._assemble(res, f"run{i}", args.resume, False)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(args.workers, args.runs)) as ex:
        results = list(ex.map(run_once, range(args.runs)))
    elapsed = time.monotonic() - t0

    finals = [r["final_score"] for r in results]
    print(f"{'#':>3}  {'YAKUNIY':>8}  {'XOM':>6}  VERDICT")
    for i, r in enumerate(results, 1):
        print(f"{i:>3}  {r['final_score']:>8.1f}  {r['raw_score']:>6.1f}  {r['verdict']}")

    print(f"\nO'rtacha    : {statistics.mean(finals):.1f}")
    print(f"Min / Max   : {min(finals):.1f} / {max(finals):.1f}  (tarqalish {max(finals)-min(finals):.1f})")
    if len(finals) > 1:
        print(f"St. og'ish  : {statistics.stdev(finals):.2f}")
    verdicts = {r["verdict"] for r in results}
    print(f"Verdictlar  : {', '.join(sorted(verdicts))}"
          f"{'  ← BARQAROR' if len(verdicts) == 1 else '  ← TURLICHA, temperaturani pasaytiring'}")

    print("\nMezonlar bo'yicha tarqalish:")
    for c in scorer.criteria:
        vals = [next(x["score"] for x in r["criteria"] if x["id"] == c.id) for r in results]
        spread = max(vals) - min(vals)
        flag = "  ⚠️" if spread > 20 else ""
        print(f"  {c.label:<30} o'rtacha {statistics.mean(vals):>5.1f}  "
              f"tarqalish {spread:>4.0f}{flag}")

    print(f"\nJami vaqt: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
