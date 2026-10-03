"""Резюме одной длинной строкой — так их выгружает CRM (живой прогон 03.10.2026).

Модель сослалась на строку с [ИМЯ] вместо огромной строки за ней, проверка кода
отвергла все доводы, и сильный директор по производству получил 25. Теперь длинная
строка делится на фрагменты, и модель, оценка и «Показать в резюме» нумеруют их одинаково.
"""

import openpyxl
import pytest
from test_evaluate import answer, mock, run  # noqa: F401 — фикстуры

from app import evaluate as ev
from app.lines import resume_lines
from app.models import Match, Vacancy
from app.parse import model_text
from app.web.present import marked_source

ONE_LINE = (
    "директор по производству АО «ОДК-Кузнецов» стаж 19 лет разработал техпроцессы "
    "механообработки, внедрил бережливое производство, снизил брак на 18 %, организовал "
    "участок литья под давлением с нуля, сократил простои оборудования на 25 % навыки "
    "КОМПАС-3D литьё ISO 9001 TPM SAP PP SolidWorks г Тольятти"
)
REQUIREMENTS = [  # портрет вакансии из живого прогона
    ("r1", "must", "Руководил производственным участком, цехом или сменой"),
    ("r2", "must", "Опыт руководства не менее 5 лет"),
    ("r3", "must", "Внедрял бережливое производство (Lean)"),
    ("r4", "nice", "Готовил производство к сертификации ISO 9001"),
    ("r5", "nice", "Работал в SAP"),
    ("r6", "avoid", "Опыт только в продажах"),
    ("r7", "avoid", "Опыт только в бухгалтерии"),
]


@pytest.fixture
def director(session, do_import, tmp_path):
    """Строка выгрузки CRM, как в crm10.xlsx: резюме — имя и одна длинная строка."""
    book = openpyxl.Workbook()
    book.active.append(["ФИО", "Телефон", "Почта", "Город", "Должность", "Текст резюме"])
    book.active.append(
        [
            "Титов Григорий Владимирович",
            "+7 917 555-44-33",
            "g.titov@mail.ru",
            "Тольятти",
            "Директор по производству",
            f"ТИТОВ ГРИГОРИЙ ВЛАДИМИРОВИЧ\n{ONE_LINE}",
        ]
    )
    book.save(tmp_path / "crm.xlsx")
    do_import(tmp_path / "crm.xlsx")
    from app.models import Candidate

    return session.query(Candidate).one()


@pytest.fixture
def vacancy(session):
    v = Vacancy(title="Начальник производственного участка", description="Начальник участка")
    v.requirements = [
        {"id": rid, "name": name, "kind": kind, "weight": ev.WEIGHTS[kind]}
        for rid, kind, name in REQUIREMENTS
    ]
    session.add(v)
    session.commit()
    return v


def lines_of(fragment: str) -> dict:
    return {"__lines__": fragment}


def test_long_line_split_into_fragments_same_length(director):
    split = resume_lines(director.raw_text)
    assert len(split) == len(director.raw_text)  # пробел → перенос: смещения те же
    pieces = resume_lines(ONE_LINE).split("\n")
    assert len(pieces) >= 5 and pieces[1] == "внедрил бережливое производство,"
    head = director.raw_text.split("\n")[:5]  # «ФИО: …», «Город: …» — короткие, как были
    assert split.split("\n")[:5] == head
    # сокращения, инициалы и диапазон лет внутри длинной строки не делятся
    tricky = (
        "В г. Самара руководил цехом литья на 120 человек и отвечал за план выпуска; "
        "рекомендации даст Иванов И. И., а с 2019 — по настоящее время работаю в АО «Завод», "
        "где подчинённых 45 человек, а смена работает в три потока круглый год без простоев."
    )
    text = resume_lines(tricky)
    assert "г. Самара" in text and "И. И." in text and "2019 — по настоящее время" in text
    assert text.count("\n") >= 1


def test_one_line_director_scored_by_fragments(session, director, vacancy, mock):  # noqa: F811
    """Записанный ответ с верными ссылками на фрагменты даёт нормальный балл; «есть»
    по «только в продажах», подкреплённое строкой о производстве, балл не режет."""
    production = lines_of("директор по производству АО")
    mock(
        [
            answer(
                vacancy,
                director,
                {
                    "r1": ("met", production),
                    "r2": ("partial", lines_of("стаж 19 лет")),
                    "r3": ("met", lines_of("внедрил бережливое производство")),
                    "r4": ("partial", lines_of("ISO 9001")),
                    "r5": ("met", lines_of("SAP PP")),
                    "r6": ("met", production),  # так ответила живая модель
                    "r7": ("met", production),
                },
            )
        ]
    )
    run(session, vacancy, ids=[director.id])
    m = session.get(Match, (vacancy.id, director.id))
    assert m.status == "ok" and m.score == 81 and ev.category(m) == ev.MAYBE
    quotes = {r["requirement_id"]: r["quote"] for r in m.reasons}
    assert quotes["r3"] == "внедрил бережливое производство,"  # фрагмент, а не вся строка
    avoid = {ch["requirement_id"]: ch["verdict"] for ch in m.checks if ch["kind"] == "avoid"}
    assert avoid == {"r6": "no_data", "r7": "no_data"}


def test_avoid_met_without_line_does_not_cap(session, director, vacancy, mock):  # noqa: F811
    mock(
        [
            answer(
                vacancy,
                director,
                {
                    "r1": ("met", lines_of("директор по производству АО")),
                    "r2": ("met", lines_of("стаж 19 лет")),
                    "r3": ("met", lines_of("внедрил бережливое производство")),
                    "r4": ("met", lines_of("ISO 9001")),
                    "r5": ("met", lines_of("SAP PP")),
                    "r6": ("met", None),
                    "r7": ("met", None),
                },
            )
        ]
    )
    run(session, vacancy, ids=[director.id])
    m = session.get(Match, (vacancy.id, director.id))
    assert m.score == 100 and ev.category(m) == ev.FIT


def test_show_in_resume_wins_over_summary_quote_on_same_fragment(director):
    """Живая перепроверка: цитата «Кратко» начиналась с того же фрагмента, что довод
    оценки, и забирала подсветку — «Показать в резюме» вела в никуда."""
    director.parsed = {"summary_quote": "директор по производству АО «ОДК-Кузнецов» стаж 19 лет"}
    numbered = model_text(director).split("\n")
    n = next(i for i, line in enumerate(numbered, 1) if line.startswith("директор по"))
    html, anchors = marked_source(director, [("e-r1", [n, n])])
    assert "e-r1" in anchors and '<mark id="e-r1">директор по производству' in html


def test_show_in_resume_marks_same_fragment(director):
    """«Показать в резюме» подсвечивает тот фрагмент, на который сослалась модель."""
    numbered = model_text(director).split("\n")
    n = next(i for i, line in enumerate(numbered, 1) if line.startswith("внедрил бережливое"))
    html, anchors = marked_source(director, [("e-r3", [n, n])])
    assert '<mark id="e-r3">внедрил бережливое производство,</mark>' in html and "e-r3" in anchors
