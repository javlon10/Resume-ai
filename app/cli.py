"""Buyruq satri interfeysi.

Misollar:
  python3 -m app.cli jobs
  python3 -m app.cli score --job data/jobs/backend-python.yaml --resume data/resumes/ali.txt
  python3 -m app.cli batch --job data/jobs/backend-python.yaml --resumes data/resumes --md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config
from .extract import find_resumes
from .interview_scorer import InterviewScorer
from .interviews import list_interviews, load_interview, select_questions
from .jobs import list_jobs, load_job
from .llm import LlamaPool
from .report import (console_detail, console_ranking, markdown_detail,
                     markdown_report, write_csv, write_jsonl)
from .scorer import ResumeScorer


def _pool() -> LlamaPool:
    pool = LlamaPool()
    pool.require_healthy()
    return pool


def cmd_jobs(args: argparse.Namespace) -> int:
    paths = list_jobs(args.dir or config.JOBS_DIR)
    if not paths:
        print(f"Vakansiya fayli topilmadi: {args.dir or config.JOBS_DIR}")
        return 1
    for p in paths:
        try:
            j = load_job(p)
            print(f"{p.name:<34} {j.title}  [{j.level or '—'}]  majburiy: {len(j.must_have)}")
        except Exception as e:
            print(f"{p.name:<34} XATO: {e}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    pool = LlamaPool()
    ok = pool.healthy()
    for ep in pool.endpoints:
        state = "ishlayapti" if ep in ok else "javob yo'q"
        print(f"{ep:<28} {state}")
    print(f"\nFaol: {len(ok)}/{len(pool.endpoints)}")
    return 0 if ok else 1


def cmd_score(args: argparse.Namespace) -> int:
    job = load_job(args.job)
    scorer = ResumeScorer(job, _pool(), mode=args.mode)

    t0 = time.monotonic()
    if args.text:
        r = scorer.score_text(Path(args.text).read_text(encoding="utf-8"), resume_id=Path(args.text).stem,
                              source=args.text)
    else:
        r = scorer.score_file(args.resume)
    elapsed = time.monotonic() - t0

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.md:
        print(markdown_detail(r))
    else:
        print(console_detail(r))
        print(f"\nBajarildi: {elapsed:.1f}s")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON saqlandi: {out}", file=sys.stderr)
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    job = load_job(args.job)
    paths = find_resumes(args.resumes)
    if not paths:
        print(f"Resume topilmadi: {args.resumes}")
        return 1

    scorer = ResumeScorer(job, _pool(), mode=args.mode)
    workers = args.workers or config.CONCURRENCY
    print(f"Vakansiya: {job.title}")
    print(f"Rejim: {args.mode} | resumelar: {len(paths)} ta | parallel: {workers} | endpointlar: {len(scorer.pool.endpoints)}")
    print("-" * 60)

    done = {"n": 0}
    t0 = time.monotonic()

    def progress(r: dict) -> None:
        done["n"] += 1
        tag = f"XATO: {r['error'][:60]}" if "error" in r else f"{r['final_score']:>5.1f}  {r['verdict']}"
        print(f"[{done['n']:>3}/{len(paths)}] {r['resume_id'][:34]:<34} {tag}", flush=True)

    results, errors = scorer.score_many(paths, max_workers=workers, on_done=progress)
    elapsed = time.monotonic() - t0

    print(console_ranking(results, errors))
    print(f"\nJami: {elapsed:.1f}s ({elapsed / max(1, len(paths)):.1f}s/resume)")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir or config.OUT_DIR)
    base = f"{job.id}-{args.mode}-{stamp}"
    jsonl = write_jsonl(out_dir / f"{base}.jsonl", results)
    csvp = write_csv(out_dir / f"{base}.csv", results)
    print(f"\nSaqlandi:\n  {jsonl}\n  {csvp}")
    if args.md:
        md = out_dir / f"{base}.md"
        md.write_text(markdown_report(job.title, results, errors), encoding="utf-8")
        print(f"  {md}")
    return 0 if results else 2


def cmd_interviews(args: argparse.Namespace) -> int:
    paths = list_interviews(args.dir or config.INTERVIEWS_DIR)
    if not paths:
        print(f"Savol banki topilmadi: {args.dir or config.INTERVIEWS_DIR}")
        return 1
    for p in paths:
        try:
            spec = load_interview(p)
            print(f"{p.stem:<24} {spec.title}")
            print(f"{'':24} {len(spec.questions)} savol | sessiyada {spec.default_count or len(spec.questions)}"
                  f" | o'tish {spec.pass_score} | mavzular: {', '.join(spec.topics)}")
            n_ans = sum(1 for q in spec.questions if q.has_answer)
            n_exp = sum(1 for q in spec.questions if q.expected)
            print(f"{'':24} mezon: {n_ans} ta savolda to'g'ri javob, "
                  f"{n_exp} ta savolda kutilgan nuqtalar")
            if args.questions:
                for q in spec.questions:
                    mark = ("javob+nuqta" if q.has_answer and q.expected
                            else "javob" if q.has_answer else "nuqtalar")
                    print(f"{'':26} [{q.id:<7} {q.topic:<22} {q.difficulty:<6} "
                          f"w={q.weight} {mark:<11}] {q.text.strip()[:60]}")
        except Exception as e:
            print(f"{p.stem:<24} XATO: {e}")
    return 0


def cmd_interview(args: argparse.Namespace) -> int:
    """Javoblar faylidan intervyuni baholaydi.

    Javoblar fayli (YAML yoki JSON):
        interview: fizika-oqituvchi
        candidate: Gulnora Rashidova
        answers:
          mex-1: "javob matni"
          elek-1: "javob matni"
    """
    import yaml as _yaml

    raw = Path(args.answers).read_text(encoding="utf-8")
    data = (json.loads(raw) if Path(args.answers).suffix.lower() == ".json"
            else _yaml.safe_load(raw))
    if not isinstance(data, dict) or "answers" not in data:
        print("Javoblar faylida `answers` bo'limi bo'lishi kerak", file=sys.stderr)
        return 1

    interview_id = args.interview or data.get("interview")
    if not interview_id:
        print("`--interview` yoki fayldagi `interview` maydoni kerak", file=sys.stderr)
        return 1

    path = Path(interview_id)
    if not path.exists():
        path = Path(config.INTERVIEWS_DIR) / f"{interview_id}.yaml"
    spec = load_interview(path)

    answers = {str(k): str(v) for k, v in (data.get("answers") or {}).items()}
    by_id = {q.id: q for q in spec.questions}
    unknown = set(answers) - set(by_id)
    if unknown:
        print(f"Noma'lum savol id: {sorted(unknown)}", file=sys.stderr)
        return 1

    # Javob berilgan savollar + (agar --all bo'lsa) qolganlar ham
    questions = ([by_id[q] for q in answers] if not args.all
                 else select_questions(spec, args.count, args.seed))

    scorer = InterviewScorer(spec, _pool(), job_title=data.get("job_title", ""))
    t0 = time.monotonic()
    r = scorer.score_session(questions, answers)
    elapsed = time.monotonic() - t0

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    name = data.get("candidate", "Nomzod")
    print("=" * 72)
    print(f"{name}  →  {r['final_score']} / 100   ({r['verdict_label']})")
    print(f"{'O' + chr(39) + 'TDI' if r['passed'] else 'O' + chr(39) + 'TMADI'}"
          f"  (chegara {r['pass_score']}) | javob berilgan: {r['answered']}/{r['total']}"
          f" | tavsiya: {r['recommendation']}")
    print("=" * 72)
    for q in r["questions"]:
        tag = "" if q["answered"] else "   [javobsiz]"
        print(f"\n[{q['topic']} · {q['difficulty']} · ulush {q['weight_pct']}%] "
              f"ball {q['score']:.0f}{tag}")
        print(f"  {q['question'].strip()[:90]}")
        if q.get("comment"):
            print(f"  → {q['comment']}")
        if q.get("missing"):
            print(f"  ✘ yetishmadi: {', '.join(q['missing'][:3])}")
        if q.get("errors"):
            print(f"  ! xato: {q['errors'][0]}")
        if q.get("manipulation_attempt"):
            print("  !! javob ichida tizimni aldashga urinish aniqlandi")
    for title, key in (("KUCHLI TOMONLAR", "strengths"), ("BO'SHLIQLAR", "weaknesses"),
                       ("TEKSHIRISH KERAK", "topics_to_probe")):
        if r.get(key):
            print(f"\n--- {title} ---")
            print("\n".join(f"  - {x}" for x in r[key]))
    if r.get("overall_comment"):
        print(f"\n--- XULOSA ---\n{r['overall_comment']}")
    print(f"\n[{elapsed:.1f}s · {r['meta']['model_calls']} model chaqiruvi]")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON saqlandi: {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="resume_analize",
        description="Resumelarni vakansiya talablariga ko'ra baholash (Qwen3.5-9B / llama.cpp)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("jobs", help="Vakansiya fayllari ro'yxati")
    p.add_argument("--dir", help="Vakansiyalar katalogi")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("health", help="llama-server holatini tekshirish")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("score", help="Bitta resumeni baholash")
    p.add_argument("--job", required=True, help="Vakansiya YAML/JSON fayli")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--resume", help="Resume fayli (pdf/docx/txt/md)")
    g.add_argument("--text", help="Tayyor matn fayli (.txt)")
    p.add_argument("--json", action="store_true", help="JSON chiqish")
    p.add_argument("--md", action="store_true", help="Markdown chiqish")
    p.add_argument("--out", help="Natijani JSON faylga saqlash")
    p.add_argument("--mode", choices=["screen", "full"], default="full",
                   help="screen = tez saralash (ball + qisqa izoh), full = to'liq tahlil (dalil, bo'shliq, savollar)")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("batch", help="Katalogdagi barcha resumelarni baholash va reyting")
    p.add_argument("--job", required=True)
    p.add_argument("--resumes", required=True, help="Fayl yoki katalog")
    p.add_argument("--workers", type=int, help=f"Parallel so'rovlar (default {config.CONCURRENCY})")
    p.add_argument("--out-dir", help="Natija katalogi")
    p.add_argument("--md", action="store_true", help="Markdown hisobot ham yozish")
    p.add_argument("--mode", choices=["screen", "full"], default="full",
                   help="screen = tez saralash (ball + qisqa izoh), full = to'liq tahlil (dalil, bo'shliq, savollar)")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("interviews", help="Intervyu savol banklari ro'yxati")
    p.add_argument("--dir", help="Savol banklari katalogi")
    p.add_argument("--questions", action="store_true", help="Savollarni ham ko'rsatish")
    p.set_defaults(func=cmd_interviews)

    p = sub.add_parser("interview", help="Javoblar faylidan intervyuni baholash")
    p.add_argument("--answers", required=True, help="Javoblar fayli (YAML/JSON)")
    p.add_argument("--interview", help="Savol banki id yoki fayl yo'li")
    p.add_argument("--all", action="store_true",
                   help="Javob berilmagan savollarni ham qo'shish (ular 0 ball oladi)")
    p.add_argument("--count", type=int, help="--all bilan: nechta savol tanlansin")
    p.add_argument("--seed", type=int, help="--all bilan: savol tanlash urug'i")
    p.add_argument("--json", action="store_true", help="JSON chiqish")
    p.add_argument("--out", help="Natijani JSON faylga saqlash")
    p.set_defaults(func=cmd_interview)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nTo'xtatildi.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"XATO: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
