"""Ночь: расписание с подменой времени, ночной прогон на моке, «Утро», письмо без ПД."""

import hashlib
from datetime import date, datetime

import apscheduler.executors.base as exec_base
import apscheduler.schedulers.base as sched_base
import numpy as np
import pytest
from apscheduler.executors.debug import DebugExecutor
from apscheduler.schedulers.base import BaseScheduler
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_anonymize import leaks
from test_evaluate import answer, gromov_checks, mock  # noqa: F401 — фикстура mock

from app import config, embed, mail, morning, night, schedule
from app import evaluate as ev
from app.jobs import run_pending
from app.main import create_app
from app.models import Candidate, Embedding, Match, NightRun, Vacancy

# ── Расписание с подменой времени ───────────────────────────────────────────


class Clock:
    now = datetime(2026, 9, 28, 1, 59)


class FakeDateTime(datetime):
    """Часы, которые двигает тест: APScheduler, расписание и ночной прогон видят их."""

    @classmethod
    def now(cls, tz=None):
        return Clock.now.astimezone().astimezone(tz) if tz else Clock.now


class ManualScheduler(BaseScheduler):
    """Настоящий планировщик APScheduler без своего потока: тест сам зовёт проверку."""

    def wakeup(self):
        pass

    def shutdown(self, wait=True):
        super().shutdown(wait)


@pytest.fixture
def clock(monkeypatch):
    for module in (sched_base, exec_base, schedule, night):
        monkeypatch.setattr(module, "datetime", FakeDateTime)
    Clock.now = datetime(2026, 9, 28, 1, 59)  # понедельник
    return Clock


@pytest.fixture
def scheduler(clock, session):
    sched = ManualScheduler(timezone=schedule._local_tz(), executors={"default": DebugExecutor()})
    schedule.configure(sched)
    sched.start()
    yield sched
    sched.shutdown(wait=False)


def tick(sched, when: datetime) -> None:
    Clock.now = when
    sched._process_jobs()


def runs(session) -> list[NightRun]:
    session.expire_all()
    return list(session.scalars(select(NightRun).order_by(NightRun.id)))


def test_cron_is_built_from_time_and_days(session):
    assert str(schedule.trigger("02:00", "daily")).startswith(
        "cron[day_of_week='*', hour='2', minute='0'"
    )
    assert str(schedule.trigger("03:30", "weekdays")).startswith(
        "cron[day_of_week='mon-fri', hour='3', minute='30'"
    )
    assert schedule.parse_time("2:00") == (2, 0) and schedule.parse_time("25:00") is None


def test_night_job_coalesces_with_six_hour_grace(scheduler):
    job = scheduler.get_job(schedule.JOB_ID)
    assert job.coalesce is True and job.misfire_grace_time == 6 * 3600
    assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 9, 28, 2, 0)


def test_runs_at_two_and_once_after_sleep_with_reason(scheduler, session):
    tick(scheduler, datetime(2026, 9, 28, 2, 0, 3))
    [first] = runs(session)
    assert first.planned_at == datetime(2026, 9, 28, 2, 0) and first.status == "queued"
    run_pending()
    [first] = runs(session)
    assert first.status == "done" and first.summary["late"] is None

    # Компьютер уснул после первого прогона и проснулся через три ночи в 04:30:
    # проспано три запуска, выполняется один, с причиной опоздания.
    tick(scheduler, datetime(2026, 10, 1, 4, 30))
    assert len(runs(session)) == 2
    run_pending()
    second = runs(session)[1]
    assert second.planned_at == datetime(2026, 10, 1, 2, 0)
    assert second.summary["late"] == (
        "Ночной прогон начался в 04:30, а не в 02:00: компьютер спал."
    )
    view = morning.view(session, Clock.now)
    assert view["alert"] == second.summary["late"] and not view["run_now"]

    # Проснулся позже шести часов: прогон пропущен, «Утро» говорит почему и предлагает
    # запустить сейчас.
    tick(scheduler, datetime(2026, 10, 2, 9, 30))
    third = runs(session)[2]
    assert third.status == "missed"
    assert third.error == (
        "Ночной прогон в 02:00 не состоялся: компьютер спал или был выключен до 09:30."
    )
    view = morning.view(session, Clock.now)
    assert view["alert"] == third.error and view["run_now"]
    assert view["run"].id == second.id  # отчёт — за последний удачный прогон


