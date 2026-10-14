"""HTTP API — bekend integratsiyasi uchun.

Ishga tushirish:
    ./scripts/start_api.sh                 # 0.0.0.0:9000
    API_PORT=8000 ./scripts/start_api.sh

Hujjat:
    http://localhost:9000/docs             # Swagger UI
    http://localhost:9000/                 # veb interfeys
    API.md                                 # bekend dasturchilar uchun qo'llanma

Muhim: uzun batch so'rovlari HTTP timeout'ga urilmasligi uchun ASINXRON —
POST /score/batch darhol task_id qaytaradi, natija GET /tasks/{id} dan olinadi.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (Body, Depends, FastAPI, File, Form, Header, HTTPException,
                     Request, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import config
from .extract import ExtractError, extract_text
from .jobs import JobSpec, list_jobs, load_job
from .interview_scorer import InterviewScorer, is_answered
from .interviews import (InterviewError, InterviewSpec, Question, SessionStore,
                         list_interviews, load_interview, select_questions)
from .llm import LlamaPool, LLMError
from .report import markdown_detail, markdown_report
from .rubric import (DEFAULT_CRITERIA, NOT_MET_EXTRA, NOT_MET_FACTOR, NOT_MET_FLOOR,
                     PARTIAL_COST, SOFT_FLOOR, UNKNOWN_COST, VERDICT_BANDS)
from .scorer import ResumeScorer

# --- sozlamalar ----------------------------------------------------------------
API_KEY = os.getenv("RA_API_KEY", "").strip()          # bo'sh = autentifikatsiya o'chiq
CORS_ORIGINS = [o.strip() for o in os.getenv("RA_CORS_ORIGINS", "*").split(",") if o.strip()]
MAX_UPLOAD_MB = float(os.getenv("RA_MAX_UPLOAD_MB", "15"))
MAX_BATCH = int(os.getenv("RA_MAX_BATCH", "200"))
TASK_TTL_S = int(os.getenv("RA_TASK_TTL", "86400"))    # tugagan taskni qancha saqlash

STATIC_DIR = Path(__file__).resolve().parent / "static"

TAGS = [
    {"name": "tizim", "description": "Holat, rubrika va vakansiyalar haqida ma'lumot"},
    {"name": "baholash", "description": "Bitta resumeni baholash (sinxron)"},
    {"name": "batch", "description": "Ko'p resumeni baholash (asinxron task)"},
    {"name": "intervyu", "description": "Savol banki, sessiya va javoblarni baholash"},
    {"name": "interfeys", "description": "Brauzer uchun veb interfeys"},
]

api = FastAPI(
    title="resume_analize API",
    description=(
        "Resumelarni vakansiya talablariga ko'ra baholash. Model lokal ishlaydi "
        "(Qwen3.5-9B Q8_0, llama.cpp, 4×Tesla T4) — ma'lumot tashqariga chiqmaydi.\n\n"
        "**Yakuniy ballni model emas, tizim hisoblaydi:** model har mezon bo'yicha "
        "baho va dalil beradi, og'irlikli yig'indi va majburiy talablar koeffitsiyenti "
        "server tomonida deterministik qo'llanadi."
    ),
    version="1.1.0",
    openapi_tags=TAGS,
)

api.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.middleware("http")
async def add_timing_header(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = f"{(time.monotonic() - t0) * 1000:.0f}"
    return response


# --- autentifikatsiya ----------------------------------------------------------
def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """RA_API_KEY o'rnatilgan bo'lsa X-API-Key sarlavhasini talab qiladi."""
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(401, "X-API-Key noto'g'ri yoki berilmagan")


# --- model pool ----------------------------------------------------------------
_pool: LlamaPool | None = None
_pool_lock = threading.Lock()
_sessions = SessionStore(config.SESSIONS_DIR)


def pool() -> LlamaPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = LlamaPool()
    return _pool


def _job_by_id(job_id: str) -> JobSpec:
    for p in list_jobs(config.JOBS_DIR):
        if p.stem == job_id:
            return load_job(p)
    raise HTTPException(404, f"Vakansiya topilmadi: '{job_id}'. Ro'yxat: GET /jobs")


def _resolve_job(job_id: str | None, job_inline: "JobIn | dict | None") -> JobSpec:
    """Vakansiyani fayldan yoki so'rov ichidan (bekenddan) oladi.

    Maydon nomlari `JobIn` modelida tekshiriladi (`extra: forbid`), shuning
    uchun noto'g'ri nom bu yergacha yetib kelmaydi.
    """
    if job_inline is not None:
        data = (job_inline.model_dump(exclude_none=True)
                if hasattr(job_inline, "model_dump") else dict(job_inline))
        try:
            return JobSpec(**data)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"Vakansiya tavsifi noto'g'ri: {e}")
    if job_id:
        return _job_by_id(job_id)
    raise HTTPException(422, "`job_id` yoki `job` dan bittasi berilishi kerak")


def _resolve_interview(interview_id: str | None,
                       inline: "InterviewIn | dict | None") -> InterviewSpec:
    """Savol bankini fayldan yoki so'rov ichidan (bekenddan) oladi.

    Bekend vakansiya uchun savollarni to'g'ri javoblari bilan birga yuboradi —
    bu holda serverda fayl yaratish shart emas.
    """
    if inline is not None:
        data = (inline.model_dump(exclude_none=True)
                if hasattr(inline, "model_dump") else dict(inline))
        try:
            spec = InterviewSpec(**data)
        except (TypeError, InterviewError, ValueError) as e:
            raise HTTPException(422, f"Intervyu tavsifi noto'g'ri: {e}")
        if not spec.id:
            spec.id = "inline"
        return spec
    if interview_id:
        return _interview_by_id(interview_id)
    raise HTTPException(422, "`interview_id` yoki `interview` dan bittasi berilishi kerak")


# --- so'rov modellari: bekend yuboradigan obyektlar ----------------------------
# DIQQAT: bu modellar atayin TIPLI (dict emas) — shunda /docs sahifasida har bir
# maydon nomi va izohi ko'rinadi va bekend dasturchi nima yuborishni bilib oladi.
# `extra: forbid` — noto'g'ri maydon jimgina e'tiborsiz qolmasin.

class JobIn(BaseModel):
    """Vakansiya. Bekend bazasidagi vakansiyani shu ko'rinishda yuboradi."""
    title: str = Field(..., description="Vakansiya nomi", examples=["Korporativ yurist"])
    description: str = Field("", description="Vakansiya tavsifi (erkin matn)")
    requirements: list[str] = Field(default_factory=list, description="Umumiy talablar")
    must_have: list[str] = Field(
        default_factory=list,
        description="MAJBURIY shartlar. Ball hisobiga eng kuchli ta'sir qiladi — "
                    "har bandni bitta aniq tekshiriladigan shart qilib yozing "
                    "('3 yil tajriba' emas, '3 yil shu lavozimda')")
    nice_to_have: list[str] = Field(default_factory=list, description="Afzallik beradi")
    skills: list[str] = Field(default_factory=list, description="Ko'nikmalar ro'yxati")
    responsibilities: list[str] = Field(default_factory=list, description="Asosiy vazifalar")
    languages: list[str] = Field(default_factory=list, description="Til talablari")
    level: str = Field("", description="junior / middle / senior / lead yoki matn")
    company: str = ""
    domain: str = Field("", description="Soha: fintex, ta'lim, huquq, ...")
    location: str = ""
    employment_type: str = ""
    min_years: float | None = Field(None, description="Eng kam tajriba (yil)")
    max_years: float | None = None
    education: str = Field("", description="Ta'lim talabi")
    notes: str = Field("", description="Qo'shimcha kontekst")
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Mezon og'irliklari, masalan {\"education\": 20}. Mezonlar: "
                    "hard_skills, experience, seniority, domain, education, "
                    "soft_and_language, stability. 0 qo'yilsa mezon hisobdan chiqadi")
    criteria_guidance: dict[str, str] = Field(
        default_factory=dict,
        description="Mezonni kasbga moslash, masalan "
                    "{\"hard_skills\": \"shartnoma huquqi va protsessual hujjatlar\"}")
    id: str = Field("", description="Sizdagi vakansiya id (ixtiyoriy)")

    model_config = {"extra": "forbid"}


