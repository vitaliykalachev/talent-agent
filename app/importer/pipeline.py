"""Конвейер импорта: партия файлов → задача → кандидаты в БД → точные дубли.

Задача обрабатывает элементы (строки выгрузки, потом файлы резюме) по порядку и
сохраняет прогресс вместе с кандидатами одной транзакцией, поэтому после
перезапуска продолжает с первого необработанного элемента.
"""

import re
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import db
from app.importer.dedup import merge_exact_duplicates
from app.importer.mapping import guess_mapping
from app.importer.normalize import (
    extract_birth_year,
    extract_city,
    extract_emails,
    extract_phones,
    extract_resume_date,
    find_name,
    is_stale,
    looks_like_name,
    normalize_city,
    normalize_name,
    parse_date,
    parse_year,
)
from app.importer.readers import (
    DOC_SUFFIXES,
    document_text,
    extract_zip,
    list_documents,
    read_table,
)
from app.jobs import enqueue, stopping
from app.models import Candidate, ImportBatch, Job

CHUNK = 50


def batch_dir(batch_id: int) -> Path:
    return db.data_dir / "uploads" / str(batch_id)


def table_path(batch: ImportBatch) -> Path | None:
    return next(iter(sorted(batch_dir(batch.id).glob("table.*"))), None)


def load_table(batch: ImportBatch) -> tuple[list[str], list[list]]:
    path = table_path(batch)
    return read_table(path) if path else ([], [])


def documents(batch: ImportBatch) -> list[Path]:
    return list_documents(batch_dir(batch.id) / "resumes")


def _copy_unique(src: Path, folder: Path) -> None:
    target, n = folder / src.name, 1
    while target.exists():
        target = folder / f"{src.stem}_{n}{src.suffix}"
        n += 1
    shutil.copyfile(src, target)


def new_batch(session: Session, table: Path | None, docs: list[Path]) -> ImportBatch:
    """Складывает файлы партии в data/uploads/<id> и угадывает маппинг колонок."""
    batch = ImportBatch(file_name=table.name if table else None)
    session.add(batch)
    session.flush()
    folder = batch_dir(batch.id)
    resumes = folder / "resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    if table:
        shutil.copyfile(table, folder / f"table{table.suffix.lower()}")
    for path in docs:
        if path.is_dir():
            for doc in list_documents(path):
                _copy_unique(doc, resumes)
        elif path.suffix.lower() == ".zip":
            extract_zip(path, resumes)
        elif path.suffix.lower() in DOC_SUFFIXES:
            _copy_unique(path, resumes)
    headers, rows = load_table(batch)
    batch.mapping = guess_mapping(headers, rows)
    session.commit()
    return batch


def start_import(session: Session, batch: ImportBatch, fields: list[str]) -> Job:
    batch.mapping = list(fields)
    job = enqueue(session, "import", {"batch_id": batch.id})
    session.commit()
    return job


def _joined(values: list) -> str:
    return " ; ".join(str(v) for v in values)


def process_row(session: Session, headers: list[str], fields: list[str], row: list) -> None:
    values: dict[str, list] = {}
    lines = []
    for header, field, value in zip(headers, fields, row, strict=False):
        if not field or value is None or not str(value).strip():
            continue
        values.setdefault(field, []).append(value)
        if field not in ("resume_text", "resume_file"):
            shown = value.strftime("%d.%m.%Y") if isinstance(value, datetime) else value
            lines.append(f"{header}: {shown}")
    resume = "\n\n".join(str(v) for v in values.get("resume_text", []))
    raw_text = "\n".join(lines) + (f"\n\n{resume}" if resume else "")

    first = {k: v[0] for k, v in values.items()}
    name = first.get("full_name") or " ".join(
        str(first[k]) for k in ("last_name", "first_name", "middle_name") if k in first
    )
    resume_date = parse_date(first.get("resume_date")) or extract_resume_date(resume)
    session.add(
        Candidate(
            external_id=str(first["external_id"]).strip() if "external_id" in first else None,
            raw_text=raw_text,
            source_file=str(first["resume_file"]).strip() if "resume_file" in first else None,
            full_name=normalize_name(name),
            phones=extract_phones(_joined(values.get("phone", []))) + extract_phones(resume),
            emails=list(
                dict.fromkeys(
                    extract_emails(_joined(values.get("email", []))) + extract_emails(resume)
                )
            ),
            city=normalize_city(first.get("city")) or extract_city(resume),
            birth_year=parse_year(first.get("birth")) or extract_birth_year(resume),
            resume_date=resume_date,
            stale=is_stale(resume_date),
        )
    )


