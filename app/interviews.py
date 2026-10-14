"""Intervyu savol banki, sessiya va yakuniy ball hisobi.

Ish tartibi:
  1. HR savollar bankini YAML faylda yozadi (data/interviews/*.yaml) —
     har savolga kutilayotgan asosiy nuqtalar (`expected`) ko'rsatiladi.
  2. Tizim shu bankdan savollarni tanlab, ARALASHTIRIB nomzodga beradi.
  3. Nomzod javob yozadi.
  4. Model FAQAT javoblarni tahlil qiladi: har javobga 0-100 ball va izoh.
  5. Yakuniy BITTA ball — Python tomonidan og'irlikli hisoblanadi (resume
     moduli bilan bir xil prinsip: model bahoni beradi, tizim hisoblaydi).
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DIFFICULTIES = ("oson", "o'rta", "qiyin")

# Qiyinlik koeffitsiyenti: qiyin savolga to'g'ri javob ko'proq "vazn" tashiydi.
# HR `weight` yozmasa shu ishlatiladi.
DIFFICULTY_WEIGHT = {"oson": 0.8, "o'rta": 1.0, "qiyin": 1.3}

# Yakuniy ball bo'yicha xulosa chegaralari
VERDICT_BANDS: tuple[tuple[float, str, str], ...] = (
    (82, "excellent", "A'lo — bilim darajasi yuqori"),
    (65, "good", "Yaxshi — talabga javob beradi"),
    (50, "fair", "Qoniqarli — bo'shliqlar bor"),
    (0, "weak", "Yetarli emas"),
)


class InterviewError(ValueError):
    pass


def _str_list(value: Any, where: str) -> list[str]:
    """YAML ro'yxatini tekshiradi (jobs.py dagi bilan bir xil tuzoq: ': ' bo'lsa dict bo'ladi)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise InterviewError(f"{where}: ro'yxat bo'lishi kerak, keldi {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value, 1):
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            pairs = ", ".join(f"{k}: {v}" for k, v in item.items())
            raise InterviewError(
                f"{where} ning {i}-bandi satr emas, lug'at bo'lib o'qildi: {{{pairs}}}\n"
                f'  YAMLda ": " bo\'lgan bandni qo\'shtirnoqqa oling: - "{pairs}"'
            )
        else:
            out.append(str(item))
    return out


@dataclass
class Question:
    """Bitta intervyu savoli.

    HR ikki usulda mezon berishi mumkin (ikkalasini birga ham):

      answer   — TO'G'RI JAVOBNING O'ZI. Eng tabiiy usul: HR savolni yozadi va
                 unga to'g'ri javobni yozib qo'yadi. Model nomzod javobini
                 shunga solishtiradi.
      expected — javobda bo'lishi kerak bo'lgan asosiy nuqtalar ro'yxati.
                 Natijada qaysi nuqta yoritilgani/yoritilmagani aniq ko'rinadi.

    Kamida bittasi bo'lishi shart — aks holda modelda solishtiradigan mezon
    bo'lmaydi va u o'z bilimiga tayanib qoladi (bu ishonchsiz, README 7-bo'lim).
    """
    id: str
    text: str
    topic: str = ""
    difficulty: str = "o'rta"
    weight: float | None = None          # ko'rsatilmasa qiyinlikdan olinadi
    answer: str = ""                     # HR yozgan to'g'ri javob
    expected: list[str] = field(default_factory=list)
    reference: str = ""                  # eski nom, `answer` bilan bir xil

    def __post_init__(self) -> None:
        if not str(self.text).strip():
            raise InterviewError(f"savol '{self.id}': `text` bo'sh")
        self.difficulty = str(self.difficulty).strip().lower()
        if self.difficulty not in DIFFICULTIES:
            raise InterviewError(
                f"savol '{self.id}': difficulty '{self.difficulty}' noto'g'ri, "
                f"mumkin: {list(DIFFICULTIES)}"
            )
        # `reference` — eski nom. Ikkalasi bo'lsa `answer` ustun.
        self.answer = str(self.answer or self.reference or "").strip()
        self.reference = ""
        self.expected = _str_list(self.expected, f"savol '{self.id}' `expected`")

        if not self.answer and not self.expected:
            raise InterviewError(
                f"savol '{self.id}': `answer` (to'g'ri javob) yoki `expected` "
                f"(kutilgan nuqtalar) dan kamida bittasi bo'lishi kerak.\n"
                f"  Modelga solishtiradigan mezon bo'lmasa, u o'z bilimiga "
                f"tayanadi — bu ishonchsiz."
            )

        if self.weight is None:
            self.weight = DIFFICULTY_WEIGHT[self.difficulty]
        self.weight = float(self.weight)
        if self.weight <= 0:
            raise InterviewError(f"savol '{self.id}': weight 0 dan katta bo'lishi kerak")

    @property
    def has_answer(self) -> bool:
        return bool(self.answer)

    def public(self) -> dict[str, Any]:
        """Nomzodga ko'rsatiladigan qism — `answer` va `expected` BERILMAYDI."""
        return {"id": self.id, "text": self.text, "topic": self.topic,
                "difficulty": self.difficulty}


