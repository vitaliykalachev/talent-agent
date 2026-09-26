import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from test_anonymize import leaks

from app import config, llm
from app.jobs import run_pending
from app.llm import MockLLM
from app.models import Candidate, Job
from app.parse import (
    apply_edit,
    company_key,
    estimate,
    start_parse,
    waiting_ids,
    years_by_positions,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sent(monkeypatch):
    """Сторож: всё, что уходит в модель, записывается для проверки."""
    calls: list[str] = []
    original = MockLLM._call

    def spy(self, schema, system, user):
        calls.append(system + "\n" + user)
        return original(self, schema, system, user)

    monkeypatch.setattr(MockLLM, "_call", spy)
    monkeypatch.setattr(llm, "RETRY_PAUSE", 0)
    return calls


@pytest.fixture
def five(do_import, session, sent):
    """Пять резюме разных профессий, загруженные обычным импортом, и мок модели."""
    config.save({"llm_provider": "mock", "llm_fixtures": str(FIXTURES / "llm")})
    do_import(None, [FIXTURES / "resumes"])
    by_file = {c.source_file: c for c in session.scalars(select(Candidate))}
    assert len(by_file) == 5
    return by_file


def parse_all(session) -> Job:
    job = start_parse(session, waiting_ids(session))
    run_pending()
    session.expire_all()
    return session.get(Job, job.id)


def test_five_professions_parsed_with_current_positions(five, session):
    job = parse_all(session)
    assert job.status == "done" and job.error is None
    assert job.payload["tokens_in"] > 0 and job.payload["tokens_out"] > 0
    session.expire_all()
    for c in five.values():
        assert c.parse_status == "parsed", c.source_file
        first = c.parsed["positions"][0]
        # «по н.в.», «настоящее время», «по наст. вр.», «сейчас», «по н. в.»
        assert first["is_current"] is True and first["end"] is None, c.source_file

    prod = five["production.txt"].parsed
    assert prod["desired_position"] == "Директор по производству"
    assert prod["positions"][0]["company_key"] == "литейный завод поволжье"
    assert prod["positions"][0]["team_size"] == 40
    assert (prod["salary_amount"], prod["salary_currency"], prod["salary_kind"]) == (
        250000,
        "RUB",
        "net",
    )
    assert prod["relocation"] == "relocation_possible"
    assert prod["positions"][0]["quote_ok"] and prod["summary_quote_ok"]
    assert five["logistics.txt"].parsed["relocation"] == "no_relocation"
    # выдуманная моделью цитата не найдена в исходнике → «проверьте»
    assert five["logistics.txt"].parsed["summary_quote_ok"] is False
    assert five["finance.txt"].parsed["languages"] == ["английский B2"]


def test_overloaded_service_on_one_resume_does_not_stop_parse(five, session, tmp_path):
    """Ответ 529 на одном резюме: оно уходит в «не удалось», остальные разобраны."""
    import shutil

    folder = tmp_path / "llm"
    shutil.copytree(FIXTURES / "llm", folder)
    busy = {"match": "ООО «СКБ Контур»", "response": {"__error__": 529}}
    (folder / "it.json").write_text(json.dumps(busy), encoding="utf-8")
    config.save({"llm_fixtures": str(folder)})
    job = parse_all(session)
    assert job.status == "done"
    assert job.error == f"{five['it.txt'].id}: сервис ИИ не ответил"
    assert five["it.txt"].parse_status == "failed"
    assert sum(c.parse_status == "parsed" for c in five.values()) == 4


def test_guard_nothing_personal_reaches_model(five, session, sent):
    parse_all(session)
    assert len(sent) == 5
    for c in five.values():
        for text in sent:
            assert leaks(text, c) == [], (c.source_file, text[:400])
    # сторож не пустой: на сыром тексте он срабатывает
    c = five["production.txt"]
    assert leaks(c.raw_text, c)


def test_stated_years_checked_against_positions(five, session):
    parse_all(session)
    sales = five["sales.txt"].parsed
    assert sales["total_years"] == years_by_positions(sales["positions"], date.today())
    assert sales["total_years_stated"] == 11 and sales["total_years_check"] is False
    it = five["it.txt"].parsed  # по местам работы ~8 лет, в ответе модели 12
    assert it["total_years_check"] is True


def test_years_merge_overlaps_and_current_until_resume_date():
    positions = [
        {"start": "2015-01", "end": "2017-12"},
        {"start": "2017-06", "end": "2018-12"},  # пересекается с первым
        {"start": "2020", "is_current": True},
    ]
    assert years_by_positions(positions, date(2021, 12, 31)) == 6.0


def test_company_key_strips_forms_and_quotes():
    assert company_key("ООО «Ромашка»") == company_key("ромашка") == "ромашка"
    assert company_key('ПАО "Северсталь"') == "северсталь"


def test_empty_resume_and_network_failure_marked_failed_then_retried(
    five, session, monkeypatch, tmp_path
):
    fixtures = tmp_path / "llm"
    fixtures.mkdir()
    for p in (FIXTURES / "llm").glob("*.json"):
        data = json.loads(p.read_text("utf-8"))
        if p.stem == "it":  # сеть падает пять раз подряд — больше, чем три повтора
            data = {
                "match": data["match"],
                "responses": [{"__error__": "network"}] * 5 + [data["response"]],
            }
        (fixtures / p.name).write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    config.save({"llm_fixtures": str(fixtures)})
    five["sales.txt"].raw_text = "   "
    session.commit()

    job = parse_all(session)
    assert job.status == "done"
    failed = {c.source_file: c.parse_error for c in five.values() if c.parse_status == "failed"}
    assert failed == {"sales.txt": "в резюме нет текста", "it.txt": "сервис ИИ не ответил"}
    assert len(job.error.splitlines()) == 2

    five["sales.txt"].raw_text = (FIXTURES / "resumes" / "sales.txt").read_text("utf-8")
    session.commit()
    config.save({"llm_fixtures": str(FIXTURES / "llm")})  # сеть починилась
    ids = [c.id for c in five.values() if c.parse_status == "failed"]
    retry = start_parse(session, ids)
    run_pending()
    session.expire_all()
    assert session.get(Job, retry.id).total == 2
    assert all(c.parse_status == "parsed" for c in five.values())


def test_bad_key_stops_job_with_human_message(five, session, tmp_path):
    (tmp_path / "auth.json").write_text(
        json.dumps({"match": "", "response": {"__error__": "auth"}})
    )
    config.save({"llm_fixtures": str(tmp_path)})
    job = parse_all(session)
    assert job.status == "failed"
    assert job.error == "Ключ доступа не подошёл. Проверьте, что скопировали его целиком."


def test_user_edit_survives_reparse(five, session):
    parse_all(session)
    c = five["production.txt"]
    apply_edit(c, "city", "Тольятти")
    apply_edit(c, "salary_amount", "300 000")
    session.commit()
    start_parse(session, [c.id])
    run_pending()
    session.expire_all()
    assert c.parsed["city"] == "Тольятти" and c.parsed["salary_amount"] == 300000
    assert c.parsed["edits"]["city"]["at"] == str(date.today())
    assert c.parsed["positions"][0]["company_key"] == "литейный завод поволжье"  # остальное свежее


def test_paused_job_waits_until_resumed(five, session):
    job = start_parse(session, waiting_ids(session))
    job.status = "paused"
    session.commit()
    run_pending()
    session.expire_all()
    assert session.get(Job, job.id).progress == 0
    session.get(Job, job.id).status = "queued"
    session.commit()
    run_pending()
    session.expire_all()
    assert session.get(Job, job.id).status == "done"


def test_cost_estimate_from_characters_and_settings_prices(five, session):
    ids = waiting_ids(session)
    config.save({"price_parse_in": "1", "price_parse_out": "5", "usd_rub": "84"})
    base = estimate(session, ids)
    assert base["count"] == 5 and base["chars"] == sum(len(c.raw_text) for c in five.values())
    assert 0 < base["rub"] < 5  # пять резюме на haiku — копейки
    assert base["rub_low"] < base["rub"] < base["rub_high"]
    config.save({"price_parse_in": "2", "price_parse_out": "10"})
    assert estimate(session, ids)["rub"] == pytest.approx(base["rub"] * 2)
    config.save({"usd_rub": "100"})
    est = estimate(session, ids)
    assert est["usd"] == pytest.approx(base["usd"] * 2) and est["rub"] == pytest.approx(
        est["usd"] * 100
    )
