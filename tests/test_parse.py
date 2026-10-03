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
    assert prod["positions"][0]["lines_ok"] and prod["summary_quote_ok"]
    start, stop = prod["positions"][0]["source_lines"]
    line = five["production.txt"].raw_text.split("\n")[start - 1]
    assert start == stop and "Литейный завод Поволжье" in line
    logistics = five["logistics.txt"].parsed
    assert logistics["relocation"] == "no_relocation"
    # строк 900–901 в резюме нет → опоры нет, на экране «проверьте»
    assert logistics["positions"][1]["source_lines"] is None
    assert logistics["positions"][1]["lines_ok"] is False
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


def test_crash_mid_batch_does_not_resend_parsed(five, session, sent, monkeypatch):
    """Сбой на третьем ответе (как SIGKILL): две записи уже сохранены, после перезапуска
    уходят в модель только оставшиеся три."""
    from app import parse

    monkeypatch.setattr(parse, "PARALLEL", 1)
    seen = []
    original = parse.to_parsed

    def crash_on_third(profile, c, text=None):
        seen.append(c.id)
        if len(seen) == 3:
            raise RuntimeError("процесс убит")
        return original(profile, c, text)

    monkeypatch.setattr(parse, "to_parsed", crash_on_third)
    job = parse_all(session)
    assert job.status == "failed"
    assert sum(c.parse_status == "parsed" for c in five.values()) == 2
    job.status = "running"  # перезапуск приложения продолжает прерванную задачу
    session.commit()
    run_pending()
    session.expire_all()
    assert all(c.parse_status == "parsed" for c in five.values())
    assert len(sent) == 5 + 3


def test_guard_nothing_personal_reaches_model(five, session, sent):
    parse_all(session)
    assert len(sent) == 5
    # резюме уходит с номерами строк, и их столько же, сколько в оригинале
    prod = five["production.txt"]
    text = next(t for t in sent if "Литейный завод Поволжье" in t)
    assert f"{prod.raw_text.count(chr(10)) + 1}| " in text
    assert f"{prod.raw_text.count(chr(10)) + 2}| " not in text
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


FINANCE = (
    "Руководитель отдела отчётности, 11 лет\n"
    "— сократил срок закрытия месяца с 12 до 5 дней\n"
    "— прошёл 4 налоговые проверки без доначислений"
)


def test_summary_sentence_with_number_not_in_resume_dropped():
    """Живой прогон 03.10.2026: «Кратко» пересчитало «с 12 до 5 дней» в «на 58 %».
    Фраза с числом, которого нет в резюме, в «Кратко» не попадает; остальные остаются."""
    from app.parse import CandidateProfile, to_parsed

    profile = CandidateProfile(
        summary=(
            "Руководитель отдела отчётности с 11-летним опытом. "
            "Сократил срок закрытия месяца на 58%. "
            "Прошёл 4 налоговые проверки без доначислений."
        )
    )
    c = Candidate(raw_text=FINANCE, full_name=None, phones=[], emails=[], links=[])
    summary = to_parsed(profile, c, seen=FINANCE)["summary"]
    assert summary == (
        "Руководитель отдела отчётности с 11-летним опытом. "
        "Прошёл 4 налоговые проверки без доначислений."
    )


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
    assert job.error == "Ключ не принят: проверьте, что он скопирован целиком."


def test_402_mid_parse_keeps_answers_already_paid(five, session, tmp_path, monkeypatch):
    """Ревью PR #3: 402 на одном резюме, а запросы по трём другим уже ушли (PARALLEL = 4)
    и оплачены. Разбор дожидается их ответов, сохраняет и только потом останавливается;
    «Повторить разбор» отправляет одно неразобранное резюме."""
    import shutil
    import time

    folder = tmp_path / "llm"
    shutil.copytree(FIXTURES / "llm", folder)
    broke = {"match": "ООО «СКБ Контур»", "response": {"__error__": 402}}
    (folder / "it.json").write_text(json.dumps(broke), encoding="utf-8")
    config.save({"llm_fixtures": str(folder)})
    names = ["it.txt", "finance.txt", "logistics.txt", "production.txt"]
    answer_slowly, paid = MockLLM._call, []

    def slow(self, schema, system, user):
        if "ООО «СКБ Контур»" not in user:  # 402 приходит сразу, остальные — через 1 с
            time.sleep(1)
        answer = answer_slowly(self, schema, system, user)
        paid.append(user)
        return answer

    monkeypatch.setattr(MockLLM, "_call", slow)
    job = start_parse(session, [five[n].id for n in names])
    run_pending()
    session.expire_all()
    job = session.get(Job, job.id)
    assert job.status == "failed" and job.error == llm.BALANCE_MESSAGE
    assert len(paid) == 3
    assert [n for n in names if five[n].parse_status == "parsed"] == names[1:]

    config.save({"llm_fixtures": str(FIXTURES / "llm")})  # баланс пополнили
    paid.clear()
    job.status, job.error = "running", None  # как «Повторить разбор»
    session.commit()
    run_pending()
    session.expire_all()
    assert len(paid) == 1 and "ООО «СКБ Контур»" in paid[0]
    assert all(five[n].parse_status == "parsed" for n in names)


def test_empty_balance_stops_job_with_human_message(five, session, tmp_path):
    """На ключе кончились деньги (402): разбор останавливается на первом отказе с
    понятной причиной, а не помечает каждое резюме «проверьте модель и адрес»."""
    (tmp_path / "balance.json").write_text(
        json.dumps({"match": "", "response": {"__error__": 402}})
    )
    config.save({"llm_fixtures": str(tmp_path)})
    job = parse_all(session)
    assert job.status == "failed" and job.error == llm.BALANCE_MESSAGE


def test_resume_without_recorded_answer_is_failed_not_empty(five, session, tmp_path):
    """Решение главной сессии: у мока нет ответа на резюме — «Не удалось разобрать»
    с причиной «ответ модели не получен» (аудит №3: без служебных слов про запись),
    а не пустой профиль со статусом «Разобрано»."""
    folder = tmp_path / "empty"
    folder.mkdir()
    config.save({"llm_fixtures": str(folder)})
    job = parse_all(session)
    assert job.status == "done"
    for c in five.values():
        session.refresh(c)
        assert (c.parse_status, c.parse_error, c.parsed) == (
            "failed",
            "ответ модели не получен",
            None,
        )


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
