"""Дубли (раздел 7 плана).

Точный дубль: совпал нормализованный телефон или почта и ФИО совместимо — записи
объединяются сами. Контакт, который встречается у трёх и более разных ФИО (общий
телефон отдела кадров), неидентифицирующий: он не склеивает и не даёт пар. Общий
контакт при несовместимом ФИО — возможный дубль: пара уходит в очередь со статусом
open. Склейка идёт только по контактам из колонок выгрузки и шапки резюме; совпадение
с контактом из тела резюме (`body_contacts`) — тоже лишь возможный дубль.

Возможный дубль по ФИО: ФИО совместимо и совпадает год рождения или работодатель в
пересекающиеся годы (`find_possible`).

Слияние обратимо: поглощённая запись хранится целиком с `duplicate_of`, а в строке
`duplicates` — снимок основной записи до слияния; «Отменить объединение» возвращает
обе записи и ставит пару обратно в очередь. «Нет, это разные люди» — статус rejected,
пара больше не предлагается и сама не склеивается.
"""

import re
from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from app.importer.normalize import email_key, is_stale, names_compatible, to_cyrillic
from app.models import Candidate, Duplicate

SHARED_BY = 3  # у стольких разных ФИО контакт уже ничего не значит
MERGED_FIELDS = ("full_name", "city", "birth_year", "resume_date", "external_id", "source_file")
LIST_FIELDS = ("phones", "emails", "links", "body_contacts")


def freshness(c: Candidate):
    return (c.resume_date or date.min, c.imported_at or datetime.min, c.id)


def _plain(value):
    return value.isoformat() if isinstance(value, date) else value


def absorb(session: Session, main: Candidate, old: Candidate, take=frozenset()) -> dict:
    """Поглощает `old` основной записью и возвращает снимок основной до слияния.

    Поля основной записи остаются, пустые дополняются из старой; поля из `take`
    рекрутер выбрал руками — они берутся из старой. Контакты складываются."""
    snapshot = {f: _plain(getattr(main, f)) for f in (*MERGED_FIELDS, *LIST_FIELDS)}
    for field in MERGED_FIELDS:
        if field in take or getattr(main, field) is None:
            setattr(main, field, getattr(old, field))
    main.phones = list(dict.fromkeys([*main.phones, *old.phones]))
    main.emails = list(dict.fromkeys([*main.emails, *old.emails]))
    main.links = list(dict.fromkeys([*main.links, *old.links]))
    main.body_contacts = [
        c
        for c in dict.fromkeys([*main.body_contacts, *old.body_contacts])
        if c not in {*main.phones, *main.emails}
    ]
    moved = list(session.scalars(select(Candidate.id).where(Candidate.duplicate_of == old.id)))
    if moved:  # прежние дубли старой записи теперь ведут к основной
        session.execute(
            update(Candidate).where(Candidate.id.in_(moved)).values(duplicate_of=main.id)
        )
    old.duplicate_of = main.id
    main.stale = is_stale(main.resume_date)
    return {**snapshot, "moved": moved}


def unmerge(session: Session, dup: Duplicate) -> None:
    """«Отменить объединение»: обе записи снова отдельные, пара — в очереди на проверку.
    Возвращается только то, что пришло из старой записи: правки после слияния целы."""
    main, old = session.get(Candidate, dup.candidate_a), session.get(Candidate, dup.candidate_b)
    snap = dup.snapshot or {}
    for field in MERGED_FIELDS:
        if field not in snap:
            continue
        before = snap[field]
        if field == "resume_date" and before:
            before = date.fromisoformat(before)
        current = getattr(main, field)
        if current != before and current == getattr(old, field):
            setattr(main, field, before)
    for field in LIST_FIELDS:
        if field in snap:
            added = set(getattr(old, field)) - set(snap[field])
            setattr(main, field, [x for x in getattr(main, field) if x not in added])
    if snap.get("moved"):
        session.execute(
            update(Candidate)
            .where(Candidate.id.in_(snap["moved"]), Candidate.duplicate_of == main.id)
            .values(duplicate_of=old.id)
        )
    old.duplicate_of = None
    main.stale, old.stale = is_stale(main.resume_date), is_stale(old.resume_date)
    dup.status, dup.snapshot, dup.postponed_at = "open", None, None


def merge_pair(session: Session, dup: Duplicate, take_from_old=frozenset()) -> None:
    """«Объединить»: основной остаётся более свежая запись; поля из `take_from_old`
    рекрутер выбрал из другой."""
    a, b = session.get(Candidate, dup.candidate_a), session.get(Candidate, dup.candidate_b)
    main, old = (a, b) if freshness(a) >= freshness(b) else (b, a)
    dup.snapshot = absorb(session, main, old, set(take_from_old) & set(MERGED_FIELDS))
    dup.candidate_a, dup.candidate_b, dup.status = main.id, old.id, "merged"


