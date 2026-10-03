"""Разбор резюме моделью в схему CandidateProfile (раздел 5 плана).

Задача `parse` берёт список id, идёт пачками по 20, в каждой пачке — до 4 запросов
параллельно. Модели уходит только обезличенный текст. После пачки прогресс и
результаты сохраняются одной транзакцией, поэтому перезапуск продолжает с места.
Исправления пользователя (`parsed["edits"]`) повторный разбор не перезаписывает.
"""

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import config, db
from app.anonymize import anonymize, numbered, quote_span, valid_lines
from app.jobs import enqueue, stopping
from app.lines import resume_lines
from app.llm import AuthError, LLMError, get_llm
from app.models import Candidate, Embedding, Job

BATCH = 20
PARALLEL = 4
MAX_CHARS = 12_000  # длиннее резюме не бывает по делу; хвост режем ради цены
MIN_CHARS = 40
CURRENT_RE = re.compile(
    r"^\s*(?:по\s+|до\s+)?(?:н\.?\s*/?\s*в\.?|наст(?:оящее|\.)?\s*(?:вр(?:емя|\.)?)?|сейчас"
    r"|сей\s+день|сих\s+пор|present|now|current)\s*\.?$",
    re.IGNORECASE,
)
COMPANY_FORMS_RE = re.compile(
    r"\b(?:ооо|оао|зао|пао|ао|ип|нко|ано|фгуп|гуп|муп|llc|ltd|inc|gmbh)\b|[«»\"'“”„]",
    re.IGNORECASE,
)
YEARS_MISMATCH = 1.0
EDITABLE = {
    "desired_position": "Желаемая должность",
    "city": "Город",
    "total_years": "Стаж, лет",
    "salary_amount": "Зарплата",
    "relocation": "Переезд",
    "skills": "Навыки",
    "languages": "Языки",
    "summary": "Кратко",
}
RELOCATION = {
    "no_relocation": "не готов к переезду",
    "relocation_possible": "переезд возможен",
    "relocation_desirable": "хочет переехать",
    "unknown": "в резюме не сказано",
}


class Position(BaseModel):
    title: str = Field(description="Должность, без названия компании")
    company: str | None = Field(None, description="Компания")
    industry: str | None = Field(None, description="Отрасль компании")
    start: str | None = Field(None, description="Начало: ГГГГ-ММ или ГГГГ")
    end: str | None = Field(None, description="Конец: ГГГГ-ММ или ГГГГ; null, если работает сейчас")
    is_current: bool = Field(False, description="Работает здесь сейчас («по н.в.»)")
    team_size: int | None = Field(None, description="Сколько людей в подчинении")
    source_lines: list[int] | None = Field(
        None, description="Номера строк резюме [от, до], где описано это место работы"
    )

    @model_validator(mode="after")
    def _current(self):
        if self.end and CURRENT_RE.match(self.end):
            self.is_current, self.end = True, None
        return self


class Education(BaseModel):
    institution: str | None = None
    degree: str | None = None
    year: int | None = None


class Salary(BaseModel):
    amount: int | None = Field(None, description="Сумма в месяц, число без пробелов")
    currency: str | None = Field(None, description="RUB, USD, EUR, KZT…")
    kind: Literal["gross", "net", "unknown"] = Field(
        "unknown", description="gross — до вычета налога, net — на руки"
    )


