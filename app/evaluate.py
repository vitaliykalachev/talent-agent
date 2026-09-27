"""Вакансия и оценка кандидатов под неё (раздел 8 плана).

1. Описание вакансии → один вызов модели → «Портрет кандидата»: до 6 обязательных,
   до 4 желательных требований и «чего точно не надо», плюс жёсткие условия из текста.
   Расплывчатые и дискриминационные формулировки помечаются с предложением замены.
2. Гибридный поиск по описанию и требованиям → жёсткие условия → первые `top_n`.
3. Каждому кандидату модель отвечает по схеме `Evaluation`: вердикт и номера строк по
   каждому требованию. Код проверяет строки (вердикт без строк → «нет данных»),
   считает балл и категорию; модель ни балла, ни категории не ставит.
4. Повторный прогон оценивает только новых и изменившихся; правка требований — всех.
   Кандидаты с решением «Не подходит» не возвращаются.
"""

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, db, search
from app.anonymize import anonymize, line_range, numbered, valid_lines
from app.jobs import enqueue, stopping
from app.llm import AuthError, LLMError, get_llm
from app.models import Candidate, Feedback, Job, Match, Vacancy
from app.parse import MAX_CHARS, MIN_CHARS, RELOCATION

MUST_MAX, NICE_MAX, AVOID_MAX = 6, 4, 3
MANY = 10  # больше требований — предупреждение: список размывает оценку
TRIAL = 5
WEIGHTS = {"must": 2, "nice": 1, "avoid": 2}
VALUES = {"met": 1.0, "partial": 0.5, "no_data": 0.25, "not_met": 0.0}
CAP = 30  # потолок балла при невыполненном обязательном или подтверждённом «не надо»
# Запасные пороги категорий: работают, когда правила по вердиктам не решают.
FIT_FROM, MAYBE_FROM = 75, 55
TIE = 5  # разница меньше — «примерно равны»
PARALLEL = 4
BATCH = 8

FIT, MAYBE, UNFIT, FAILED = "fit", "maybe", "unfit", "failed"
CATEGORIES = {
    FIT: "Подходят",
    MAYBE: "Можно рассмотреть",
    UNFIT: "Скорее не подходят",
    FAILED: "Не удалось оценить",
}
VERDICTS = {
    "met": "есть",
    "partial": "частично",
    "not_met": "нет",
    "no_data": "в резюме не сказано",
}
KINDS = {"must": "Обязательно", "nice": "Желательно", "avoid": "Чего точно не надо"}
CHANGE = {
    "resume_updated": "резюме обновлено",
    "vacancy_changed": "вы изменили требования вакансии",
}

DISCRIMINATORY_RE = re.compile(
    r"возраст|\bлет\s+до\b|\bдо\s+\d{2}\s+лет|\bот\s+\d{2}\s+до\s+\d{2}\s+лет|моложе|старше\s+\d"
    r"|мужчин|женщин|\bпол\b|национальн|славян|внешност|семейн|замуж|женат|без\s+детей"
    r"|\bдет(?:и|ей)\b|беремен|прописк|\b\d{2}\s*[–—-]\s*\d{2}\s*лет|девушк|\bпар(?:ень|ни|ня)\b"
    r"|молод(?:ой|ая|ые|ых|ого|ому|ым|ую)\b|холост|мужск|женск|\bпола\b|детьми|бездет"
    r"|\b\d{2}\s*\+",
    re.IGNORECASE,
)
VAGUE_RE = re.compile(
    r"^(?:есть\s+|наличие\s+|иметь\s+)?опыт(?:\s+работы)?\.?$|^хорош\w*\s|^отличн\w*\s"
    r"|стрессоустойчив|коммуникабельн|ответственн|исполнительн|желание\s+работать|обучаем",
    re.IGNORECASE,
)
FAIR_HINT = "Уберите: возраст, пол и семейное положение в требованиях указывать нельзя."
VAGUE_HINT = "Уточните, что именно и сколько лет: «руководил цехом от 3 лет»."


# ── Разбор описания вакансии ────────────────────────────────────────────────


class DraftRequirement(BaseModel):
    name: str = Field(description="Требование коротко, 3–10 слов, проверяемое по резюме")
    kind: Literal["must", "nice", "avoid"] = Field(
        description="must — обязательно, nice — желательно, avoid — чего точно не надо"
    )
    flag: Literal["vague", "discriminatory"] | None = Field(
        None,
        description="vague — расплывчато («есть опыт»); discriminatory — возраст, пол и т. п.",
    )
    suggestion: str | None = Field(None, description="Чем заменить помеченную формулировку")