def test_weekdays_skip_weekend(clock, session):
    config.save({"night_days": "weekdays"})
    saturday = datetime(2026, 10, 3, 1, 0)
    assert schedule.next_run(saturday) == datetime(2026, 10, 5, 2, 0)
    assert schedule.previous_run(datetime(2026, 10, 5, 1, 0)) == datetime(2026, 10, 2, 2, 0)


def test_morning_without_expected_run_offers_run_now(clock, session):
    run = NightRun(
        planned_at=datetime(2026, 9, 27, 2, 0),
        started_at=datetime(2026, 9, 27, 2, 0),
        status="done",
        summary={"vacancies": [], "checked_vacancies": 0, "checked_resumes": 0},
    )
    session.add(run)
    session.commit()
    view = morning.view(session, datetime(2026, 9, 28, 9, 0))  # в 02:00 приложение не работало
    assert view["run_now"] and "не состоялся: приложение не было запущено" in view["alert"]


# ── Ночной прогон на моке ───────────────────────────────────────────────────


def fake_encode(texts, kind):
    """Отпечатки без модели: детерминированный вектор по тексту."""
    out = []
    for text in texts:
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        v = np.random.default_rng(seed).standard_normal(16).astype(np.float32)
        out.append(v / np.linalg.norm(v))
    return np.vstack(out)


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
        "Начальник цеха литья, ООО «ТехноПласт», 2015 — 2024\n"
        "— литьё под давлением, цех 60 человек\n— бережливое производство, ISO 9001"
    ),
    "Белова Мария Олеговна": (
        "Белова Мария Олеговна\n+7 903 123-45-67\nГлавный бухгалтер, ООО «Счёт», 2010 — 2023\n"
        "— отчётность МСФО"
    ),
}


CONTACTS = {
    "Громов Илья Сергеевич": ["+79175551234", "gromov.ilya@mail.ru"],
    "Орлова Анна Викторовна": ["+79120001122", "a.orlova@yandex.ru"],
    "Белова Мария Олеговна": ["+79031234567"],
}


@pytest.fixture
def night_base(session, mock, monkeypatch):  # noqa: F811
    monkeypatch.setattr(embed, "encode", fake_encode)
    people = {}
    for name, text in RESUMES.items():
        c = Candidate(
            full_name=name,
            raw_text=text,
            phones=[CONTACTS[name][0]],
            emails=CONTACTS[name][1:],
            parse_status="new" if name.startswith("Белова") else "parsed",
            parsed=None if name.startswith("Белова") else {"positions": []},
            resume_date=date(2023, 1, 10) if name.startswith("Белова") else date.today(),
            stale=False,
        )
        session.add(c)
        people[name.split()[0]] = c
    v = Vacancy(title="Начальник цеха литья", description="Начальник цеха литья под давлением")
    v.requirements = [
        {"id": "r1", "name": "Руководил цехом литья", "kind": "must", "weight": 2},
        {"id": "r2", "name": "Литьё под давлением", "kind": "must", "weight": 2},
        {"id": "r3", "name": "Бережливое производство", "kind": "nice", "weight": 1},
        {"id": "r4", "name": "ISO 9001", "kind": "nice", "weight": 1},
        {"id": "r5", "name": "Только продажи, без производства", "kind": "avoid", "weight": 2},
    ]
    v.schedule_enabled = True
    session.add(v)
    session.commit()
    gromov = people["Громов"]
    session.add(  # Громова уже оценили по текущим требованиям — ночью его не трогаем
        Match(
            vacancy_id=v.id,
            candidate_id=gromov.id,
            status="ok",
            score=100,
            checks=[],
            requirements_version=v.requirements_version,
            resume_hash=ev.resume_hash(gromov),
            change_reason="new",
            evaluated_at=datetime(2026, 9, 1),
        )
    )
    session.commit()
    orlova = people["Орлова"]
    mock(
        [
            answer(
                v,
                orlova,
                gromov_checks(
                    r1=("met", {"__lines__": "Начальник цеха литья"}),
                    r3=("met", {"__lines__": "бережливое производство"}),
                    r4=("met", {"__lines__": "ISO 9001"}),
                ),
            )
        ]
    )
    return v, people


