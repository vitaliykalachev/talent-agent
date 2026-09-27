"""Правки по ревью этапа 3: №5–9 и №12 (остальные — рядом с кодом, который правят)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evaluate import base, matches, mock, run, standard  # noqa: F401 — фикстуры

from app import evaluate as ev
from app.jobs import run_pending
from app.main import create_app
from app.models import Feedback, Job, Vacancy


@pytest.fixture
def web(session):
    return TestClient(create_app())


@pytest.mark.parametrize(
    "field, value", [("min_years", "inf"), ("max_salary", "1e999"), ("top_n", "inf")]
)
def test_5_infinite_numbers_in_conditions_are_rejected(web, session, base, field, value):  # noqa: F811
    v, _ = base
    data = {"min_years": "", "max_salary": "", "top_n": "40", field: value}
    r = web.post(f"/vacancies/{v.id}/conditions", data=data, follow_redirects=False)
    assert r.status_code == 400 and "обычное число" in r.text
    session.expire_all()
    assert session.get(Vacancy, v.id).hard_filters == {} and session.get(Vacancy, v.id).top_n == 40


def test_6_evaluate_while_paused_does_not_queue_second(session, base, mock):  # noqa: F811
    v, _ = base
    job = ev.start(session, v, v.top_n)
    job.status = "paused"
    session.commit()
    assert ev.start(session, v, v.top_n).id == job.id
    live = session.scalars(select(Job).where(Job.kind == "evaluate"))
    assert len(list(live)) == 1


def test_7_vacancies_table_stacks_on_phone(web, session, base):  # noqa: F811
    assert '<table class="stack">' in web.get("/vacancies").text


@pytest.mark.parametrize("back", ["//evil.example/x", "https://evil.example/x", "/\\evil.example"])
def test_8_no_open_redirect_after_delete(web, session, back):
    f = Feedback(target="reason", text="Литьё", kind="irrelevant", scope="all")
    session.add(f)
    session.commit()
    r = web.post(f"/feedback/{f.id}/delete", data={"back": back}, follow_redirects=False)
    assert r.headers["location"] == "/vacancies"


@pytest.mark.parametrize(
    "text",
    [
        "Возраст 25–35 лет",
        "Девушка с опытом продаж",
        "Парень до 30",
        "Молодой специалист до 30",
        "Только молодые и активные",
        "Холост, готов к командировкам",
    ],
)
def test_9_more_discriminatory_wordings_are_flagged(text):
    assert ev.DISCRIMINATORY_RE.search(text), text


def test_9_ordinary_requirements_not_flagged():
    for text in ("Опыт 3–5 лет в литье", "Руководил цехом от 5 лет", "Пароизоляция, монтаж"):
        assert not ev.DISCRIMINATORY_RE.search(text), text


def test_12_non_numeric_candidate_id_is_400(web, session, base):  # noqa: F811
    v, _ = base
    for url, data in (
        (f"/vacancies/{v.id}/decision", {"candidate_id": "abc", "decision": "invite"}),
        (f"/vacancies/{v.id}/feedback", {"candidate_id": "abc", "kind": "other", "text": "x"}),
    ):
        r = web.post(url, data=data, follow_redirects=False)
        assert r.status_code == 400 and "обновите страницу" in r.text, url


def test_12_unknown_ids_are_skipped_with_russian_note(session, base, mock):  # noqa: F811
    v, p = base
    mock(standard(v, p))
    job = run(session, v, ids=[p["Громов"].id, 99999])
    assert job.status == "done"
    assert "99999: такого кандидата в базе нет" in job.error
    assert set(matches(session, v)) == {"Громов"}


def test_12_requirements_removed_while_queued_give_failed_not_crash(session, base, mock):  # noqa: F811
    v, p = base
    mock(standard(v, p))
    job = ev.start(session, v, v.top_n)
    v.requirements = []
    session.commit()
    run_pending()
    session.expire_all()
    assert session.get(Job, job.id).status == "done"
    got = matches(session, v)
    assert got and all(ev.category(m) == ev.FAILED for m in got.values())
    assert ev.score([]) == 0
