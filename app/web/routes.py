"""Экраны раздела 2 плана: главная, загрузка, кандидаты, карточка, настройки."""

import re
import tempfile
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from starlette.datastructures import UploadFile

from app import config, db, evaluate, parse
from app.importer.mapping import FIELDS
from app.importer.normalize import looks_like_name
from app.importer.pipeline import documents, load_table, new_batch, start_import
from app.jobs import enqueue
from app.llm import LLMError, get_llm
from app.models import Candidate, Duplicate, ImportBatch, Job, Match, Vacancy
from app.search import Filters, search
from app.web import present

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["ru_date"] = lambda d: d.strftime("%d.%m.%Y") if d else "—"
templates.env.filters["short_date"] = present.short_date
templates.env.filters["iso_short"] = lambda v: present.short_date(date.fromisoformat(v))
templates.env.filters["num"] = present.num
templates.env.filters["job_status"] = present.job_status
templates.env.filters["stale_label"] = present.stale_label
templates.env.globals["PARSE_STATUS"] = present.PARSE_STATUS
templates.env.globals["problem_summary"] = present.problem_summary
templates.env.globals["remaining"] = present.remaining
templates.env.globals["plural"] = present.plural
templates.env.globals["RELOCATION"] = parse.RELOCATION
templates.env.globals["EDITABLE"] = parse.EDITABLE
POSITION_RE = re.compile(r"^(?:желаемая\s+)?должность\s*:\s*(.+)$", re.IGNORECASE)
TRIAL = 20


def headline(raw_text: str) -> str:
    """Должность для списка до разбора: строка «Должность: …» или первая содержательная."""
    lines = [ln.strip(" #*_\t") for ln in (raw_text or "").splitlines() if ln.strip(" #*_\t")]
    for line in lines:
        if match := POSITION_RE.match(line):
            return match.group(1)[:100]
    for line in lines[:6]:
        if ":" not in line and not looks_like_name(line) and len(line) < 100:
            return line
    return ""


def current_job(c: Candidate) -> tuple[str, str]:
    """Должность и компания из разбора, до разбора — строка из текста."""
    parsed = c.parsed or {}
    positions = parsed.get("positions") or []
    now = next((p for p in positions if p.get("is_current")), positions[0] if positions else {})
    title = parsed.get("desired_position") or now.get("title") or headline(c.raw_text)
    return title or "", now.get("company") or ""


templates.env.filters["headline"] = headline
templates.env.filters["current_job"] = current_job


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def active():
    return Candidate.duplicate_of.is_(None)


def latest(s, kind: str) -> Job | None:
    return s.scalar(select(Job).where(Job.kind == kind).order_by(Job.id.desc()).limit(1))


def pipeline(s) -> dict:
    """Этапы обработки базы для карточки прогресса."""
    jobs = {kind: latest(s, kind) for kind in ("import", "parse", "embed")}

    def state(job):
        if job is None:
            return "ждёт"
        return {"done": "готово", "failed": "не получилось", "paused": "приостановлено"}.get(
            job.status, "идёт"
        )

    stages = [
        ("Читаем файлы", state(jobs["import"])),
        ("Приводим в порядок", state(jobs["import"])),
        ("Разбираем резюме", state(jobs["parse"])),
        ("Считаем смысловые отпечатки", state(jobs["embed"])),
    ]
    live = next(
        (
            j
            for j in (jobs["parse"], jobs["embed"])
            if j and j.status in ("queued", "running", "paused")
        ),
        None,
    )
    return {"jobs": jobs, "stages": stages, "live": live}


