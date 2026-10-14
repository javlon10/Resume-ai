"""Natijalarni odam o'qiydigan ko'rinishga aylantirish: konsol, Markdown, CSV, JSONL."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

VERDICT_ICON = {
    "strong_fit": "🟢",
    "fit": "🔵",
    "borderline": "🟡",
    "no_fit": "🔴",
}
MH_ICON = {"met": "✅", "partial": "🟡", "not_met": "❌", "unknown": "❔"}
MH_UZ = {
    "met": "bajarilgan",
    "partial": "qismiy",
    "not_met": "bajarilmagan",
    "unknown": "ma'lumot yo'q",
}
CONF_UZ = {"high": "yuqori", "medium": "o'rta", "low": "past"}


def _md(value: object, empty: str = "—") -> str:
    """Markdown jadvali katakchasi uchun xavfsiz matn ("|" qochiriladi)."""
    if value is None or value == "":
        return empty
    return str(value).replace("|", "\\|").replace("\n", " ")


def _bar(score: float, width: int = 20) -> str:
    filled = int(round(score / 100 * width))
    return "█" * filled + "░" * (width - filled)


# --- konsol -------------------------------------------------------------------
def console_detail(r: dict[str, Any]) -> str:
    L: list[str] = []
    icon = VERDICT_ICON.get(r["verdict"], "•")
    cand = r.get("candidate", {})
    name = cand.get("name") or r["resume_id"]

    L.append("=" * 72)
    L.append(f"{icon}  {name}   →   {r['final_score']} / 100   ({r['verdict_label']})")
    L.append(f"Vakansiya: {r['job_title']}")
    if cand.get("current_role"):
        L.append(f"Hozirgi lavozim: {cand['current_role']}")
    exp = cand.get("total_experience_years")
    rel = cand.get("relevant_experience_years")
    if exp is not None:
        L.append(f"Tajriba: umumiy {exp} yil, mos yo'nalishda {rel} yil")
    if r.get("penalty_reason"):
        L.append(f"⚠️  {r['penalty_reason']}")
        L.append(f"    xom ball {r['raw_score']} → yakuniy {r['final_score']}")
    L.append("=" * 72)

    if r.get("resume_summary"):
        L.append(f"\n{r['resume_summary']}")

    if r.get("must_have"):
        L.append("\n--- MAJBURIY TALABLAR ---")
        for m in r["must_have"]:
            L.append(f"{MH_ICON.get(m['status'], '•')} {m['requirement']}  [{MH_UZ.get(m['status'], m['status'])}]")
            if m.get("evidence"):
                L.append(f"     dalil: {m['evidence']}")

    L.append("\n--- MEZONLAR BO'YICHA ---")
    for c in r["criteria"]:
        L.append(
            f"\n{c['label']}  ({c['weight_pct']}%)  {_bar(c['score'])} {c['score']:.0f}/100"
            f"   [ishonch: {CONF_UZ.get(c.get('confidence',''), c.get('confidence',''))}]"
        )
        if c.get("comment"):
            L.append(f"   {c['comment']}")
        if c.get("matched"):
            L.append(f"   ✔ mos: {', '.join(c['matched'])}")
        if c.get("missing"):
            L.append(f"   ✘ yetishmaydi: {', '.join(c['missing'])}")
        for e in c.get("evidence", []):
            L.append(f"   › \"{e}\"")

    for title, key, icon in (
        ("KUCHLI TOMONLAR", "strengths", "+"),
        ("BO'SHLIQLAR", "gaps", "-"),
        ("XAVF SIGNALLARI", "red_flags", "!"),
    ):
        if r.get(key):
            L.append(f"\n--- {title} ---")
            L.extend(f" {icon} {x}" for x in r[key])

    if r.get("interview_questions"):
        L.append("\n--- INTERVYU SAVOLLARI ---")
        L.extend(f" {i}. {q}" for i, q in enumerate(r["interview_questions"], 1))

    if r.get("overall_comment"):
        L.append(f"\n--- XULOSA ---\n{r['overall_comment']}")

    m = r.get("meta", {})
    u = m.get("usage", {})
    L.append(
        f"\n[{m.get('latency_s')}s · {u.get('prompt_tokens','?')}→{u.get('completion_tokens','?')} token"
        f" · {m.get('endpoint','')}]"
    )
    return "\n".join(L)


def console_ranking(results: list[dict[str, Any]], errors: list[dict[str, str]] | None = None) -> str:
    if not results:
        return "Natija yo'q."
    L = ["", f"{'#':>3}  {'BALL':>5}  {'VERDICT':<12} {'NOMZOD':<26} {'MAJB.':<7} FAYL", "-" * 96]
    for i, r in enumerate(results, 1):
        cand = r.get("candidate", {})
        name = (cand.get("name") or r["resume_id"])[:26]
        mh = r.get("must_have") or []
        met = sum(1 for m in mh if m["status"] == "met")
        mh_txt = f"{met}/{len(mh)}" if mh else "—"
        icon = VERDICT_ICON.get(r["verdict"], "•")
        fname = Path(r.get("resume_file") or r["resume_id"]).name
        L.append(f"{i:>3}  {r['final_score']:>5.1f}  {icon} {r['verdict']:<10} {name:<26} {mh_txt:<7} {fname}")
    if errors:
        L.append("")
        L.append(f"XATOLAR ({len(errors)}):")
        L.extend(f"  ✗ {Path(e['resume_file']).name}: {e['error']}" for e in errors)
    return "\n".join(L)


# --- Markdown -----------------------------------------------------------------
def markdown_detail(r: dict[str, Any]) -> str:
    cand = r.get("candidate", {})
    name = cand.get("name") or r["resume_id"]
    icon = VERDICT_ICON.get(r["verdict"], "")
    L = [
        f"## {icon} {name} — {r['final_score']}/100 ({r['verdict_label']})",
        "",
        f"- **Vakansiya:** {r['job_title']}",
        f"- **Hozirgi lavozim:** {cand.get('current_role') or '—'}",
        f"- **Tajriba:** umumiy {cand.get('total_experience_years')} yil / "
        f"mos yo'nalishda {cand.get('relevant_experience_years')} yil",
        f"- **Joylashuv:** {cand.get('location') or '—'}",
        f"- **Xom ball (og'irlikli):** {r['raw_score']}",
    ]
    if r.get("penalty_reason"):
        L.append(f"- **⚠️ Jazo:** {r['penalty_reason']} (xom {r['raw_score']} → {r['final_score']})")
    if r.get("resume_summary"):
        L += ["", f"> {r['resume_summary']}"]

    if r.get("must_have"):
        L += ["", "### Majburiy talablar", "", "| Holat | Talab | Dalil |", "|---|---|---|"]
        for m in r["must_have"]:
            icon = MH_ICON.get(m["status"], "")
            label = MH_UZ.get(m["status"], m["status"])
            L.append(f"| {icon} {label} | {_md(m.get('requirement'))} | {_md(m.get('evidence'))} |")

    L += ["", "### Mezonlar", "", "| Mezon | Ulush | Ball | Hissa | Ishonch |", "|---|---:|---:|---:|---|"]
    for c in r["criteria"]:
        L.append(
            f"| {c['label']} | {c['weight_pct']}% | **{c['score']:.0f}** | "
            f"{c['contribution']:.1f} | {CONF_UZ.get(c.get('confidence',''), '')} |"
        )

    L.append("")
    for c in r["criteria"]:
        L.append(f"**{c['label']} — {c['score']:.0f}/100**")
        if c.get("comment"):
            L.append(f"{c['comment']}")
        if c.get("matched"):
            L.append(f"- ✔ Mos: {', '.join(c['matched'])}")
        if c.get("missing"):
            L.append(f"- ✘ Yetishmaydi: {', '.join(c['missing'])}")
        for e in c.get("evidence", []):
            L.append(f"- › *\"{e}\"*")
        L.append("")

    for title, key in (("Kuchli tomonlar", "strengths"), ("Bo'shliqlar", "gaps"),
                       ("Xavf signallari", "red_flags")):
        if r.get(key):
            L += [f"### {title}", ""] + [f"- {x}" for x in r[key]] + [""]

    if r.get("interview_questions"):
        L += ["### Intervyu savollari", ""] + [f"{i}. {q}" for i, q in enumerate(r["interview_questions"], 1)] + [""]
    if r.get("overall_comment"):
        L += ["### Xulosa", "", r["overall_comment"], ""]
    return "\n".join(L)


def markdown_report(job_title: str, results: list[dict[str, Any]],
                    errors: list[dict[str, str]] | None = None) -> str:
    L = [f"# Resume tahlili — {job_title}", "",
         f"Jami baholandi: **{len(results)}** nomzod", "",
         "## Reyting", "",
         "| # | Ball | Verdict | Nomzod | Majburiy | Fayl |", "|---:|---:|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        cand = r.get("candidate", {})
        mh = r.get("must_have") or []
        mh_txt = f"{sum(1 for m in mh if m['status'] == 'met')}/{len(mh)}" if mh else "—"
        name = cand.get("name") or r["resume_id"]
        fname = Path(r.get("resume_file") or "").name or "—"
        L.append(
            f"| {i} | **{r['final_score']}** | {VERDICT_ICON.get(r['verdict'], '')} "
            f"{r['verdict_label']} | {name} | {mh_txt} | `{fname}` |"
        )
    if errors:
        L += ["", "## Xatolar", ""] + [f"- `{Path(e['resume_file']).name}`: {e['error']}" for e in errors]
    L += ["", "---", ""]
    for r in results:
        L.append(markdown_detail(r))
        L.append("\n---\n")
    return "\n".join(L)


# --- fayllarga yozish ----------------------------------------------------------
def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def write_csv(path: str | Path, results: list[dict[str, Any]]) -> Path:
    """Reyting jadvali — Excel/Sheets uchun."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    crit_ids = [c["id"] for c in results[0]["criteria"]] if results else []
    cols = (["rank", "resume_id", "candidate", "final_score", "raw_score", "verdict",
             "must_have_met", "must_have_total", "total_years", "relevant_years"]
            + [f"score_{c}" for c in crit_ids]
            + ["red_flags", "overall_comment", "resume_file"])
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, r in enumerate(results, 1):
            cand = r.get("candidate", {})
            mh = r.get("must_have") or []
            row = {
                "rank": i,
                "resume_id": r["resume_id"],
                "candidate": cand.get("name", ""),
                "final_score": r["final_score"],
                "raw_score": r["raw_score"],
                "verdict": r["verdict"],
                "must_have_met": sum(1 for m in mh if m["status"] == "met"),
                "must_have_total": len(mh),
                "total_years": cand.get("total_experience_years"),
                "relevant_years": cand.get("relevant_experience_years"),
                "red_flags": " | ".join(r.get("red_flags", [])),
                "overall_comment": r.get("overall_comment", ""),
                "resume_file": r.get("resume_file", ""),
            }
            for c in r["criteria"]:
                row[f"score_{c['id']}"] = c["score"]
            w.writerow(row)
    return p