def test_night_marks_stale_embeds_new_and_evaluates_only_new(session, night_base, mock):  # noqa: F811
    v, people = night_base
    night.enqueue()
    run_pending()
    [run] = runs(session)
    assert run.status == "done", run.error
    assert len(mock.calls) == 1 and f"кандидат {people['Орлова'].id}\n" in mock.calls[0]
    session.expire_all()
    assert session.get(Candidate, people["Белова"].id).stale is True
    embedded = set(session.scalars(select(Embedding.candidate_id)))
    assert embedded == {people["Громов"].id, people["Орлова"].id}  # разобранные без отпечатка
    s = run.summary
    assert s["vacancies"] == [
        {
            "id": v.id,
            "title": "Начальник цеха литья",
            "fit": 1,
            "maybe": 0,
            "unfit": 0,
            "changed": 0,
            "failed": 0,
        }
    ]
    assert s["base"]["stale"] == 1 and s["base"]["embedded"] == 2
    assert s["decide"]["stale"] == 1 and s["checked_resumes"] == 3
    assert s["mail"] is None  # почта не настроена — письма нет, «Утро» есть

    view = morning.view(session)
    assert view["alert"] is None and view["empty"] is None
    titles = [sec["title"] for sec in view["sections"]]
    assert titles == ["Новые кандидаты по вакансиям", "Нужно решить", "Что изменилось в базе"]
    first = view["sections"][0]["rows"][0]
    assert first == {
        "text": "Начальник цеха литья: 1 подходит",
        "link": f"/vacancies/{v.id}/results",
    }

    # Вторая ночь: ничего нового — пустое утро с числами, модель не вызывалась.
    night.enqueue()
    run_pending()
    assert len(mock.calls) == 1
    view = morning.view(session)
    assert view["empty"] == (
        "За ночь новых подходящих кандидатов нет. Проверили 1 вакансию по 3 резюме."
    )


def test_failed_night_run_shows_reason_first(session, monkeypatch):
    def boom(_session):
        raise RuntimeError("база занята")

    monkeypatch.setattr(night, "_mark_stale", boom)
    night.enqueue()
    run_pending()
    view = morning.view(session)
    assert view["alert"] == "Ночной прогон не получился: база занята" and view["run_now"]


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, **_):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return False

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_report_mail_has_numbers_and_links_but_no_personal_data(
    session,
    night_base,
    mock,  # noqa: F811
    monkeypatch,
):
    v, people = night_base
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    config.save({"smtp_host": "smtp.test", "smtp_port": "587", "smtp_to": "boss@example.ru"})
    night.enqueue()
    run_pending()
    [msg] = FakeSMTP.sent
    body = msg.get_content()
    assert "Начальник цеха литья: 1 подходит: http://127.0.0.1:8000/vacancies/" in body
    assert "Нужно решить" in body and "http://127.0.0.1:8000/morning" in body
    for c in people.values():
        assert leaks(body, c) == [], c.full_name
        for word in c.full_name.split():
            assert word not in body
    assert runs(session)[0].summary["mail"] == "sent"


def test_test_mail_without_settings_explains(session):
    assert mail.send_test() == "Заполните адрес почтового сервера и адрес получателя."


# ── Экраны ──────────────────────────────────────────────────────────────────


@pytest.fixture
def web(session):
    return TestClient(create_app())


def test_morning_screen_empty_base_offers_run_now(web):
    r = web.get("/morning")
    assert r.status_code == 200
    assert "Ночного прогона ещё не было." in r.text and "Запустить сейчас" in r.text
    r = web.post("/night/run", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/morning"
    run_pending()
    r = web.get("/morning")
    assert "За ночь новых подходящих кандидатов нет. Проверили 0 вакансий по 0 резюме." in r.text


def test_home_says_what_happens_tonight(web, session):
    session.add(Vacancy(title="Технолог", description="технолог", schedule_enabled=True))
    session.add(Candidate(full_name="Иванов Иван", raw_text="Технолог"))
    session.commit()
    r = web.get("/")
    assert r.status_code == 200
    assert (
        "Этой ночью в 02:00: 1 вакансия и 1 новое резюме. Компьютер должен быть включён." in r.text
    ) or "Ближайший ночной прогон" in r.text
