"""Готовая демо-вакансия с записанными ответами модели: «Результат по вакансии» без ключа.

Ответы модели для демо не берутся из сети, их пишет код по тексту резюме — так же,
как записанные ответы разбора в `app/demo.py`: вердикт по требованию ставится по
ключевым словам в строке обезличенного резюме, строка становится опорой довода.
Дальше всё идёт обычным путём: разбор вакансии и оценка через мок, проверка строк,
балл и категория считаются кодом.
"""

import json
import re
from pathlib import Path

from app import db
from app import evaluate as ev
from app.jobs import run_pending
from app.models import Candidate, Vacancy

DESCRIPTION = (
    "Ищем начальника литейного производства на машиностроительный завод. Человек "
    "руководил цехом или участком, сам работал с литьём: под давлением, в кокиль или "
    "в песчаные формы. Хорошо, если внедрял бережливое производство и готовил завод к "
    "сертификации ISO 9001. Кандидаты, которые работали только в продажах, не нужны."
)
REQUIREMENTS = [
    {"name": "Руководил цехом или участком", "kind": "must"},
    {"name": "Опыт в литейном производстве", "kind": "must"},
    {"name": "Внедрял бережливое производство", "kind": "nice"},
    {"name": "Готовил производство к ISO 9001", "kind": "nice"},
    {"name": "Опыт только в продажах, без производства", "kind": "avoid"},
]
# Для каждого требования: (что даёт «есть», что даёт «частично»).
SIGNS = [
    (r"начальник (?:цеха|участка|литейного)|директор по производству|главный инженер|"
     r"запустил новый цех|организовал участок", r"мастер участка|руководил сменой"),
    (r"литейн|литья|литьё|литье", r"металлург|механообработ"),
    (r"бережлив", r"\b5s\b|\btpm\b"),
    (r"iso 9001", r"сертификац"),
]  # fmt: skip
PRODUCTION = r"производств|цех|участ|литей|завод|технолог|инженер"
SALES = r"продаж|коммерческ|торгов"


def _line(lines: list[str], pattern: str) -> str | None:
    return next((ln for ln in lines if re.search(pattern, ln, re.IGNORECASE)), None)


def _where(line: str) -> dict:
    """Опора ответа: строка, в которой стоит фрагмент (мок подставит её номер)."""
    return {"__lines__": line.split("| ", 1)[-1].strip()[:60]}


def answer(v: Vacancy, c: Candidate) -> dict:
    """Что ответила бы честная модель по этому резюме."""
    lines = [ln for ln in ev.model_text(c).split("\n") if ln.strip()]
    ids = [r["id"] for r in v.requirements]
    checks, gaps = [], []
    for rid, req, (full, part) in zip(ids, v.requirements, SIGNS, strict=False):
        if hit := _line(lines, full):
            checks.append({"requirement_id": rid, "verdict": "met", "evidence_lines": _where(hit)})
        elif hit := _line(lines, part):
            checks.append(
                {"requirement_id": rid, "verdict": "partial", "evidence_lines": _where(hit)}
            )
        elif (
            req["kind"] == "must"
            and not _line(lines, PRODUCTION)
            and (job := _line(lines, r"должность|опыт работы|стаж"))
        ):
            # в резюме другая профессия — требование прямо не выполнено; иначе «нет данных»
            checks.append(
                {"requirement_id": rid, "verdict": "not_met", "evidence_lines": _where(job)}
            )
            gaps.append(req["name"])
        else:
            checks.append({"requirement_id": rid, "verdict": "no_data", "evidence_lines": None})
            gaps.append(req["name"])
    sales, production = _line(lines, SALES), _line(lines, PRODUCTION)
    checks.append(
        {"requirement_id": ids[-1], "verdict": "met", "evidence_lines": _where(sales)}
        if sales and not production
        else {"requirement_id": ids[-1], "verdict": "not_met", "evidence_lines": None}
    )
    concerns = []
    if c.stale and (old := _line(lines, r"резюме обновлено")):
        concerns.append({"text": "Резюме давно не обновлялось", "evidence_lines": _where(old)})
    for gap in gaps[:2]:
        if len(concerns) < 2:
            concerns.append({"text": f"Не видно: {gap.lower()}", "evidence_lines": None})
    questions = [f"{gap}: в резюме не сказано — расскажите о таком опыте?" for gap in gaps]
    questions += [
        "Сколько человек было в подчинении и какой объём выпуска?",
        "Почему рассматриваете смену работы сейчас?",
        "Какие результаты за последний год считаете главными?",
    ]
    return {
        "match": f"Вакансия {v.id}, кандидат {c.id}\n",
        "schema": "Evaluation",
        "response": {
            "checks": checks,
            "concerns": concerns,
            "questions": questions[:3],
            "fit_summary": "Итог по требованиям вакансии — в проверке выше.",
        },
    }


def create(fixtures: Path) -> Vacancy:
    """Создаёт вакансию обычным путём (разбор описания и оценка через мок)."""
    draft = {
        "match": DESCRIPTION[:60],
        "schema": "VacancyDraft",
        "response": {"title": "Начальник литейного производства", "requirements": REQUIREMENTS},
    }
    (fixtures / "vacancy.json").write_text(json.dumps([draft], ensure_ascii=False), "utf-8")
    with db.SessionLocal() as s:
        v = Vacancy(title="", description=DESCRIPTION, hard_filters={}, top_n=40)
        s.add(v)
        s.commit()
        ev.parse_vacancy(s, v)
        ids = ev.pool(s, v)[: v.top_n]
        answers = [answer(v, s.get(Candidate, cid)) for cid in ids]
        (fixtures / "evaluations.json").write_text(
            json.dumps(answers, ensure_ascii=False, indent=1), "utf-8"
        )
        ev.start(s, v, v.top_n)
    run_pending()
    return v