class CandidateProfile(BaseModel):
    desired_position: str | None = Field(None, description="Желаемая должность из резюме")
    positions: list[Position] = Field(
        default_factory=list, description="Места работы, новые первыми"
    )
    skills: list[str] = Field(default_factory=list)
    total_years: float | None = Field(None, description="Общий стаж в годах, как в резюме")
    city: str | None = None
    relocation: Literal[
        "no_relocation", "relocation_possible", "relocation_desirable", "unknown"
    ] = Field(
        "unknown",
        description="no_relocation — переезд не рассматривает; relocation_possible — готов "
        "рассмотреть («рассматриваю переезд»); relocation_desirable — сам хочет переехать",
    )
    salary_expect: Salary | None = Field(None, description="Ожидаемая зарплата")
    languages: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    summary: str = Field(
        "", description="Три предложения: кто это и чем силён — только то, что есть в резюме"
    )
    summary_quote: str | None = Field(
        None, description="Дословный фрагмент резюме до 15 слов, на котором основано summary"
    )
    resume_date: str | None = Field(None, description="Дата резюме ГГГГ-ММ-ДД, если видна")


SYSTEM = """Ты разбираешь резюме кандидата для кадрового агентства и заполняешь схему.

Правила:
- Бери только то, что написано в резюме. Не выдумывай: нет данных — null или пустой список.
- desired_position — желаемая должность, если она названа.
- Места работы — от новых к старым. Даты — ГГГГ-ММ или ГГГГ. title — только должность,
  без названия компании: компания — в поле company.
- Если работа идёт сейчас («по н.в.», «по н/в», «по настоящее время», «наст. время»,
  «сейчас», «по сей день», «до сих пор»), ставь is_current = true и end = null.
- total_years — общий стаж в годах, как он указан в резюме; не указан — null.
- salary_expect — сумма в месяц числом, валюта, gross/net, если сказано.
- relocation: no_relocation — переезд не рассматривает («переезд не рассматриваю»,
  «без переезда»); relocation_possible — готов рассмотреть («рассматриваю переезд»,
  «готов рассматривать переезд», «готов к переезду»); relocation_desirable — сам хочет
  переехать («хочу переехать», «планирую переезд в …»); unknown — не сказано.
- summary — три коротких предложения своими словами: кто это, опыт, чем силён. Только
  то, что есть в резюме: без оценочных слов («успешно», «эффективно», «сильный»), без
  чисел, которых нет в тексте, и без пересчётов («на 58 %» вместо «с 12 до 5 дней»).
- Строки резюме пронумерованы: «12| текст». source_lines у каждого места работы —
  номера строк [от, до], где оно описано; номер ставь из начала строки, сам номер
  в значения полей не переноси.
- summary_quote — дословный фрагмент резюме до 15 слов, на котором основано summary.
  Копируй символ в символ, без номера строки.
- Метки вида [ИМЯ], [ТЕЛЕФОН], [ПОЧТА], [ССЫЛКА], [ДАТА РОЖДЕНИЯ], [ДОКУМЕНТ] —
  скрытые данные, не пытайся их восстановить."""


def model_text(c: Candidate) -> str:
    """Обезличенный текст резюме для разбора и оценки; строки совпадают со строками
    `resume_lines(raw_text)` — по ним же подсвечивает «Показать в резюме»."""
    text = resume_lines(c.raw_text)[:MAX_CHARS]
    return anonymize(text, c.full_name, c.phones, c.emails, c.links)


def model_input(c: Candidate) -> str:
    return numbered(model_text(c))


def company_key(name: str | None) -> str | None:
    """Код компании для сравнения: без ООО/АО/ИП, кавычек и регистра."""
    if not name:
        return None
    return " ".join(COMPANY_FORMS_RE.sub(" ", name).lower().replace("ё", "е").split()) or None


def _month(value: str | None, end: bool = False) -> int | None:
    """«2019-03» → номер месяца от нулевого года; «2019» — январь (или декабрь для конца)."""
    match = re.match(r"^\s*(\d{4})(?:-(\d{1,2}))?", value or "")
    if not match:
        return None
    month = int(match.group(2) or (12 if end else 1))
    return int(match.group(1)) * 12 + month - 1