class VacancyDraft(BaseModel):
    title: str = Field(description="Название вакансии, 2–6 слов")
    requirements: list[DraftRequirement]
    city: str | None = Field(None, description="Город, если он жёстко задан")
    min_years: float | None = Field(None, description="Минимальный стаж в годах, если назван")
    max_salary: int | None = Field(None, description="Потолок зарплаты в месяц, если назван")


SYSTEM_VACANCY = f"""Ты помогаешь рекрутеру кадрового агентства понять вакансию.
Выдели из описания требования к кандидату:
- до {MUST_MAX} обязательных (kind = must) — без них кандидат не подходит;
- до {NICE_MAX} желательных (kind = nice) — плюс, но не условие;
- «чего точно не надо» (kind = avoid), если в описании это сказано, до {AVOID_MAX}.
Каждое требование — короткое и проверяемое по тексту резюме: что именно человек делал,
сколько лет, в какой отрасли. Не выдумывай требований, которых нет в описании.
Расплывчатое («есть опыт», «хорошие навыки», «ответственность») помечай flag = vague и
предлагай в suggestion конкретную замену. Требования про возраст, пол, семейное
положение, внешность, национальность помечай flag = discriminatory и предлагай убрать
или заменить на профессиональное.
Жёсткие условия — город, минимальный стаж в годах, потолок зарплаты — заполняй, только
если они прямо названы в описании."""


def _flag(name: str, flag: str | None, suggestion: str | None) -> tuple[str | None, str | None]:
    """Пометка модели плюс проверка кодом: дискриминационное не проходит незамеченным."""
    if DISCRIMINATORY_RE.search(name):
        return "discriminatory", suggestion if flag == "discriminatory" and suggestion else None
    if flag == "discriminatory":
        return flag, suggestion
    if flag == "vague" or VAGUE_RE.search(name.strip()):
        return "vague", suggestion
    return None, None


def _next_id(requirements: list[dict]) -> str:
    numbers = [int(r["id"][1:]) for r in requirements if r["id"][1:].isdigit()]
    return f"r{max(numbers, default=0) + 1}"


def _requirement(requirements, name, kind, source, flag=None, suggestion=None) -> dict:
    flag, suggestion = _flag(name, flag, suggestion)
    return {
        "id": _next_id(requirements),
        "name": name.strip(),
        "kind": kind,
        "weight": WEIGHTS[kind],
        "source": source,
        "flag": flag,
        "suggestion": suggestion,
    }


def parse_vacancy(session: Session, v: Vacancy) -> None:
    """Один вызов модели: портрет кандидата и жёсткие условия из описания. Условия,
    которые рекрутер задал сам, главнее догадки модели."""
    draft = get_llm("eval").complete_structured(VacancyDraft, SYSTEM_VACANCY, v.description)
    limits = {"must": MUST_MAX, "nice": NICE_MAX, "avoid": AVOID_MAX}
    requirements: list[dict] = []
    for item in draft.requirements:
        if sum(r["kind"] == item.kind for r in requirements) < limits[item.kind]:
            requirements.append(
                _requirement(requirements, item.name, item.kind, "ai", item.flag, item.suggestion)
            )
    if v.requirements:
        v.requirements_version += 1
    v.requirements = requirements
    v.title = v.title or draft.title
    filters = dict(v.hard_filters or {})
    for key in ("city", "min_years", "max_salary"):
        if filters.get(key) in (None, "") and getattr(draft, key) not in (None, ""):
            filters[key] = getattr(draft, key)
    v.hard_filters = filters
    session.commit()


def _changed(session: Session, v: Vacancy, requirements: list[dict]) -> None:
    v.requirements = requirements
    v.requirements_version += 1
    session.commit()


def add_requirement(session: Session, v: Vacancy, name: str, kind: str) -> None:
    if name.strip() and kind in KINDS:
        reqs = list(v.requirements)
        _changed(session, v, [*reqs, _requirement(reqs, name, kind, "user")])


