"""Поиск кандидатов: гибридный (BM25 + вектор → слияние RRF → фильтры) и по контактам.

Гибрид по разделу 8 плана: BM25 (`bm25s`, русский стеммер PyStemmer) по полному
тексту резюме ловит точные термины из хвоста, которого не видит вектор; вектор по
поисковой карточке ловит смысл без общих слов. Из каждого берутся 200 лучших,
списки сливаются по RRF: балл = Σ 1 / (60 + место). Индекс BM25 держится в памяти
и перестраивается, когда меняется состав базы.

Фильтры — город, минимальный стаж, потолок зарплаты, свежесть — работают в SQL по
полям записи и разобранного резюме. Для каждого фильтра считается, скольких он
убирает, — это подпись на плашке и подсказка при пустой выдаче.
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import date

import bm25s
import numpy as np
import Stemmer
from markupsafe import Markup, escape
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app import db, embed
from app.importer.normalize import EMAIL_RE, extract_phones, find_name, normalize_phone
from app.models import Candidate

POOL = 200  # сколько лучших берётся из BM25 и из вектора
RRF_K = 60
PAGE_SIZE = 20
VERY_CLOSE, CLOSE = 0.40, 0.25  # пороги близости для BERTA, подобраны на демо-базе


@dataclass
class Filters:
    city: str = ""
    min_years: float | None = None
    max_salary: int | None = None
    fresh: str = ""  # fresh | stale | ""
    batch: int | None = None  # «Из загрузки…»
    batch_label: str = ""

    def active(self) -> dict[str, str]:
        """Включённые фильтры с подписью для плашки."""
        labels = {}
        if self.batch is not None:
            labels["batch"] = self.batch_label.lower() or "из загрузки"
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
        if self.batch is not None and skip != "batch":
            found.append(Candidate.batch_id == self.batch)
        return found


@dataclass
class Result:
    # meaning | contacts | list | words (до разбора, отпечатков ещё нет) |
    # warming (модель поиска ещё грузится после запуска — по словам, BM25)
    mode: str
    hits: list = field(default_factory=list)  # (Candidate, близость | None, фрагмент | None)
    total: int = 0
    removed: dict[str, int] = field(default_factory=dict)
    relax: tuple[str, int] | None = None  # какое условие снять, чтобы кто-то нашёлся
    searched: int = 0  # среди скольких с отпечатком искали
    base: int = 0  # сколько всего кандидатов
    broken: str = ""  # загрузка модели упала дважды — честная строка вместо «загружается»


def closeness(score: float) -> str:
    if score >= VERY_CLOSE:
        return "очень близко"
    return "близко" if score >= CLOSE else "возможно"


def active():
    return Candidate.duplicate_of.is_(None)


_bm25_lock = threading.Lock()
_bm25: dict = {"key": None, "ids": [], "retriever": None}


def _tokens(texts: list[str]):
    return bm25s.tokenize(
        texts,
        stopwords="ru",
        stemmer=Stemmer.Stemmer("russian"),
        return_ids=False,
        show_progress=False,
    )


def _bm25_index(session: Session):
    """Индекс BM25 по полному `raw_text` активных записей; ключ — папка базы, число
    записей и последний id: импорт и слияние дублей меняют ключ, и индекс строится заново."""
    count, last = session.execute(
        select(func.count(Candidate.id), func.max(Candidate.id)).where(active())
    ).one()
    key = (str(db.data_dir), count, last)
    with _bm25_lock:
        if _bm25["key"] != key:
            rows = session.execute(
                select(Candidate.id, Candidate.raw_text).where(active()).order_by(Candidate.id)
            ).all()
            retriever = None
            if rows:
                retriever = bm25s.BM25()
                retriever.index(_tokens([r[1] or "" for r in rows]), show_progress=False)
            _bm25.update(key=key, ids=[r[0] for r in rows], retriever=retriever)
        return _bm25["ids"], _bm25["retriever"]


def reset() -> None:
    """База очищена: id в SQLite начнутся заново, и тот же ключ указал бы на старый индекс."""
    with _bm25_lock:
        _bm25.update(key=None, ids=[], retriever=None)


def lexical(session: Session, query: str, k: int = POOL) -> list[int]:
    """До k лучших по BM25; кандидаты без единого общего слова с запросом не берутся."""
    ids, retriever = _bm25_index(session)
    if retriever is None:
        return []
    docs, scores = retriever.retrieve(_tokens([query]), k=min(k, len(ids)), show_progress=False)
    return [ids[d] for d, sc in zip(docs[0], scores[0], strict=True) if sc > 0]


def rrf(*rankings: list[int], k: int = RRF_K) -> list[int]:
    """Слияние списков по местам: Σ 1 / (k + место), место считается с единицы."""
    total: dict[int, float] = {}
    for ranking in rankings:
        for place, cid in enumerate(ranking, start=1):
            total[cid] = total.get(cid, 0.0) + 1.0 / (k + place)
    return sorted(total, key=lambda cid: -total[cid])


def _ranked(session: Session, query: str, mode: str = "hybrid"):
    """Слитый список, близость по вектору и кандидаты, у которых есть слова запроса."""
    ids, matrix = embed.index()
    closeness_by_id: dict[int, float] = {}
    by_vector: list[int] = []
    if len(ids) and mode in ("hybrid", "vector") and embed.usable():
        scores = matrix @ embed.encode([query], "query")[0]
        top = np.argsort(-scores)[:POOL]
        by_vector = [int(ids[i]) for i in top]
        closeness_by_id = {int(i): float(s) for i, s in zip(ids, scores, strict=True)}
    by_words = lexical(session, query) if mode in ("hybrid", "bm25") else []
    return rrf(by_vector, by_words), closeness_by_id, set(by_words)


def hybrid(session: Session, query: str, mode: str = "hybrid") -> tuple[list[int], dict]:
    """Кандидаты по запросу до фильтров и близость по вектору для каждого из них.

    mode: hybrid — BM25 + вектор (по умолчанию), vector — только вектор, bm25 — только
    BM25; два последних нужны набору проверки `eval/`.
    """
    pool, scores, _ = _ranked(session, query, mode)
    return pool, scores


def _surname(word: str):
    """Условие «фамилия — это слово»: ФИО начинается с него, «Никитина» не подходит."""
    name = func.pylower(Candidate.full_name)
    return or_(name == word, name.like(f"{word} %"))


def looks_like_contact(query: str, session: Session | None = None) -> str | None:
    q = query.strip()
    if EMAIL_RE.fullmatch(q):
        return "email"
    if sum(ch.isdigit() for ch in q) >= 10 and normalize_phone(q):
        return "phone"
    if 2 <= len(q.split()) <= 3 and find_name(q.title(), lines=1):
        return "name"
    word = q.lower()
    if session is not None and re.fullmatch(r"[а-яёa-z-]{2,}", word):
        # одно слово, и в базе есть такая фамилия — ищем человека, а не смысл
        if session.scalar(select(Candidate.id).where(active(), _surname(word)).limit(1)):
            return "surname"
    return None


def by_contacts(session: Session, query: str, kind: str) -> list[Candidate]:
    q = query.strip()
    if kind == "phone":
        cond = cast(Candidate.phones, Text).contains(f'"{normalize_phone(q)}"')
    elif kind == "email":
        cond = func.pylower(cast(Candidate.emails, Text)).contains(f'"{q.lower()}"')
    elif kind == "surname":
        cond = _surname(q.lower())
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


# Строки шапки CRM и контактов во фрагмент не идут: «ID: 10062 ФИО: … Телефон: …».
CRM_KEY_RE = re.compile(
    r"(?i)^\s*(id|ид|код|номер|фио|ф\.?\s?и\.?\s?о\.?|имя|фамилия|отчество|кандидат|"
    r"телефон\w*|тел\.?|моб\w*|e-?mail|email|почта|эл\.?\s?почта|дата рождения|"
    r"год рождения|др|дата обновления|дата резюме|обновлено|файл\w*)\s*:"
)
FIELD_LINE_RE = re.compile(r"^[^:]{1,30}:\s")
SNIPPET = 160  # знаков во фрагменте под строкой выдачи


def _paragraphs(c: Candidate) -> list[str]:
    """Абзацы резюме без шапки CRM, строк с контактами и строки с ФИО."""
    name = (c.full_name or "").lower().replace("ё", "е")
    kept = []
    for line in (c.raw_text or "").splitlines():
        text = line.strip(" #*_\t")
        if (
            CRM_KEY_RE.match(text)
            or EMAIL_RE.search(text)
            or extract_phones(text)
            or (name and text.lower().replace("ё", "е") == name)
        ):
            text = ""
        kept.append(text)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", "\n".join(kept)) if b.strip()]
    # блоки в начале только из строк «Колонка: значение» — остаток шапки CRM и резюме
    # (город, должность, компания); во фрагмент они не идут, если есть что-то ещё
    while len(blocks) > 1 and all(FIELD_LINE_RE.match(ln) for ln in blocks[0].splitlines()):
        blocks = blocks[1:]
    out = []
    for block in blocks:
        out += [ln for ln in block.splitlines() if ln.strip()] if len(block) > 400 else [block]
    return [" ".join(p.split()) for p in out if len(p) > 20]


def _stem(word: str) -> str:
    return word[:5].lower().replace("ё", "е")


def _window(text: str, at: int) -> str:
    """Не длиннее SNIPPET знаков вокруг позиции `at`, края по словам, с многоточиями."""
    if len(text) <= SNIPPET:
        return text
    start = max(0, min(at - 40, len(text) - SNIPPET))
    if start:
        start = text.find(" ", start) + 1 or start
    piece = text[start : start + SNIPPET - 2]
    if start + len(piece) < len(text):
        piece = piece.rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"
    return ("…" if start else "") + piece


def fragment(c: Candidate, query: str) -> Markup:
    """Абзац резюме со словами запроса (они подсвечены); без них — «Кратко» из разбора
    или первый содержательный абзац. Модель здесь не нужна: фрагмент ищется по словам
    за миллисекунды, а не кодируется заново на каждого кандидата страницы."""
    stems = {_stem(w) for w in re.findall(r"\w{4,}", query)}
    paragraphs = _paragraphs(c)
    hits = [sum(_stem(w) in stems for w in re.findall(r"\w{4,}", p)) for p in paragraphs]
    if hits and max(hits) > 0:
        best = paragraphs[hits.index(max(hits))]
        first = next(m.start() for m in re.finditer(r"\w{4,}", best) if _stem(m.group()) in stems)
        best = _window(best, first)
    else:
        summary = " ".join(((c.parsed or {}).get("summary") or "").split())
        best = _window(summary or (paragraphs[0] if paragraphs else ""), 0)
    return Markup("").join(
        Markup(f"<mark>{escape(w)}</mark>") if len(w) >= 4 and _stem(w) in stems else escape(w)
        for w in re.split(r"(\w+)", best)
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

    if kind := looks_like_contact(query, session):
        found = by_contacts(session, query, kind)
        if found or kind != "name":
            return Result("contacts", [(c, None, None) for c in found], len(found), base=base)

    ids, _ = embed.index()
    # Отпечатков ещё нет (до разбора) или модель ещё грузится после запуска — ищем по
    # словам (BM25): каждое слово запроса отдельно, а не всю строку одной фразой
    if not len(ids):
        mode = "words"
    else:
        mode = "meaning" if embed.ready() else "warming"
    if mode == "warming":
        embed.ensure()  # загрузка упала — запускаем снова в фоне
    pool, scores, by_words = _ranked(session, query, "hybrid" if mode == "meaning" else "bm25")
    # Ниже порога близости и без единого слова запроса — не «возможно», а мимо.
    pool = [i for i in pool if i in by_words or scores.get(i, 0.0) >= CLOSE]
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
    hits = [(c, closeness(scores.get(c.id, 0.0)), fragment(c, query)) for c in page_items]
    return Result(
        mode,
        hits,
        len(ordered),
        removed,
        relax,
        searched=len(ids),
        base=base,
        broken=embed.broken() if mode == "warming" else "",
    )
