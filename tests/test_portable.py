"""Портативная сборка для Windows: запуск без терминала, свободный порт, демо без ключа."""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, db, demo, main
from app.jobs import run_pending
from app.llm import MockLLM, get_llm
from app.models import Vacancy
from app.parse import start_parse, waiting_ids
from app.web import present, routes

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def web(session):
    return TestClient(main.create_app())  # без lifespan: базу подключил session


def listening() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def test_health(web):
    r = web.get("/health")
    assert r.status_code == 200 and r.text == "ok"
    assert r.headers[main.INSTANCE] == main.instance(db.data_dir)


def test_busy_port_skipped():
    with listening() as busy:
        port = busy.getsockname()[1]
        chosen = main.free_port("127.0.0.1", port)
    assert chosen is not None and port < chosen < port + main.PORTS


def test_announce_prints_address_and_opens_browser(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(main.webbrowser, "open", opened.append)
    with listening() as server:
        port = server.getsockname()[1]
        main.announce(port, open_browser=True, timeout=5)
        main.announce(port, open_browser=False, timeout=5)
    url = f"http://127.0.0.1:{port}/"
    assert opened == [url]
    out = capsys.readouterr().out
    assert out.count(url) == 2 and "не закрывайте это окно, пока работаете" in out


@pytest.fixture
def own_data(tmp_path, monkeypatch):
    """Своя папка данных для run(): блокировка не должна остаться на папке другого теста."""
    monkeypatch.setenv("TA_DATA_DIR", str(tmp_path / "run"))
    yield tmp_path / "run"
    if main._held:
        main._held.close()
        main._held = None


def test_run_takes_next_port_everywhere(session, monkeypatch, own_data):
    """8000 занят: сервер, браузер и ссылки в письме — на один и тот же следующий порт."""
    started, announced = {}, []
    monkeypatch.setattr(main.uvicorn, "run", lambda app, host, port: started.update(port=port))
    monkeypatch.setattr(main, "announce", lambda port, _open: announced.append(port))
    monkeypatch.delenv("TA_PUBLIC_URL", raising=False)
    with listening() as busy:
        wanted = busy.getsockname()[1]
        monkeypatch.setenv("TA_PORT", str(wanted))
        main.run()
    time.sleep(0.1)  # announce идёт в потоке
    port = started["port"]
    assert port != wanted and announced == [port]
    assert config.get("public_url") == f"http://127.0.0.1:{port}"


def test_recorded_marker_resolves_and_old_path_still_works(session):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED})
    assert config.get("llm_fixtures") == str(ROOT / "app" / "demo_data" / "llm")
    llm = get_llm("parse")
    assert isinstance(llm, MockLLM) and llm.fixtures  # ответы нашлись
    config.save({"llm_fixtures": str(demo.RECORDED)})  # база, собранная до маркера
    assert config.get("llm_fixtures") == str(demo.RECORDED) and get_llm("parse").fixtures


def test_demo_prefills_real_service_from_env_example():
    example = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text("utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert demo.REAL_SERVICE == {
        "llm_base_url": example["LLM_BASE_URL"],
        "llm_model_parse": example["LLM_MODEL_PARSE"],
        "llm_model_eval": example["LLM_MODEL_EVAL"],
    }


def test_key_first_other_ai_fields_under_additional(web):
    page = web.get("/settings").text
    form = page[page.index('action="/settings">') : page.index("</form>")]
    assert form.index('name="llm_api_key"') < form.index("<details")
    assert "Ключ вам передадут отдельно" in form and "Проверить подключение" in form
    extra = form[form.index("<details") :]
    assert extra.startswith('<details class="admin">\n  <summary>Дополнительно')
    for name in ("llm_provider", "llm_base_url", "llm_model_parse", "llm_model_eval"):
        assert f'name="{name}"' in extra


def test_key_on_recorded_answers_switches_to_real_service_and_back(web):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED, **demo.REAL_SERVICE})
    text = web.post("/settings", data={"llm_provider": "mock", "llm_api_key": "ключ"}).text
    assert "Записанные ответы отключены, теперь работает настоящий сервис ИИ" in text
    assert config.get("llm_provider") == "anthropic" and config.get("llm_api_key") == "ключ"
    assert config.get("llm_base_url") == "https://api.claudehub.fun"
    web.post("/settings", data={"night_time": "03:00"})  # без нового ключа сервис не меняется
    assert config.get("llm_provider") == "anthropic"
    text = web.post("/settings/key-delete").text
    assert "снова работает на записанных ответах" in text
    assert config.get("llm_provider") == "mock" and not config.get("llm_api_key")


def test_key_delete_without_recorded_answers_keeps_provider(web):
    config.save({"llm_provider": "anthropic", "llm_api_key": "ключ"})
    assert "Ключ удалён." in web.post("/settings/key-delete").text
    assert config.get("llm_provider") == "anthropic"


