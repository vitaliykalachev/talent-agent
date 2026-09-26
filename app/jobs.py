"""Фоновые задачи: таблица jobs и воркер в том же процессе.

Воркер берёт задачи со статусом queued и running по порядку. Задача в статусе
running после перезапуска приложения — это прерванная задача, её обработчик
продолжает с сохранённого прогресса.
"""

import asyncio
import logging
import threading
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db
from app.models import Job

log = logging.getLogger(__name__)
stopping = threading.Event()  # выставляется при остановке приложения


def _handlers():
    from app.embed import run_embed
    from app.evaluate import run_evaluate
    from app.importer.pipeline import run_import
    from app.night import run_night
    from app.parse import run_parse

    return {
        "import": run_import,
        "parse": run_parse,
        "embed": run_embed,
        "evaluate": run_evaluate,
        "night": run_night,
    }


def enqueue(session: Session, kind: str, payload: dict) -> Job:
    job = Job(kind=kind, payload=payload, status="queued")
    session.add(job)
    session.flush()
    return job


def next_job_id() -> int | None:
    with db.SessionLocal() as session:
        q = select(Job.id).where(Job.status.in_(("queued", "running"))).order_by(Job.id)
        return session.scalar(q.limit(1))


def run_job(job_id: int) -> None:
    try:
        with db.SessionLocal() as session:
            kind = session.get(Job, job_id).kind
        _handlers()[kind](job_id)
    except Exception as exc:
        log.exception("задача %s упала", job_id)
        with db.SessionLocal() as session:
            job = session.get(Job, job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = datetime.now()
            session.commit()


def run_pending() -> None:
    """Выполняет все незавершённые задачи по очереди (воркер, демо, тесты)."""
    while not stopping.is_set() and (job_id := next_job_id()) is not None:
        run_job(job_id)


async def worker(poll_seconds: float = 1.0) -> None:
    while True:
        await asyncio.to_thread(run_pending)
        await asyncio.sleep(poll_seconds)
