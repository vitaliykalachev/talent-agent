import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_parse import FIXTURES, five, parse_all, sent  # noqa: F401 — фикстуры

from app import config
from app.jobs import run_pending
from app.main import create_app
from app.models import Job
from app.parse import start_parse, waiting_ids

BANNED = ("провайдер", "токен", "эмбеддинг", "парсинг")


@pytest.fixture
def web(five):  # noqa: F811
    """Экраны без фонового воркера: задачи в тестах гоняет run_pending."""
    return TestClient(create_app())


def page(web, url, **kw) -> str:
    r = web.get(url, **kw)
    assert r.status_code == 200, url
    for word in BANNED:
        assert word not in r.text.lower(), (url, word)
    return r.text


@pytest.mark.parametrize("url", ["/parse", "/parse/preview", "/progress", "/settings"])
def test_new_screens_on_empty_base(client, url):
    assert client.get(url).status_code == 200


def test_trial_parse_offer_confirm_and_preview(web, five, session):  # noqa: F811
    home = page(web, "/")
    assert "Разобрать 5 для проверки" in home and "Начать разбор всей базы" in home
    assert re.search(r'data-stat="waiting">\s*5\s*<', home)

    confirm = page(web, "/parse", params={"scope": "trial"})
    assert "Разберём 5 резюме. Займёт меньше минуты, будет стоить примерно" in confirm
    assert "₽" in confirm and "оригиналы остаются у вас" in confirm
    assert "Показать, что уходит модели" in confirm

    preview = page(web, "/parse/preview")
    c = min(five.values(), key=lambda c: c.id)
    assert c.phones[0] not in preview and c.full_name.split()[0] not in preview
    assert "[ТЕЛЕФОН]" in preview or "[ПОЧТА]" in preview

    r = web.post("/parse", data={"scope": "trial"}, follow_redirects=False)
    assert r.status_code == 303
    run_pending()
    home = page(web, "/")
    assert re.search(r'data-stat="parsed">\s*5\s*<', home)
    assert "Разбор резюме: Готово" in page(web, "/progress")


def test_double_click_starts_one_parse(web, five, session, sent):  # noqa: F811
    """Два одновременных «Разобрать для проверки» — одна задача и по вызову на резюме."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        codes = list(
            pool.map(
                lambda _: (
                    web.post("/parse", data={"scope": "trial"}, follow_redirects=False).status_code
                ),
                range(2),
            )
        )
    assert codes == [303, 303]
    web.post("/parse", data={"scope": "all"}, follow_redirects=False)  # и «всей базы» следом
    assert len(list(session.scalars(select(Job).where(Job.kind == "parse")))) == 1
    run_pending()
    assert len(sent) == 5


def test_pause_and_resume_buttons(web, session):
    job = start_parse(session, waiting_ids(session))
    fragment = page(web, "/progress")
    assert "Разбираем резюме: 0 из 5" in fragment and "Приостановить" in fragment
    assert "Можно закрыть вкладку и работать дальше" in fragment
    assert (
        "Разбираем резюме — идёт" in fragment and "Считаем смысловые отпечатки — ждёт" in fragment
    )
    web.post(f"/jobs/{job.id}/pause")
    session.expire_all()
    assert session.get(Job, job.id).status == "paused"
    assert "Продолжить" in page(web, "/progress")
    web.post(f"/jobs/{job.id}/resume")
    session.expire_all()
    assert session.get(Job, job.id).status == "queued"


def test_problems_list_and_retry(web, five, session):  # noqa: F811
    five["sales.txt"].raw_text = ""
    session.commit()
    job = parse_all(session)
    fragment = page(web, "/progress")
    assert "Готово, есть проблемы" in fragment
    assert "1 не получилось: 1 — в резюме нет текста" in fragment
    problems = page(web, f"/jobs/{job.id}/problems")
    assert five["sales.txt"].full_name in problems and "в резюме нет текста" in problems
    retry = page(web, "/parse", params={"scope": "retry", "job": job.id})
    assert "Разберём 1 резюме" in retry


def test_candidate_card_shows_understanding_next_to_source(web, five, session):  # noqa: F811
    parse_all(session)
    prod = five["production.txt"]
    card = page(web, f"/candidates/{prod.id}")
    assert "Что понял агент" in card and "Резюме" in card
    assert "Директор по производству" in card and "Разобрано" in card
    assert 'href="#q-p0"' in card and '<mark id="q-p0">' in card  # поле ведёт к строке исходника
    assert "в резюме не сказано" in card  # языков нет
    assert "не готов к переезду" in page(web, f"/candidates/{five['logistics.txt'].id}")
    assert "проверьте" in page(web, f"/candidates/{five['logistics.txt'].id}")

    web.post(f"/candidates/{prod.id}/fix", data={"field": "city", "value": "Тольятти"})
    card = page(web, f"/candidates/{prod.id}")
    assert "Тольятти" in card and "исправлено вами" in card

    web.post(f"/candidates/{prod.id}/reparse")
    assert "Ждёт разбора" in page(web, f"/candidates/{prod.id}")


def test_candidates_search_by_meaning_with_chips(web, five, session):  # noqa: F811
    parse_all(session)
    text = page(web, "/candidates", params={"q": "литейное производство"})
    assert "Ищем по смыслу: «литейное производство»" in text
    assert re.search(r"очень близко|близко|возможно", text)
    assert "Директор по производству" in text and "АО «Литейный завод Поволжье»" in text
    assert not re.search(r"\b0[.,]\d{2,}\b", text)  # числовая близость не показывается

    text = page(web, "/candidates", params={"q": "литейное производство", "max_salary": "200000"})
    assert "зарплата до 200\u00a0000 ₽" in text and "убирает 1" in text
    assert "Литейный завод Поволжье" not in text

    text = page(web, "/candidates", params={"q": five["it.txt"].phones[0]})
    assert "Ищем по контактам" in text and five["it.txt"].full_name in text


def test_settings_save_key_hidden_and_check(web, tmp_path):
    r = web.post(
        "/settings",
        data={"llm_provider": "mock", "llm_api_key": "секрет-123", "llm_model_parse": "m1"},
    )
    assert "секрет-123" not in r.text and "Ключ задан" in r.text
    assert config.get("llm_api_key") == "секрет-123" and config.get("llm_model_parse") == "m1"
    web.post("/settings", data={"llm_api_key": ""})
    assert config.get("llm_api_key") == "секрет-123"  # пустое поле ключ не стирает

    assert "Подключение работает" in web.post("/settings/check").text
    (tmp_path / "auth.json").write_text(
        json.dumps({"match": "", "response": {"__error__": "auth"}})
    )
    config.save({"llm_fixtures": str(tmp_path)})
    text = web.post("/settings/check").text
    assert "Ключ доступа не подошёл. Проверьте, что скопировали его целиком." in text
    for word in BANNED:
        assert word not in page(web, "/settings").lower()
