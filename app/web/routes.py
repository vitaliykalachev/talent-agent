"""Экраны раздела 2 плана: главная, загрузка, кандидаты, карточка, настройки."""

import math
import re
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from starlette.datastructures import UploadFile

from app import config, db, evaluate, mail, morning, parse, schedule
from app.anonymize import anonymize
from app.importer.dedup import auto_merged, open_pairs
from app.importer.mapping import FIELDS, confidence
from app.importer.normalize import looks_like_name
from app.importer.pipeline import documents, load_table, new_batch, preview, start_import
from app.importer.readers import read_table
from app.jobs import enqueue
from app.llm import AuthError, get_llm
from app.models import Candidate, Duplicate, Feedback, ImportBatch, Job, Match, NightRun, Vacancy
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
templates.env.filters["batch_label"] = present.batch_label
templates.env.filters["count"] = present.count
templates.env.filters["years"] = present.years
templates.env.filters["phone"] = present.phone
templates.env.filters["cut"] = present.cut
templates.env.filters["import_done"] = present.import_done
templates.env.filters["settings_hint"] = present.settings_hint
templates.env.globals["reasons"] = present.reasons
templates.env.globals["network_errors"] = present.network_errors
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
            return present.cut(match.group(1), 100)
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
    """Предложение разобрать резюме; после пробного разбора — ссылка на его список."""
    count = lambda *w: s.scalar(select(func.count(Candidate.id)).where(active(), *w))  # noqa: E731
    last = latest(s, "parse")
    trial = last if last and last.status == "done" and last.payload.get("trial") else None
    return {
        "waiting": count(Candidate.parse_status == "new"),
        "parsed": count(Candidate.parse_status == "parsed"),
        "failed": count(Candidate.parse_status == "failed"),
        "trial": TRIAL,
        "trial_job": trial.id if trial else None,
        "trial_parsed": count(
            Candidate.parse_status == "parsed", Candidate.id.in_(trial.payload["ids"])
        )
        if trial
        else 0,
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
            "auto_merged": auto_merged(s),
            "possible": len(open_pairs(s)),
            "stale": count(active(), Candidate.stale.is_(True)),
            **parse_offer(s),
        }
        night = s.scalar(select(NightRun).order_by(NightRun.id.desc()).limit(1))
        tonight = morning.next_line(s)
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
        batch_names = present.batch_labels(s, [j.payload.get("batch_id") for j in with_errors])
    return render(
        request,
        "home.html",
        batch_names=batch_names,
        stats=stats,
        night=night,
        tonight=tonight,
        running=importing,
        with_errors=with_errors,
        flow=flow,
        has_key=bool(config.get("llm_api_key")) or config.get("llm_provider") == "mock",
    )


def history(s) -> list[dict]:
    """История загрузок с итогами: сколько строк прочитали, сколько новых кандидатов,
    сколько записей оказались дублями, сколько файлов не получилось прочитать."""
    rows = []
    for b in s.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(30)):
        mine = Candidate.batch_id == b.id
        job = _batch_job(s, b.id)
        rows.append(
            {
                "batch": b,
                "label": present.batch_label(b),
                "new": s.scalar(select(func.count(Candidate.id)).where(mine, active())),
                "merged": s.scalar(
                    select(func.count(Candidate.id)).where(
                        mine, Candidate.duplicate_of.is_not(None)
                    )
                ),
                "problems": len(present.problems(job)) if job else 0,
                "job": job,
            }
        )
    return rows


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    with db.SessionLocal() as s:
        batches = history(s)
        offer = parse_offer(s)
    return render(request, "upload.html", batches=batches, error=None, offer=offer)


def _upload_error(request: Request, error: str) -> HTMLResponse:
    with db.SessionLocal() as s:
        batches = history(s)
    return render(request, "upload.html", status_code=400, batches=batches, offer=None, error=error)


def broken_file(table: Path | None, docs: list[Path]) -> str | None:
    """Текст ошибки, если выгрузка или архив не открываются; None — всё читается.
    Проверка идёт до создания загрузки, чтобы битый файл не оставлял пустую загрузку."""
    if table:
        try:
            headers, rows = read_table(table)
        except Exception:  # BadZipFile, InvalidFileException, битые XML внутри XLSX
            return (
                f"Файл «{table.name}» не открылся: он повреждён или это не Excel. "
                "Сохраните выгрузку заново и загрузите ещё раз."
            )
        if not rows and not docs:
            return (
                f"В файле «{table.name}» нет строк с данными. Проверьте, что выгрузили нужный лист."
            )
    for path in docs:
        if path.suffix.lower() == ".zip" and not zipfile.is_zipfile(path):
            return (
                f"Архив «{path.name}» не открылся: он повреждён или это не ZIP. "
                "Упакуйте резюме заново и загрузите ещё раз."
            )
    return None


