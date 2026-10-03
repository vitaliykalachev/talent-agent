import os

# Тесты не читают рабочий .env и не ходят в сеть: провайдер и ключ задаются в самих тестах.
# markitdown при импорте сам ищет .env вверх по папкам (load_dotenv) и находит рабочий,
# поэтому он импортируется до очистки окружения.
import markitdown  # noqa: E402, F401

os.environ["TA_ENV_FILE"] = os.devnull
for _name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_PARSE", "LLM_MODEL_EVAL"):
    os.environ.pop(_name, None)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, jobs
from app.importer.pipeline import new_batch, start_import
from app.jobs import run_pending
from app.main import create_app
from app.models import Candidate, Job


@pytest.fixture(autouse=True)
def testclient_is_this_computer(monkeypatch):
    """TestClient ходит на http://testserver: для тестов это имя «этого компьютера»,
    иначе защита от чужих сайтов (routes.foreign) отклонит любой POST."""
    from app.web import routes

    monkeypatch.setattr(routes, "LOCAL_HOSTS", {*routes.LOCAL_HOSTS, "testserver"})


@pytest.fixture(autouse=True)
def worker_not_stopped():
    """Выход из TestClient останавливает воркер (jobs.stopping); следующий тест должен
    начинать с работающим run_pending, в каком бы порядке ни шли тесты."""
    jobs.stopping.clear()


@pytest.fixture
def session(tmp_path):
    db.configure(tmp_path / "data")
    with db.SessionLocal() as s:
        yield s


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as c:
        yield c
    jobs.stopping.clear()  # остановка приложения не должна глушить run_pending в других тестах


@pytest.fixture
def do_import(session):
    """Загружает файлы через обычный конвейер и прогоняет задачу до конца."""

    def _run(table=None, docs=(), mapping=None) -> Job:
        batch = new_batch(session, table, list(docs))
        job = start_import(session, batch, mapping or batch.mapping)
        run_pending()
        session.expire_all()
        return session.get(Job, job.id)

    return _run


@pytest.fixture
def active(session):
    """Кандидаты без пометки «дубль» в порядке создания."""

    def _active() -> list[Candidate]:
        q = select(Candidate).where(Candidate.duplicate_of.is_(None)).order_by(Candidate.id)
        return list(session.scalars(q))

    return _active


@pytest.fixture(scope="session")
def demo_base(tmp_path_factory):
    """Демо-база из 300 кандидатов, разобранная на записанных ответах, с отпечатками и
    готовой демо-вакансией.

    Общая на весь прогон и только для чтения; вызов подключает её и возвращает папку.
    """
    from app import config
    from app.demo import generate
    from app.parse import start_parse, waiting_ids

    root = tmp_path_factory.mktemp("demo")
    db.configure(root / "data")
    table, resumes = generate(root / "source")
    with db.SessionLocal() as s:
        start_import(s, batch := new_batch(s, table, [resumes]), batch.mapping)
    run_pending()
    config.save({"llm_provider": "mock", "llm_fixtures": str(root / "source" / "llm")})
    with db.SessionLocal() as s:
        start_parse(s, waiting_ids(s))
    run_pending()
    from app import demo_vacancy, night

    demo_vacancy.create(root / "source" / "llm")  # вакансия, которую оценит ночной прогон
    night.enqueue()  # как в make demo: ночь оценивает вакансию и оставляет «Утро»
    run_pending()

    def connect():
        db.configure(root / "data")
        return root / "data"

    return connect
