"""Правки по приёмочному ревью этапа 4."""

from datetime import datetime

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evaluate import answer, base, gromov_checks, matches, mock, run  # noqa: F401
from test_night import FakeSMTP

from app import config, mail, morning, night
from app import evaluate as ev
from app.anonymize import anonymize
from app.jobs import enqueue, run_pending
from app.main import create_app
from app.models import Job, NightRun, Vacancy


@pytest.fixture
def web(session):
    return TestClient(create_app())


# ── 1. Строки [n] и [a, b, c] ───────────────────────────────────────────────


def test_1_single_line_and_list_of_lines_read_as_range(session, base, mock):  # noqa: F811
    """Живая модель присылает [31] и [7, 21, 23] вместо [от, до]: это [31, 31] и [7, 23],
    а не «в резюме не сказано». Схема для модели — ровно два целых."""
    v, p = base
    gromov = p["Громов"]
    mock([answer(v, gromov, gromov_checks(r1=("met", [5]), r2=("met", [6, 4, 5])))])
    run(session, v, ids=[gromov.id])
    m = matches(session, v)["Громов"]
    lines = {ch["requirement_id"]: ch["evidence_lines"] for ch in m.checks}
    assert lines["r1"] == [5, 5] and lines["r2"] == [4, 6]
    assert {ch["verdict"] for ch in m.checks if ch["kind"] == "must"} == {"met"}
    prop = ev.Check.model_json_schema()["properties"]["evidence_lines"]["anyOf"][0]
    assert (prop["minItems"], prop["maxItems"]) == (2, 2)


# ── 2. Потолки чисел ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_salary", "1e308"),
        ("max_salary", "1" * 22),
        ("max_salary", "100000001"),
        ("min_years", "61"),
        ("top_n", "501"),
    ],
)
def test_2_huge_numbers_in_vacancy_are_400_and_page_stays_alive(web, session, base, field, value):  # noqa: F811
    v, _ = base
    data = {"min_years": "", "max_salary": "", "top_n": "40", field: value}
    r = web.post(f"/vacancies/{v.id}/conditions", data=data, follow_redirects=False)
    assert r.status_code == 400 and "не больше" in r.text, r.text
    assert web.get(f"/vacancies/{v.id}").status_code == 200


def test_2_limits_themselves_are_accepted(web, session, base):  # noqa: F811
    v, _ = base
    data = {"min_years": "60", "max_salary": "100000000", "top_n": "500"}
    assert web.post(f"/vacancies/{v.id}/conditions", data=data).status_code == 200
    session.expire_all()
    assert session.get(Vacancy, v.id).top_n == 500


@pytest.mark.parametrize(
    "params",
    [
        {"min_years": "inf"},
        {"max_salary": "nan"},
        {"max_salary": "1e21"},
        {"page": "9" * 25},
        {"batch": "9" * 25},
    ],
)
def test_2_candidates_filters_reject_huge_numbers(web, params):
    r = web.get("/candidates", params=params)
    assert r.status_code == 400 and "число" in r.text


def test_2_huge_candidate_id_in_decision_is_400(web, session, base):  # noqa: F811
    v, _ = base
    r = web.post(
        f"/vacancies/{v.id}/decision", data={"candidate_id": "9" * 23, "decision": "invite"}
    )
    assert r.status_code == 400


# ── 3. Пробное письмо берёт поля формы ──────────────────────────────────────


def test_3_test_mail_saves_form_first_and_keeps_fields(web, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    form = {
        "smtp_host": "smtp.test",
        "smtp_port": "587",
        "smtp_user": "hr@example.ru",
        "smtp_password": "пароль",
        "smtp_to": "boss@example.ru",
    }
    r = web.post("/settings/mail-test", data=form)
    assert "Пробное письмо ушло на boss@example.ru." in r.text
    assert 'value="smtp.test"' in r.text and 'value="boss@example.ru"' in r.text
    assert len(FakeSMTP.sent) == 1 and config.get("smtp_password") == "пароль"


# ── 4. Зависший прогон не блокирует ночь ────────────────────────────────────


def test_4_stuck_running_night_is_failed_and_new_one_queued(session):
    stuck = NightRun(status="running", started_at=datetime(2026, 9, 27, 2, 0), summary={})
    session.add(stuck)
    session.flush()
    job = enqueue(session, "night", {"run_id": stuck.id})
    job.status = "done"
    session.commit()
    fresh = night.enqueue()
    assert fresh.id != stuck.id
    session.expire_all()
    stuck = session.get(NightRun, stuck.id)
    assert stuck.status == "failed" and "оборвался" in stuck.error
    run_pending()
    view = morning.view(session)
    assert view["run"].id == fresh.id and view["alert"] is None


def test_4_morning_shows_broken_run_with_run_now(session):
    stuck = NightRun(status="running", started_at=datetime(2026, 9, 27, 2, 0), summary={})
    session.add(stuck)
    session.commit()  # задачи night нет — прогон оборвался
    view = morning.view(session)
    assert view["alert"] == night.BROKEN and view["run_now"]


def test_mini_odd_ids_are_400_or_404(web, session, base):  # noqa: F811
    v, _ = base
    r = web.post(f"/vacancies/{v.id}/decision", data={"candidate_id": "²", "decision": "invite"})
    assert r.status_code == 400
    assert web.get("/candidates/" + "9" * 23).status_code == 404


def test_mini_docker_image_has_tzdata():
    from pathlib import Path

    assert "tzdata" in (Path(__file__).parent.parent / "Dockerfile").read_text()


def test_4_live_run_is_not_duplicated(session):
    first = night.enqueue()
    assert night.enqueue().id == first.id
    assert len(list(session.scalars(select(Job).where(Job.kind == "night")))) == 1


# ── Мелочи ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["Мужского пола", "Лицо женского пола", "С детьми не рассматриваем", "Бездетная",
     "Возраст 45+", "Кандидат 45+", "Не старше 45+", "25–35 лет", "Возраст 25–35 лет",
     "Девушка с опытом продаж", "Парень до 30", "Молодой специалист до 30", "Холост",
     "Мужчина до 40 лет", "Только молодые и активные"],
)  # fmt: skip
def test_more_discriminatory_wordings(text):
    assert ev.DISCRIMINATORY_RE.search(text), text


@pytest.mark.parametrize(
    "text",
    ["Опыт работы 10+ лет", "Руководил командой 20+ человек", "Стаж 15+", "Укладка пола",
     "Ремонт пола в цехах", "Женская одежда, опыт продаж", "Мужская обувь: категорийный менеджер",
     "Опыт руководства 10+ лет", "Опыт 10–15 лет"],
)  # fmt: skip
def test_ordinary_requirements_are_not_discriminatory(text):
    """Второй круг приёмки: обычные требования не помечаются и не пропадают из оценки."""
    assert not ev.DISCRIMINATORY_RE.search(text), text
    assert ev._requirement([], text, "must", "ai")["flag"] != "discriminatory"


@pytest.mark.parametrize("text", ["Возраст — 45 лет", "мне уже 45 лет"])
def test_age_with_dash_or_already_is_masked(text):
    assert "45" not in anonymize(text)


def test_compose_runs_in_moscow_time():
    from pathlib import Path

    compose = yaml.safe_load((Path(__file__).parent.parent / "compose.yaml").read_text())
    assert "TZ=Europe/Moscow" in compose["services"]["app"]["environment"]
