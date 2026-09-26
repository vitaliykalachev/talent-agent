import os

# Тесты не читают рабочий .env и не ходят в сеть: провайдер и ключ задаются в самих тестах.
os.environ["TA_ENV_FILE"] = os.devnull
for _name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_PARSE", "LLM_MODEL_EVAL"):
    os.environ.pop(_name, None)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.importer.pipeline import new_batch, start_import
from app.jobs import run_pending
from app.main import create_app
from app.models import Candidate, Job


@pytest.fixture
def session(tmp_path):
    db.configure(tmp_path / "data")
    with db.SessionLocal() as s:
        yield s


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as c:
        yield c


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