class QuestionIn(BaseModel):
    """Intervyu savoli — HR kiritgan to'g'ri javobi bilan."""
    id: str = Field(..., max_length=64, description="Sizdagi savol id; takrorlanmasin")
    text: str = Field(..., min_length=3, description="Savol matni")
    answer: str = Field(
        "", description="TO'G'RI JAVOB — asosiy solishtirish mezoni. "
                        "`expected` bo'lmasa majburiy")
    expected: list[str] = Field(
        default_factory=list,
        description="Javobda bo'lishi kerak bo'lgan asosiy fikrlar. "
                    "`answer` bo'lmasa majburiy")
    topic: str = Field("", description="Mavzu — teng taqsimlash va hisobot uchun")
    difficulty: Literal["oson", "o'rta", "qiyin"] = Field(
        "o'rta", description="Og'irlikni belgilaydi: oson 0.8, o'rta 1.0, qiyin 1.3")
    weight: float | None = Field(None, gt=0, description="Qo'lda og'irlik (ixtiyoriy)")

    model_config = {"extra": "forbid"}


class InterviewIn(BaseModel):
    """Vakansiya uchun intervyu savollari to'plami (bekenddan)."""
    title: str = Field(..., description="Intervyu nomi")
    questions: list[QuestionIn] = Field(..., min_length=1, max_length=50)
    pass_score: float = Field(60.0, ge=0, le=100, description="O'tish chegarasi")
    description: str = ""
    # Bekend odatda vakansiya uchun AYNAN kerakli savollarni yuboradi, shuning
    # uchun bu yerda default — hammasini, HR bergan tartibda. (Fayl bankida
    # aksincha: u savollar OMBORI, undan tanlab aralashtiriladi.)
    shuffle: bool = Field(False, description="Savollarni aralashtirish")
    stratify_by_topic: bool = Field(False, description="Mavzular bo'yicha teng taqsimlash")
    default_count: int = Field(0, ge=0, description="Nechta savol berilsin (0 = hammasi)")
    time_limit_min: int = Field(0, ge=0, description="Vaqt chegarasi (0 = cheklovsiz)")
    job_id: str = ""
    id: str = ""

    model_config = {"extra": "forbid"}


# --- so'rov/javob modellari ----------------------------------------------------
class ScoreRequest(BaseModel):
    resume_text: str = Field(..., min_length=50, description="Resume matni")
    job_id: str | None = Field(None, description="data/jobs ichidagi fayl nomi (kengaytmasiz)")
    job: JobIn | None = Field(
        None, description="Vakansiyani to'g'ridan-to'g'ri berish (fayl kerak emas)"
    )
    mode: Literal["screen", "full"] = Field("full", description="screen = tez, full = dalilli")
    resume_id: str = Field("resume", description="Natijada ko'rinadigan identifikator")
    markdown: bool = Field(False, description="Javobga markdown hisobotni ham qo'shish")




