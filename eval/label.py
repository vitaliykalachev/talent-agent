"""Разметка набора проверки: кто из демо-базы подходит под пять вакансий `vacancies.json`.

Оценки 0–3 в формате TREC qrels («v1 0 <ID из CRM> 3»), файл `eval/qrels.txt`.
Разметка по фактам генератора демо, а не по словам резюме: профессия и нынешняя
должность кандидата (то, что генератор вложил в текст и в записанный ответ разбора).
3 — ровно та роль; 2 — смежная роль, которую рекрутер тоже позвал бы; 1 — та же
профессия, но другая роль; 0 — не пишется. Демо детерминировано (seed 42), поэтому
ID из CRM у одних и тех же людей не меняются.

Запуск: `uv run python -m eval.label data/eval` — база должна быть собрана `make eval`.
"""

import sys
from pathlib import Path

from sqlalchemy import select

from app import db
from app.demo import PROFESSIONS
from app.models import Candidate

HERE = Path(__file__).parent
GRADES = {
    "v1": {  # Начальник литейного производства
        "profession": "производство",
        3: {"Начальник цеха", "Директор по производству", "Начальник литейного участка"},
        2: {"Технолог литейного производства", "Главный инженер", "Мастер участка"},
        "casting": True,  # роль 3 и 2 — только если в резюме есть литьё
    },
    "v2": {
        "profession": "финансы",
        3: {"Главный бухгалтер"},
        2: {"Руководитель отдела отчётности", "Бухгалтер-калькулятор", "Финансовый контролёр"},
    },
    "v3": {
        "profession": "продажи",
        3: {"Руководитель отдела продаж", "Коммерческий директор"},
        2: {"Менеджер по работе с ключевыми клиентами", "Региональный менеджер"},
    },
    "v4": {
        "profession": "ИТ",
        3: {"Python-разработчик"},
        2: {"Data Scientist", "Руководитель группы разработки"},
        "python": True,  # роль 2 — только если в резюме есть Python
    },
    "v5": {
        "profession": "логистика",
        3: {"Начальник склада"},
        2: {"Директор по логистике", "Руководитель отдела логистики"},
    },
}


def profession(title: str) -> str | None:
    return next((p for p, spec in PROFESSIONS.items() if title in spec["titles"]), None)


def grade(rule: dict, title: str, text: str) -> int:
    if title == "Начальник литейного участка":  # витрина из демо — производство
        prof = "производство"
    else:
        prof = profession(title)
    if prof != rule["profession"]:
        return 0
    low = text.lower()
    level = 3 if title in rule[3] else 2 if title in rule[2] else 1
    if rule.get("casting") and level > 1 and "лить" not in low and "литей" not in low:
        level = 1
    if rule.get("python") and level == 2 and "python" not in low:
        level = 1
    return level


def main(data_dir: str) -> None:
    db.configure(data_dir)
    lines = []
    with db.SessionLocal() as s:
        people = s.scalars(
            select(Candidate).where(Candidate.duplicate_of.is_(None)).order_by(Candidate.id)
        )
        people = [
            (c.external_id, (c.parsed or {}).get("desired_position"), c.raw_text) for c in people
        ]
    for qid, rule in GRADES.items():
        for ext_id, title, text in people:
            if ext_id and title and (level := grade(rule, title, text)):
                lines.append(f"{qid} 0 {ext_id} {level}")
    (HERE / "qrels.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Записано {len(lines)} оценок в {HERE / 'qrels.txt'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/eval")
