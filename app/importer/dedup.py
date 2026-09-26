"""Точные дубли: совпал нормализованный телефон или почта — записи объединяются сами.

Склейка идёт только по контактам из колонок выгрузки и шапки резюме. Совпадение с
контактом из тела резюме (`body_contacts`) — лишь возможный дубль: пара уходит в
очередь со статусом open, объединяет человек.
"""

from datetime import date, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.importer.normalize import is_stale
from app.models import Candidate, Duplicate

MERGED_FIELDS = ("full_name", "city", "birth_year", "resume_date", "external_id", "source_file")


def _freshness(c: Candidate):
    return (c.resume_date or date.min, c.imported_at or datetime.min, c.id)


def merge_exact_duplicates(session: Session) -> int:
    """Объединяет группы по общим контактам; возвращает число записей, ставших дублями."""
    active = list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    parent = {c.id: c.id for c in active}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for c in active:
        for key in [*c.phones, *c.emails]:
            if key in owner:
                parent[root(c.id)] = root(owner[key])
            else:
                owner[key] = c.id

    groups: dict[int, list[Candidate]] = {}
    for c in active:
        groups.setdefault(root(c.id), []).append(c)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=_freshness, reverse=True)
        main, older = group[0], group[1:]
        for old in older:
            for field in MERGED_FIELDS:
                if getattr(main, field) is None:
                    setattr(main, field, getattr(old, field))
            main.phones = list(dict.fromkeys([*main.phones, *old.phones]))
            main.emails = list(dict.fromkeys([*main.emails, *old.emails]))
            main.links = list(dict.fromkeys([*main.links, *old.links]))
            old.duplicate_of = main.id
            session.execute(  # прежние дубли старой записи теперь ведут к основной
                update(Candidate)
                .where(Candidate.duplicate_of == old.id)
                .values(duplicate_of=main.id)
            )
            session.add(
                Duplicate(candidate_a=main.id, candidate_b=old.id, confidence=1.0, status="merged")
            )
            merged += 1
            main.body_contacts = [
                c
                for c in dict.fromkeys([*main.body_contacts, *old.body_contacts])
                if c not in {*main.phones, *main.emails}
            ]
        main.stale = is_stale(main.resume_date)
    _queue_body_matches(session)
    session.commit()
    return merged


def _queue_body_matches(session: Session) -> None:
    session.flush()
    active = list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    owner = {key: c.id for c in active for key in [*c.phones, *c.emails]}
    seen = {
        frozenset(pair)
        for pair in session.execute(select(Duplicate.candidate_a, Duplicate.candidate_b))
    }
    for c in active:
        for key in c.body_contacts:
            other = owner.get(key)
            if other is None or other == c.id or frozenset((other, c.id)) in seen:
                continue
            seen.add(frozenset((other, c.id)))
            session.add(Duplicate(candidate_a=other, candidate_b=c.id, confidence=0.5))