def test_python_m_app_main_serves_with_utf8_mode(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        TA_PORT=str(port),
        TA_DATA_DIR=str(tmp_path / "data"),
        TA_MODELS_DIR=str(tmp_path / "models"),
        HF_HUB_OFFLINE="1",
    )
    env.pop("TA_OPEN_BROWSER", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    assert r.read() == b"ok"
                time.sleep(1)  # строка с адресом печатается из потока, опрос раз в 0,2 с
                break
            except OSError:
                assert proc.poll() is None and time.monotonic() < deadline
                time.sleep(0.3)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=30)[0].decode("utf-8")
    assert f"http://127.0.0.1:{port}/ — не закрывайте это окно" in out


DEMO_BANNER = "Демо-режим: данные вымышленные, ИИ работает на записанных ответах"
DEMO_MISS = "В демо-версии ИИ отключён — новые вакансии и резюме не разбираются"


def test_demo_banner_on_home_until_key(web):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED, **demo.REAL_SERVICE})
    assert DEMO_BANNER in web.get("/").text
    web.post("/settings", data={"llm_provider": "mock", "llm_api_key": "ключ"})
    assert DEMO_BANNER not in web.get("/").text


def test_own_vacancy_in_demo_gets_hint_not_error(web, session):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED})
    ready = Vacancy(title="Начальник литейного производства", description="…", hard_filters={})
    session.add(ready)
    session.commit()
    r = web.post(
        "/vacancies/new", data={"description": "Нужен бариста в кофейню у метро, опыт год."}
    )
    assert r.status_code == 200 and DEMO_MISS in r.text and "Traceback" not in r.text
    assert f'<a href="/vacancies/{ready.id}">«Начальник литейного производства»</a>' in r.text
    assert session.scalar(select(func.count(Vacancy.id))) == 1  # чужая вакансия не осталась


def test_own_resume_in_demo_lists_same_hint(web, session, do_import):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED})
    do_import(None, [ROOT / "tests" / "fixtures" / "resumes"])
    job = start_parse(session, waiting_ids(session))
    run_pending()
    session.refresh(job)
    assert job.status == "done" and all(r == DEMO_MISS for _, r in present.problems(job))
    page = web.get(f"/jobs/{job.id}/problems")
    assert page.status_code == 200 and page.text.count(DEMO_MISS) >= 2


def test_check_with_key_in_demo_asks_real_service(web, monkeypatch):
    """Ключ вставлен, а в скрытом «Сервисе» остался mock: проверяется настоящий сервис."""
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED, **demo.REAL_SERVICE})
    asked = []

    class Stub:
        retries = 1

        def complete_structured(self, *_):
            return None

    def fake(purpose, overrides, timeout):
        asked.append(overrides["llm_provider"])
        return Stub()

    monkeypatch.setattr(routes, "get_llm", fake)
    form = {"llm_provider": "mock", "llm_api_key": "ключ", **demo.REAL_SERVICE}
    text = web.post("/settings/check", data=form).text
    assert asked == ["anthropic"] and "Подключение работает" in text
    assert config.get("llm_provider") == "mock"  # проверка ничего не сохраняет


def start(data: Path, port: int) -> subprocess.Popen:
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        TA_PORT=str(port),
        TA_DATA_DIR=str(data),
        TA_MODELS_DIR=str(data / "models"),
        HF_HUB_OFFLINE="1",
    )
    env.pop("TA_OPEN_BROWSER", None)
    return subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_second_launch_on_same_data_opens_first(tmp_path):
    """Второй двойной клик, пока первый работает: второго сервера на той же базе нет,
    вторая копия называет адрес первой и выходит."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    first = start(tmp_path / "data", port)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).close()
                break
            except OSError:
                assert first.poll() is None and time.monotonic() < deadline
                time.sleep(0.3)
        second = start(tmp_path / "data", port)
        out = second.communicate(timeout=60)[0].decode("utf-8")
        assert second.returncode == 0, out
        assert f"Агент уже запущен: http://127.0.0.1:{port}/" in out
        assert "Uvicorn running" not in out and first.poll() is None
    finally:
        first.terminate()
        first.communicate(timeout=30)


def test_locked_data_without_answer_stops_with_hint(tmp_path, monkeypatch):
    """Копия держит папку, но ещё не отвечает и так и не ответила: сервер не стартует."""
    data = tmp_path / "data"
    data.mkdir()
    other = main.hold(data)
    assert other is not None and main.hold(data) is None
    monkeypatch.setenv("TA_DATA_DIR", str(data))
    monkeypatch.setattr(main, "WAIT", 0)
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **k: pytest.fail("второй сервер"))
    with pytest.raises(SystemExit, match="Агент уже запущен, но не отвечает"):
        main.run()
    other.close()
    free = main.hold(data)  # закрылась копия — папка свободна
    assert free is not None
    free.close()