def edit_requirement(session: Session, v: Vacancy, rid: str, name: str, kind: str) -> None:
    reqs = []
    for r in v.requirements:
        if r["id"] == rid and name.strip() and kind in KINDS:
            flag, suggestion = _flag(name, None, None)
            r = {**r, "name": name.strip(), "kind": kind, "weight": WEIGHTS[kind]}
            r.update(source="user", flag=flag, suggestion=suggestion)
        reqs.append(r)
    _changed(session, v, reqs)


def accept_suggestion(session: Session, v: Vacancy, rid: str) -> None:
    reqs = [
        {**r, "name": r["suggestion"], "source": "user", "flag": None, "suggestion": None}
        if r["id"] == rid and r.get("suggestion")
        else r
        for r in v.requirements
    ]
    _changed(session, v, reqs)


def delete_requirement(session: Session, v: Vacancy, rid: str) -> None:
    _changed(session, v, [r for r in v.requirements if r["id"] != rid])


def used(v: Vacancy) -> list[dict]:
    """Требования, по которым идёт оценка. Дискриминационные (возраст, пол) в оценку не
    идут, пока рекрутер их не заменит или не удалит: модель не должна судить по ним."""
    return [r for r in v.requirements if r.get("flag") != "discriminatory"]


def scored(v: Vacancy) -> list[dict]:
    return [r for r in used(v) if r["kind"] in ("must", "nice")]


# ── Кого оценивать ──────────────────────────────────────────────────────────


def query_text(v: Vacancy) -> str:
    wanted = [r["name"] for r in used(v) if r["kind"] != "avoid"]
    return "\n".join([v.title or "", *wanted, v.description or ""])


def filters_of(v: Vacancy) -> search.Filters:
    f = v.hard_filters or {}
    return search.Filters(
        city=f.get("city") or "",
        min_years=float(f["min_years"]) if f.get("min_years") not in (None, "") else None,
        max_salary=int(f["max_salary"]) if f.get("max_salary") not in (None, "") else None,
        fresh="fresh" if f.get("fresh") else "",
    )


def pool(session: Session, v: Vacancy, mode: str = "hybrid") -> list[int]:
    """Кандидаты по порядку поиска после жёстких условий, без отклонённых рекрутером."""
    found, _ = search.hybrid(session, query_text(v), mode)
    rejected = set(
        session.scalars(
            select(Match.candidate_id).where(Match.vacancy_id == v.id, Match.decision == "reject")
        )
    )
    passing = set(
        session.scalars(
            select(Candidate.id).where(
                search.active(), Candidate.id.in_(found), *filters_of(v).conditions()
            )
        )
    )
    return [cid for cid in found if cid in passing and cid not in rejected]


def resume_hash(c: Candidate) -> str:
    return hashlib.sha1(f"{c.raw_text}\n{c.resume_date}".encode()).hexdigest()


def plan(
    session: Session, v: Vacancy, limit: int, ids: list[int] | None = None, order=None
) -> list:
    """Что оценить: [(кандидат, причина)]. Причина — new, resume_updated или
    vacancy_changed; кто оценён по текущей версии и не менялся, не трогается.
    `ids` — оценить именно этих (повтор упавших), `order` — готовая выдача поиска."""
    if ids is not None:
        ordered = list(ids)
    else:
        ordered = (order if order is not None else pool(session, v))[:limit]
    if ids is None and limit >= v.top_n:  # полный прогон: и все прежние по старой версии
        old = session.scalars(
            select(Match.candidate_id).where(
                Match.vacancy_id == v.id,
                Match.requirements_version != v.requirements_version,
            )
        )
        ordered = [*ordered, *[cid for cid in old if cid not in ordered]]
    matches = {
        m.candidate_id: m
        for m in session.scalars(
            select(Match).where(Match.vacancy_id == v.id, Match.candidate_id.in_(ordered))
        )
    }
    feedback_at = _feedback_times(session, v, ordered)
    todo = []
    for cid in ordered:
        c = session.get(Candidate, cid)
        if c is None:  # кандидата удалили или id не из базы — пропускаем
            continue
        m = matches.get(cid)
        if m and m.decision == "reject":
            continue
        if m is None:
            reason = "new"
        elif m.requirements_version != v.requirements_version or (
            feedback_at.get(cid, datetime.min) > m.evaluated_at
        ):
            reason = "vacancy_changed"
        elif m.resume_hash != resume_hash(c):
            reason = "resume_updated"
        elif m.status == "failed":
            reason = m.change_reason or "new"
        else:
            continue
        todo.append((c, reason))
    return todo


