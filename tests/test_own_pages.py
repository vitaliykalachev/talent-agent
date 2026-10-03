"""Всё, что меняет данные или тратит деньги, — только со страниц программы.

Повторное ревью PR #3: ревьюер живьём увёл сохранённый ключ. Чужой сайт в том же
браузере прислал «Проверить подключение» со своим адресом сервиса, а ключ подставился
из настроек. Теперь любой POST, PUT или DELETE с чужим Origin, Sec-Fetch-Site или Host
(DNS-rebinding) получает 403 и ничего не делает. Чужое имя в Host получает 403 и на
GET: иначе чужой домен, указывающий на 127.0.0.1, читал бы страницы с резюме.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.main import create_app
from app.models import NightRun
from app.web import routes

REFUSED = "Запрос пришёл не из программы"
OWN = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def web(session):
    return TestClient(create_app())  # без lifespan: базу подключил session


def test_check_from_other_site_does_not_send_saved_key(web, monkeypatch):
    config.save({"llm_api_key": "ключ-клиента"})
    asked = []
    monkeypatch.setattr(routes, "get_llm", lambda *args, **kw: asked.append(args))
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    form = {"llm_provider": "anthropic", "llm_base_url": "https://evil.example"}
    r = web.post("/settings/check", data=form, headers=evil)
    assert r.status_code == 403 and REFUSED in r.text and asked == []


def test_settings_over_rebinding_host_refused(web):
    """Чужое имя, которое указывает на 127.0.0.1: браузер считает запрос «своим» для
    того сайта и шлёт его имя в Host."""
    r = web.post("/settings", data={"parse_limit": "5"}, headers={"Host": "evil.example:8000"})
    assert r.status_code == 403 and config.get("parse_limit") == "200"


def test_night_run_from_other_site_refused(web, session):
    r = web.post("/night/run", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert session.scalar(select(func.count(NightRun.id))) == 0


def test_own_forms_and_htmx_pass(web, session):
    htmx = {**OWN, "HX-Request": "true"}
    r = web.post("/settings", data={"parse_limit": "150"}, headers=htmx)
    assert r.status_code == 200 and config.get("parse_limit") == "150"
    assert web.post("/night/run", follow_redirects=False).status_code == 303  # форма без заголовков
    assert session.scalar(select(func.count(NightRun.id))) == 1
    for host in ("127.0.0.1:8000", "localhost:8001", "[::1]:8000"):
        assert web.post("/settings", data={}, headers={"Host": host}).status_code == 200, host


def test_get_over_rebinding_host_refused_mail_links_pass(web):
    """Ревью PR #4: GET с чужим именем в Host — тоже 403, иначе чужой домен, указывающий
    на 127.0.0.1, прочитал бы страницы с резюме. Ссылки из письма ведут на 127.0.0.1 и
    проходят, даже когда браузер помечает переход «с другого сайта» (почта в браузере)."""
    for path in ("/settings", "/candidates", "/static/app.css"):
        r = web.get(path, headers={"Host": "evil.example:8000"})
        assert r.status_code == 403 and REFUSED in r.text, path
    mail = {"Host": "127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"}
    for path in ("/morning", "/health", "/static/app.css"):
        assert web.get(path, headers=mail).status_code == 200, path