class BatchItem(BaseModel):
    resume_id: str = Field(..., description="Har bir resume uchun unikal nom")
    resume_text: str = Field(..., min_length=50)


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(..., min_length=1, description=f"Ko'pi bilan {MAX_BATCH} ta")
    job_id: str | None = None
    job: JobIn | None = None
    mode: Literal["screen", "full"] = "screen"


class TaskCreated(BaseModel):
    task_id: str
    status: str
    total: int
    poll_url: str


# --- asinxron task'lar ---------------------------------------------------------
@dataclass
class Task:
    """Batch baholash taski. Xotirada saqlanadi (bitta worker uchun)."""
    id: str
    job_id: str
    job_title: str
    mode: str
    total: int
    status: str = "queued"                 # queued | running | done | error | cancelled
    done: int = 0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    results: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    message: str | None = None
    cancel: bool = False

    def public(self, include_results: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "job_id": self.job_id,
            "job_title": self.job_title,
            "mode": self.mode,
            "total": self.total,
            "done": self.done,
            "progress_pct": round(100 * self.done / self.total, 1) if self.total else 0.0,
            "created_at": self.created_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.created_at, 1),
            "error_count": len(self.errors),
        }
        if self.message:
            d["message"] = self.message
        if include_results and self.status in {"done", "cancelled", "error"}:
            d["results"] = self.results
            d["errors"] = self.errors
        return d


_tasks: dict[str, Task] = {}
_tasks_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="batch")


def _gc_tasks() -> None:
    """Eski tugagan tasklarni tozalaydi."""
    now = time.time()
    with _tasks_lock:
        stale = [
            t.id for t in _tasks.values()
            if t.finished_at and now - t.finished_at > TASK_TTL_S
        ]
        for tid in stale:
            _tasks.pop(tid, None)


def _run_batch(task: Task, job: JobSpec, items: list[BatchItem]) -> None:
    task.status = "running"
    scorer = ResumeScorer(job, pool(), mode=task.mode)
    lock = threading.Lock()

    def one(item: BatchItem) -> None:
        if task.cancel:
            return
        try:
            r = scorer.score_text(item.resume_text, resume_id=item.resume_id,
                                  priority="batch")
            with lock:
                task.results.append(r)
        except Exception as e:
            with lock:
                task.errors.append({"resume_id": item.resume_id, "error": f"{type(e).__name__}: {e}"})
        finally:
            with lock:
                task.done += 1

    try:
        workers = max(1, min(config.CONCURRENCY, len(items)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))
        task.results.sort(key=lambda r: r["final_score"], reverse=True)
        task.status = "cancelled" if task.cancel else "done"
    except Exception as e:                                  # kutilmagan xato
        task.status = "error"
        task.message = f"{type(e).__name__}: {e}"
    finally:
        task.finished_at = time.time()


# --- tizim endpointlari --------------------------------------------------------
@api.get("/health", tags=["tizim"], summary="Tizim holati")
def health() -> dict[str, Any]:
    p = pool()
    ok = p.healthy()
    with _tasks_lock:
        running = sum(1 for t in _tasks.values() if t.status in {"queued", "running"})
    return {
        "status": "ok" if ok else "llm_unavailable",
        "model": config.MODEL_ALIAS,
        "endpoints_total": len(p.endpoints),
        "endpoints_up": len(ok),
        "endpoints": {ep: ("up" if ep in ok else "down") for ep in p.endpoints},
        "slots_total": config.TOTAL_SLOTS,
        "slots_reserved_for_interactive": config.RESERVED_SLOTS,
        "batch_concurrency": config.BATCH_CONCURRENCY,
        "load": p.load_stats(),
        "auth_required": bool(API_KEY),
        "active_tasks": running,
        # --- VOICE:BEGIN ---
        "voice_enabled": config.VOICE_ENABLED,
        # --- VOICE:END ---
    }


@api.get("/rubric", tags=["tizim"], summary="Baholash rubrikasi va chegaralar")
def rubric() -> dict[str, Any]:
    """Ball qanday hisoblanishini ochib beradi — integratsiya qiluvchi uchun."""
    return {
        "criteria": [
            {"id": c.id, "label": c.label, "default_weight": c.weight, "guidance": c.guidance}
            for c in DEFAULT_CRITERIA
        ],
        "verdict_bands": [
            {"min_score": t, "verdict": v, "label": lbl} for t, v, lbl in VERDICT_BANDS
        ],
        "must_have_penalty": {
            "not_met": {
                "factor": NOT_MET_FACTOR,
                "extra_per_additional": NOT_MET_EXTRA,
                "floor": NOT_MET_FLOOR,
            },
            "partial_cost": PARTIAL_COST,
            "unknown_cost": UNKNOWN_COST,
            "soft_floor": SOFT_FLOOR,
        },
        "formula": "final_score = raw_weighted_score * must_have_factor",
        "generation": {
            "temperature": config.TEMPERATURE,
            "seed": config.SEED,
            "note": "Continuous batching sababli bir xil resume uchun ±2-3 ball farq bo'lishi normal",
        },
    }


@api.get("/jobs", tags=["tizim"], summary="Vakansiyalar ro'yxati")
def jobs() -> list[dict[str, Any]]:
    out = []
    for p in list_jobs(config.JOBS_DIR):
        try:
            j = load_job(p)
            out.append({
                "job_id": p.stem,
                "title": j.title,
                "level": j.level,
                "company": j.company,
                "domain": j.domain,
                "min_years": j.min_years,
                "must_have_count": len(j.must_have),
            })
        except Exception as e:
            out.append({"job_id": p.stem, "error": str(e)})
    return out