def years_by_positions(positions: list[dict], resume_date: date) -> float | None:
    """Стаж по местам работы: пересечения объединяются, текущее — до даты резюме."""
    spans = []
    for p in positions:
        start = _month(p.get("start"))
        stop = resume_date.year * 12 + resume_date.month - 1 if p.get("is_current") else None
        stop = stop if stop is not None else _month(p.get("end"), end=True)
        if start is not None and stop is not None and stop >= start:
            spans.append((start, stop + 1))
    if not spans:
        return None
    total, cur_start, cur_stop = 0, *sorted(spans)[0]
    for start, stop in sorted(spans)[1:]:
        if start > cur_stop:
            total += cur_stop - cur_start
            cur_start, cur_stop = start, stop
        else:
            cur_stop = max(cur_stop, stop)
    return round((total + cur_stop - cur_start) / 12, 1)


# Прямые слова резюме о переезде главнее догадки модели: «рассматриваю переезд» она
# понимала как «хочет переехать». Отказ проверяется первым — в нём те же слова.
RELOCATION_SAID = [
    (
        re.compile(
            r"\bне\s+(?:рассматрива\w*|готов\w*\s+к)\s+переезд|\bпереезд\w*\s+не\s+рассматрива"
            r"|\bбез\s+переезда",
            re.IGNORECASE,
        ),
        "no_relocation",
    ),
    (
        re.compile(
            r"\b(?:рассматрива\w*|готов\w*\s+(?:рассматривать|рассмотреть|к))\s+переезд"
            r"|\bпереезд\w*\s+(?:возмож|рассматрива)",
            re.IGNORECASE,
        ),
        "relocation_possible",
    ),
]
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def grounded(summary: str, text: str) -> str:
    """«Кратко» без фраз с числами, которых нет в резюме: модель пересчитывает «с 12 до
    5 дней» в «на 58 %» и приписывает «команду из 45 человек»."""
    have = {n.replace(",", ".") for n in NUMBER_RE.findall(text)}
    kept = [
        s
        for s in SENTENCE_END_RE.split(summary or "")
        if {n.replace(",", ".") for n in NUMBER_RE.findall(s)} <= have
    ]
    return " ".join(kept)


def without_company(title: str, company: str | None) -> str:
    """Должность без названия компании: в резюме одной строкой модель их склеивала
    («директор по производству АО «ОДК-Кузнецов»»). Только компания — как есть."""
    at = (title or "").lower().find((company or "").lower()) if company else -1
    if at < 0:
        return title
    rest = " ".join((title[:at] + title[at + len(company) :]).split()).strip(" ,—–-:;")
    return rest or title


def to_parsed(profile: CandidateProfile, c: Candidate, seen: str | None = None) -> dict:
    """Результат модели + проверки кода + прежние правки пользователя.

    Строки-источники мест работы проверяются по тексту, который видела модель
    (`lines_ok`), цитата summary — по оригиналу; код компании и стаж считает код;
    стаж модели остаётся только для сверки: расхождение больше года — «проверьте».
    """
    data = profile.model_dump()
    seen = model_text(c) if seen is None else seen
    data["summary"] = grounded(data["summary"], seen)
    said = next((value for pattern, value in RELOCATION_SAID if pattern.search(seen)), None)
    data["relocation"] = said or data["relocation"]
    for pos in data["positions"]:
        pos["title"] = without_company(pos["title"], pos["company"])
        pos["source_lines"] = valid_lines(pos["source_lines"], seen)
        pos["lines_ok"] = pos["source_lines"] is not None
        pos["company_key"] = company_key(pos["company"])
    data["summary_quote_ok"] = bool(
        data["summary_quote"] and quote_span(data["summary_quote"], c.raw_text)
    )
    stated = data["total_years"]
    counted = years_by_positions(data["positions"], c.resume_date or date.today())
    data["total_years"] = counted if counted is not None else stated
    data["total_years_stated"] = stated
    data["total_years_check"] = (
        stated is not None and counted is not None and abs(stated - counted) > YEARS_MISMATCH
    )
    salary = data.pop("salary_expect") or {}
    data["salary_amount"] = salary.get("amount")
    data["salary_currency"] = salary.get("currency")
    data["salary_kind"] = salary.get("kind", "unknown")
    edits = (c.parsed or {}).get("edits") or {}
    for field, edit in edits.items():
        data[field] = edit["value"]
    data["edits"] = edits
    return data