@router.post("/upload")
async def upload(request: Request):
    form = await request.form()
    table = form.get("table")
    files = [f for key in ("resumes", "folder") for f in form.getlist(key)]
    files = [f for f in files if isinstance(f, UploadFile) and f.filename]
    has_table = isinstance(table, UploadFile) and bool(table.filename)
    if not has_table and not files:
        return _upload_error(request, "Выберите выгрузку из CRM или файлы резюме.")
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
        loose = sorted(docs_dir.iterdir())
        if error := broken_file(table_path, loose):
            return _upload_error(request, error)
        docs = [p for p in loose if p.suffix.lower() == ".zip"] + [docs_dir]
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
        outcome = None if job else preview(s, headers, batch.mapping, rows)
    columns = [
        {
            "index": i,
            "header": h,
            "field": field,
            "label": confidence(h, field),
            "examples": [str(r[i]) for r in rows[:3] if i < len(r) and r[i] not in (None, "")],
        }
        for i, h in enumerate(headers)
        for field in [batch.mapping[i] if i < len(batch.mapping) else ""]
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
        outcome=outcome,
    )


@router.post("/upload/{batch_id}/preview", response_class=HTMLResponse)
async def batch_preview(request: Request, batch_id: int):
    """«Что получится» заново, когда рекрутёр поменял значение колонки (HTMX)."""
    form = await request.form()
    with db.SessionLocal() as s:
        batch = s.get(ImportBatch, batch_id)
        if not batch:
            raise HTTPException(404)
        headers, rows = load_table(batch)
        outcome = preview(s, headers, _fields(form, headers), rows)
    return render(request, "batch_outcome.html", outcome=outcome, docs_count=len(documents(batch)))


def _fields(form, headers: list[str]) -> list[str]:
    fields = [str(form.get(f"col_{i}", "")) for i in range(len(headers))]
    return [f if f in FIELDS else "" for f in fields]


@router.post("/upload/{batch_id}/start")
async def batch_start(request: Request, batch_id: int):
    form = await request.form()
    with db.SessionLocal() as s:
        batch = s.get(ImportBatch, batch_id)
        if not batch:
            raise HTTPException(404)
        if not _batch_job(s, batch_id):
            headers, _ = load_table(batch)
            skip = form.get("nameless") == "skip"
            start_import(s, batch, _fields(form, headers), skip_nameless=skip)
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
        duration=present.duration(est["seconds_low"], est["seconds_high"], True),
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
            job = parse.start_parse(s, ids)
            if scope == "trial" and job.payload["ids"] == ids:  # новая задача, не дописка
                job.payload = {**job.payload, "trial": True}
                s.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/parse/trial", response_class=HTMLResponse)
def parse_trial(request: Request, job: int):
    """«Посмотреть 20 разобранных»: как агент понял резюме пробного разбора."""
    with db.SessionLocal() as s:
        found = s.get(Job, job)
        if not found or found.kind != "parse":
            raise HTTPException(404)
        rows = list(
            s.scalars(
                select(Candidate)
                .where(Candidate.id.in_(found.payload.get("ids", [])))
                .order_by(Candidate.id)
            )
        )
        offer = parse_offer(s)
    return render(request, "parse_trial.html", rows=rows, offer=offer)


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


NUMBER_ERROR = "Укажите обычное число, например 3 или 150 000."


def _number(value: str) -> float | None:
    """Число фильтра; пусто или не число — None; inf, nan и больше миллиарда — 400."""
    try:
        number = float(value.replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(number) or abs(number) > 1e9:
        raise HTTPException(400, NUMBER_ERROR)
    return number


def _small_int(value: str, default: int | None) -> int | None:
    """Номер страницы или загрузки из адреса: не число — умолчание, огромное — 400."""
    value = str(value).strip()
    if not value.isdigit():
        return default
    if len(value) > 9:
        raise HTTPException(400, NUMBER_ERROR)
    return int(value)


@router.get("/candidates", response_class=HTMLResponse)
def candidates(
    request: Request,
    q: str = "",
    city: str = "",
    min_years: str = "",
    max_salary: str = "",
    fresh: str = "",
    batch: str = "",
    sort: str = "meaning",
    page: str = "1",
):
    salary = _number(max_salary)
    page = max(_small_int(page, 1), 1)
    params = {
        "q": q,
        "city": city,
        "min_years": min_years,
        "max_salary": max_salary,
        "fresh": fresh,
        "batch": batch,
        "sort": sort,
    }
    with db.SessionLocal() as s:
        batches = [
            (b.id, present.batch_label(b))
            for b in s.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()))
        ]
        batch_id = _small_int(batch, None)
        filters = Filters(
            city=city,
            min_years=_number(min_years),
            max_salary=int(salary) if salary is not None else None,
            fresh=fresh if fresh in ("fresh", "stale") else "",
            batch=batch_id,
            batch_label=dict(batches).get(batch_id, ""),
        )
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
        "batches": batches,
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
        undo = dict(
            s.execute(
                select(Duplicate.candidate_b, Duplicate.id).where(
                    Duplicate.candidate_a == c.id, Duplicate.status == "merged"
                )
            ).all()
        )
        pair = next((d.id for d, a, b in open_pairs(s) if c.id in (a.id, b.id)), None)
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
        undo=undo,
        pair=pair,
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
            raise HTTPException(400, "Нужно число не меньше нуля, например 12,5") from None
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
    "night_time",
    "night_days",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_to",
]
SAMPLE_NAME = "Петров Сергей Иванович"
SAMPLE = (
    "Петров Сергей Иванович\nТел.: +7 912 345-67-89, petrov.s@mail.ru\n"
    "Дата рождения: 12.03.1984\nЖелаемая должность: начальник цеха\n\nОпыт работы\n"
    "2019 — по н.в.: АО «Литейный завод», начальник цеха литья\n"
    "— руководил цехом из 40 человек, запустил участок литья под давлением\n"
    "Рекомендации: Смирнова Ольга, главный инженер, +7 903 111-22-33"
)