@api.get("/jobs/{job_id}", tags=["tizim"], summary="Vakansiya tafsiloti")
def job_detail(job_id: str) -> dict[str, Any]:
    return _job_by_id(job_id).to_dict()


# --- bitta resume --------------------------------------------------------------
SCORE_EXAMPLES = {
    "bekenddan": {
        "summary": "Tashqi bekenddan — vakansiya so'rov ichida",
        "description": "Serverda YAML fayl kerak emas. Vakansiya sizning "
                       "bazangizdan to'g'ridan-to'g'ri keladi.",
        "value": {
            "resume_id": "hr-4821",
            "resume_text": "NILUFAR ERGASHEVA\nKorporativ yurist\n6 yillik tajriba...",
            "mode": "screen",
            "job": {
                "title": "Korporativ yurist",
                "description": "Kompaniya shartnomalarini yuritadigan va sudda "
                               "manfaatni himoya qiladigan yurist",
                "requirements": ["Shartnoma huquqi", "Mehnat qonunchiligi"],
                "must_have": [
                    "Yuridik yo'nalishda oliy ma'lumot",
                    "Kamida 3 yil yurist sifatida ishlash tajribasi",
                    "Xo'jalik shartnomalarini mustaqil tuzish tajribasi",
                ],
                "nice_to_have": ["Advokat guvohnomasi"],
                "min_years": 3,
                "education": "Yuridik yo'nalishda oliy ma'lumot",
            },
        },
    },
    "fayldan": {
        "summary": "Lokal fayldan — data/jobs ichidagi vakansiya",
        "description": "Vakansiya serverda YAML fayl sifatida saqlangan bo'lsa.",
        "value": {
            "job_id": "fizika-oqituvchi",
            "resume_text": "GULNORA RASHIDOVA\nFizika o'qituvchisi\n9 yillik tajriba...",
            "mode": "screen",
            "resume_id": "gulnora",
        },
    },
    "toliq_tahlil": {
        "summary": "To'liq tahlil — dalil sitatalari va intervyu savollari bilan",
        "description": "mode=full: sekinroq (~110 s), lekin har ball ostida "
                       "resumedan sitata keltiriladi.",
        "value": {
            "resume_id": "hr-4821",
            "resume_text": "...",
            "mode": "full",
            "markdown": True,
            "job": {"title": "Korporativ yurist", "must_have": ["Yuridik diplom"]},
        },
    },
}


@api.post("/score", tags=["baholash"], summary="Resume matnini baholash",
          dependencies=[Depends(require_api_key)])
def score(req: Annotated[ScoreRequest, Body(openapi_examples=SCORE_EXAMPLES)]) -> dict[str, Any]:
    job = _resolve_job(req.job_id, req.job)
    scorer = ResumeScorer(job, pool(), mode=req.mode)
    try:
        result = scorer.score_text(req.resume_text, resume_id=req.resume_id)
    except LLMError as e:
        raise HTTPException(503, f"Model javob bermadi: {e}")
    if req.markdown:
        result["markdown"] = markdown_detail(result)
    return result


@api.post("/score/upload", tags=["baholash"], summary="Resume faylini baholash (PDF/DOCX/TXT)",
          dependencies=[Depends(require_api_key)])
async def score_upload(
    file: UploadFile = File(..., description="PDF, DOCX, TXT yoki MD fayl"),
    job_id: str | None = Form(None),
    mode: Literal["screen", "full"] = Form("full"),
    markdown: bool = Form(False),
) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fayl {MAX_UPLOAD_MB} MB dan katta")

    job = _resolve_job(job_id, None)
    suffix = Path(file.filename or "resume.txt").suffix or ".txt"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            text = extract_text(tmp.name)
        except ExtractError as e:
            raise HTTPException(422, str(e))

    scorer = ResumeScorer(job, pool(), mode=mode)
    try:
        result = scorer.score_text(
            text, resume_id=Path(file.filename or "resume").stem, source=file.filename
        )
    except LLMError as e:
        raise HTTPException(503, f"Model javob bermadi: {e}")
    if markdown:
        result["markdown"] = markdown_detail(result)
    return result


# --- batch (asinxron) ----------------------------------------------------------
@api.post("/score/batch", tags=["batch"], response_model=TaskCreated, status_code=202,
          summary="Ko'p resumeni baholashni boshlash",
          dependencies=[Depends(require_api_key)])
def score_batch(req: BatchRequest) -> TaskCreated:
    """Darhol `task_id` qaytaradi. Natijani `GET /tasks/{task_id}` dan oling."""
    if len(req.items) > MAX_BATCH:
        raise HTTPException(413, f"Bir so'rovda ko'pi bilan {MAX_BATCH} ta resume")
    ids = [i.resume_id for i in req.items]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "`resume_id` qiymatlari takrorlanmasligi kerak")

    job = _resolve_job(req.job_id, req.job)
    _gc_tasks()
    task = Task(
        id=uuid.uuid4().hex[:12],
        job_id=job.id,
        job_title=job.title,
        mode=req.mode,
        total=len(req.items),
    )
    with _tasks_lock:
        _tasks[task.id] = task
    _executor.submit(_run_batch, task, job, req.items)
    return TaskCreated(
        task_id=task.id, status=task.status, total=task.total, poll_url=f"/tasks/{task.id}"
    )


