"""Model chiqishi uchun JSON Schema (llama.cpp grammar bilan majburlanadi).

Dizayn qarorlari:

1) `criteria` va `must_have_checks` — massiv emas, OBYEKT. Kalitlari oldindan
   belgilangan (mezon id'lari / r1..rN). Shunda model har bir mezonni va har bir
   majburiy talabni aynan bir marta, to'g'ri tartibda to'ldirishga majbur bo'ladi —
   massivdagi "tushib qolish"/"almashish" muammosi yo'q.

2) Har bir mezon ichida maydon tartibi: dalil → mos → yetishmayotgan → izoh → BALL.
   Grammar maydonlarni shu tartibda chiqarishga majbur qiladi, ya'ni model avval
   dalilni yozib, keyin ball qo'yadi (chain-of-thought o'rnini bosadi).

3) maxLength/maxItems — grammar darajasida majburlanadi, ya'ni chiqish hajmi
   (va demak vaqt) kafolatlangan chegarada qoladi. Chegaralar atayin saxiy
   qilingan: model ularga urilmasligi kerak, aks holda matn o'rtasidan kesiladi.
   Qisqalikni asosan prompt ta'minlaydi, maxLength esa xavfsizlik to'ri.

Ikki rejim:
  "screen" — tez saralash: faqat ball + qisqa izoh (~600 token, ~25 s)
  "full"   — to'liq tahlil: dalillar, bo'shliqlar, intervyu savollari (~2500 token)
"""
from __future__ import annotations

from .rubric import Criterion

CONFIDENCE = ["high", "medium", "low"]
MH_STATUS = ["met", "partial", "not_met", "unknown"]
MODES = ("screen", "full")


def _sarr(max_items: int, max_len: int, min_items: int = 0) -> dict:
    d: dict = {
        "type": "array",
        "items": {"type": "string", "maxLength": max_len},
        "maxItems": max_items,
    }
    if min_items:
        d["minItems"] = min_items
    return d


def _criterion_schema(mode: str) -> dict:
    props: dict = {}
    if mode == "full":
        props["evidence"] = _sarr(2, 200)      # resume matnidan qisqa sitata
        props["matched"] = _sarr(6, 60)        # faqat qisqa nomlar
        props["missing"] = _sarr(6, 60)
    props["comment"] = {"type": "string", "maxLength": 400 if mode == "full" else 200}
    props["score"] = {"type": "integer", "minimum": 0, "maximum": 100}
    props["confidence"] = {"type": "string", "enum": CONFIDENCE}
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def _must_have_schema(mode: str) -> dict:
    props: dict = {}
    if mode == "full":
        props["evidence"] = {"type": "string", "maxLength": 200}
    props["status"] = {"type": "string", "enum": MH_STATUS}
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def must_have_keys(n: int) -> list[str]:
    """Majburiy talablar uchun barqaror kalitlar: r1, r2, ..."""
    return [f"r{i + 1}" for i in range(n)]


def build_schema(criteria: list[Criterion], n_must_have: int, mode: str = "full") -> dict:
    """Shu vakansiya va rejim uchun aniq JSON Schema yasaydi."""
    if mode not in MODES:
        raise ValueError(f"mode '{mode}' noto'g'ri, mumkin: {list(MODES)}")

    props: dict = {
        "candidate": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 80},
                "current_role": {"type": "string", "maxLength": 80},
                "total_experience_years": {"type": "number"},
                "relevant_experience_years": {"type": "number"},
                "location": {"type": "string", "maxLength": 60},
            },
            "required": [
                "name", "current_role", "total_experience_years",
                "relevant_experience_years", "location",
            ],
            "additionalProperties": False,
        },
        "resume_summary": {"type": "string", "maxLength": 600 if mode == "full" else 250},
    }

    if n_must_have:
        keys = must_have_keys(n_must_have)
        props["must_have_checks"] = {
            "type": "object",
            "properties": {k: _must_have_schema(mode) for k in keys},
            "required": keys,
            "additionalProperties": False,
        }

    props["criteria"] = {
        "type": "object",
        "properties": {c.id: _criterion_schema(mode) for c in criteria},
        "required": [c.id for c in criteria],
        "additionalProperties": False,
    }

    if mode == "full":
        props["strengths"] = _sarr(4, 180, min_items=1)
        props["gaps"] = _sarr(4, 180)
        props["red_flags"] = _sarr(3, 180)
        props["interview_questions"] = _sarr(4, 250, min_items=3)

    props["overall_comment"] = {"type": "string", "maxLength": 700 if mode == "full" else 300}

    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


# =============================================================================
# INTERVYU: bitta javobni baholash sxemasi
# =============================================================================
RELEVANCE = ["on_topic", "partly", "off_topic", "empty"]


def build_answer_schema(n_expected: int) -> dict:
    """Nomzodning bitta javobini baholash uchun sxema.

    Maydon tartibi muhim: avval qamrab olingan/yetishmagan nuqtalar va xatolar
    yoziladi, ball ENG OXIRIDA qo'yiladi — model avval dalilni ko'rib, keyin
    baholaydi.
    """
    cap = max(1, min(n_expected or 4, 8))
    return {
        "type": "object",
        "properties": {
            # Kutilgan nuqtalardan javobda bor bo'lganlari
            "covered": _sarr(cap, 80),
            # Javobda yo'q qolgan muhim nuqtalar
            "missing": _sarr(cap, 80),
            # Javobdagi ANIQ xatolar (noto'g'ri fakt, noto'g'ri formula)
            "errors": _sarr(3, 160),
            "comment": {"type": "string", "maxLength": 400},
            "relevance": {"type": "string", "enum": RELEVANCE},
            # Javob ichida modelga qaratilgan buyruq bo'lsa (manipulyatsiya urinishi)
            "manipulation_attempt": {"type": "boolean"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "confidence": {"type": "string", "enum": CONFIDENCE},
        },
        "required": ["covered", "missing", "errors", "comment", "relevance",
                     "manipulation_attempt", "score", "confidence"],
        "additionalProperties": False,
    }


def build_interview_summary_schema() -> dict:
    """Barcha javoblar bo'yicha umumiy xulosa (ball emas — ball tizim hisoblaydi)."""
    return {
        "type": "object",
        "properties": {
            "strengths": _sarr(4, 180, min_items=1),
            "weaknesses": _sarr(4, 180),
            "topics_to_probe": _sarr(4, 140),
            "overall_comment": {"type": "string", "maxLength": 700},
            "recommendation": {
                "type": "string",
                "enum": ["hire", "next_round", "hold", "reject"],
            },
        },
        "required": ["strengths", "weaknesses", "topics_to_probe",
                     "overall_comment", "recommendation"],
        "additionalProperties": False,
    }
