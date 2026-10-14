"""Vakansiya (job) tavsifi: YAML/JSON fayldan o'qish va validatsiya."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from .rubric import CRITERION_IDS


@dataclass
class JobSpec:
    """Vakansiya tavsifi.

    Ikki manbadan kelishi mumkin:
      * `data/jobs/*.yaml` fayldan (lokal ishlash uchun);
      * tashqi bekenddan to'g'ridan-to'g'ri JSON ichida (`job` maydoni) —
        bu holda fayl yaratish shart emas.

    Bekend tabiiy yuboradigan nomlar qo'llanadi: `title` (nomi),
    `description` (tavsifi), `requirements` (talablari),
    `must_have` (majburiy shartlari).
    """
    title: str
    id: str = ""
    description: str = ""                 # vakansiyaning erkin matnli tavsifi
    level: str = ""                       # junior / middle / senior / lead
    company: str = ""
    domain: str = ""
    location: str = ""
    employment_type: str = ""
    min_years: float | None = None
    max_years: float | None = None
    education: str = ""
    languages: list[str] = field(default_factory=list)
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    # Umumiy talablar. Majburiy emas (majburiysi — `must_have`), lekin
    # baholashda hisobga olinadi. Bekend odatda shu nom bilan yuboradi.
    requirements: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    notes: str = ""
    # Rubrika og'irliklarini shu vakansiya uchun o'zgartirish: {"education": 0}
    weights: dict[str, float] = field(default_factory=dict)
    # Mezon ko'rsatmalarini aniqlashtirish — bir xil rubrikani turli kasblarga
    # moslash uchun: {"hard_skills": "fizika fani bilimi va dars metodikasi"}
    criteria_guidance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title or not str(self.title).strip():
            raise ValueError("Vakansiyada `title` majburiy")
        if not self.id:
            self.id = _slug(self.title)
        for name in ("languages", "must_have", "nice_to_have", "requirements",
                     "skills", "responsibilities"):
            setattr(self, name, _clean_str_list(getattr(self, name), name))
        for name in ("weights", "criteria_guidance"):
            bad = set(getattr(self, name)) - set(CRITERION_IDS)
            if bad:
                raise ValueError(
                    f"`{name}` ichida noma'lum mezon: {sorted(bad)}. "
                    f"Ruxsat etilgan: {list(CRITERION_IDS)}"
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_str_list(value: Any, field_name: str) -> list[str]:
    """Ro'yxat maydonini tekshiradi va faqat satrlardan iborat qilib qaytaradi.

    YAML tuzog'i: `- PostgreSQL: sxema dizayni` satr emas, LUG'AT (dict) bo'lib
    o'qiladi, chunki ichida ": " bor. Bunday qiymat promptga "{'PostgreSQL': ...}"
    ko'rinishida kirib ketsa, model noto'g'ri talabni baholaydi va buni sezish qiyin.
    Shuning uchun shu yerda darhol aniq xato beramiz.
    """
    if value is None:
        return []
    if isinstance(value, str):                     # bitta satr yozilgan bo'lsa
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(f"`{field_name}` ro'yxat bo'lishi kerak, keldi: {type(value).__name__}")

    out: list[str] = []
    for i, item in enumerate(value, 1):
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            pairs = ", ".join(f"{k}: {v}" for k, v in item.items())
            raise ValueError(
                f"`{field_name}` ning {i}-bandi satr emas, lug'at bo'lib o'qildi: {{{pairs}}}\n"
                f"  Sabab: YAMLda ro'yxat bandi ichida \": \" bo'lsa u lug'at deb tushuniladi.\n"
                f"  Yechim: bandni qo'shtirnoqqa oling, masalan:\n"
                f'    - "{pairs}"'
            )
        elif isinstance(item, (int, float, bool)):
            out.append(str(item))
        else:
            raise ValueError(
                f"`{field_name}` ning {i}-bandi satr bo'lishi kerak, keldi: {type(item).__name__}"
            )
    return out


def _slug(text: str) -> str:
    out = []
    for ch in text.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "job"


def load_job(path: str | Path) -> JobSpec:
    """YAML (.yaml/.yml) yoki JSON (.json) fayldan vakansiya o'qiydi."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Vakansiya fayli topilmadi: {p}")
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw) if p.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: ildizda obyekt (key: value) bo'lishi kerak")
    known = {f for f in JobSpec.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{p}: noma'lum maydon(lar): {sorted(unknown)}")
    job = JobSpec(**data)
    if not job.id or job.id == _slug(job.title):
        job.id = data.get("id") or p.stem
    return job


def list_jobs(jobs_dir: str | Path) -> list[Path]:
    d = Path(jobs_dir)
    if not d.exists():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}
    )