@api.post("/score/batch/upload", tags=["batch"], status_code=202,
          summary="Ko'p faylni yuklab baholashni boshlash",
          dependencies=[Depends(require_api_key)])
async def score_batch_upload(
    files: list[UploadFile] = File(..., description="Bir nechta PDF/DOCX/TXT fayl"),
    job_id: str = Form(...),
    mode: Literal["screen", "full"] = Form("screen"),
) -> dict[str, Any]:
    if len(files) > MAX_BATCH:
        raise HTTPException(413, f"Ko'pi bilan {MAX_BATCH} ta fayl")
    job = _resolve_job(job_id, None)

    items: list[BatchItem] = []
    skipped: list[dict[str, str]] = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            skipped.append({"file": f.filename or "?", "error": f"{MAX_UPLOAD_MB} MB dan katta"})
            continue
        suffix = Path(f.filename or "resume.txt").suffix or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(raw)
            tmp.flush()
            try:
                items.append(BatchItem(
                    resume_id=Path(f.filename or "resume").stem,
                    resume_text=extract_text(tmp.name),
                ))
            except (ExtractError, ValueError) as e:
                skipped.append({"file": f.filename or "?", "error": str(e)})

    if not items:
        raise HTTPException(422, {"message": "Hech bir fayldan matn chiqmadi", "skipped": skipped})

    _gc_tasks()
    task = Task(id=uuid.uuid4().hex[:12], job_id=job.id, job_title=job.title,
                mode=mode, total=len(items))
    with _tasks_lock:
        _tasks[task.id] = task
    _executor.submit(_run_batch, task, job, items)
    return {
        "task_id": task.id, "status": task.status, "total": task.total,
        "poll_url": f"/tasks/{task.id}", "skipped": skipped,
    }


@api.get("/tasks", tags=["batch"], summary="Tasklar ro'yxati",
         dependencies=[Depends(require_api_key)])
def tasks_list() -> list[dict[str, Any]]:
    with _tasks_lock:
        items = list(_tasks.values())
    items.sort(key=lambda t: t.created_at, reverse=True)
    return [t.public(include_results=False) for t in items]


@api.get("/tasks/{task_id}", tags=["batch"], summary="Task holati va natijasi",
         dependencies=[Depends(require_api_key)])
def task_status(task_id: str, markdown: bool = False) -> dict[str, Any]:
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(404, f"Task topilmadi: {task_id} (eskirgan bo'lishi mumkin)")
    out = task.public()
    if markdown and task.status == "done":
        out["markdown"] = markdown_report(task.job_title, task.results, task.errors)
    return out


@api.delete("/tasks/{task_id}", tags=["batch"], summary="Taskni bekor qilish / o'chirish",
            dependencies=[Depends(require_api_key)])
def task_cancel(task_id: str) -> dict[str, Any]:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            raise HTTPException(404, f"Task topilmadi: {task_id}")
        if task.status in {"queued", "running"}:
            task.cancel = True
            return {"task_id": task_id, "status": "cancelling"}
        _tasks.pop(task_id, None)
    return {"task_id": task_id, "status": "deleted"}


