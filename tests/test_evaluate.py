"""Вакансия и оценка на моке: портрет, проверка строк, формула, категории, повторные прогоны."""

import json
from datetime import date

import pytest
from sqlalchemy import select
from test_anonymize import leaks

from app import config, llm
from app import evaluate as ev
from app.jobs import run_pending
from app.llm import MockLLM
from app.models import Candidate, Feedback, Job, Match, Vacancy

RESUMES = {
    "Громов Илья Сергеевич": (
        "Громов Илья Сергеевич\nТел.: +7 (917) 555-12-34, gromov.ilya@mail.ru\n"
        "Желаемая должность: начальник литейного цеха\n\nОпыт работы\n"
        "2019 — по н.в.: АО «Литейный завод», начальник цеха литья\n"
        "— руководил цехом из 40 человек, литьё под давлением\n"
        "— внедрил бережливое производство\nНавыки: ISO 9001, SAP PP"
    ),
    "Орлова Анна Викторовна": (
        "Орлова Анна Викторовна\n+7 912 000-11-22, a.orlova@yandex.ru\n"
        "Мастер участка литья, ООО «ТехноПласт», 2015 — 2024\n"
        "— литьё алюминия, смена 12 человек\nНавыки: литьё, 5S"
    ),
    "Сидоров Пётр Ильич": (
        "Сидоров Пётр Ильич\n8 900 333 44 55\n"
        "Менеджер по продажам литейного оборудования, 2018 — 2025\n"
        "— продажи станков литья, 40 клиентов\nНавыки: переговоры"
    ),
}
REQUIREMENTS = [
    {"id": "r1", "name": "Руководил цехом литья от 3 лет", "kind": "must", "weight": 2},
    {"id": "r2", "name": "Литьё под давлением", "kind": "must", "weight": 2},
    {"id": "r3", "name": "Бережливое производство", "kind": "nice", "weight": 1},
    {"id": "r4", "name": "ISO 9001", "kind": "nice", "weight": 1},
    {"id": "r5", "name": "Только продажи, без производства", "kind": "avoid", "weight": 2},
]