@dataclass
class InterviewSpec:
    title: str
    id: str = ""
    job_id: str = ""                     # qaysi vakansiyaga tegishli (ixtiyoriy)
    description: str = ""
    pass_score: float = 60.0             # o'tish chegarasi
    default_count: int = 0               # sessiyada nechta savol (0 = hammasi)
    shuffle: bool = True                 # savollarni aralashtirish
    stratify_by_topic: bool = True       # mavzular bo'yicha teng taqsimlash
    time_limit_min: int = 0              # 0 = cheklovsiz (faqat ma'lumot uchun)
    questions: list[Question] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not str(self.title).strip():
            raise InterviewError("`title` majburiy")
        qs: list[Question] = []
        for i, q in enumerate(self.questions, 1):
            if isinstance(q, Question):
                qs.append(q)
            elif isinstance(q, dict):
                known = set(Question.__dataclass_fields__)
                unknown = set(q) - known
                if unknown:
                    raise InterviewError(f"{i}-savolda noma'lum maydon: {sorted(unknown)}")
                q = dict(q)
                q.setdefault("id", f"q{i}")
                qs.append(Question(**q))
            else:
                raise InterviewError(f"{i}-savol obyekt bo'lishi kerak, keldi {type(q).__name__}")
        if not qs:
            raise InterviewError("kamida bitta savol bo'lishi kerak")
        ids = [q.id for q in qs]
        dup = {x for x in ids if ids.count(x) > 1}
        if dup:
            raise InterviewError(f"savol id'lari takrorlangan: {sorted(dup)}")
        self.questions = qs
        if self.default_count and self.default_count > len(qs):
            raise InterviewError(
                f"default_count={self.default_count}, lekin bankda {len(qs)} ta savol bor"
            )

    @property
    def topics(self) -> list[str]:
        seen: list[str] = []
        for q in self.questions:
            t = q.topic or "—"
            if t not in seen:
                seen.append(t)
        return seen

    def to_dict(self, include_answers: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_answers:
            d["questions"] = [q.public() for q in self.questions]
        return d


def _slug(text: str) -> str:
    out = []
    for ch in str(text).lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "intervyu"


def load_interview(path: str | Path) -> InterviewSpec:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Intervyu fayli topilmadi: {p}")
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw) if p.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise InterviewError(f"{p}: ildizda obyekt (key: value) bo'lishi kerak")
    unknown = set(data) - set(InterviewSpec.__dataclass_fields__)
    if unknown:
        raise InterviewError(f"{p}: noma'lum maydon(lar): {sorted(unknown)}")
    spec = InterviewSpec(**data)
    if not spec.id:
        spec.id = p.stem
    return spec


def list_interviews(d: str | Path) -> list[Path]:
    p = Path(d)
    if not p.exists():
        return []
    return sorted(f for f in p.iterdir()
                  if f.is_file() and f.suffix.lower() in {".yaml", ".yml", ".json"})


# --- savol tanlash va aralashtirish ------------------------------------------
def select_questions(spec: InterviewSpec, count: int | None = None,
                     seed: int | None = None) -> list[Question]:
    """Bankdan savollarni tanlaydi va aralashtiradi.

    `stratify_by_topic` yoqilgan bo'lsa savollar mavzular bo'yicha navbatma-navbat
    olinadi — shunda 8 ta savoldan 6 tasi bitta mavzudan chiqib qolmaydi.
    """
    n = count or spec.default_count or len(spec.questions)
    n = max(1, min(int(n), len(spec.questions)))
    rng = random.Random(seed)

    picked: list[Question]

    if n >= len(spec.questions):
        # Hammasi kerak — tanlash shart emas, ASL TARTIB saqlanadi.
        # Bu bekend uchun muhim: HR savollarni ataylab tartiblab qo'ygan
        # bo'lishi mumkin va `shuffle: false` deganda ular o'zgarmasligi kerak.
        picked = list(spec.questions)
    elif spec.stratify_by_topic:
        # Qism kerak, mavzular bo'yicha teng taqsimlab olamiz
        buckets: dict[str, list[Question]] = {}
        for q in spec.questions:
            buckets.setdefault(q.topic or "—", []).append(q)
        for b in buckets.values():
            rng.shuffle(b)
        picked = []
        topics = list(buckets)
        rng.shuffle(topics)
        while len(picked) < n:
            progressed = False
            for t in topics:
                if buckets[t] and len(picked) < n:
                    picked.append(buckets[t].pop())
                    progressed = True
            if not progressed:
                break
    else:
        # Qism kerak, mavzuga qaramay tasodifiy tanlaymiz
        pool = list(spec.questions)
        rng.shuffle(pool)
        picked = pool[:n]

    if spec.shuffle:
        rng.shuffle(picked)          # nomzodga beriladigan yakuniy tartib
    return picked