# --- veb interfeys -------------------------------------------------------------
@api.get("/", tags=["interfeys"], include_in_schema=False, response_class=HTMLResponse)
def ui() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>Interfeys fayli topilmadi</h1>"
            f"<p>Kutilgan joy: <code>{index}</code></p>"
            '<p>API hujjati: <a href="/docs">/docs</a></p>',
            status_code=500,
        )
    return HTMLResponse(index.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


# =============================================================================
# INTERVYU
# =============================================================================
class SessionCreate(BaseModel):
    interview_id: str | None = Field(
        None, description="data/interviews ichidagi fayl nomi (lokal bank)")
    interview: InterviewIn | None = Field(
        None,
        description="Vakansiya uchun savollar to'plami — bekenddan. Har savolda "
                    "`answer` (to'g'ri javob) yoki `expected` bo'lishi shart.")
    candidate_name: str = Field("", max_length=120)
    candidate_id: str = Field("", max_length=120, description="o'z bazangizdagi id")
    job_id: str | None = Field(None, description="vakansiyani fayldan bog'lash")
    job: JobIn | None = Field(
        None, description="vakansiyani to'g'ridan-to'g'ri berish (kontekst uchun)")
    count: int | None = Field(None, ge=1, le=50, description="nechta savol berilsin")
    seed: int | None = Field(None, description="aynan shu savollar to'plamini takrorlash uchun")


class AnswersIn(BaseModel):
    answers: dict[str, str] = Field(..., description="{savol_id: javob matni}")


def _interview_by_id(iid: str) -> InterviewSpec:
    for p in list_interviews(config.INTERVIEWS_DIR):
        if p.stem == iid:
            try:
                return load_interview(p)
            except InterviewError as e:
                raise HTTPException(422, f"Savol banki noto'g'ri: {e}")
    raise HTTPException(404, f"Intervyu topilmadi: '{iid}'. Ro'yxat: GET /interviews")


def _job_title_for(spec: InterviewSpec) -> str:
    if not spec.job_id:
        return spec.title
    try:
        return _job_by_id(spec.job_id).title
    except HTTPException:
        return spec.title


def _session_or_404(sid: str) -> dict[str, Any]:
    try:
        sess = _sessions.load(sid)
    except InterviewError as e:
        raise HTTPException(422, str(e))
    if sess is None:
        raise HTTPException(404, f"Sessiya topilmadi: {sid}")
    return sess


def _candidate_view(sess: dict[str, Any]) -> dict[str, Any]:
    """Nomzodga ko'rsatiladigan ko'rinish — kutilgan javoblar OLIB TASHLANADI."""
    out = {k: v for k, v in sess.items() if k != "result"}
    out["questions"] = [
        {"id": q["id"], "text": q["text"], "topic": q.get("topic", ""),
         "difficulty": q.get("difficulty", "")}
        for q in sess.get("questions", [])
    ]
    return out


@api.get("/interviews", tags=["intervyu"], summary="Savol banklari ro'yxati")
def interviews() -> list[dict[str, Any]]:
    out = []
    for p in list_interviews(config.INTERVIEWS_DIR):
        try:
            spec = load_interview(p)
            out.append({
                "interview_id": p.stem,
                "title": spec.title,
                "job_id": spec.job_id,
                "description": spec.description,
                "question_count": len(spec.questions),
                "default_count": spec.default_count or len(spec.questions),
                "topics": spec.topics,
                "pass_score": spec.pass_score,
                "time_limit_min": spec.time_limit_min,
            })
        except Exception as e:
            out.append({"interview_id": p.stem, "error": str(e)})
    return out


@api.get("/interviews/{interview_id}", tags=["intervyu"],
         summary="Savol banki (kutilgan javoblar bilan — faqat HR uchun)",
         dependencies=[Depends(require_api_key)])
def interview_detail(interview_id: str) -> dict[str, Any]:
    """DIQQAT: javobda `answer` (to'g'ri javob) va `expected` bor — nomzodga ko'rsatilmasin."""
    return _interview_by_id(interview_id).to_dict(include_answers=True)


SESSION_EXAMPLES = {
    "bekenddan": {
        "summary": "Tashqi bekenddan — savollar to'g'ri javoblari bilan",
        "description": "HR vakansiya uchun savol va javoblarni sizning "
                       "bazangizga kiritgan. Shu ko'rinishda yuboring — serverda "
                       "fayl kerak emas. Javobda savollar TO'G'RI JAVOBLARSIZ "
                       "qaytadi, shuning uchun uni nomzodga berish xavfsiz.",
        "value": {
            "candidate_name": "Nilufar Ergasheva",
            "candidate_id": "hr-4821",
            "job": {
                "title": "Korporativ yurist",
                "description": "Kompaniya shartnomalarini yuritadigan yurist",
                "must_have": ["Yuridik yo'nalishda oliy ma'lumot"],
            },
            "interview": {
                "title": "Korporativ yurist — intervyu",
                "pass_score": 65,
                "questions": [
                    {
                        "id": "s1",
                        "topic": "Shartnoma huquqi",
                        "difficulty": "o'rta",
                        "text": "Shartnoma ekspertizasida nimaga e'tibor berasiz?",
                        "answer": "Avval imzolovchining vakolatini tekshiraman — "
                                  "ustav yoki ishonchnoma bo'yicha. Keyin predmet "
                                  "aniqligini, to'lov shartlari va muddatlarni, "
                                  "javobgarlik va nizolarni hal qilish tartibini.",
                        "expected": ["imzolovchi vakolati", "predmet aniqligi",
                                     "to'lov shartlari", "javobgarlik"],
                    },
                    {
                        "id": "s2",
                        "topic": "Mehnat huquqi",
                        "difficulty": "qiyin",
                        "text": "Xodimni 'yaramaydi' deb bo'shatish so'ralsa nima deysiz?",
                        "answer": "Bunday baho qonuniy asos emas. Asos hujjat bilan "
                                  "tasdiqlanishi kerak, xodimdan yozma tushuntirish "
                                  "olinadi, himoyalangan toifalar tekshiriladi. "
                                  "Tartib buzilsa xodim sudda ishga tiklanadi.",
                    },
                ],
            },
        },
    },
    "fayldan": {
        "summary": "Lokal savol bankidan",
        "description": "Savollar serverda YAML fayl sifatida saqlangan bo'lsa. "
                       "Bankdan `count` ta savol mavzular bo'yicha tanlanadi.",
        "value": {
            "interview_id": "matematika-oqituvchi",
            "candidate_name": "Shahnoza Qodirova",
            "count": 6,
        },
    },
}


@api.post("/interview/sessions", tags=["intervyu"], status_code=201,
          summary="Sessiya ochish — nomzod javob berishni boshlashi uchun",
          dependencies=[Depends(require_api_key)])
def session_create(
    req: Annotated[SessionCreate, Body(openapi_examples=SESSION_EXAMPLES)],
) -> dict[str, Any]:
    """Javobda kutilgan javoblar BERILMAYDI — bu ko'rinishni nomzodga berish xavfsiz."""
    spec = _resolve_interview(req.interview_id, req.interview)
    seed = req.seed if req.seed is not None else int(time.time() * 1000) % 2**31
    questions = select_questions(spec, req.count, seed)
    _sessions.gc(config.SESSION_TTL_S)

    # Vakansiya nomi: so'rovda berilgan bo'lsa undan, bo'lmasa bankdan
    if req.job or req.job_id:
        job_title = _resolve_job(req.job_id, req.job).title
    else:
        job_title = _job_title_for(spec)

    sess = {
        "session_id": _sessions.new_id(),
        "interview_id": spec.id,
        "interview_title": spec.title,
        "job_id": spec.job_id or (req.job_id or ""),
        "job_title": job_title,
        "pass_score": spec.pass_score,
        "time_limit_min": spec.time_limit_min,
        "candidate": {"name": req.candidate_name, "id": req.candidate_id},
        "status": "open",
        "seed": seed,
        "created_at": time.time(),
        "submitted_at": None,
        # to'liq savol obyektlari (kutilgan javoblar bilan) — serverda saqlanadi
        "questions": [
            {"id": q.id, "text": q.text, "topic": q.topic, "difficulty": q.difficulty,
             "weight": q.weight, "expected": q.expected, "answer": q.answer}
            for q in questions
        ],
        "answers": {},
        "result": None,
    }
    _sessions.save(sess)
    return _candidate_view(sess)


class EvaluateQuestion(BaseModel):
    """Bitta savol + to'g'ri javob + nomzod javobi."""
    id: str = Field(..., max_length=64)
    text: str = Field(..., min_length=3)
    candidate_answer: str = Field("", description="nomzod javobi; bo'sh = 0 ball")
    answer: str = Field("", description="HR/bekend yozgan to'g'ri javob")
    expected: list[str] = Field(default_factory=list, description="kutilgan asosiy fikrlar")
    topic: str = ""
    difficulty: str = Field("o'rta", description="oson | o'rta | qiyin")
    weight: float | None = Field(None, gt=0, description="berilmasa qiyinlikdan olinadi")


class EvaluateRequest(BaseModel):
    questions: list[EvaluateQuestion] = Field(..., min_length=1, max_length=50)
    job_title: str = Field("", max_length=200)
    candidate_name: str = Field("", max_length=120)
    pass_score: float = Field(60.0, ge=0, le=100)
    summary: bool = Field(True, description="umumiy xulosa ham kerakmi (bitta qo'shimcha chaqiruv)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_title": "Korporativ yurist",
                "candidate_name": "Nilufar Ergasheva",
                "pass_score": 65,
                "questions": [
                    {
                        "id": "s1",
                        "topic": "Shartnoma huquqi",
                        "difficulty": "o'rta",
                        "text": "Shartnoma ekspertizasida nimaga e'tibor berasiz?",
                        "answer": "Avval imzolovchining vakolatini tekshiraman, "
                                  "keyin predmet aniqligini, to'lov shartlari va "
                                  "muddatlarni, javobgarlik va nizolarni hal "
                                  "qilish tartibini ko'raman.",
                        "expected": ["imzolovchi vakolati", "predmet aniqligi"],
                        "candidate_answer": "Vakolatni tekshiraman, predmet va "
                                            "to'lov shartlarini ko'raman.",
                    },
                    {
                        "id": "s2",
                        "topic": "Mehnat huquqi",
                        "difficulty": "qiyin",
                        "text": "Xodimni 'yaramaydi' deb bo'shatish so'ralsa nima deysiz?",
                        "answer": "Bunday baho qonuniy asos emas — hujjat bilan "
                                  "tasdiqlanishi va yozma tushuntirish olinishi kerak.",
                        "candidate_answer": "",
                    },
                ],
            }
        }
    }


