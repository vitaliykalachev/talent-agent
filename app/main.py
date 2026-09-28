"""Точка входа: FastAPI-приложение, воркер фоновых задач стартует вместе с ним."""

import asyncio
import contextlib
import os
import socket
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import db, embed, jobs, schedule
from app.web.duplicates import router as duplicates_router
from app.web.morning import router as morning_router
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
        schedule.start()
        yield
        schedule.stop()
        jobs.stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(Exception):
            await warm

    app = FastAPI(title="Кадровый агент", lifespan=lifespan)
    # id больше, чем помещается в базу (/candidates/<23 цифры>), — такой записи нет
    app.add_exception_handler(
        OverflowError, lambda _r, _e: PlainTextResponse("Такой записи нет.", status_code=404)
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(router)
    app.include_router(vacancies_router)
    app.include_router(duplicates_router)
    app.include_router(morning_router)
    app.add_api_route("/health", lambda: PlainTextResponse("ok"), include_in_schema=False)
    return app


PORTS = 11  # 8000 занят — пробуем 8001–8010


def free_port(host: str, first: int) -> int | None:
    """Первый порт из first…first+10, на котором можно слушать; None — все заняты."""
    for port in range(first, first + PORTS):
        with socket.socket() as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    return None


def announce(port: int, open_browser: bool, timeout: float = 600) -> None:
    """Ждёт, пока сервер начнёт отвечать, пишет адрес в консоль и открывает браузер."""
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        return
    print(f"Кадровый агент работает: {url} — не закрывайте это окно, пока работаете.", flush=True)
    if open_browser:
        webbrowser.open(url)


def run() -> None:
    host = os.environ.get("TA_HOST", "127.0.0.1")
    wanted = int(os.environ.get("TA_PORT", "8000"))
    port = free_port(host, wanted)
    if port is None:
        sys.exit(f"Порты {wanted}–{wanted + PORTS - 1} заняты. Закройте другие копии агента.")
    os.environ.setdefault("TA_PUBLIC_URL", f"http://127.0.0.1:{port}")  # ссылки в письме
    threading.Thread(
        target=announce, args=(port, os.environ.get("TA_OPEN_BROWSER") == "1"), daemon=True
    ).start()
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    run()