class Ping(BaseModel):
    ok: bool


def _sample() -> str:
    """Пример того, что уходит модели: первое резюме базы, в пустой базе — образец."""
    with db.SessionLocal() as s:
        c = s.scalar(
            select(Candidate)
            .where(active(), func.length(Candidate.raw_text) > 200)
            .order_by(Candidate.id)
            .limit(1)
        )
    if c:
        return parse.model_input(c)
    return anonymize(SAMPLE, SAMPLE_NAME, ["+79123456789"], ["petrov.s@mail.ru"])


def _memory() -> list:
    with db.SessionLocal() as s:
        return s.execute(
            select(Feedback, Vacancy.title)
            .outerjoin(Vacancy, Vacancy.id == Feedback.vacancy_id)
            .order_by(Feedback.id.desc())
        ).all()


def _settings_page(request: Request, message: str | None = None, error: bool = False):
    values = {key: config.get(key) for key in SETTING_FIELDS}
    return render(
        request,
        "settings.html",
        values=values,
        has_key=bool(config.get("llm_api_key")),
        has_password=bool(config.get("smtp_password")),
        sample=_sample(),
        memory=_memory(),
        message=message,
        error=error,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    return _settings_page(request)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    error = _save_settings(await request.form())
    if error:
        return _settings_page(request, error, error=True)
    return _settings_page(request, "Сохранено.")


def _save_settings(form) -> str | None:
    """Сохраняет присланные поля «Настроек»; ошибка ввода — текст, ничего не сохранено."""
    values = {key: str(form.get(key, "")).strip() for key in SETTING_FIELDS if key in form}
    for secret in ("llm_api_key", "smtp_password"):
        if value := str(form.get(secret, "")).strip():
            values[secret] = value  # пустое поле — ключ или пароль не меняется
    if "night_time" in values and not schedule.parse_time(values["night_time"]):
        return "Время укажите как 02:00."
    if values.get("night_days", "daily") not in schedule.DAYS:
        values.pop("night_days")
    if values.get("smtp_port") and not values["smtp_port"].isdigit():
        return "Порт почтового сервера — число, например 587."
    model_changed = values.get("embed_model") not in (None, config.get("embed_model"))
    config.save(values)
    schedule.reschedule()
    if model_changed:  # отпечатки другой модели несравнимы — пересчитываем базу
        with db.SessionLocal() as s:
            enqueue(s, "embed", {})
            s.commit()
    return None


@router.post("/settings/check", response_class=HTMLResponse)
def settings_check(request: Request):
    started = time.monotonic()
    try:
        get_llm("parse").complete_structured(
            Ping, "Проверка связи. Ответь ok = true.", "Проверка связи: ответь ok = true."
        )
    except AuthError as exc:
        return _settings_page(request, f"Не получилось: {exc}", error=True)
    except Exception:  # адрес не тот, сервис недоступен или отвечает не то
        return _settings_page(
            request, "Адрес сервиса не отвечает, проверьте его в Настройках.", error=True
        )
    seconds = f"{time.monotonic() - started:.1f}".replace(".", ",")
    return _settings_page(request, f"Подключение работает: ответ за {seconds} с.")


@router.post("/settings/mail-test", response_class=HTMLResponse)
async def settings_mail_test(request: Request):
    """Кнопка стоит в форме почты: сначала сохраняем то, что в полях, потом шлём."""
    error = _save_settings(await request.form()) or mail.send_test()
    if error:
        return _settings_page(request, error, error=True)
    return _settings_page(request, f"Пробное письмо ушло на {config.get('smtp_to')}.")


@router.post("/feedback/clear")
def feedback_clear():
    """«Забыть все исправления» в «Настройках»."""
    with db.SessionLocal() as s:
        s.execute(delete(Feedback))
        s.commit()
    return RedirectResponse("/settings#memory", status_code=303)