@api.post("/interview/evaluate", tags=["intervyu"],
          summary="Savol + to'g'ri javob + nomzod javobini bir so'rovda baholash",
          dependencies=[Depends(require_api_key)])
def interview_evaluate(req: EvaluateRequest) -> dict[str, Any]:
    """Sessiyasiz baholash — bekend o'z holatini o'zi yuritadigan holat uchun.

    Bekend savollarni, to'g'ri javoblarni va nomzod javoblarini bir so'rovda
    yuboradi, javobda YAKUNIY BITTA ball va har savol bo'yicha tahlil qaytadi.
    Serverda hech narsa saqlanmaydi.

    Savollar parallel baholanadi — 6-8 savol odatda 40-60 sekundda tugaydi.
    """
    ids = [q.id for q in req.questions]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "`id` qiymatlari takrorlanmasligi kerak")

    try:
        questions = [
            Question(id=q.id, text=q.text, topic=q.topic, difficulty=q.difficulty,
                     weight=q.weight, answer=q.answer, expected=q.expected)
            for q in req.questions
        ]
    except InterviewError as e:
        raise HTTPException(422, str(e))

    spec = InterviewSpec(title=req.job_title or "Intervyu", id="evaluate",
                         pass_score=req.pass_score, questions=questions)
    answers = {q.id: q.candidate_answer for q in req.questions}

    scorer = InterviewScorer(spec, pool(), job_title=req.job_title)
    try:
        result = scorer.score_session(questions, answers, with_summary=req.summary)
    except LLMError as e:
        raise HTTPException(503, f"Model javob bermadi: {e}")

    result["job_title"] = req.job_title
    result["candidate"] = {"name": req.candidate_name, "id": ""}
    return result


@api.get("/interview/sessions", tags=["intervyu"], summary="Sessiyalar ro'yxati",
         dependencies=[Depends(require_api_key)])
def session_list(limit: int = 100) -> list[dict[str, Any]]:
    return _sessions.list(limit)


@api.get("/interview/sessions/{sid}", tags=["intervyu"],
         summary="Sessiya holati (nomzod ko'rinishi)")
def session_get(sid: str, hr: bool = False,
                x_api_key: str | None = Header(None, alias="X-API-Key")) -> dict[str, Any]:
    """`hr=true` — kutilgan javoblar va to'liq natija bilan (API kalit talab qilinadi)."""
    sess = _session_or_404(sid)
    if hr:
        require_api_key(x_api_key)
        return sess
    return _candidate_view(sess)


@api.put("/interview/sessions/{sid}/answers", tags=["intervyu"],
         summary="Javoblarni saqlash (baholamasdan)")