def parse_offer(s) -> dict:
    count = lambda *w: s.scalar(select(func.count(Candidate.id)).where(active(), *w))  # noqa: E731
    return {
        "waiting": count(Candidate.parse_status == "new"),
        "parsed": count(Candidate.parse_status == "parsed"),
        "failed": count(Candidate.parse_status == "failed"),
        "trial": TRIAL,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.SessionLocal() as s:
        count = lambda *where: s.scalar(select(func.count(Candidate.id)).where(*where))  # noqa: E731
        stats = {
            "candidates": count(active()),
            "records": count(),
            "duplicates": s.scalar(
                select(func.count(Duplicate.id)).where(Duplicate.status == "merged")
            ),
            "stale": count(active(), Candidate.stale.is_(True)),
            **parse_offer(s),
        }
        main, old = aliased(Candidate), aliased(Candidate)
        merged = s.execute(
            select(Duplicate, main, old)
            .join(main, main.id == Duplicate.candidate_a)
            .join(old, old.id == Duplicate.candidate_b)
            .where(Duplicate.status == "merged")
            .order_by(Duplicate.id.desc())
            .limit(10)
        ).all()
        importing = list(
            s.scalars(
                select(Job).where(Job.kind == "import", Job.status.in_(("queued", "running")))
            )
        )
        with_errors = list(
            s.scalars(
                select(Job)
                .where(Job.kind == "import", Job.error.is_not(None))
                .order_by(Job.id.desc())
                .limit(5)
            )
        )
        flow = pipeline(s)
    return render(
        request,
        "home.html",
        stats=stats,
        merged=merged,
        running=importing,
        with_errors=with_errors,
        flow=flow,
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    with db.SessionLocal() as s:
        batches = list(s.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)))
        offer = parse_offer(s)
    return render(request, "upload.html", batches=batches, error=None, offer=offer)


@router.post("/upload")
async def upload(request: Request):
    form = await request.form()
    table = form.get("table")
    files = [f for key in ("resumes", "folder") for f in form.getlist(key)]
    files = [f for f in files if isinstance(f, UploadFile) and f.filename]
    has_table = isinstance(table, UploadFile) and bool(table.filename)
    if not has_table and not files:
        with db.SessionLocal() as s:
            batches = list(s.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)))
        return render(
            request,
            "upload.html",
            status_code=400,
            batches=batches,
            offer=None,
            error="Выберите выгрузку из CRM или файлы резюме.",
        )
    incoming = db.data_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=incoming) as tmp:
        tmp_path = Path(tmp)
        table_path = None
        if has_table:
            table_path = tmp_path / Path(table.filename).name
            table_path.write_bytes(await table.read())
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        for i, f in enumerate(files):
            name = Path(f.filename).name
            target = docs_dir / name
            if target.exists():
                target = docs_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            target.write_bytes(await f.read())
        docs = [p for p in docs_dir.iterdir() if p.suffix.lower() == ".zip"] + [docs_dir]
        with db.SessionLocal() as s:
            batch = new_batch(s, table_path, docs)
    return RedirectResponse(f"/upload/{batch.id}", status_code=303)


def _batch_job(s, batch_id: int) -> Job | None:
    q = select(Job).where(Job.kind == "import").order_by(Job.id.desc())
    return next((j for j in s.scalars(q) if j.payload.get("batch_id") == batch_id), None)


