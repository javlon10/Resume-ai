"""Baholash rubrikasi va yakuniy ballni hisoblash.

Muhim prinsip: YAKUNIY BALLNI MODEL EMAS, PYTHON HISOBLAYDI.
Model faqat har bir mezon uchun 0-100 baho + dalil beradi; og'irlikli yig'indi,
"must-have" shartlarini tekshirish va verdict shu yerda deterministik chiqadi.
Shuning uchun natija takrorlanuvchi va auditga ochiq.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Criterion:
    id: str
    label: str          # o'zbekcha nom (hisobotda ko'rinadi)
    weight: float       # nisbiy og'irlik
    guidance: str       # modelga ko'rsatma: bu mezonni qanday baholash kerak


# Standart rubrika — KASBGA BOG'LIQ EMAS.
#
# Ko'rsatmalar (guidance) atayin umumiy yozilgan: bir xil rubrika dasturchiga ham,
# fizika o'qituvchisiga ham, buxgalterga ham ishlashi kerak. Aniqlashtirish
# vakansiya faylidagi `criteria_guidance` orqali qilinadi, masalan o'qituvchi
# vakansiyasida `hard_skills` uchun "fan bilimi va dars berish metodikasi".
#
# Og'irliklarni ham vakansiya belgilaydi: o'qituvchiga `education` muhim,
# dasturchiga esa ko'pincha unchalik emas.
DEFAULT_CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "hard_skills", "Kasbiy ko'nikmalar va bilim", 30,
        "Vakansiyada talab qilingan aniq kasbiy ko'nikmalar, bilimlar, metodlar va "
        "asboblar nomzodda bormi (kasbga qarab: texnologiya va dasturlar, fan "
        "bo'limlari, metodika, uskuna, normativ hujjatlar). Faqat resumeda "
        "yozilgan yoki tajriba tavsifidan ochiq ko'rinib turgan narsani hisobla; "
        "taxmin qilma.",
    ),
    Criterion(
        "experience", "Tajriba (yil va mosligi)", 25,
        "Umumiy va ayni shu rol bo'yicha tajriba yillari talabga mosmi. Tajriba "
        "yaqinligi muhim: talabga mos rolda 3 yil > boshqa rolda 8 yil.",
    ),
    Criterion(
        "seniority", "Daraja va mas'uliyat ko'lami", 10,
        "Ish ko'lami va murakkabligi, mustaqillik darajasi, boshqalarni "
        "o'qitish/nazorat qilish, mustaqil qarorlar qabul qilish. Vakansiya "
        "darajasiga (boshlang'ich / o'rta / katta / rahbar) mosligi.",
    ),
    Criterion(
        "domain", "Soha va muhit tajribasi", 10,
        "Vakansiya sohasida yoki shunga o'xshash muhitda ishlagan tajribasi bormi "
        "(masalan: fintex, e-commerce, maktab, litsey, oliy ta'lim, davlat "
        "muassasasi, ishlab chiqarish, tibbiyot).",
    ),
    Criterion(
        "education", "Ta'lim va sertifikatlar", 10,
        "Ma'lumoti, ixtisosligi, diplomi va tegishli sertifikat/toifalari talabga "
        "mosmi. Agar vakansiya diplomni talab qilmasa, bu mezonni yumshoq baho; "
        "talab qilsa — qat'iy.",
    ),
    Criterion(
        "soft_and_language", "Yumshoq ko'nikmalar va til", 10,
        "Til bilish darajasi (o'zbek/rus/ingliz), kommunikatsiya, jamoada ishlash, "
        "odamlar bilan ishlash (mijoz, o'quvchi, bemor, hamkor) — resumeda dalili "
        "bormi.",
    ),
    Criterion(
        "stability", "Barqarorlik va o'sish", 5,
        "Ish joyini juda tez-tez o'zgartirish, tushunarsiz uzun tanaffuslar, "
        "karyera o'sishi ko'rinadimi. Dalil yo'q bo'lsa — neytral 60-70 ball.",
    ),
)

CRITERION_IDS: tuple[str, ...] = tuple(c.id for c in DEFAULT_CRITERIA)

# Verdict chegaralari (yakuniy 0-100 ball bo'yicha).
VERDICT_BANDS: tuple[tuple[float, str, str], ...] = (
    (82, "strong_fit", "To'liq mos — darhol intervyuga"),
    (68, "fit", "Mos — intervyuga tavsiya etiladi"),
    (52, "borderline", "Chegarada — qo'shimcha tekshirish kerak"),
    (0, "no_fit", "Mos emas"),
)

# --- Majburiy talablar jazosi --------------------------------------------------
#
# Xom (og'irlikli) ball koeffitsiyentga ko'paytiriladi. Nega qattiq chegara (min)
# emas: chegara qo'ysak, talabni bajarmagan barcha nomzodlar aynan bir xil ballda
# to'planib qoladi va reyting ularni ajratmaydi. Ko'paytiruvchi tartibni saqlaydi.
#
# Kalibrovka mantig'i ikki xil:
#
#   not_met  — majburiy talabning ma'nosi shu: bajarilmasa nomzod o'tmaydi.
#              Shuning uchun jazo qat'iy va boshqa mezonlardagi yuqori ballar
#              uni qoplay olmaydi. Bajarilmagan talab ko'p bo'lsa — yanada qattiq.
#
#   partial /
#   unknown  — bu "talab bajarilmagan" degani emas, "dalil to'liq emas" degani
#              (masalan resume "REST API yozdim" deydi, lekin "versiyalash"ni
#              aytmaydi). Bunday holat intervyuda aniqlanadi, shuning uchun jazo
#              yengil: kuchli nomzod bitta noaniq ibora uchun reytingdan
#              tushib ketmasligi kerak.
NOT_MET_FACTOR = 0.45        # bitta majburiy talab bajarilmagan
NOT_MET_EXTRA = 0.05         # har bir qo'shimcha bajarilmagan talab uchun
NOT_MET_FLOOR = 0.25         # pastki chegara
PARTIAL_COST = 0.08          # har bir qismiy bajarilgan talab
UNKNOWN_COST = 0.05          # har bir "ma'lumot yo'q" talab
SOFT_FLOOR = 0.70            # partial/unknown jazosining pastki chegarasi


def must_have_factor(statuses: list[str]) -> tuple[float, str | None]:
    """Majburiy talablar holatidan koeffitsiyent va tushuntirish hisoblaydi."""
    n_not_met = sum(1 for s in statuses if s == "not_met")
    n_partial = sum(1 for s in statuses if s == "partial")
    n_unknown = sum(1 for s in statuses if s == "unknown")

    if n_not_met:
        factor = max(NOT_MET_FLOOR, NOT_MET_FACTOR - NOT_MET_EXTRA * (n_not_met - 1))
        reason = (
            f"{n_not_met} ta majburiy talab bajarilmagan → ball {factor:.2f} "
            f"koeffitsiyentga ko'paytirildi"
        )
        return factor, reason

    if n_partial or n_unknown:
        factor = max(SOFT_FLOOR, 1.0 - PARTIAL_COST * n_partial - UNKNOWN_COST * n_unknown)
        parts = []
        if n_partial:
            parts.append(f"{n_partial} ta talab qismangina bajarilgan")
        if n_unknown:
            parts.append(f"{n_unknown} ta talab bo'yicha resumeda ma'lumot yo'q")
        reason = (
            f"{' va '.join(parts)} → ball {factor:.2f} koeffitsiyentga ko'paytirildi "
            f"(intervyuda aniqlashtirish kerak)"
        )
        return factor, reason

    return 1.0, None


def resolve_criteria(
    weights: dict[str, float] | None = None,
    guidance: dict[str, str] | None = None,
) -> list[Criterion]:
    """Vakansiyaga moslangan mezonlar ro'yxatini qaytaradi.

    weights  — og'irliklarni almashtiradi. 0 qo'yilsa mezon butunlay chiqariladi
               (masalan diplom muhim bo'lmagan vakansiyalarda `education: 0`).
    guidance — modelga beriladigan ko'rsatmani almashtiradi yoki aniqlashtiradi.
               Shu orqali bir xil rubrika turli kasblarga moslashadi: o'qituvchi
               vakansiyasida `hard_skills` = "fan bilimi va dars metodikasi",
               dasturchida = "texnologiyalar va asboblar".
    """
    for name, given in (("weights", weights), ("criteria_guidance", guidance)):
        unknown = set(given or {}) - set(CRITERION_IDS)
        if unknown:
            raise ValueError(
                f"`{name}` ichida noma'lum mezon: {sorted(unknown)}. "
                f"Ruxsat etilgan: {list(CRITERION_IDS)}"
            )

    out: list[Criterion] = []
    for c in DEFAULT_CRITERIA:
        w = float((weights or {}).get(c.id, c.weight))
        if w <= 0:
            continue
        extra = (guidance or {}).get(c.id)
        text = f"{c.guidance}\n  Bu vakansiyada aynan: {extra.strip()}" if extra else c.guidance
        out.append(Criterion(c.id, c.label, w, text))
    if not out:
        raise ValueError("Kamida bitta mezon og'irligi 0 dan katta bo'lishi kerak")
    return out


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class ScoreBreakdown:
    """Hisoblash natijasi — hisobotda to'liq ko'rsatiladi."""
    raw_score: float                  # og'irlikli yig'indi (jazodan oldin)
    final_score: float                # majburiy talab koeffitsiyentidan keyin
    verdict: str
    verdict_label: str
    penalty_factor: float             # 1.0 = jazo yo'q
    penalty_reason: str | None
    per_criterion: list[dict] = field(default_factory=list)
    missing_criteria: list[str] = field(default_factory=list)


def compute_score(
    criteria: list[Criterion],
    model_scores: dict[str, float],
    must_have_statuses: list[str] | None = None,
) -> ScoreBreakdown:
    """Og'irlikli ball + must-have gating → yakuniy verdict.

    criteria           — shu vakansiya uchun faol mezonlar (og'irliklari bilan)
    model_scores       — {criterion_id: 0-100}, modeldan kelgan bahoar
    must_have_statuses — ["met"|"partial"|"not_met"|"unknown", ...]
    """
    total_w = sum(c.weight for c in criteria)
    acc = 0.0
    rows: list[dict] = []
    missing: list[str] = []

    for c in criteria:
        if c.id in model_scores and model_scores[c.id] is not None:
            s = _clamp(float(model_scores[c.id]))
        else:
            # Model bu mezonni qaytarmasa — neytral 50, va buni hisobotda belgilaymiz.
            s = 50.0
            missing.append(c.id)
        contrib = s * c.weight / total_w
        acc += contrib
        rows.append(
            {
                "id": c.id,
                "label": c.label,
                "weight": round(c.weight, 2),
                "weight_pct": round(100 * c.weight / total_w, 1),
                "score": round(s, 1),
                "contribution": round(contrib, 2),
            }
        )

    raw = _clamp(acc)

    # --- majburiy talablar jazosi ---
    factor, penalty_reason = must_have_factor(list(must_have_statuses or []))
    final = _clamp(raw * factor)

    verdict, verdict_label = "no_fit", "Mos emas"
    for threshold, v, label in VERDICT_BANDS:
        if final >= threshold:
            verdict, verdict_label = v, label
            break

    return ScoreBreakdown(
        raw_score=round(raw, 1),
        final_score=round(final, 1),
        verdict=verdict,
        verdict_label=verdict_label,
        penalty_factor=round(factor, 3),
        penalty_reason=penalty_reason,
        per_criterion=rows,
        missing_criteria=missing,
    )