def session_save_answers(sid: str, body: AnswersIn) -> dict[str, Any]:
    """Nomzod yozayotgan javoblarni oraliqda saqlash uchun (qoralama)."""
    sess = _session_or_404(sid)
    if sess["status"] != "open":
        raise HTTPException(409, f"Sessiya yopilgan (status: {sess['status']})")
    valid = {q["id"] for q in sess["questions"]}
    unknown = set(body.answers) - valid
    if unknown:
        raise HTTPException(422, f"Noma'lum savol id: {sorted(unknown)}")
    sess["answers"].update({k: str(v) for k, v in body.answers.items()})
    _sessions.save(sess)
    return {"session_id": sid, "saved": len(sess["answers"]), "total": len(valid)}


@api.post("/interview/sessions/{sid}/submit", tags=["intervyu"],
          summary="Javoblarni yuborish va baholash")
def session_submit(sid: str, body: AnswersIn | None = None) -> dict[str, Any]:
    """Javoblarni baholaydi va YAKUNIY BITTA ball qaytaradi.

    Har bir savol alohida, parallel baholanadi — 8 savolli intervyu odatda
    20-30 sekundda tugaydi. Javob berilmagan savol modelga yuborilmaydi
    (0 ball oladi).
    """
    sess = _session_or_404(sid)
    if sess["status"] == "evaluated":
        return sess["result"]
    if body and body.answers:
        valid = {q["id"] for q in sess["questions"]}
        unknown = set(body.answers) - valid
        if unknown:
            raise HTTPException(422, f"Noma'lum savol id: {sorted(unknown)}")
        sess["answers"].update({k: str(v) for k, v in body.answers.items()})

    if not any(is_answered(v) for v in sess["answers"].values()):
        raise HTTPException(422, "Birorta ham javob yozilmagan")

    # Savollar SESSIYANING O'ZIDAN tiklanadi, fayldan emas. Ikki sababga ko'ra:
    #   * inline intervyuda (bekend yuborgan) qayta o'qiladigan fayl yo'q;
    #   * fayl sessiya ochilgandan keyin o'zgarsa ham, nomzod ko'rgan savollar
    #     bo'yicha baholanadi — bu to'g'rirog'i.
    try:
        questions = [Question(**{k: v for k, v in q.items()
                                 if k in Question.__dataclass_fields__})
                     for q in sess["questions"]]
    except InterviewError as e:
        raise HTTPException(500, f"Sessiyadagi savollar buzilgan: {e}")

    spec = InterviewSpec(
        title=sess.get("interview_title", "Intervyu"),
        id=sess.get("interview_id", "session"),
        pass_score=float(sess.get("pass_score", 60.0)),
        questions=questions,
    )
    scorer = InterviewScorer(spec, pool(), job_title=sess.get("job_title", ""))
    try:
        result = scorer.score_session(questions, sess["answers"])
    except LLMError as e:
        raise HTTPException(503, f"Model javob bermadi: {e}")

    result["session_id"] = sid
    result["interview_id"] = sess["interview_id"]
    result["interview_title"] = sess["interview_title"]
    result["job_title"] = sess.get("job_title", "")
    result["candidate"] = sess["candidate"]

    sess["status"] = "evaluated"
    sess["submitted_at"] = time.time()
    sess["result"] = result
    _sessions.save(sess)
    return result


@api.get("/interview/sessions/{sid}/result", tags=["intervyu"],
         summary="Baholangan natija", dependencies=[Depends(require_api_key)])
def session_result(sid: str) -> dict[str, Any]:
    sess = _session_or_404(sid)
    if not sess.get("result"):
        raise HTTPException(409, f"Sessiya hali baholanmagan (status: {sess['status']})")
    return sess["result"]


@api.delete("/interview/sessions/{sid}", tags=["intervyu"], summary="Sessiyani o'chirish",
            dependencies=[Depends(require_api_key)])
def session_delete(sid: str) -> dict[str, Any]:
    if not _sessions.delete(sid):
        raise HTTPException(404, f"Sessiya topilmadi: {sid}")
    return {"session_id": sid, "status": "deleted"}


# --- VOICE:BEGIN -------------------------------------------------------------
# Ovozli intervyu routeri. `RA_VOICE=0` bo'lsa umuman ulanmaydi — endpointlar
# ham, interfeysdagi tugmalar ham paydo bo'lmaydi.
# Butunlay olib tashlash: scripts/remove_voice.sh
if config.VOICE_ENABLED:
    from .voice_api import router as voice_router
    api.include_router(voice_router)
# --- VOICE:END ---------------------------------------------------------------


@api.get("/samples", tags=["interfeys"], include_in_schema=False)
def samples_list() -> list[dict[str, Any]]:
    """data/resumes ichidagi namuna resumelar (interfeysda sinab ko'rish uchun)."""
    out = []
    for p in sorted(config.RESUMES_DIR.glob("*.txt")):
        out.append({"id": p.stem, "size": p.stat().st_size})
    return out


@api.get("/samples/{name}", tags=["interfeys"], include_in_schema=False,
         response_class=PlainTextResponse)
def sample_text(name: str) -> PlainTextResponse:
    """Namuna resume matnini qaytaradi.

    Xavfsizlik: `name` to'g'ridan-to'g'ri yo'lga qo'shilmaydi — katalogdagi
    mavjud fayllar ro'yxatidan aniq moslik bo'yicha tanlanadi, shuning uchun
    "../" kabi yo'l traversali ishlamaydi.
    """
    allowed = {p.stem: p for p in config.RESUMES_DIR.glob("*.txt")}
    path = allowed.get(name)
    if path is None:
        raise HTTPException(404, f"Namuna topilmadi: {name}")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@api.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"Model xatosi: {exc}"})
