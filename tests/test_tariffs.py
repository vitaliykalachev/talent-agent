"""Цена на экранах разбора и оценки против списаний ClaudeHub в живом прогоне 03.10.2026.

Тот же объём, что тогда: 10 резюме из выгрузки (длины текста — как у них) и вакансия
с тем же портретом. Хаб списал за разбор 0,87 ₽, за оценку 5 для проверки — 0,74 ₽, за
оценку остальных 5 — 0,71 ₽. Экран тогда обещал 3,0–5,5 и 4,9–9,1 ₽.
"""

import pytest

from app import evaluate as ev
from app import parse
from app.models import Candidate, Vacancy

LENGTHS = [737, 769, 800, 465, 803, 917, 278, 713, 770, 813]
FIRST, REST = [1, 0, 3, 2, 5], [4, 6, 7, 8, 9]  # кого оценила первая задача, кого вторая
DESCRIPTION = (
    "Ищем начальника производственного участка на машиностроительный завод. Обязательно: "
    "руководил производственным участком, цехом или сменой не меньше 5 лет, внедрял "
    "бережливое производство. Желательно: готовил производство к сертификации ISO 9001, "
    "работал в SAP. Кандидаты только с опытом продаж или бухгалтерии не подходят."
)
REQUIREMENTS = [
    ("must", "Руководил производственным участком, цехом или сменой"),
    ("must", "Опыт руководства не менее 5 лет"),
    ("must", "Внедрял бережливое производство (Lean)"),
    ("nice", "Готовил производство к сертификации ISO 9001"),
    ("nice", "Работал в SAP"),
    ("avoid", "Опыт только в продажах"),
    ("avoid", "Опыт только в бухгалтерии"),
]


@pytest.fixture
def measured(session):
    people = [Candidate(raw_text="х" * n, full_name=None) for n in LENGTHS]
    v = Vacancy(title="Начальник производственного участка", description=DESCRIPTION)
    v.requirements = [
        {"id": f"r{i}", "name": name, "kind": kind, "weight": ev.WEIGHTS[kind]}
        for i, (kind, name) in enumerate(REQUIREMENTS, start=1)
    ]
    session.add_all([*people, v])
    session.commit()
    return [c.id for c in people], v


def test_parse_estimate_brackets_hub_charge(session, measured):
    ids, _ = measured
    est = parse.estimate(session, ids)
    assert est["rub_low"] <= 0.87 <= est["rub_high"]


@pytest.mark.parametrize("who, charged", [(FIRST, 0.74), (REST, 0.71)])
def test_evaluate_estimate_brackets_hub_charge(session, measured, who, charged):
    ids, v = measured
    est = ev.estimate(session, v, ev.TRIAL, order=[ids[i] for i in who])
    assert est["count"] == 5 and est["rub_low"] <= charged <= est["rub_high"]