def merge_exact_duplicates(session: Session) -> int:
    """Объединяет группы по общим контактам; возвращает число записей, ставших дублями."""
    active = list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    parent = {c.id: c.id for c in active}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    holders: dict[str, list[Candidate]] = {}
    for c in active:
        for key in dict.fromkeys([*c.phones, *map(email_key, c.emails)]):
            holders.setdefault(key, []).append(c)
    seen = _seen_pairs(session)
    for group in holders.values():
        if len(group) < 2 or _people(group) >= SHARED_BY:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                pair = frozenset((a.id, b.id))
                if pair in seen:  # пара уже в очереди или «разные люди» — сама не склеивается
                    continue
                if names_compatible(a.full_name, b.full_name):
                    parent[root(b.id)] = root(a.id)
                else:
                    seen.add(pair)
                    session.add(Duplicate(candidate_a=a.id, candidate_b=b.id, confidence=0.5))

    groups: dict[int, list[Candidate]] = {}
    for c in active:
        groups.setdefault(root(c.id), []).append(c)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=freshness, reverse=True)
        main, older = group[0], group[1:]
        for old in older:
            snapshot = absorb(session, main, old)
            session.add(
                Duplicate(
                    candidate_a=main.id,
                    candidate_b=old.id,
                    confidence=1.0,
                    status="merged",
                    snapshot=snapshot,
                )
            )
            merged += 1
    _queue_body_matches(session)
    session.commit()
    return merged


def _people(group: list[Candidate]) -> int:
    """Сколько разных людей по ФИО: «Королёв Пётр» и «Petr Korolev» — один."""
    names: list[str] = []
    for c in group:
        if c.full_name and not any(names_compatible(c.full_name, n) for n in names):
            names.append(c.full_name)
    return len(names)


def _seen_pairs(session: Session) -> set[frozenset]:
    session.flush()
    return {
        frozenset(pair)
        for pair in session.execute(select(Duplicate.candidate_a, Duplicate.candidate_b))
    }


def _queue_body_matches(session: Session) -> None:
    active = list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    owner = {key: c.id for c in active for key in [*c.phones, *c.emails]}
    seen = _seen_pairs(session)
    for c in active:
        for key in c.body_contacts:
            other = owner.get(key)
            if other is None or other == c.id or frozenset((other, c.id)) in seen:
                continue
            seen.add(frozenset((other, c.id)))
            session.add(Duplicate(candidate_a=other, candidate_b=c.id, confidence=0.5))


def open_pairs(session: Session) -> list:
    """Очередь «Похоже на дубль»: [(Duplicate, a, b)], отложенные — в конце."""
    a, b = aliased(Candidate), aliased(Candidate)
    return session.execute(
        select(Duplicate, a, b)
        .join(a, a.id == Duplicate.candidate_a)
        .join(b, b.id == Duplicate.candidate_b)
        .where(Duplicate.status == "open", a.duplicate_of.is_(None), b.duplicate_of.is_(None))
        .order_by(Duplicate.postponed_at.is_not(None), Duplicate.postponed_at, Duplicate.id)
    ).all()


def auto_merged(session: Session) -> int:
    return session.scalar(
        select(func.count(Duplicate.id)).where(
            Duplicate.status == "merged", Duplicate.confidence >= 1.0
        )
    )


# ── Возможные дубли по ФИО ──────────────────────────────────────────────────


def _tokens(name: str | None) -> set[str]:
    return set(re.findall(r"\w{3,}", to_cyrillic(name or "").lower().replace("ё", "е")))


def employers(c: Candidate) -> dict[str, tuple[int, int, str]]:
    """Работодатели из разбора: код компании → (год с, год по, название)."""
    found = {}
    for p in (c.parsed or {}).get("positions") or []:
        key, start = p.get("company_key"), str(p.get("start") or "")[:4]
        if not key or not start.isdigit():
            continue
        end = str(p.get("end") or "")[:4]
        found[key] = (int(start), int(end) if end.isdigit() else 9999, p.get("company") or key)
    return found


def shared_employer(a: Candidate, b: Candidate) -> str | None:
    """Общий работодатель в пересекающиеся годы — его название."""
    theirs = employers(b)
    for key, (start, end, name) in employers(a).items():
        if key in theirs and start <= theirs[key][1] and theirs[key][0] <= end:
            return name
    return None


def find_possible(session: Session) -> int:
    """Пары «ФИО совместимо и совпадает год рождения или работодатель в пересекающиеся
    годы» — в очередь «Похоже на дубль»; возвращает, сколько новых пар добавлено."""
    active = list(
        session.scalars(
            select(Candidate).where(
                Candidate.duplicate_of.is_(None), Candidate.full_name.is_not(None)
            )
        )
    )
    buckets: dict[tuple, list[Candidate]] = {}
    for c in active:  # корзины по слову ФИО и признаку: пары ищутся только внутри корзины
        signs = [("year", c.birth_year)] if c.birth_year else []
        signs += [("company", key) for key in employers(c)]
        for token in _tokens(c.full_name):
            for sign in signs:
                buckets.setdefault((token, *sign), []).append(c)
    seen = _seen_pairs(session)
    added = 0
    for group in buckets.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                pair = frozenset((a.id, b.id))
                if pair in seen or not names_compatible(a.full_name, b.full_name):
                    continue
                if (a.birth_year and a.birth_year == b.birth_year) or shared_employer(a, b):
                    seen.add(pair)
                    session.add(Duplicate(candidate_a=a.id, candidate_b=b.id, confidence=0.7))
                    added += 1
    session.commit()
    return added