def _feedback_times(session: Session, v: Vacancy, ids: list[int]) -> dict[int, datetime]:
    """Когда рекрутер последний раз поправил доводы по кандидату. «Для этой вакансии
    неважно» касается всех кандидатов вакансии."""
    mine = (Feedback.vacancy_id == v.id) | (Feedback.scope == "all")
    rows = dict(
        session.execute(
            select(Feedback.candidate_id, func.max(Feedback.created_at))
            .where(mine, Feedback.candidate_id.in_(ids))
            .group_by(Feedback.candidate_id)
        ).all()
    )
    general = session.scalar(
        select(func.max(Feedback.created_at)).where(mine, Feedback.kind == "irrelevant")
    )
    if general:
        rows = {cid: max(rows.get(cid, general), general) for cid in ids}
    return rows


def estimate(session: Session, v: Vacancy, limit: int, order=None) -> dict:
    """Цена и время оценки до старта, диапазоном: по объёму резюме и тарифу из настроек."""
    todo = plan(session, v, limit, order=order)
    n = len(todo)
    chars = sum(min(len(c.raw_text or ""), MAX_CHARS) for c, _ in todo)
    tokens_in = chars / 3 + n * (len(SYSTEM_EVAL) / 3 + len(query_text(v)) / 3 + 1500)
    tokens_out = n * 1200  # живой замер: 900–1560 на кандидата
    usd = (
        tokens_in * config.number("price_eval_in") + tokens_out * config.number("price_eval_out")
    ) / 1e6
    rub = usd * config.number("usd_rub")
    rounds = -(-n // PARALLEL)
    return {
        "count": n,
        "rub_low": rub * 0.7,
        "rub_high": rub * 1.3,
        # живой замер через хаб: 5 кандидатов за 20–29 с (два захода по 4 запроса)
        "seconds_low": rounds * 10,
        "seconds_high": rounds * 30,
    }


# ── Оценка одного кандидата ─────────────────────────────────────────────────


# Модель иногда присылает [31] или [7, 21, 23] вместо [от, до]: читаем как [31, 31] и
# [7, 23]. Схема для модели — ровно два целых.
Lines = Annotated[
    Annotated[list[int], Field(min_length=2, max_length=2)] | None,
    BeforeValidator(line_range),
]


class Check(BaseModel):
    requirement_id: str
    verdict: Literal["met", "partial", "not_met", "no_data"]
    evidence_lines: Lines = Field(
        None, description="Номера строк резюме [от, до], на которых основан вердикт"
    )
    note: str = Field("", description="Коротко, что именно в этих строках")


class Concern(BaseModel):
    text: str
    evidence_lines: Lines = Field(None, description="[от, до] или null")


class Evaluation(BaseModel):
    checks: list[Check] = Field(description="По одному на каждое требование вакансии")
    concerns: list[Concern] = Field(default_factory=list, description="Два главных сомнения")
    questions: list[str] = Field(default_factory=list, description="Три вопроса на созвон")
    fit_summary: str = Field("", description="Итог в одно-два предложения")


SYSTEM_EVAL = """Ты проверяешь резюме кандидата на соответствие вакансии для рекрутера.
Строки резюме пронумерованы: «12| текст». По каждому требованию из списка верни ровно
один check с тем же requirement_id:
- verdict: met — в резюме прямо есть; partial — есть частично или близкое;
  not_met — резюме прямо показывает, что этого нет; no_data — в резюме не сказано;
- evidence_lines — номера строк [от, до], на которых основан вердикт. Без строк вердикт
  считается no_data. Не угадывай: нет опоры в тексте — no_data.
- note — одна короткая фраза, что именно написано в этих строках.
Для требований «чего точно не надо» verdict = met значит, что это нежелательное в
резюме есть и видно в указанных строках.
concerns — два главных сомнения, со строками или с evidence_lines = null, если сомнение в
том, чего в резюме нет. questions — три вопроса на первый созвон; сначала о том, чего
нет в резюме. fit_summary — итог в одно-два предложения.
Метки [ИМЯ], [ТЕЛЕФОН], [ПОЧТА], [ССЫЛКА], [ДАТА РОЖДЕНИЯ], [ДОКУМЕНТ] — скрытые данные,
не опирайся на них и не пытайся их восстановить. Балл и итоговую категорию не ставь."""


def _structure(c: Candidate) -> str:
    p = c.parsed or {}
    if c.parse_status != "parsed":
        return "не разобрано"
    data = {
        "желаемая должность": p.get("desired_position"),
        "места работы": [
            {k: pos.get(k) for k in ("title", "company", "industry", "start", "end", "is_current")}
            for pos in p.get("positions", [])
        ],
        "навыки": p.get("skills"),
        "стаж, лет": p.get("total_years"),
        "город": p.get("city"),
        "переезд": RELOCATION.get(p.get("relocation") or "unknown"),
        "зарплата": p.get("salary_amount"),
    }
    return json.dumps(data, ensure_ascii=False)


def _corrections(session: Session, v: Vacancy, c: Candidate) -> list[str]:
    """Поправки рекрутера («Неверно»), которые касаются этой оценки."""
    rows = session.scalars(
        select(Feedback)
        .where(
            (Feedback.vacancy_id == v.id) | (Feedback.scope == "all"),
            (Feedback.candidate_id == c.id) | (Feedback.kind == "irrelevant"),
        )
        .order_by(Feedback.id)
    )
    labels = {
        "not_in_resume": "этого нет в резюме",
        "misread": "опыт понят неправильно",
        "irrelevant": "для этой вакансии неважно",
        "other": "рекрутер не согласен",
    }
    return [f"«{f.text}» — {labels.get(f.kind, f.kind)}" for f in rows]


def model_text(c: Candidate) -> str:
    return anonymize(c.raw_text[:MAX_CHARS], c.full_name, c.phones, c.emails, c.links)


SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def fair_description(v: Vacancy) -> str:
    """Описание без предложений про возраст, пол, семью и т. п.: модель оценки не должна
    их видеть. Вырезается предложение, где срабатывает DISCRIMINATORY_RE или стоит
    требование, помеченное как дискриминационное."""
    flagged = [r["name"].lower() for r in v.requirements if r.get("flag") == "discriminatory"]
    kept = [
        part
        for part in SENTENCE_RE.split(v.description or "")
        if part.strip()
        and not DISCRIMINATORY_RE.search(part)
        and not any(name in part.lower() for name in flagged)
    ]
    return " ".join(kept)


def prompt(session: Session, v: Vacancy, c: Candidate, text: str) -> str:
    reqs = "\n".join(f"- {r['id']} [{KINDS[r['kind']].lower()}] {r['name']}" for r in used(v))
    corrections = [
        anonymize(line, c.full_name, c.phones, c.emails) for line in _corrections(session, v, c)
    ]
    parts = [
        f"Вакансия {v.id}, кандидат {c.id}",
        f"Вакансия: {v.title}",
        f"Описание вакансии:\n{fair_description(v)}",
        f"Требования:\n{reqs}",
    ]
    if corrections:
        parts.append("Поправки рекрутера к прошлым оценкам:\n" + "\n".join(corrections))
    parts += [
        f"Что понял разбор резюме: {_structure(c)}",
        f"Резюме (строки пронумерованы):\n{numbered(text)}",
    ]
    return "\n\n".join(parts)


def _quote(text: str, lines: list[int] | None, limit: int = 240) -> str | None:
    if not lines:
        return None
    quote = " ".join(" ".join(text.split("\n")[lines[0] - 1 : lines[1]]).split())
    return quote if len(quote) <= limit else quote[: limit - 1].rstrip() + "…"


def checked(evaluation: Evaluation, requirements: list[dict], text: str) -> list[dict]:
    """Вердикты по требованиям вакансии: строки проверены по тексту, который видела модель;
    вердикт без существующих строк → «нет данных»; требование без ответа — тоже."""
    answers = {ch.requirement_id: ch for ch in evaluation.checks}
    out = []
    for r in requirements:
        ch = answers.get(r["id"])
        lines = valid_lines(ch.evidence_lines, text) if ch else None
        verdict = ch.verdict if ch and lines else "no_data"
        out.append(
            {
                "requirement_id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "weight": r["weight"],
                "verdict": verdict,
                "evidence_lines": lines if verdict != "no_data" else None,
                "note": (ch.note if ch and verdict != "no_data" else "") or "",
                "quote": _quote(text, lines) if verdict != "no_data" else None,
            }
        )
    return out


def score(checks: list[dict]) -> int:
    """100 × Σ(вес × значение) / Σ(вес) по обязательным и желательным; обязательное
    «нет» или подтверждённое «чего точно не надо» — не выше 30."""
    counted = [ch for ch in checks if ch["kind"] in ("must", "nice")]
    total = sum(ch["weight"] for ch in counted)
    if not total:  # требования удалили, пока оценка стояла в очереди
        return 0
    value = round(100 * sum(ch["weight"] * VALUES[ch["verdict"]] for ch in counted) / total)
    return min(value, CAP) if vetoed(checks) else value


def vetoed(checks: list[dict]) -> bool:
    return any(
        (ch["kind"] == "must" and ch["verdict"] == "not_met")
        or (ch["kind"] == "avoid" and ch["verdict"] == "met")
        for ch in checks
    )


def category(m: Match) -> str:
    """Категорию считает сервер по вердиктам: «Подходят» — все обязательные есть и
    подтверждено большинство желательных; «Можно рассмотреть» — обязательные есть, по
    части желательных нет подтверждения. Где правила не решают (обязательное частично
    или без данных, вакансия без обязательных), работают пороги 75 и 55 — но без всех
    обязательных выше «Можно рассмотреть» кандидат не поднимается, а «Можно рассмотреть»
    требует хотя бы половины обязательных «есть» или «частично»."""
    if m.status != "ok" or m.score is None:
        return FAILED
    if vetoed(m.checks):
        return UNFIT
    must = [ch for ch in m.checks if ch["kind"] == "must"]
    nice = [ch for ch in m.checks if ch["kind"] == "nice"]
    if must and all(ch["verdict"] == "met" for ch in must):
        confirmed = sum(ch["verdict"] == "met" for ch in nice)
        return FIT if not nice or confirmed > len(nice) / 2 else MAYBE
    if not must and m.score >= FIT_FROM:
        return FIT
    if 2 * sum(ch["verdict"] in ("met", "partial") for ch in must) < len(must):
        return UNFIT  # меньше половины обязательных хотя бы частично — не «Можно рассмотреть»
    return MAYBE if m.score >= MAYBE_FROM else UNFIT


def tied(scores: list[int]) -> list[bool]:
    """Кто из идущих подряд по баллу «примерно равен» соседу: разница меньше TIE."""
    return [
        (i > 0 and abs(scores[i - 1] - s) < TIE)
        or (i + 1 < len(scores) and abs(s - scores[i + 1]) < TIE)
        for i, s in enumerate(scores)
    ]


def counter(m: Match) -> str:
    """«обязательные 5 из 6 · желательные 2 из 4»."""
    parts = []
    for kind, label in (("must", "обязательные"), ("nice", "желательные")):
        items = [ch for ch in m.checks if ch["kind"] == kind]
        if items:
            met = sum(ch["verdict"] == "met" for ch in items)
            parts.append(f"{label} {met} из {len(items)}")
    return " · ".join(parts)


def reasons(checks: list[dict]) -> list[dict]:
    """«Почему подходит»: три требования с лучшими вердиктами и их цитаты."""
    good = [ch for ch in checks if ch["kind"] != "avoid" and ch["verdict"] in ("met", "partial")]
    good.sort(key=lambda ch: (ch["verdict"] != "met", ch["kind"] != "must"))
    return [
        {k: ch[k] for k in ("requirement_id", "name", "verdict", "evidence_lines", "quote", "note")}
        for ch in good[:3]
    ]


def evaluate_one(llm, session: Session, v: Vacancy, c: Candidate) -> dict:
    text = model_text(c)
    if len(text.strip()) < MIN_CHARS:
        raise LLMError("в резюме нет текста")
    if not scored(v):
        raise LLMError("у вакансии нет требований, оценивать не по чему")
    answer = llm.complete_structured(Evaluation, SYSTEM_EVAL, prompt(session, v, c, text))
    checks = checked(answer, used(v), text)
    concerns = []
    for item in answer.concerns[:2]:
        lines = valid_lines(item.evidence_lines, text)
        concerns.append({"text": item.text, "evidence_lines": lines, "quote": _quote(text, lines)})
    return {
        "status": "ok",
        "error": None,
        "checks": checks,
        "raw_checks": [ch.model_dump() for ch in answer.checks],
        "score": score(checks),
        "reasons": reasons(checks),
        "concerns": concerns,
        "questions": [q for q in answer.questions if q.strip()][:3],
        "fit_summary": answer.fit_summary,
    }


# ── Задача evaluate ─────────────────────────────────────────────────────────


_start_lock = threading.Lock()


def start(session: Session, v: Vacancy, limit: int, ids: list[int] | None = None) -> Job:
    """Ставит оценку. Пока по вакансии идёт оценка, вторая не ставится: двойной клик
    не оплачивает прогон дважды."""
    with _start_lock:
        live = session.scalars(
            select(Job).where(
                Job.kind == "evaluate", Job.status.in_(("queued", "running", "paused"))
            )
        )
        if job := next((j for j in live if j.payload.get("vacancy_id") == v.id), None):
            return job
        payload = {"vacancy_id": v.id, "limit": limit}
        if ids is not None:
            payload["ids"] = ids
        job = enqueue(session, "evaluate", payload)
        session.commit()
        return job


def _save(session, v, c, reason, result, model) -> None:
    m = session.get(Match, (v.id, c.id)) or Match(vacancy_id=v.id, candidate_id=c.id)
    if m.status == "ok" and m.score is not None:
        m.prev_score = m.score
    elif reason == "new":
        m.prev_score = None
    for key, value in result.items():
        setattr(m, key, value)
    if result["status"] == "failed":
        m.score = None
    m.change_reason = reason
    m.requirements_version = v.requirements_version
    m.resume_hash = resume_hash(c)
    m.evaluated_at = datetime.now()
    m.model = model
    session.add(m)


def run_evaluate(job_id: int) -> None:
    llm = get_llm("eval")
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        v = session.get(Vacancy, job.payload["vacancy_id"])
        if job.status == "queued":
            v.last_run_at = datetime.now()
        order = pool(session, v)
        ranks = {cid: i for i, cid in enumerate(order, start=1)}
        for m in session.scalars(select(Match).where(Match.vacancy_id == v.id)):
            m.rank = ranks.get(m.candidate_id)
        todo = plan(session, v, job.payload["limit"], job.payload.get("ids"), order)
        job.status, job.total = "running", len(todo) + job.progress
        session.commit()
        errors = job.error.splitlines() if job.error else []
        known = {c.id for c, _ in todo} | set(ranks)
        errors += [
            f"{cid}: такого кандидата в базе нет"
            for cid in job.payload.get("ids") or []
            if cid not in known and session.get(Candidate, cid) is None
        ]
        job.error = "\n".join(errors) or None
        with ThreadPoolExecutor(PARALLEL) as ex:
            for i in range(0, len(todo), BATCH):
                session.refresh(job)
                if stopping.is_set() or job.status == "paused":
                    return
                futures = {ex.submit(_attempt, llm, v, c): (c, r) for c, r in todo[i : i + BATCH]}
                # Каждый ответ сохраняется, как только пришёл: после сбоя или остановки
                # оплаченные оценки не пропадают и повторно не отправляются.
                for future in as_completed(futures):
                    c, reason = futures[future]
                    result = future.result()
                    if isinstance(result, AuthError):
                        for other in futures:
                            other.cancel()
                        job.status, job.error = "failed", str(result)
                        job.finished_at = datetime.now()
                        session.commit()
                        return
                    if isinstance(result, Exception):
                        errors.append(f"{c.id}: {result}")
                        result = {"status": "failed", "error": str(result)}
                    _save(session, v, c, reason, result, llm.model)
                    session.get(Match, (v.id, c.id)).rank = ranks.get(c.id)
                    job.progress += 1
                    job.error = "\n".join(errors) or None
                    with llm._lock:
                        spent = llm.tokens_in, llm.tokens_out
                        llm.tokens_in = llm.tokens_out = 0
                    job.payload = {
                        **job.payload,
                        "tokens_in": job.payload.get("tokens_in", 0) + spent[0],
                        "tokens_out": job.payload.get("tokens_out", 0) + spent[1],
                    }
                    session.commit()
        job.status, job.finished_at = "done", datetime.now()
        session.commit()


def _attempt(llm, v: Vacancy, c: Candidate):
    """Оценка в своей сессии (потоки не делят сессию); сбой — ошибка, а не падение задачи.
    Временный сбой сети адаптер уже повторил трижды; упавших повторяет следующий прогон."""
    try:
        with db.SessionLocal() as s:
            return evaluate_one(llm, s, s.get(Vacancy, v.id), s.get(Candidate, c.id))
    except LLMError as exc:  # в том числе AuthError: задача остановится с понятным текстом
        return exc
