"""Этап 5, аудит интерфейса: вакансии, результат, карточка кандидата, дубли."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_evaluate import answer, base, matches, mock, run, standard  # noqa: F401 — фикстуры
from test_merge import csv, namesakes  # noqa: F401 — фикстуры

from app import evaluate as ev
from app.main import create_app
from app.models import Candidate, Feedback, Match, Vacancy

CSS = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text()


@pytest.fixture
def web(session):
    return TestClient(create_app())


def test_7_undo_button_visible_and_returns_to_card(web, session, base, mock):  # noqa: F811
    v, p = base
    mock(standard(v, p))
    run(session, v)
    gid = p["Громов"].id
    r = web.post(
        f"/vacancies/{v.id}/feedback",
        data={"candidate_id": gid, "target": "reason", "text": "Литьё", "kind": "misread"},
        follow_redirects=False,
    )
    assert r.headers["location"].endswith(f"#c{gid}")
    toast = web.get(r.headers["location"]).text
    assert f'name="back" value="/vacancies/{v.id}/results#c{gid}"' in toast
    f = session.scalar(select(Feedback))
    back = web.post(
        f"/feedback/{f.id}/delete",
        data={"back": f"/vacancies/{v.id}/results#c{gid}"},
        follow_redirects=False,
    )
    assert back.headers["location"] == f"/vacancies/{v.id}/results#c{gid}"
    assert ".toast .button.ghost{background:transparent;color:#fff" in CSS


def test_8_cut_sentence_shown_above_portrait(web, session):
    v = Vacancy(
        title="Технолог",
        description="Нужен технолог литья. Мужчина 25–35 лет, желательно без детей. Опыт от 3 лет.",
        requirements=[{"id": "r1", "name": "Технолог литья", "kind": "must", "weight": 3}],
    )
    session.add(v)
    session.commit()
    assert "Мужчина" not in ev.fair_description(v)
    text = web.get(f"/vacancies/{v.id}").text
    line = (
        "Из описания не взяли: «Мужчина 25–35 лет, желательно без детей.» — "
        "возраст, пол и семья в оценке не участвуют."
    )
    assert line in text
    assert text.index(line) < text.index("Портрет кандидата")


def test_12_double_submit_creates_one_vacancy_and_button_locks(web, session, mock):  # noqa: F811
    description = "Нужен начальник литейного цеха, литьё под давлением."
    mock(
        [
            {
                "match": description[:30],
                "schema": "VacancyDraft",
                "response": {"title": "Начальник цеха", "requirements": []},
            }
        ]
    )
    form = web.get("/vacancies/new").text
    assert 'hx-disabled-elt="find button[type=submit]"' in form
    assert "Разбираем описание…" in form
    first = web.post("/vacancies/new", data={"description": description}, follow_redirects=False)
    again = web.post("/vacancies/new", data={"description": description}, follow_redirects=False)
    assert first.headers["location"] == again.headers["location"]
    assert session.scalar(select(func.count(Vacancy.id))) == 1
    hx = web.post("/vacancies/new", data={"description": description}, headers={"HX-Request": "1"})
    assert hx.headers["HX-Redirect"] == first.headers["location"]


def test_13_quote_without_leading_dash_and_cut_by_word():
    text = "— запустил участок литья под давлением на заводе 2019 года выпуска"
    assert ev._quote(text, [1, 1]).startswith("запустил")
    cut = ev._quote(text, [1, 1], limit=40)
    assert cut.endswith("…") and cut[:-1] in text and not cut[:-1].endswith(" ")
    assert text.split()[len(cut[:-1].split())].isalnum()  # оборвали между словами


def test_14_18_20_results_page(web, session, base, mock):  # noqa: F811
    v, p = base
    mock(standard(v, p))
    run(session, v)
    text = web.get(f"/vacancies/{v.id}/results").text
    assert "может ошибиться" in text and "Понятно" not in text
    assert "Выбрать, что скачать" in text
    assert re.search(r'value="invite" disabled> Отмеченных «Позвать» \(0\)', text)
    assert 'name="kind"' not in text  # ни одной скрытой формы «Неверно»
    link = re.search(r'href="(/vacancies/\d+/wrong\?[^"]+)"', text).group(1)
    part = web.get(link.replace("&amp;", "&"), headers={"HX-Request": "1"}).text
    assert "<html" not in part and part.count('name="kind"') == 4
    full = web.get(link.replace("&amp;", "&")).text
    assert "<html" in full and 'action="/vacancies/' in full
    web.post(
        f"/vacancies/{v.id}/decision", data={"candidate_id": p["Громов"].id, "decision": "invite"}
    )
    text = web.get(f"/vacancies/{v.id}/results").text
    assert 'value="invite" checked> Отмеченных «Позвать» (1)' in text


def test_16_bad_number_in_fix_shows_error_at_field(web, session, base):  # noqa: F811
    _, p = base
    c = p["Громов"]
    r = web.post(f"/candidates/{c.id}/fix", data={"field": "total_years", "value": "много"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert "Нужно число не меньше нуля, например 12,5." in r.text
    assert '<details class="fix" open>' in r.text and 'value="много"' in r.text
    assert '{"detail"' not in r.text


def test_21_24_candidate_card_typography_and_sources(web, session):
    from datetime import date

    c = Candidate(
        full_name="Громов Илья",
        raw_text="Громов Илья\nГород: Самара\nНавыки: литьё, SAP\nГотов к переезду",
        phones=["+79120000000"],
        parse_status="parsed",
        parsed={
            "total_years": 11.5,
            "city": "Самара",
            "skills": ["литьё", "SAP"],
            "relocation": "relocation_possible",
            "positions": [],
        },
        resume_date=date(2099, 1, 1),
    )
    session.add(c)
    session.add(Candidate(full_name="Новиков Пётр", raw_text="Технолог", parse_status="new"))
    session.commit()
    text = web.get(f"/candidates/{c.id}").text
    assert "+7\xa0950\xa0476-39-28" in text and "+79120000000" not in text
    assert "11,5\xa0года" in text
    assert "дата в будущем, проверьте" in text
    for anchor in ("q-city", "q-skills", "q-relocation"):
        assert f'href="#{anchor}"' in text and f'<mark id="{anchor}">' in text
    fresh = web.get(f"/candidates/{c.id + 1}").text
    assert ">Разобрать</button>" in fresh


def test_21_plural_and_cut_helpers():
    from app.web.present import count, cut, years

    assert count(1, "запись", "записи", "записей") == "1\xa0запись"
    assert count(3, "запись", "записи", "записей") == "3\xa0записи"
    assert count(11, "кандидат", "кандидата", "кандидатов") == "11\xa0кандидатов"
    assert years(2) == "2\xa0года" and years(20) == "20\xa0лет" and years(1) == "1\xa0год"
    assert cut("Переводчик по техническому переводу", 28) == "Переводчик по техническому…"


def test_23_merge_shows_line_with_undo(web, namesakes, session):  # noqa: F811
    r = web.post(f"/duplicates/{namesakes.id}/merge", data={})
    assert "Объединили: Кузнецов А. П." in r.text and "Отменить" in r.text
    back = web.post(
        f"/duplicates/{namesakes.id}/unmerge", data={"back": "pair"}, follow_redirects=False
    )
    assert back.headers["location"] == f"/duplicates/{namesakes.id}"


def test_15_settings_check_uses_typed_fields_and_names_the_cause(web, session):
    import time

    from app import config

    page = web.get("/settings").text
    form = page[page.index('<form class="card settings" method="post" action="/settings">') :]
    form = form[: form.index("</form>")]
    assert 'formaction="/settings/check"' in form  # проверка внутри формы, с её полями
    assert '<details class="admin">' in form and "Модель для разбора резюме" in form
    text = web.post("/settings/check", data={"llm_provider": "anthropic"}).text
    assert "Ключ доступа не задан" in text and "Адрес сервиса не отвечает" not in text
    started = time.monotonic()
    text = web.post(
        "/settings/check",
        data={
            "llm_provider": "anthropic",
            "llm_base_url": "http://127.0.0.1:9",
            "llm_api_key": "проверочный",
        },
    ).text
    assert time.monotonic() - started < 10
    assert "Адрес сервиса не отвечает за 10 секунд" in text
    assert 'value="http://127.0.0.1:9"' in text  # введённый адрес остался в поле
    assert not config.get("llm_api_key")  # проверка ничего не сохраняет
    config.save({"llm_api_key": "старый"})
    assert "Удалить ключ" in web.get("/settings").text
    text = web.post("/settings/key-delete").text
    assert "Ключ удалён." in text and not config.get("llm_api_key")


def test_15_key_delete_warns_when_key_comes_from_env(web, session, monkeypatch):
    from app import config

    config.save({"llm_api_key": "из-настроек"})
    monkeypatch.setenv("LLM_API_KEY", "из-env")
    text = web.post("/settings/key-delete").text
    assert "Ключ из настроек удалён, но приложение берёт ключ из файла .env" in text
    assert "Ключ удалён." not in text


def test_15_mail_error_in_plain_russian():
    import smtplib
    import socket

    from app.mail import _error

    assert "не найден" in _error(socket.gaierror(8, "nodename nor servname provided"))
    assert "порту" in _error(ConnectionRefusedError())
    assert "логин или пароль" in _error(smtplib.SMTPAuthenticationError(535, b"no"))
    for exc in (socket.gaierror(8, "x"), OSError("x"), TimeoutError()):
        assert "gaierror" not in _error(exc) and "Error" not in _error(exc)


def test_23_same_crm_id_is_the_first_reason(web, namesakes, session):  # noqa: F811
    a = session.get(Candidate, namesakes.candidate_a)
    b = session.get(Candidate, namesakes.candidate_b)
    assert "тот же ID в CRM" not in web.get(f"/duplicates/{namesakes.id}").text
    b.external_id = a.external_id
    session.commit()
    text = web.get(f"/duplicates/{namesakes.id}").text
    assert "Почему похожи: тот же ID в CRM, то же ФИО" in text


def test_25_vacancies_list_counts_failures(web, session):
    v = Vacancy(title="Технолог", description="технолог литья")
    c = Candidate(full_name="Громов Илья", raw_text="Технолог")
    session.add_all([v, c])
    session.commit()
    session.add(Match(vacancy_id=v.id, candidate_id=c.id, status="failed", error="сбой"))
    session.commit()
    text = web.get("/vacancies").text
    assert "Не удалось оценить 1" in text and "Оценено 0" not in text
    assert re.search(r"\d+\xa0\w{3} \d{4}, \d\d:\d\d", text)  # дата и время создания