def apply_edit(c: Candidate, field: str, raw: str) -> None:
    """Правка пользователя: значение и отметка «исправлено вами <дата>»."""
    if field in ("skills", "languages"):
        value = [v.strip() for v in raw.split(",") if v.strip()]
    elif field in ("total_years", "salary_amount"):
        number = float(re.sub(r"\s", "", raw).replace(",", "."))
        if not math.isfinite(number) or number < 0:
            raise ValueError("нужно неотрицательное число")
        value = number if field == "total_years" else int(number)
    else:
        value = raw.strip()
    parsed = dict(c.parsed or {})
    parsed[field] = value
    parsed["edits"] = {**parsed.get("edits", {}), field: {"value": value, "at": str(date.today())}}
    c.parsed = parsed


_start_lock = threading.Lock()


def start_parse(session: Session, ids: list[int]) -> Job:
    """Ставит разбор. Если разбор уже идёт (в очереди, идёт или на паузе), новой задачи
    не будет: недостающие записи дописываются в живую — двойной клик не оплачивает
    разбор дважды."""
    with _start_lock:
        session.query(Candidate).filter(Candidate.id.in_(ids)).update(
            {Candidate.parse_status: "new", Candidate.parse_error: None},
            synchronize_session=False,
        )
        live = session.scalar(
            select(Job)
            .where(Job.kind == "parse", Job.status.in_(("queued", "running", "paused")))
            .order_by(Job.id)
            .limit(1)
        )
        if live:
            known = live.payload.get("ids", [])
            added = [i for i in ids if i not in set(known)]
            live.payload = {**live.payload, "ids": [*known, *added]}
            live.total = len(known) + len(added)
            session.commit()
            return live
        job = enqueue(session, "parse", {"ids": ids})
        job.total = len(ids)
        session.commit()
        return job


def waiting_ids(session: Session, limit: int | None = None) -> list[int]:
    q = (
        select(Candidate.id)
        .where(Candidate.duplicate_of.is_(None), Candidate.parse_status == "new")
        .order_by(Candidate.id)
    )
    return list(session.scalars(q.limit(limit) if limit else q))


