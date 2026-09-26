"""Экраны раздела 2 плана. Этап 1: главная, загрузка, кандидаты, карточка; остальное — заглушки."""

import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased
from starlette.datastructures import UploadFile

from app import db
from app.importer.mapping import FIELDS
from app.importer.normalize import looks_like_name
from app.importer.pipeline import documents, load_table, new_batch, start_import
from app.models import Candidate, Duplicate, ImportBatch, Job

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["ru_date"] = lambda d: d.strftime("%d.%m.%Y") if d else "—"
PAGE_SIZE = 50
POSITION_RE = re.compile(r"^(?:желаемая\s+)?должность\s*:\s*(.+)$", re.IGNORECASE)


def headline(raw_text: str) -> str:
    """Должность для списка: строка «Должность: …» или первая содержательная строка."""
    lines = [ln.strip(" #*_\t") for ln in (raw_text or "").splitlines() if ln.strip(" #*_\t")]
    for line in lines:
        if match := POSITION_RE.match(line):
            return match.group(1)[:100]
    for line in lines[:6]:
        if ":" not in line and not looks_like_name(line) and len(line) < 100:
            return line
    return ""


templates.env.filters["headline"] = headline


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def active():
    return Candidate.duplicate_of.is_(None)


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
            "parsed": count(active(), Candidate.parse_status == "parsed"),
        }
        main, old = aliased(Candidate), aliased(Candidate)
        merged = s.execute(
            select(Duplicate, main, old)
            .join(main, main.id == Duplicate.candidate_a)
            .join(old, old.id == Duplicate.candidate_b)
            .order_by(Duplicate.id.desc())
            .limit(10)
        ).all()
        running = list(s.scalars(select(Job).where(Job.status.in_(("queued", "running")))))
        with_errors = list(
            s.scalars(select(Job).where(Job.error.is_not(None)).order_by(Job.id.desc()).limit(5))
        )
    return render(
        request, "home.html", stats=stats, merged=merged, running=running, with_errors=with_errors
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    with db.SessionLocal() as s:
        batches = list(s.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)))
    return render(request, "upload.html", batches=batches, error=None)


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


@router.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, q: str = "", city: str = "", fresh: str = "", page: int = 1):
    where = [active()]
    if q.strip():
        needle = q.strip().lower()
        where.append(
            or_(
                func.pylower(Candidate.full_name).contains(needle, autoescape=True),
                func.pylower(Candidate.raw_text).contains(needle, autoescape=True),
                func.pylower(Candidate.city).contains(needle, autoescape=True),
            )
        )
    if city:
        where.append(Candidate.city == city)
    if fresh in ("fresh", "stale"):
        where.append(Candidate.stale.is_(fresh == "stale"))
    page = max(page, 1)
    with db.SessionLocal() as s:
        total = s.scalar(select(func.count(Candidate.id)).where(*where))
        items = list(
            s.scalars(
                select(Candidate)
                .where(*where)
                .order_by(Candidate.resume_date.desc().nulls_last(), Candidate.id)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
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
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "q": q,
        "city": city,
        "fresh": fresh,
        "cities": sorted(cities),
    }
    if request.headers.get("HX-Request"):
        return render(request, "candidates_rows.html", **context)
    return render(request, "candidates.html", **context)


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
def candidate(request: Request, candidate_id: int):
    with db.SessionLocal() as s:
        c = s.get(Candidate, candidate_id)
        if not c:
            raise HTTPException(404)
        main = s.get(Candidate, c.duplicate_of) if c.duplicate_of else None
        merged = list(s.scalars(select(Candidate).where(Candidate.duplicate_of == c.id)))
    return render(request, "candidate.html", c=c, main=main, merged=merged)


STUBS = {
    "/vacancies": "Вакансии",
    "/morning": "Утро",
    "/settings": "Настройки",
}


def _stub(title: str):
    def view(request: Request):
        return render(request, "stub.html", title=title)

    return view


for _path, _title in STUBS.items():
    router.add_api_route(_path, _stub(_title), response_class=HTMLResponse, methods=["GET"])
