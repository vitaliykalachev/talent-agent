"""«Удалить демо-данные» / «Очистить базу» в «Настройках»: база пустеет, настройки остаются.

Удаляются кандидаты, отпечатки, вакансии, оценки, исправления «Неверно», дубли, задачи,
ночные прогоны и загрузки вместе с файлами в папке данных; кэши поиска в памяти
сбрасываются. Таблица settings (ключ, адрес сервиса, расписание, почта) не трогается.
Задача, которая шла в этот момент, теряет свою запись и останавливается на первом же
сохранении (`jobs.run_job` это переживает).
"""

import shutil

from sqlalchemy import delete

from app import config, db, embed, search
from app.models import (
    Candidate,
    Duplicate,
    Embedding,
    Feedback,
    ImportBatch,
    Job,
    Match,
    NightRun,
    Vacancy,
)

TABLES = (Match, Feedback, Embedding, Duplicate, Candidate, Vacancy, Job, NightRun, ImportBatch)
FOLDERS = ("uploads", "source", "incoming")  # файлы загрузок и исходники демо в папке данных


def clear_base() -> None:
    with db.SessionLocal() as s:
        for table in TABLES:
            s.execute(delete(table))
        s.commit()
    for name in FOLDERS:
        shutil.rmtree(db.data_dir / name, ignore_errors=True)
    embed.reset()
    search.reset()
    config.save({"demo_cleared": "1"})
