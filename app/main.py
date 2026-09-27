"""Точка входа: FastAPI-приложение, воркер фоновых задач стартует вместе с ним."""

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db, embed, jobs
from app.web.routes import router
from app.web.vacancies import router as vacancies_router

STATIC = Path(__file__).parent / "static"


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.configure(data_dir)
        jobs.stopping.clear()
        # Модель поиска и матрица грузятся один раз, в фоне: экраны открываются сразу.
        warm = asyncio.create_task(asyncio.to_thread(embed.warm_up))
        task = asyncio.create_task(jobs.worker())
        yield
        jobs.stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(Exception):
            await warm

    app = FastAPI(title="Кадровый агент", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(router)
    app.include_router(vacancies_router)
    return app


def run() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("TA_HOST", "127.0.0.1"),
        port=int(os.environ.get("TA_PORT", "8000")),
    )