def estimate(session: Session, ids: list[int]) -> dict:
    """Примерная цена и время разбора до старта: по числу символов и тарифу из настроек."""
    chars = 0
    for i in range(0, len(ids), 500):
        part = ids[i : i + 500]
        chars += session.scalar(
            select(
                func.coalesce(func.sum(func.min(func.length(Candidate.raw_text), MAX_CHARS)), 0)
            ).where(Candidate.id.in_(part))
        )
    n = len(ids)
    tokens_in = chars / 3 + n * (len(SYSTEM) / 3 + 900)  # 900 — схема ответа
    tokens_out = n * 700
    usd = (
        tokens_in * config.number("price_parse_in") + tokens_out * config.number("price_parse_out")
    ) / 1e6
    rub = usd * config.number("usd_rub")
    return {
        "count": n,
        "chars": chars,
        "usd": usd,
        "rub": rub,
        # Живой замер этапа 3 через хаб: 30 резюме — 9,4 ₽ при вилке 9,0–16,6 ₽; 5 резюме
        # за 16–20 с, 30 — за 68–77 с. У ревью этапа 2 цена была у верха вилки, а 5 резюме
        # шли 60 с — с повторами ответов не по форме, которые теперь чинятся без запроса.
        # Время: запросы идут по PARALLEL сразу, ответ хаба — 8–20 с.
        "rub_low": rub * 0.7,
        "rub_high": rub * 1.3,
        "seconds_low": -(-n // PARALLEL) * 8,
        "seconds_high": -(-n // PARALLEL) * 20,
    }


def _ask(llm, text: str):
    try:
        return llm.complete_structured(CandidateProfile, SYSTEM, text)
    except AuthError:
        raise
    except LLMError as exc:
        return exc


def _tick(job: Job) -> None:
    """Точки «время — прогресс» за последние 15 минут: по ним считается остаток."""
    now = time.time()
    ticks = [t for t in job.payload.get("ticks", []) if now - t[0] <= 15 * 60]
    job.payload = {**job.payload, "ticks": [*ticks, [now, job.progress]]}


def run_parse(job_id: int) -> None:
    llm = get_llm("parse")
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        job.status, job.total = "running", len(job.payload["ids"])
        _tick(job)
        session.commit()
        errors = job.error.splitlines() if job.error else []
        with ThreadPoolExecutor(PARALLEL) as pool:
            while job.progress < job.total:
                session.refresh(job)
                if stopping.is_set() or job.status == "paused":
                    return
                ids = job.payload["ids"]  # «Разобрать заново» могла дописать запись
                job.total = len(ids)
                batch = [
                    session.get(Candidate, i) for i in ids[job.progress : job.progress + BATCH]
                ]
                # разобранное другой задачей не отправляем повторно
                batch = [c for c in batch if c and c.parse_status != "parsed"]
                empty = [c for c in batch if len(c.raw_text.strip()) < MIN_CHARS]
                todo = [c for c in batch if c not in empty]
                texts = {c.id: model_text(c) for c in todo}
                futures = {pool.submit(_ask, llm, numbered(texts[c.id])): c for c in todo}
                for c in empty:
                    c.parse_status, c.parse_error = "failed", "в резюме нет текста"
                session.commit()
                # Каждая запись сохраняется, как только пришёл ответ: после сбоя или
                # остановки разобранные повторно не отправляются. Отказ ключа или баланса
                # отменяет запросы, которые ещё не ушли, а ушедшие (до PARALLEL) оплачены:
                # их ответы дожидаемся, сохраняем и только потом останавливаем задачу.
                stop = None
                for future in as_completed(futures):
                    if future.cancelled():
                        continue
                    c = futures[future]
                    try:
                        result = future.result()
                    except AuthError as exc:
                        stop = stop or exc
                        for other in futures:
                            other.cancel()
                        continue
                    if isinstance(result, LLMError):
                        c.parse_status, c.parse_error = "failed", str(result)
                    else:
                        c.parsed, c.parse_status, c.parse_error = (
                            to_parsed(result, c, texts[c.id]),
                            "parsed",
                            None,
                        )
                        session.execute(delete(Embedding).where(Embedding.candidate_id == c.id))
                    session.commit()
                if stop:  # прогресс не двигаем: повтор пройдёт пачку и пропустит разобранных
                    job.status, job.error = "failed", str(stop)
                    job.finished_at = datetime.now()
                    session.commit()
                    return
                errors += [f"{c.id}: {c.parse_error}" for c in batch if c.parse_status == "failed"]
                job.progress = min(job.progress + BATCH, job.total)
                job.error = "\n".join(errors) or None
                session.refresh(job, ["payload"])  # не затереть дописанные записи
                job.payload = {
                    **job.payload,
                    "tokens_in": job.payload.get("tokens_in", 0) + llm.tokens_in,
                    "tokens_out": job.payload.get("tokens_out", 0) + llm.tokens_out,
                }
                llm.tokens_in = llm.tokens_out = 0
                _tick(job)
                session.commit()
        job.status, job.finished_at = "done", datetime.now()
        _queue_embed(session)  # отпечатки для всех разобранных без отпечатка
        session.commit()


def _queue_embed(session: Session) -> None:
    live = select(Job.id).where(Job.kind == "embed", Job.status.in_(("queued", "running")))
    if session.scalar(live) is None:
        enqueue(session, "embed", {})