@pytest.fixture
def mock(tmp_path, monkeypatch):
    """Папка записанных ответов; answers(...) дописывает их, calls — что ушло в модель."""
    folder = tmp_path / "llm"
    folder.mkdir()
    config.save({"llm_provider": "mock", "llm_fixtures": str(folder)})
    monkeypatch.setattr(llm, "RETRY_PAUSE", 0)
    calls: list[str] = []
    original = MockLLM._call

    def spy(self, schema, system, user):
        calls.append(user)
        return original(self, schema, system, user)

    monkeypatch.setattr(MockLLM, "_call", spy)

    def answers(items: list[dict], name: str = "answers.json"):
        (folder / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    answers.calls = calls
    return answers


@pytest.fixture
def base(session):
    people = {}
    for name, text in RESUMES.items():
        c = Candidate(
            full_name=name,
            raw_text=text,
            phones=["+79175551234"] if name.startswith("Громов") else [],
            emails=["gromov.ilya@mail.ru"] if name.startswith("Громов") else [],
            parse_status="parsed",
            parsed={"desired_position": text.split("\n")[2], "positions": []},
            resume_date=date(2026, 5, 1),
        )
        session.add(c)
        people[name.split()[0]] = c
    v = Vacancy(title="Начальник цеха литья", description="Начальник цеха литья под давлением")
    v.requirements = [{**r, "source": "ai", "flag": None, "suggestion": None} for r in REQUIREMENTS]
    v.top_n = 40
    session.add(v)
    session.commit()
    return v, people


def answer(v, c, checks, concerns=(), questions=("Какой был объём выпуска?",)) -> dict:
    return {
        "match": f"Вакансия {v.id}, кандидат {c.id}\n",
        "schema": "Evaluation",
        "response": {
            "checks": [
                {"requirement_id": rid, "verdict": verdict, "evidence_lines": lines, "note": ""}
                for rid, (verdict, lines) in checks.items()
            ],
            "concerns": list(concerns),
            "questions": list(questions),
            "fit_summary": "Итог",
        },
    }


def gromov_checks(**over):
    checks = {
        "r1": ("met", {"__lines__": "начальник цеха литья"}),
        "r2": ("met", {"__lines__": "литьё под давлением"}),
        "r3": ("met", {"__lines__": "бережливое производство"}),
        "r4": ("met", {"__lines__": "ISO 9001"}),
        "r5": ("not_met", None),
    }
    return {**checks, **over}


def standard(v, p) -> list[dict]:
    return [
        answer(v, p["Громов"], gromov_checks()),
        answer(
            v,
            p["Орлова"],
            {
                "r1": ("partial", {"__lines__": "Мастер участка литья"}),
                "r2": ("no_data", None),
                "r3": ("no_data", None),
                "r4": ("no_data", None),
                "r5": ("not_met", None),
            },
        ),
        answer(
            v,
            p["Сидоров"],
            {
                "r1": ("not_met", {"__lines__": "Менеджер по продажам"}),
                "r2": ("no_data", None),
                "r3": ("no_data", None),
                "r4": ("no_data", None),
                "r5": ("met", {"__lines__": "Менеджер по продажам"}),
            },
        ),
    ]


def run(session, v, limit=None, ids=None) -> Job:
    job = ev.start(session, v, limit or v.top_n, ids)
    run_pending()
    session.expire_all()
    return session.get(Job, job.id)


def matches(session, v) -> dict[str, Match]:
    rows = session.scalars(select(Match).where(Match.vacancy_id == v.id))
    return {session.get(Candidate, m.candidate_id).full_name.split()[0]: m for m in rows}


# ── Разбор вакансии ─────────────────────────────────────────────────────────


def test_vacancy_parsed_into_portrait_with_flags_filters_and_version(session, mock):
    mock(
        [
            {
                "match": "Ищем начальника цеха",
                "schema": "VacancyDraft",
                "response": {
                    "title": "Начальник цеха литья",
                    "requirements": [
                        *[{"name": f"Обязательное {i}", "kind": "must"} for i in range(1, 8)],
                        *[{"name": f"Желательное {i}", "kind": "nice"} for i in range(1, 6)],
                        {"name": "Частая смена работы", "kind": "avoid"},
                        {
                            "name": "Есть опыт",
                            "kind": "nice",
                            "flag": "vague",
                            "suggestion": "Руководил цехом от 3 лет",
                        },
                        {"name": "Мужчина до 35 лет", "kind": "must"},  # модель не пометила
                    ],
                    "city": "Самара",
                    "min_years": 5,
                    "max_salary": 300000,
                },
            }
        ]
    )
    v = Vacancy(
        title="",
        description="Ищем начальника цеха литья в Самару, стаж от 5 лет",
        hard_filters={"max_salary": 250000},  # задано рекрутером — модель не перебивает
    )
    session.add(v)
    session.commit()
    ev.parse_vacancy(session, v)
    kinds = [r["kind"] for r in v.requirements]
    assert kinds.count("must") == 6 and kinds.count("nice") == 4 and kinds.count("avoid") == 1
    assert v.title == "Начальник цеха литья" and v.requirements_version == 1
    assert v.hard_filters == {"max_salary": 250000, "city": "Самара", "min_years": 5}
    assert all(r["source"] == "ai" and r["weight"] == ev.WEIGHTS[r["kind"]] for r in v.requirements)
    assert len({r["id"] for r in v.requirements}) == len(v.requirements)

    # «Мужчина до 35 лет» шёл седьмым обязательным и отрезан лимитом; пометки — на плашках
    ev.add_requirement(session, v, "Мужчина до 35 лет", "nice")
    ev.add_requirement(session, v, "Есть опыт", "nice")
    flags = {r["name"]: r["flag"] for r in v.requirements if r["source"] == "user"}
    assert flags == {"Мужчина до 35 лет": "discriminatory", "Есть опыт": "vague"}
    assert v.requirements_version == 3

    rid = v.requirements[0]["id"]
    ev.edit_requirement(session, v, rid, "Руководил цехом литья от 3 лет", "must")
    assert v.requirements[0]["source"] == "user" and v.requirements_version == 4
    ev.delete_requirement(session, v, rid)
    assert rid not in [r["id"] for r in v.requirements] and v.requirements_version == 5


def test_suggestion_replaces_vague_requirement(session, mock):
    mock(
        [
            {
                "match": "Нужен технолог",
                "schema": "VacancyDraft",
                "response": {
                    "title": "Технолог",
                    "requirements": [
                        {
                            "name": "Хорошие знания техпроцессов",
                            "kind": "must",
                            "flag": "vague",
                            "suggestion": "Разрабатывал техпроцессы механообработки",
                        }
                    ],
                },
            }
        ]
    )
    v = Vacancy(title="", description="Нужен технолог")
    session.add(v)
    session.commit()
    ev.parse_vacancy(session, v)
    (r,) = v.requirements
    assert r["flag"] == "vague" and r["suggestion"]
    ev.accept_suggestion(session, v, r["id"])
    assert v.requirements[0]["name"] == "Разрабатывал техпроцессы механообработки"
    assert v.requirements[0]["flag"] is None and v.requirements_version == 2
    ev.parse_vacancy(session, v)  # «Разобрать описание» ещё раз — тоже новая версия
    assert v.requirements_version == 3


# ── Проверка строк, формула, категории ──────────────────────────────────────


TEXT = "[ИМЯ]\n[ТЕЛЕФОН], [ПОЧТА]\nНачальник цеха литья, 2019 — по н.в.\nЛитьё под давлением"


def test_verdict_without_valid_lines_becomes_no_data():
    evaluation = ev.Evaluation(
        checks=[
            ev.Check(requirement_id="r1", verdict="met", evidence_lines=[3, 3]),
            ev.Check(requirement_id="r2", verdict="met", evidence_lines=None),  # без строк
            ev.Check(requirement_id="r3", verdict="met", evidence_lines=[4, 9]),  # строки 9 нет
            ev.Check(requirement_id="r4", verdict="partial", evidence_lines=[1, 2]),  # метки
            # r5 модель не вернула
        ]
    )
    checks = ev.checked(evaluation, REQUIREMENTS, TEXT)
    verdicts = {ch["requirement_id"]: ch["verdict"] for ch in checks}
    assert verdicts == {
        "r1": "met",
        "r2": "no_data",
        "r3": "no_data",
        "r4": "no_data",
        "r5": "no_data",
    }
    assert checks[0]["quote"] == "Начальник цеха литья, 2019 — по н.в."
    assert all(ch["evidence_lines"] is None for ch in checks[1:])


def mk(**verdicts) -> list[dict]:
    return [
        {
            **r,
            "requirement_id": r["id"],
            "verdict": verdicts.get(r["id"], "met"),
            "evidence_lines": [1, 1],
            "quote": "цитата",
            "note": "",
        }
        for r in REQUIREMENTS
        if r["id"] in verdicts or r["kind"] != "avoid"
    ]


def test_score_formula_and_cap():
    assert ev.score(mk()) == 100
    # must: 2×1 + 2×0,5; nice: 1×0,25 + 1×0 → 3,25 / 6
    assert ev.score(mk(r2="partial", r3="no_data", r4="not_met")) == round(100 * 3.25 / 6)
    # обязательное «нет» — не выше 30, хотя по формуле 67
    assert ev.score(mk(r1="not_met")) == 30
    # подтверждённое «чего точно не надо» — тоже не выше 30
    assert ev.score(mk(r5="met")) == 30
    assert ev.score(mk(r5="not_met")) == 100


def category_of(checks, status="ok") -> str:
    m = Match(status=status, checks=checks)
    m.score = ev.score(checks) if status == "ok" else None
    return ev.category(m)


def test_categories_by_checks_then_fallback_thresholds():
    assert category_of(mk()) == ev.FIT
    assert category_of(mk(r4="no_data")) == ev.MAYBE  # желательные: подтверждено 1 из 2
    assert category_of(mk(r3="no_data", r4="no_data")) == ev.MAYBE
    assert category_of(mk(r1="not_met")) == ev.UNFIT
    assert category_of(mk(r5="met")) == ev.UNFIT
    assert category_of(mk(), status="failed") == ev.FAILED
    # обязательное «частично» правила не решают — порог 55; без всех обязательных
    # выше «Можно рассмотреть» не поднимается, даже при 83
    assert ev.score(mk(r2="partial")) == 83 and category_of(mk(r2="partial")) == ev.MAYBE
    low = mk(r2="partial", r3="no_data", r4="not_met")
    assert ev.score(low) == 54 and category_of(low) == ev.UNFIT
    mid = mk(r2="no_data", r4="no_data")
    assert ev.score(mid) == 62 and category_of(mid) == ev.MAYBE
    # вакансия без обязательных — только пороги 75 и 55
    only_nice = [ch for ch in mk(r3="partial") if ch["kind"] == "nice"]
    assert ev.score(only_nice) == 75 and category_of(only_nice) == ev.FIT
    only_nice = [ch for ch in mk(r3="partial", r4="partial") if ch["kind"] == "nice"]
    assert category_of(only_nice) == ev.UNFIT  # 50
    assert (ev.FIT_FROM, ev.MAYBE_FROM) == (75, 55)


def test_close_scores_are_a_tie():
    assert ev.tied([80, 77, 60, 58, 40]) == [True, True, True, True, False]
    assert ev.tied([90, 80]) == [False, False]
    assert ev.tied([]) == []


def test_counter_and_reasons():
    m = Match(status="ok", checks=mk(r2="partial", r4="no_data"))
    assert ev.counter(m) == "обязательные 1 из 2 · желательные 1 из 2"
    top = ev.reasons(m.checks)
    assert [r["requirement_id"] for r in top] == ["r1", "r3", "r2"]  # met раньше partial


# ── Прогон ──────────────────────────────────────────────────────────────────


def test_trial_run_scores_and_categories_with_model_and_version(session, base, mock):
    v, p = base
    mock(standard(v, p))
    job = run(session, v, limit=ev.TRIAL)
    assert job.status == "done" and job.progress == 3
    assert job.payload["tokens_in"] > 0
    got = matches(session, v)
    assert {k: ev.category(m) for k, m in got.items()} == {
        "Громов": ev.FIT,
        "Орлова": ev.UNFIT,  # 33: обязательное «частично», остальное без данных
        "Сидоров": ev.UNFIT,
    }
    assert got["Громов"].score == 100 and got["Сидоров"].score <= 30
    assert got["Орлова"].score == 33
    for m in got.values():
        assert m.model == "claude-sonnet-5" and m.requirements_version == 1
        assert m.change_reason == "new" and m.prev_score is None
    gromov = got["Громов"]
    assert [r["requirement_id"] for r in gromov.reasons] == ["r1", "r2", "r3"]
    assert gromov.reasons[0]["quote"].startswith("2019 — по н.в.")
    assert gromov.questions == ["Какой был объём выпуска?"]


def test_rerun_touches_only_new_and_changed(session, base, mock):
    v, p = base
    mock(standard(v, p))
    run(session, v)
    first = len(mock.calls)
    assert first == 3

    run(session, v)
    assert len(mock.calls) == first  # никто не менялся — никого не оцениваем

    # резюме обновилось у одного и появился новый кандидат
    p["Орлова"].raw_text += "\n— литьё под давлением, 3 года"
    new = Candidate(
        full_name="Лебедев Олег",
        raw_text="Лебедев Олег\nНачальник цеха литья под давлением, 2010 — 2026",
        parse_status="parsed",
        parsed={},
    )
    session.add(new)
    session.commit()
    mock(
        [
            *standard(v, p),
            answer(v, new, gromov_checks(r3=("no_data", None), r4=("no_data", None))),
        ]
    )
    run(session, v)
    assert len(mock.calls) == first + 2
    got = matches(session, v)
    assert got["Орлова"].change_reason == "resume_updated"
    assert got["Орлова"].prev_score == got["Орлова"].score  # ответ тот же — балл тот же
    assert got["Лебедев"].change_reason == "new"
    assert got["Громов"].change_reason == "new"  # не трогали


def test_requirements_change_reevaluates_everyone(session, base, mock):
    v, p = base
    mock(standard(v, p))
    run(session, v)
    ev.delete_requirement(session, v, "r4")
    mock(
        [
            answer(v, p["Громов"], {**gromov_checks(r3=("no_data", None))}),
            *standard(v, p)[1:],
        ]
    )
    run(session, v)
    assert len(mock.calls) == 6
    got = matches(session, v)
    assert {m.change_reason for m in got.values()} == {"vacancy_changed"}
    assert all(m.requirements_version == 2 for m in got.values())
    gromov = got["Громов"]
    assert (gromov.prev_score, gromov.score) == (100, 85)  # (2 + 2 + 0,25) / 5
    assert "r4" not in [ch["requirement_id"] for ch in gromov.checks]


def test_decision_survives_rerun_and_rejected_do_not_return(session, base, mock):
    v, p = base
    mock(standard(v, p))
    run(session, v)
    got = matches(session, v)
    got["Громов"].decision = "invite"
    got["Сидоров"].decision, got["Сидоров"].decision_reason = "reject", "Нет нужного опыта"
    session.commit()
    ev.add_requirement(session, v, "SAP PP", "nice")
    run(session, v)
    got = matches(session, v)
    assert got["Громов"].decision == "invite" and got["Громов"].requirements_version == 2
    assert got["Сидоров"].requirements_version == 1  # отклонённого не переоценивали
    assert p["Сидоров"].id not in ev.pool(session, v)
    assert len(mock.calls) == 5


def test_failed_evaluation_goes_to_its_group_and_is_retried(session, base, mock):
    v, p = base
    items = standard(v, p)
    items[1] = {**items[1], "response": "не JSON"}  # модель дважды ответила не по форме
    mock(items)
    job = run(session, v)
    assert job.status == "done" and job.error  # «Готово, есть проблемы»
    orlova = matches(session, v)["Орлова"]
    assert orlova.status == "failed" and ev.category(orlova) == ev.FAILED
    assert orlova.score is None
    mock(standard(v, p))
    run(session, v, ids=[p["Орлова"].id])  # «Оценить ещё раз»
    assert matches(session, v)["Орлова"].status == "ok"


def test_feedback_reevaluates_candidate_and_reaches_prompt(session, base, mock):
    v, p = base
    mock(standard(v, p))
    run(session, v)
    session.add(
        Feedback(
            candidate_id=p["Громов"].id,
            vacancy_id=v.id,
            target="reason",
            text="Бережливое производство",
            kind="not_in_resume",
        )
    )
    session.commit()
    run(session, v)
    assert len(mock.calls) == 4
    assert "«Бережливое производство» — этого нет в резюме" in mock.calls[-1]
    assert matches(session, v)["Громов"].change_reason == "vacancy_changed"


def test_discriminatory_requirement_not_sent_to_model_nor_scored(session, base, mock):
    v, p = base
    ev.add_requirement(session, v, "Мужчина до 40 лет", "nice")
    assert v.requirements[-1]["flag"] == "discriminatory"
    mock(standard(v, p))
    run(session, v)
    assert all("Мужчина до 40 лет" not in text for text in mock.calls)
    gromov = matches(session, v)["Громов"]
    assert "r6" not in [ch["requirement_id"] for ch in gromov.checks]
    assert gromov.score == 100


def test_discriminatory_sentence_of_description_not_sent_to_model(session, base, mock):
    """Правка ревью этапа 3, №1: «мужчина до 35 лет, без детей» из описания вакансии
    не доходит до модели оценки, остальное описание доходит."""
    v, p = base
    v.description = (
        "Начальник цеха литья под давлением. Мужчина до 35 лет, без детей. "
        "Желательно бережливое производство."
    )
    session.commit()
    mock(standard(v, p))
    run(session, v)
    assert mock.calls
    for text in mock.calls:
        assert "Мужчина до 35 лет" not in text and "без детей" not in text
        assert "Желательно бережливое производство." in text


def test_guard_nothing_personal_reaches_model(session, base, mock):
    v, p = base
    mock(standard(v, p))
    run(session, v)
    assert len(mock.calls) == 3
    for c in p.values():
        for text in mock.calls:
            assert leaks(text, c) == [], (c.full_name, text[:300])
    assert "2| Тел.: [ТЕЛЕФОН], [ПОЧТА]" in next(t for t in mock.calls if "Литейный завод" in t)


def _checks(musts: list[str], nices: list[str]) -> list[dict]:
    kinds = [("must", v) for v in musts] + [("nice", v) for v in nices]
    return [
        {
            "requirement_id": f"r{i}",
            "name": f"требование {i}",
            "kind": kind,
            "weight": ev.WEIGHTS[kind],
            "verdict": verdict,
            "evidence_lines": None if verdict == "no_data" else [1, 1],
        }
        for i, (kind, verdict) in enumerate(kinds, start=1)
    ]


def test_maybe_needs_at_least_half_of_must_requirements():
    """Решение по этапу 3: «Можно рассмотреть» — только если хотя бы половина
    обязательных «есть» или «частично», иначе «Скорее не подходят»."""
    one_of_five = _checks(["met", "no_data", "no_data", "no_data", "no_data"], ["met"] * 4)
    assert ev.score(one_of_five) >= ev.MAYBE_FROM  # по баллу прошёл бы
    assert category_of(one_of_five) == ev.UNFIT
    half = _checks(["met", "partial", "no_data", "no_data"], ["met"] * 4)
    assert ev.score(half) >= ev.MAYBE_FROM and category_of(half) == ev.MAYBE
    below_half = _checks(["partial", "no_data", "no_data"], ["met"] * 4)
    assert category_of(below_half) == ev.UNFIT


def test_unexpected_reply_stops_after_batch_and_keeps_paid(session, base, mock, monkeypatch):
    """Повторное ревью PR #3: хаб ответил 200 с HTML на втором из пяти (PARALLEL = 4) —
    адаптер падает с AttributeError, а не LLMError. Остальные запросы ушли и оплачены:
    пачка доходит до конца, четыре ответа сохраняются, задача останавливается с общей
    фразой, а «Повторить» отправляет только одного."""
    import time

    from app.jobs import UNEXPECTED

    v, p = base
    text = RESUMES["Громов Илья Сергеевич"]
    for name in ("Белов Сергей Петрович", "Котов Андрей Ильич"):
        c = Candidate(full_name=name, raw_text=text.replace("Громов Илья Сергеевич", name))
        c.parse_status, c.resume_date = "parsed", date(2026, 5, 1)
        session.add(c)
        p[name.split()[0]] = c
    session.commit()
    order = ev.pool(session, v)
    second = order[1]
    extra = [answer(v, p[n], gromov_checks()) for n in ("Белов", "Котов")]
    mock([*standard(v, p), *extra])
    broken, answer_slowly = {"on": True}, MockLLM._call

    def html_on_second(self, schema, system, user):
        if broken["on"] and f"кандидат {second}\n" in user:
            raise AttributeError("'str' object has no attribute 'usage'")  # HTML вместо ответа
        time.sleep(1)
        return answer_slowly(self, schema, system, user)

    monkeypatch.setattr(MockLLM, "_call", html_on_second)
    job = run(session, v)
    assert len(order) == 5 and job.status == "failed" and job.error == UNEXPECTED
    saved = set(session.scalars(select(Match.candidate_id).where(Match.vacancy_id == v.id)))
    assert saved == set(order) - {second}

    broken["on"] = False
    sent = len(mock.calls)
    job.status, job.error = "running", None  # как «Повторить оценку»
    session.commit()
    run_pending()
    resent = mock.calls[sent:]
    assert len(resent) == 1 and f"кандидат {second}\n" in resent[0]
    session.expire_all()
    assert session.get(Job, job.id).status == "done" and len(matches(session, v)) == 5


@pytest.mark.parametrize(
    "requirement, line, shown",
    [
        ("Опыт только в продажах", "Менеджер по продажам B2B, 2018 — 2024", True),
        ("Опыт только в продажах", "Опыт производственный, не только продажи", False),
        ("Опыт только в продажах", "Работал во всех отделах, кроме продаж", False),
        ("Только с опытом в продажах", "Руководил цехом, опытом делился с мастерами", False),
        ("Опыт только в продажах", "директор по производству АО «ОДК-Кузнецов» стаж 19 лет", False),
    ],
)
def test_avoid_met_needs_the_word_itself(requirement, line, shown):
    """Ревью PR #4: «есть» по «чего точно не надо» вида «только X» подтверждает строка,
    где X стоит целым словом (любая форма) и без «не» или «кроме» перед ним. Основы слов
    ловили «не только продажи», а в «только с опытом…» искали «опыто»."""
    assert ev._shows(line, [1, 1], requirement) is shown


def test_crash_mid_batch_keeps_paid_evaluations(session, base, mock, monkeypatch):
    """Правка ревью этапа 3, №4: сбой на третьем ответе (как SIGKILL) — два оплаченных
    ответа уже сохранены, после перезапуска в модель уходит только третий кандидат."""
    v, p = base
    mock(standard(v, p))
    monkeypatch.setattr(ev, "PARALLEL", 1)
    saved, original = [], ev._save

    def crash_on_third(*args):
        saved.append(args[2].id)
        if len(saved) == 3:
            raise RuntimeError("процесс убит")
        return original(*args)

    monkeypatch.setattr(ev, "_save", crash_on_third)
    job = run(session, v)
    assert job.status == "failed"
    assert len(matches(session, v)) == 2
    monkeypatch.setattr(ev, "_save", original)
    job.status = "running"  # перезапуск продолжает прерванную задачу
    session.commit()
    run_pending()
    session.expire_all()
    assert len(matches(session, v)) == 3
    assert len(mock.calls) == 3 + 1


def test_checkset_counts_raw_model_answers(session, base, mock):
    """Правка ревью этапа 3, №10: счётчики набора проверки смотрят сырой ответ модели,
    поэтому «есть» без строк и несуществующие строки видны, хотя код их уже отбросил."""
    import importlib

    checkset = importlib.import_module("ev" + "al.run")
    v, p = base
    items = standard(v, p)
    items[0]["response"]["checks"][0]["evidence_lines"] = None  # r1 «есть» без строк
    items[1]["response"]["checks"][0]["evidence_lines"] = [90, 95]  # строк нет
    mock(items)
    run(session, v)
    gromov = matches(session, v)["Громов"]
    assert gromov.checks[0]["verdict"] == "no_data"  # код очистил
    stats = checkset.check_evidence(session)
    assert stats["«есть» без строк"] == 1 and stats["строк нет в резюме"] == 1