# --- yakuniy ball -------------------------------------------------------------
@dataclass
class InterviewScore:
    final_score: float
    verdict: str
    verdict_label: str
    passed: bool
    pass_score: float
    answered: int
    total: int
    unanswered: list[str] = field(default_factory=list)
    per_question: list[dict] = field(default_factory=list)


def compute_interview_score(
    questions: list[Question],
    answer_scores: dict[str, float],
    pass_score: float = 60.0,
    unanswered: set[str] | None = None,
) -> InterviewScore:
    """Og'irlikli yakuniy ball.

    Javob berilmagan savol 0 ball oladi (modelga yuborilmaydi ham) — aks holda
    nomzod savollarni tashlab ketib yuqori ball olishi mumkin edi.
    """
    unanswered = unanswered or set()
    total_w = sum(q.weight for q in questions) or 1.0
    acc = 0.0
    rows: list[dict] = []

    for q in questions:
        if q.id in unanswered:
            s = 0.0
        else:
            s = max(0.0, min(100.0, float(answer_scores.get(q.id, 0.0))))
        contrib = s * q.weight / total_w
        acc += contrib
        rows.append({
            "id": q.id,
            "topic": q.topic,
            "difficulty": q.difficulty,
            "weight": round(q.weight, 2),
            "weight_pct": round(100 * q.weight / total_w, 1),
            "score": round(s, 1),
            "contribution": round(contrib, 2),
            "answered": q.id not in unanswered,
        })

    final = round(max(0.0, min(100.0, acc)), 1)
    verdict, label = "weak", "Yetarli emas"
    for t, v, lbl in VERDICT_BANDS:
        if final >= t:
            verdict, label = v, lbl
            break

    return InterviewScore(
        final_score=final,
        verdict=verdict,
        verdict_label=label,
        passed=final >= pass_score,
        pass_score=pass_score,
        answered=len(questions) - len(unanswered),
        total=len(questions),
        unanswered=sorted(unanswered),
        per_question=rows,
    )


# --- sessiyalar (diskda saqlanadi) --------------------------------------------
_SID_RE = re.compile(r"^[a-f0-9]{12}$")


class SessionStore:
    """Sessiyalar diskda JSON fayl sifatida saqlanadi.

    Xotirada emas — chunki nomzod savollarga javob yozayotganda bir necha daqiqa
    o'tadi va shu orada API qayta ishga tushsa sessiya yo'qolmasligi kerak.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        if not _SID_RE.match(sid):
            raise InterviewError(f"sessiya id noto'g'ri: {sid!r}")
        return self.dir / f"{sid}.json"

    def new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def save(self, session: dict[str, Any]) -> dict[str, Any]:
        p = self._path(session["session_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)                       # atomik almashtirish
        return session

    def load(self, sid: str) -> dict[str, Any] | None:
        p = self._path(sid)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def delete(self, sid: str) -> bool:
        p = self._path(sid)
        if p.exists():
            p.unlink()
            return True
        return False

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append({
                "session_id": d.get("session_id"),
                "interview_id": d.get("interview_id"),
                "interview_title": d.get("interview_title"),
                "candidate": d.get("candidate", {}).get("name"),
                "status": d.get("status"),
                "created_at": d.get("created_at"),
                "question_count": len(d.get("questions", [])),
                "final_score": (d.get("result") or {}).get("final_score"),
                "verdict": (d.get("result") or {}).get("verdict"),
            })
            if len(out) >= limit:
                break
        return out

    def gc(self, ttl_s: int) -> int:
        """Eski sessiyalarni tozalaydi."""
        now, removed = time.time(), 0
        for p in self.dir.glob("*.json"):
            if now - p.stat().st_mtime > ttl_s:
                p.unlink(missing_ok=True)
                removed += 1
        return removed
