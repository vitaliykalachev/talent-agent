"""Точка входа: FastAPI-приложение, воркер фоновых задач стартует вместе с ним."""

import asyncio
import contextlib
import hashlib
import os
import socket
import sys
import threading
import time
import urllib.request
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
from app.web.routes import foreign, router
from app.web.vacancies import router as vacancies_router

STATIC = Path(__file__).parent / "static"


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.configure(data_dir)
        jobs.stopping.clear()
        # Модель поиска и матрица грузятся в фоне: экраны открываются сразу. Сбой загрузки
        # пишется в журнал, а экраны запускают её снова (embed.ensure).
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

    @app.middleware("http")
    async def own_pages_only(request, call_next):
        """Только имена этого компьютера, а всё, что меняет данные или тратит деньги
        (POST, PUT, DELETE), — только со страниц программы: остальное получает 403
        (routes.foreign)."""
        if refused := foreign(request):
            return refused
        return await call_next(request)

    # id больше, чем помещается в базу (/candidates/<23 цифры>), — такой записи нет
    app.add_exception_handler(
        OverflowError, lambda _r, _e: PlainTextResponse("Такой записи нет.", status_code=404)
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(router)
    app.include_router(vacancies_router)
    app.include_router(duplicates_router)
    app.include_router(morning_router)
    app.add_api_route("/health", health, include_in_schema=False)
    return app


PORTS = 11  # 8000 занят — пробуем 8001–8010
WAIT = 60  # секунд ждём, пока ответит уже запущенная копия
INSTANCE = "X-Agent-Instance"
_held = None  # блокировка папки данных живёт, пока жив процесс
_instances: dict[str, str] = {}


def instance(data_dir: Path | str) -> str:
    """Признак экземпляра: хэш пути к папке данных и её inode, запомненный при первом
    вызове. Установщик переносит прежнюю папку в .old и кладёт на её место новую: агент,
    работающий из .old, отвечает старым признаком и за новую копию себя не выдаёт."""
    path = os.path.normcase(str(Path(data_dir).resolve()))
    if path not in _instances:
        key = f"{path}:{os.stat(path).st_ino}"
        _instances[path] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return _instances[path]


def health() -> PlainTextResponse:
    return PlainTextResponse("ok", headers={INSTANCE: instance(db.data_dir)})


def hold(data_dir: Path):
    """Блокировка agent.lock в папке данных; None — её держит другая копия агента.
    Система снимает блокировку сама, когда процесс завершается, даже аварийно."""
    handle = open(data_dir / "agent.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def running(me: str, first: int, timeout: float) -> str | None:
    """Адрес уже запущенной копии с той же папкой данных, если она ответит за timeout."""
    deadline = time.monotonic() + timeout
    while True:
        for port in range(first, first + PORTS):
            url = f"http://127.0.0.1:{port}/"
            try:
                with urllib.request.urlopen(url + "health", timeout=0.5) as r:
                    if r.headers.get(INSTANCE) == me:
                        return url
            except OSError:
                continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


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
    global _held
    host = os.environ.get("TA_HOST", "127.0.0.1")
    wanted = int(os.environ.get("TA_PORT", "8000"))
    open_browser = os.environ.get("TA_OPEN_BROWSER") == "1"
    data_dir = Path(os.environ.get("TA_DATA_DIR") or "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    _held = hold(data_dir)
    me = instance(data_dir)  # признак запоминается сейчас, пока папка на своём месте
    if _held is None:  # второй двойной клик: не второй сервер на той же базе, а окно первого
        print("Агент уже запускается, ждём ответа…", flush=True)
        url = running(me, wanted, WAIT)
        if url is None:
            sys.exit("Агент уже запущен, но не отвечает. Закройте его окно и запустите снова.")
        print(f"Агент уже запущен: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    port = free_port(host, wanted)
    if port is None:
        sys.exit(f"Порты {wanted}–{wanted + PORTS - 1} заняты. Закройте другие копии агента.")
    os.environ.setdefault("TA_PUBLIC_URL", f"http://127.0.0.1:{port}")  # ссылки в письме
    threading.Thread(target=announce, args=(port, open_browser), daemon=True).start()
    # Портативный режим: клик в окно консоли Windows включает выделение и блокирует
    # вывод, а с ним и сервер; строка на каждый запрос делала это вероятным.
    uvicorn.run(create_app(), host=host, port=port, access_log=not open_browser)


if __name__ == "__main__":
    run()
