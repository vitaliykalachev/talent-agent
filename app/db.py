"""SQLite в TA_DATA_DIR/app.db (по умолчанию data/app.db), миграции alembic при старте."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
SessionLocal = sessionmaker(expire_on_commit=False)
data_dir: Path = Path(os.environ.get("TA_DATA_DIR", "data"))
engine: Engine | None = None


def _lower(value):
    return value.lower() if isinstance(value, str) else value


def _on_connect(dbapi_conn, _record):
    # SQLite умеет lower() только для латиницы; для поиска по-русски регистрируем свою.
    dbapi_conn.create_function("pylower", 1, _lower, deterministic=True)
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def configure(directory: Path | str | None = None) -> Engine:
    """Создаёт папку данных, подключает движок и накатывает миграции."""
    global engine, data_dir
    data_dir = Path(directory or os.environ.get("TA_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{data_dir / 'app.db'}"
    if engine is not None:
        engine.dispose()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(engine, "connect", _on_connect)
    SessionLocal.configure(bind=engine)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return engine
