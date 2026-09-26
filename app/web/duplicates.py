"""Возможные дубли: очередь «Похоже на дубль», экран пары, объединение и его отмена."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app import db
from app.importer.dedup import freshness, merge_pair, open_pairs, shared_employer, unmerge
from app.importer.normalize import names_compatible
from app.models import Candidate, Duplicate
from app.web import present
from app.web.routes import current_job, render

router = APIRouter()
CHOOSABLE = [("full_name", "ФИО"), ("city", "Город"), ("birth_year", "Год рождения"),
             ("resume_date", "Дата резюме")]  # fmt: skip


def _norm_name(name: str | None) -> str:
    return " ".join(sorted((name or "").lower().replace("ё", "е").split()))


def compare(a: Candidate, b: Candidate) -> tuple[set[str], str]:
    """Какие поля совпали (их подсвечиваем) и причина словами: «то же ФИО, тот же город,
    год рождения 1984 у обоих, телефоны разные»."""
    same, words = set(), []
    if a.full_name and b.full_name and names_compatible(a.full_name, b.full_name):
        same.add("full_name")
        exact = _norm_name(a.full_name) == _norm_name(b.full_name)
        words.append("то же ФИО" if exact else "похожее ФИО")
    elif a.full_name and b.full_name:
        words.append("ФИО разные")
    if a.city and b.city and a.city.lower() == b.city.lower():
        same.add("city")
        words.append("тот же город")
    if a.birth_year and a.birth_year == b.birth_year:
        same.add("birth_year")
        words.append(f"год рождения {a.birth_year} у обоих")
    if company := shared_employer(a, b):
        same.add("job")
        words.append(f"оба работали в «{company}»")
    for field, common, differ in (
        ("phones", "общий телефон", "телефоны разные"),
        ("emails", "общая почта", "почта разная"),
    ):
        mine, theirs = set(getattr(a, field)), set(getattr(b, field))
        if mine & theirs:
            same.add(field)
            words.append(common)
        elif mine and theirs:
            words.append(differ)
    if set(a.body_contacts) & {*b.phones, *b.emails} or set(b.body_contacts) & {
        *a.phones,
        *a.emails,
    }:
        words.append("контакт одного встречается в тексте резюме другого")
    return same, ", ".join(words)


def open_count(s) -> int:
    return len(open_pairs(s))


@router.get("/duplicates", response_class=HTMLResponse)
def queue(request: Request):
    with db.SessionLocal() as s:
        rows = [(d, a, b, compare(a, b)[1]) for d, a, b in open_pairs(s)]
    return render(request, "duplicates.html", rows=rows)


@router.get("/duplicates/merged", response_class=HTMLResponse)
def merged_list(request: Request):
    main, old = aliased(Candidate), aliased(Candidate)
    with db.SessionLocal() as s:
        rows = s.execute(
            select(Duplicate, main, old)
            .join(main, main.id == Duplicate.candidate_a)
            .join(old, old.id == Duplicate.candidate_b)
            .where(Duplicate.status == "merged", Duplicate.confidence >= 1.0)
            .order_by(Duplicate.id.desc())
        ).all()
    return render(request, "duplicates_merged.html", rows=rows)


@router.get("/duplicates/{dup_id}", response_class=HTMLResponse)
def pair(request: Request, dup_id: int):
    with db.SessionLocal() as s:
        dup = s.get(Duplicate, dup_id)
        if not dup:
            raise HTTPException(404)
        if dup.status == "merged":
            return RedirectResponse(f"/candidates/{dup.candidate_a}", status_code=303)
        a, b = s.get(Candidate, dup.candidate_a), s.get(Candidate, dup.candidate_b)
        same, reason = compare(a, b)
        fresher = a if freshness(a) >= freshness(b) else b
        left = present.batch_labels(s, [a.batch_id, b.batch_id])
        waiting = open_count(s)
    other = b if fresher is a else a
    cards = [
        {"c": c, "job": current_job(c), "batch": left.get(c.batch_id)} for c in (fresher, other)
    ]
    choices = [
        (field, label)
        for field, label in CHOOSABLE
        if getattr(a, field) != getattr(b, field)
        and getattr(a, field) is not None
        and getattr(b, field) is not None
    ]
    return render(
        request,
        "duplicate.html",
        dup=dup,
        cards=cards,
        same=same,
        reason=reason,
        fresher=fresher,
        choices=choices,
        waiting=waiting,
    )


def _next(s) -> RedirectResponse:
    rows = open_pairs(s)
    target = f"/duplicates/{rows[0][0].id}" if rows else "/duplicates"
    return RedirectResponse(target, status_code=303)


@router.post("/duplicates/{dup_id}/merge")
async def merge(request: Request, dup_id: int):
    form = await request.form()
    with db.SessionLocal() as s:
        dup = s.get(Duplicate, dup_id)
        if not dup or dup.status != "open":
            raise HTTPException(404)
        a, b = s.get(Candidate, dup.candidate_a), s.get(Candidate, dup.candidate_b)
        old = b if freshness(a) >= freshness(b) else a
        take = {field for field, _ in CHOOSABLE if form.get(field) == str(old.id)}
        merge_pair(s, dup, take)
        s.commit()
        return _next(s)


@router.post("/duplicates/{dup_id}/reject")
def reject(dup_id: int):
    """«Нет, это разные люди»: пара больше не предлагается."""
    with db.SessionLocal() as s:
        dup = s.get(Duplicate, dup_id)
        if not dup or dup.status != "open":
            raise HTTPException(404)
        dup.status = "rejected"
        s.commit()
        return _next(s)


@router.post("/duplicates/{dup_id}/postpone")
def postpone(dup_id: int):
    with db.SessionLocal() as s:
        dup = s.get(Duplicate, dup_id)
        if not dup or dup.status != "open":
            raise HTTPException(404)
        dup.postponed_at = datetime.now()
        s.commit()
        return _next(s)


@router.post("/duplicates/{dup_id}/unmerge")
def undo(dup_id: int):
    """«Отменить объединение» на основной карточке: обе записи снова отдельно."""
    with db.SessionLocal() as s:
        dup = s.get(Duplicate, dup_id)
        if not dup or dup.status != "merged":
            raise HTTPException(404)
        unmerge(s, dup)
        s.commit()
        return RedirectResponse(f"/candidates/{dup.candidate_a}", status_code=303)
