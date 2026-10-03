"""Цена на экранах разбора и оценки против списаний ClaudeHub в живом прогоне 03.10.2026.

Тот же объём, что тогда: 10 резюме из выгрузки (длины текста — как у них) и вакансия
с тем же портретом. Хаб списал за разбор 0,87 ₽, за оценку 5 для проверки — 0,74 ₽, за
оценку остальных 5 — 0,71 ₽. Экран тогда обещал 3,0–5,5 и 4,9–9,1 ₽.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, parse
from app import evaluate as ev
from app.main import create_app
from app.models import Candidate, Vacancy

ROOT = Path(__file__).resolve().parent.parent
PRICES = ("price_parse_in", "price_parse_out", "price_eval_in", "price_eval_out")

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


def test_old_default_prices_replaced_once_own_kept(tmp_path, caplog):
    """«Сохранить» в «Настройках» записывает все поля, в том числе цены. У тех, кто
    нажимал его в 0.3, в базе остались прежние умолчания $1/$5/$2/$10, и новая версия
    их не меняла бы. При обновлении они один раз заменяются замером, свои — нет."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    folder = tmp_path / "old"
    folder.mkdir()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{folder / 'app.db'}")
    command.upgrade(cfg, "0004")  # база версии 0.3
    saved = {"price_parse_in": "1", "price_parse_out": "5", "price_eval_in": "0,3"}
    engine = create_engine(f"sqlite:///{folder / 'app.db'}")
    with engine.begin() as conn:
        for key, value in {**saved, "price_eval_out": "10"}.items():
            conn.execute(
                text("insert into settings (key, value) values (:k, :v)"), {"k": key, "v": value}
            )
    engine.dispose()

    db.configure(folder)  # первый старт новой версии
    assert {key: config.get(key) for key in PRICES} == {
        "price_parse_in": "0.44",
        "price_parse_out": "0.44",
        "price_eval_in": "0,3",  # своё рекрутера
        "price_eval_out": "0.49",
    }
    assert "price_parse_in" in caplog.text and "price_eval_in" not in caplog.text
    config.save({"price_parse_in": "1"})  # потом сам поставил 1 — это его решение
    db.configure(folder)
    assert config.get("price_parse_in") == "1"


def test_settings_say_where_prices_come_from(session):
    page = TestClient(create_app()).get("/settings").text
    assert "По списаниям ClaudeHub на 03.10.2026, можно поправить" in page
    assert "Как в прайсе сервиса" not in page