@router.get("/upload/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: int):
    with db.SessionLocal() as s:
        batch = s.get(ImportBatch, batch_id)
        if not batch:
            raise HTTPException(404)
        job = _batch_job(s, batch_id)
        offer = parse_offer(s) if job and job.status == "done" else None
    headers, rows = load_table(batch)
    columns = [
        {
            "index": i,
            "header": h,
            "field": batch.mapping[i] if i < len(batch.mapping) else "",
            "examples": [str(r[i]) for r in rows[:3] if i < len(r) and r[i] not in (None, "")],
        }
        for i, h in enumerate(headers)
    ]
    return render(
        request,
        "batch.html",
        batch=batch,
        job=job,
        columns=columns,
        rows_count=len(rows),
        docs_count=len(documents(batch)),
        fields=FIELDS,
        offer=offer,
    )


@router.post("/upload/{batch_id}/start")
async def batch_start(request: Request, batch_id: int):
    form = await request.form()
    with db.SessionLocal() as s:
        batch = s.get(ImportBatch, batch_id)
        if not batch:
            raise HTTPException(404)
        if not _batch_job(s, batch_id):
            headers, _ = load_table(batch)
            fields = [str(form.get(f"col_{i}", "")) for i in range(len(headers))]
            start_import(s, batch, [f if f in FIELDS else "" for f in fields])
    return RedirectResponse(f"/upload/{batch_id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_progress(request: Request, job_id: int):
    with db.SessionLocal() as s:
        job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    return render(request, "job.html", job=job)


# ── Разбор резюме: предложение, подтверждение, прогресс ─────────────────────────


def _scope_ids(s, scope: str, job_id: int | None = None) -> list[int]:
    if scope == "retry" and job_id:
        job = s.get(Job, job_id)
        ids = job.payload.get("ids", []) if job else []
        return list(
            s.scalars(
                select(Candidate.id).where(
                    Candidate.id.in_(ids), Candidate.parse_status == "failed"
                )
            )
        )
    return parse.waiting_ids(s, TRIAL if scope == "trial" else None)


@router.get("/parse", response_class=HTMLResponse)
def parse_confirm(request: Request, scope: str = "trial", job: int | None = None):
    with db.SessionLocal() as s:
        ids = _scope_ids(s, scope, job)
        est = parse.estimate(s, ids)
    return render(
        request,
        "parse_confirm.html",
        scope=scope,
        job_id=job,
        est=est,
        duration=present.duration(est["seconds_low"], est["seconds_high"]),
        price=present.rub_range(est["rub_low"], est["rub_high"]),
    )


@router.post("/parse")
async def parse_start(request: Request):
    form = await request.form()
    scope = str(form.get("scope", "trial"))
    job_id = int(form["job"]) if form.get("job") else None
    with db.SessionLocal() as s:
        ids = _scope_ids(s, scope, job_id)
        if ids:
            parse.start_parse(s, ids)
    return RedirectResponse("/", status_code=303)


@router.get("/parse/preview", response_class=HTMLResponse)
def parse_preview(request: Request):
    with db.SessionLocal() as s:
        c = s.scalar(
            select(Candidate)
            .where(active(), Candidate.parse_status == "new")
            .order_by(Candidate.id)
            .limit(1)
        ) or s.scalar(select(Candidate).where(active()).order_by(Candidate.id).limit(1))
    text = parse.model_input(c) if c else ""
    return render(request, "parse_preview.html", c=c, text=text)


@router.get("/progress", response_class=HTMLResponse)
def progress(request: Request):
    with db.SessionLocal() as s:
        flow = pipeline(s)
    return render(request, "progress.html", flow=flow)


@router.post("/jobs/{job_id}/pause")
def job_pause(job_id: int):
    with db.SessionLocal() as s:
        job = s.get(Job, job_id)
        if job and job.status in ("queued", "running"):
            job.status = "paused"
            s.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/jobs/{job_id}/resume")
def job_resume(job_id: int):
    with db.SessionLocal() as s:
        job = s.get(Job, job_id)
        if job and job.status == "paused":
            job.status = "queued"
            s.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/jobs/{job_id}/problems", response_class=HTMLResponse)
def job_problems(request: Request, job_id: int):
    with db.SessionLocal() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        rows = present.problems(job)
        names = {}
        if job.kind == "parse":
            ids = [int(what) for what, _ in rows if what.isdigit()]
            names = dict(
                s.execute(
                    select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(ids))
                ).all()
            )
    return render(request, "problems.html", job=job, rows=rows, names=names)


# ── Кандидаты ───────────────────────────────────────────────────────────────


def _number(value: str) -> float | None:
    try:
        return float(value.replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


@router.get("/candidates", response_class=HTMLResponse)
def candidates(
    request: Request,
    q: str = "",
    city: str = "",
    min_years: str = "",
    max_salary: str = "",
    fresh: str = "",
    sort: str = "meaning",
    page: int = 1,
):
    salary = _number(max_salary)
    filters = Filters(
        city=city,
        min_years=_number(min_years),
        max_salary=int(salary) if salary is not None else None,
        fresh=fresh if fresh in ("fresh", "stale") else "",
    )
    page = max(page, 1)
    params = {
        "q": q,
        "city": city,
        "min_years": min_years,
        "max_salary": max_salary,
        "fresh": fresh,
        "sort": sort,
    }
    with db.SessionLocal() as s:
        result = search(s, q, filters, sort, page)
        cities = list(
            s.scalars(
                select(Candidate.city)
                .where(active(), Candidate.city.is_not(None))
                .group_by(Candidate.city)
                .order_by(func.count(Candidate.id).desc())
                .limit(60)
            )
        )
    context = {
        "r": result,
        "chips": filters.active(),
        "page": page,
        "pages": max(1, -(-result.total // 20)),
        "cities": sorted(cities),
        "params": params,
        "without": lambda key: {**params, key: "", "page": ""},
        **params,
    }
    if request.headers.get("HX-Request"):
        return render(request, "candidates_rows.html", **context)
    return render(request, "candidates.html", **context)


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
def candidate(request: Request, candidate_id: int, v: int | None = None):
    """Карточка кандидата; `v` — вакансия, чьи доводы подсветить в резюме
    («Показать в резюме»): строки каждого довода получают якорь e-<требование>."""
    with db.SessionLocal() as s:
        c = s.get(Candidate, candidate_id)
        if not c:
            raise HTTPException(404)
        main = s.get(Candidate, c.duplicate_of) if c.duplicate_of else None
        merged = list(s.scalars(select(Candidate).where(Candidate.duplicate_of == c.id)))
        history = s.execute(
            select(Match, Vacancy)
            .join(Vacancy, Vacancy.id == Match.vacancy_id)
            .where(Match.candidate_id == c.id)
            .order_by(Match.evaluated_at.desc())
        ).all()
        shown = next((m for m, vac in history if vac.id == v), None)
    extra = [
        (f"e-{ch['requirement_id']}", ch["evidence_lines"])
        for ch in (shown.checks if shown else [])
        if ch.get("evidence_lines")
    ]
    source, anchors = present.marked_source(c, extra)
    return render(
        request,
        "candidate.html",
        c=c,
        p=c.parsed or {},
        main=main,
        merged=merged,
        source=source,
        anchors=anchors,
        history=[(m, vac, evaluate.category(m)) for m, vac in history],
        shown=shown,
        CATEGORIES=evaluate.CATEGORIES,
    )


@router.post("/candidates/{candidate_id}/reparse")
def candidate_reparse(candidate_id: int):
    with db.SessionLocal() as s:
        if not s.get(Candidate, candidate_id):
            raise HTTPException(404)
        parse.start_parse(s, [candidate_id])
    return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)


@router.post("/candidates/{candidate_id}/fix")
async def candidate_fix(request: Request, candidate_id: int):
    form = await request.form()
    field = str(form.get("field", ""))
    with db.SessionLocal() as s:
        c = s.get(Candidate, candidate_id)
        if not c or field not in parse.EDITABLE:
            raise HTTPException(404)
        try:
            parse.apply_edit(c, field, str(form.get("value", "")))
        except ValueError:
            raise HTTPException(400, "Нужно число") from None
        s.commit()
    return RedirectResponse(f"/candidates/{candidate_id}#f-{field}", status_code=303)


# ── Настройки ───────────────────────────────────────────────────────────────

SETTING_FIELDS = [
    "llm_provider",
    "llm_base_url",
    "llm_model_parse",
    "llm_model_eval",
    "price_parse_in",
    "price_parse_out",
    "price_eval_in",
    "price_eval_out",
    "usd_rub",
    "embed_model",
]


class Ping(BaseModel):
    ok: bool


def _settings_page(request: Request, message: str | None = None, error: bool = False):
    values = {key: config.get(key) for key in SETTING_FIELDS}
    return render(
        request,
        "settings.html",
        values=values,
        has_key=bool(config.get("llm_api_key")),
        message=message,
        error=error,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    return _settings_page(request)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    values = {key: str(form.get(key, "")).strip() for key in SETTING_FIELDS if key in form}
    if key := str(form.get("llm_api_key", "")).strip():
        values["llm_api_key"] = key  # пустое поле — ключ не меняется
    model_changed = values.get("embed_model") not in (None, config.get("embed_model"))
    config.save(values)
    if model_changed:  # отпечатки другой модели несравнимы — пересчитываем базу
        with db.SessionLocal() as s:
            enqueue(s, "embed", {})
            s.commit()
    return _settings_page(request, "Сохранено.")


@router.post("/settings/check", response_class=HTMLResponse)
def settings_check(request: Request):
    started = time.monotonic()
    try:
        get_llm("parse").complete_structured(
            Ping, "Проверка связи. Ответь ok = true.", "Проверка связи: ответь ok = true."
        )
    except LLMError as exc:
        return _settings_page(request, f"Не получилось: {exc}", error=True)
    except Exception as exc:  # адрес не тот, сервис недоступен — показываем как есть
        return _settings_page(request, f"Не получилось подключиться: {exc}", error=True)
    seconds = time.monotonic() - started
    return _settings_page(request, f"Подключение работает: ответ за {seconds:.1f} с.")


STUBS = {"/morning": "Утро"}


def _stub(title: str):
    def view(request: Request):
        return render(request, "stub.html", title=title)

    return view


for _path, _title in STUBS.items():
    router.add_api_route(_path, _stub(_title), response_class=HTMLResponse, methods=["GET"])
