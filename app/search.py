"""Поиск кандидатов: по смыслу (вектор запроса → 200 ближайших → фильтры) и по контактам.

Фильтры — город, минимальный стаж, потолок зарплаты, свежесть — работают в SQL по
полям записи и разобранного резюме. Для каждого фильтра считается, скольких он
убирает, — это подпись на плашке и подсказка при пустой выдаче.
"""

import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from markupsafe import Markup, escape
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app import embed
from app.importer.normalize import EMAIL_RE, find_name, normalize_phone
from app.models import Candidate

POOL = 200
PAGE_SIZE = 20
VERY_CLOSE, CLOSE = 0.40, 0.25  # пороги близости для BERTA, подобраны на демо-базе


@dataclass
class Filters:
    city: str = ""
    min_years: float | None = None
    max_salary: int | None = None
    fresh: str = ""  # fresh | stale | ""

    def active(self) -> dict[str, str]:
        """Включённые фильтры с подписью для плашки."""
        labels = {}
        if self.city:
            labels["city"] = f"город {self.city}"
        if self.min_years is not None:
            labels["min_years"] = f"стаж от {self.min_years:g} лет"
        if self.max_salary is not None:
            labels["max_salary"] = f"зарплата до {self.max_salary:,} ₽".replace(",", " ")
        if self.fresh == "fresh":
            labels["fresh"] = "резюме свежее полутора лет"
        elif self.fresh == "stale":
            labels["fresh"] = "резюме старше полутора лет"
        return labels

    def conditions(self, skip: str | None = None) -> list:
        parsed = Candidate.parsed
        found = []
        if self.city and skip != "city":
            found.append(
                or_(Candidate.city == self.city, func.json_extract(parsed, "$.city") == self.city)
            )
        if self.min_years is not None and skip != "min_years":
            found.append(func.json_extract(parsed, "$.total_years") >= self.min_years)
        if self.max_salary is not None and skip != "max_salary":
            amount = func.json_extract(parsed, "$.salary_amount")
            found.append(or_(amount.is_(None), amount <= self.max_salary))
        if self.fresh in ("fresh", "stale") and skip != "fresh":
            found.append(Candidate.stale.is_(self.fresh == "stale"))
        return found


@dataclass
class Result:
    mode: str  # meaning | contacts | list | words (до разбора, отпечатков ещё нет)
    hits: list = field(default_factory=list)  # (Candidate, близость | None, фрагмент | None)
    total: int = 0
    removed: dict[str, int] = field(default_factory=dict)
    relax: tuple[str, int] | None = None  # какое условие снять, чтобы кто-то нашёлся
    searched: int = 0  # среди скольких с отпечатком искали
    base: int = 0  # сколько всего кандидатов


def closeness(score: float) -> str:
    if score >= VERY_CLOSE:
        return "очень близко"
    return "близко" if score >= CLOSE else "возможно"


def active():
    return Candidate.duplicate_of.is_(None)


def looks_like_contact(query: str) -> str | None:
    q = query.strip()
    if EMAIL_RE.fullmatch(q):
        return "email"
    if sum(ch.isdigit() for ch in q) >= 10 and normalize_phone(q):
        return "phone"
    if 2 <= len(q.split()) <= 3 and find_name(q.title(), lines=1):
        return "name"
    return None


def by_contacts(session: Session, query: str, kind: str) -> list[Candidate]:
    q = query.strip()
    if kind == "phone":
        cond = cast(Candidate.phones, Text).contains(f'"{normalize_phone(q)}"')
    elif kind == "email":
        cond = func.pylower(cast(Candidate.emails, Text)).contains(f'"{q.lower()}"')
    else:
        cond = func.pylower(Candidate.full_name).contains(" ".join(q.lower().split()))
    return list(session.scalars(select(Candidate).where(active(), cond).limit(PAGE_SIZE)))


def _count(session: Session, ids, conditions) -> int:
    where = [active(), *conditions]
    if ids is not None:
        where.append(Candidate.id.in_(ids))
    return session.scalar(select(func.count(Candidate.id)).where(*where))


def _filter_stats(session: Session, filters: Filters, ids, total: int) -> tuple[dict, tuple | None]:
    removed, relax = {}, None
    for key in filters.active():
        without = _count(session, ids, filters.conditions(skip=key))
        removed[key] = without - total
        if not total and without and (relax is None or without > relax[1]):
            relax = (key, without)
    return removed, relax


def _paragraphs(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text or "") if b.strip()]
    out = []
    for block in blocks:
        out += (
            [ln.strip() for ln in block.splitlines() if ln.strip()] if len(block) > 400 else [block]
        )
    return [p for p in out if len(p) > 20][:40]


def fragment(c: Candidate, qvec: np.ndarray, query: str) -> Markup:
    """Ближайший к запросу абзац резюме с подсвеченными словами запроса."""
    paragraphs = _paragraphs(c.raw_text)
    if not paragraphs:
        return escape((c.parsed or {}).get("summary", ""))
    best = paragraphs[int(np.argmax(embed.encode(paragraphs, "passage") @ qvec))]
    stems = {w[:5].lower() for w in re.findall(r"\w{4,}", query)}
    words = re.split(r"(\w+)", best[:300])
    return Markup("").join(
        Markup(f"<mark>{escape(w)}</mark>") if w[:5].lower() in stems and len(w) >= 4 else escape(w)
        for w in words
    )


def search(session: Session, query: str, filters: Filters, sort: str = "meaning", page: int = 1):
    base = _count(session, None, [])
    if not query.strip():
        conds = filters.conditions()
        total = _count(session, None, conds)
        items = session.scalars(
            select(Candidate)
            .where(active(), *conds)
            .order_by(Candidate.resume_date.desc().nulls_last(), Candidate.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        removed, relax = _filter_stats(session, filters, None, total)
        return Result("list", [(c, None, None) for c in items], total, removed, relax, base=base)

    if kind := looks_like_contact(query):
        found = by_contacts(session, query, kind)
        if found or kind != "name":
            return Result("contacts", [(c, None, None) for c in found], len(found), base=base)

    ids, _ = embed.index()
    if not len(ids):  # отпечатков ещё нет — ищем по словам, как до разбора
        needle = query.strip().lower()
        conds = [
            *filters.conditions(),
            or_(
                func.pylower(Candidate.full_name).contains(needle, autoescape=True),
                func.pylower(Candidate.raw_text).contains(needle, autoescape=True),
                func.pylower(Candidate.city).contains(needle, autoescape=True),
            ),
        ]
        items = list(session.scalars(select(Candidate).where(active(), *conds).limit(PAGE_SIZE)))
        return Result("words", [(c, None, None) for c in items], len(items), base=base)
    nearest = embed.nearest(query, POOL)
    scores = dict(nearest)
    pool = list(scores)
    passing = {
        c.id: c
        for c in session.scalars(
            select(Candidate).where(active(), Candidate.id.in_(pool), *filters.conditions())
        )
    }
    ordered = [passing[i] for i in pool if i in passing]
    if sort == "fresh":
        ordered.sort(key=lambda c: c.resume_date or date.min, reverse=True)
    removed, relax = _filter_stats(session, filters, pool, len(ordered))
    page_items = ordered[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    qvec = embed.encode([query], "query")[0] if page_items else None
    hits = [(c, closeness(scores[c.id]), fragment(c, qvec, query)) for c in page_items]
    return Result("meaning", hits, len(ordered), removed, relax, searched=len(ids), base=base)