def _unique(session: Session, *conditions) -> Candidate | None:
    q = select(Candidate).where(Candidate.duplicate_of.is_(None), *conditions).limit(2)
    found = list(session.scalars(q))
    return found[0] if len(found) == 1 else None


def find_owner(session: Session, path: Path, text: str) -> Candidate | None:
    """Строка выгрузки для файла: по ID в имени файла, по колонке с файлом, по ФИО."""
    session.flush()
    token = re.split(r"[\s_\-.]+", path.stem)[0]
    if token and (c := _unique(session, Candidate.external_id == token)):
        return c
    if c := _unique(session, Candidate.source_file == path.name):
        return c
    stem_name = " ".join(path.stem.replace("_", " ").split())
    names = [normalize_name(stem_name) if looks_like_name(stem_name) else None, find_name(text)]
    for name in filter(None, names):
        if c := _unique(session, Candidate.full_name == name):
            return c
        short = " ".join(name.split()[:2])
        if c := _unique(
            session, or_(Candidate.full_name == short, Candidate.full_name.like(f"{short} %"))
        ):
            return c
    return None


def process_document(session: Session, path: Path) -> str | None:
    """Добавляет текст файла к строке выгрузки или создаёт кандидата; возвращает ошибку."""
    try:
        text = document_text(path)
    except Exception as exc:  # битый или нечитаемый файл не должен останавливать партию
        return f"{path.name}: {type(exc).__name__}"
    if not text:
        return f"{path.name}: пустой файл"
    owner = find_owner(session, path, text)
    phones, emails = extract_phones(text), extract_emails(text)
    if owner:
        owner.raw_text = f"{owner.raw_text}\n\n{text}".strip()
        owner.source_file = path.name
        owner.phones = list(dict.fromkeys([*owner.phones, *phones]))
        owner.emails = list(dict.fromkeys([*owner.emails, *emails]))
        owner.city = owner.city or extract_city(text)
        owner.birth_year = owner.birth_year or extract_birth_year(text)
        owner.resume_date = owner.resume_date or extract_resume_date(text)
        owner.stale = is_stale(owner.resume_date)
        return None
    resume_date = extract_resume_date(text)
    session.add(
        Candidate(
            raw_text=text,
            source_file=path.name,
            full_name=find_name(text),
            phones=phones,
            emails=emails,
            city=extract_city(text),
            birth_year=extract_birth_year(text),
            resume_date=resume_date,
            stale=is_stale(resume_date),
        )
    )
    return None


def run_import(job_id: int) -> None:
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        batch = session.get(ImportBatch, job.payload["batch_id"])
        headers, rows = load_table(batch)
        docs = documents(batch)
        job.status = "running"
        job.total = batch.rows_total = len(rows) + len(docs)
        session.commit()

        errors = job.error.splitlines() if job.error else []
        while job.progress < job.total:
            if stopping.is_set():  # приложение останавливается: продолжим после перезапуска
                return
            end = min(job.progress + CHUNK, job.total)
            for i in range(job.progress, end):
                if i < len(rows):
                    process_row(session, headers, batch.mapping, rows[i])
                    batch.rows_ok += 1
                elif error := process_document(session, docs[i - len(rows)]):
                    errors.append(error)
                else:
                    batch.rows_ok += 1
            job.progress = end
            job.error = "\n".join(errors) or None
            session.commit()

        merge_exact_duplicates(session)
        job.status = "done"
        job.finished_at = datetime.now()
        session.commit()
