"""Версия 0.3, «свои данные»: очистка базы с сохранением настроек."""

from pathlib import Path

import numpy as np
import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, db, demo, embed, jobs, main, night, parse, wipe
from app.jobs import run_pending
from app.llm import DEMO_MISS
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

ROOT = Path(__file__).resolve().parent.parent
RESUMES = ROOT / "tests" / "fixtures" / "resumes"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
BANNER = f"Демо-режим: данные вымышленные. {DEMO_MISS}."
NEED_KEY = "Чтобы разбирать свои резюме, вставьте ключ в"
EMPTY = "База пуста. Загрузите выгрузку из CRM"
KEPT = {
    "llm_api_key": "ключ-клиента",
    "llm_base_url": "https://api.claudehub.fun",
    "night_time": "03:30",
    "night_days": "weekdays",
    "smtp_to": "hr@example.ru",
}


@pytest.fixture
def web(session):
    return TestClient(main.create_app())  # без lifespan: базу подключил session


def counts(session) -> dict:
    tables = (Candidate, Embedding, Vacancy, Match, Feedback, Duplicate, Job, NightRun, ImportBatch)
    session.expire_all()
    return {t.__name__: session.scalar(select(func.count()).select_from(t)) for t in tables}


def resume_xlsx(path: Path, rows: int = 6) -> Path:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ФИО", "Телефон", "Почта", "Город", "Должность", "Текст резюме"])
    for i in range(rows):
        sheet.append(
            [
                f"Сидоров{'аеиоуя'[i]}в Пётр Ильич",
                f"+7 912 000-00-{i:02d}",
                f"sidorov{i}@example.ru",
                "Екатеринбург",
                "Технолог литейного производства",
                f"Технолог литейного производства, стаж {i + 3} лет. "
                "Отливки из чугуна и стали, техпроцессы, контроль брака, работа с ОТК.",
            ]
        )
    book.save(path)
    return path


def full_base(session, do_import) -> None:
    """База со всем, что удаляет очистка: кандидаты, отпечаток, вакансия, оценка,
    исправление, дубль, задачи, ночной прогон, загрузка и файлы в папке данных."""
    do_import(None, [RESUMES])
    first, second = session.scalars(select(Candidate.id).limit(2)).all()
    vacancy = Vacancy(title="Технолог", description="…", hard_filters={})
    session.add(vacancy)
    session.flush()
    session.add_all(
        [
            Embedding(candidate_id=first, model="m", vector=b"\0" * 8),
            Match(vacancy_id=vacancy.id, candidate_id=first),
            Feedback(
                candidate_id=first, vacancy_id=vacancy.id, target="reason", text="т", kind="other"
            ),
            Duplicate(candidate_a=first, candidate_b=second, confidence=0.9),
            NightRun(status="done"),
        ]
    )
    session.commit()
    (db.data_dir / "source").mkdir()
    (db.data_dir / "source" / "crm.csv").write_text("демо")


def test_clear_asks_first_then_keeps_settings(web, session, do_import):
    config.save(KEPT)
    full_base(session, do_import)
    assert (db.data_dir / "uploads").exists()
    page = web.post("/settings/clear")
    assert page.status_code == 200 and "Да, удалить" in page.text
    assert counts(session)["Candidate"] == 5  # первый шаг ничего не удаляет
    r = web.post("/settings/clear", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert set(counts(session).values()) == {0}
    assert not (db.data_dir / "uploads").exists() and not (db.data_dir / "source").exists()
    assert {key: config.get(key) for key in KEPT} == KEPT


def test_demo_label_banner_and_key_line(web, session, do_import):
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED, **demo.REAL_SERVICE})
    do_import(None, [RESUMES])
    assert (
        "Удалить вымышленные данные (5\xa0кандидатов и демо-вакансия)" in web.get("/settings").text
    )
    assert BANNER in web.get("/").text
    web.post("/settings/clear", data={"confirm": "1"})
    home = web.get("/").text
    assert BANNER not in home and EMPTY in home and NEED_KEY in home
    assert "Очистить базу" in web.get("/settings").text
    assert config.is_demo()  # записанные ответы остались: без ключа подсказка, а не ошибка
    web.post("/settings", data={"llm_api_key": "ключ"})
    assert NEED_KEY not in web.get("/").text


def test_own_base_shows_clear_base(web):
    assert "Очистить базу" in web.get("/settings").text
    assert NEED_KEY not in web.get("/").text and BANNER not in web.get("/").text


def test_search_and_upload_work_after_clear(web, session, do_import, tmp_path):
    full_base(session, do_import)
    # матрица отпечатков в памяти, как после поиска по демо-базе
    embed._index.update(
        key=(str(db.data_dir), embed.model_name()),
        ids=np.array([1, 2], dtype=np.int64),
        matrix=np.ones((2, 4), dtype=np.float32),
    )
    wipe.clear_base()
    ids, matrix = embed.index()
    assert len(ids) == 0 and matrix.size == 0
    # пустая матрица — поиск по словам, модель поиска не нужна
    assert web.get("/candidates", params={"q": "технолог"}).status_code == 200
    table = resume_xlsx(tmp_path / "crm.xlsx")
    r = web.post("/upload", files={"table": ("crm.xlsx", table.read_bytes())})
    assert r.status_code == 200 and "Текст резюме" in r.text
    batch = session.scalar(select(ImportBatch))
    fields = {f"col_{i}": f for i, f in enumerate(batch.mapping)}
    assert batch.mapping[5] == "resume_text"
    web.post(f"/upload/{batch.id}/start", data=fields)
    run_pending()
    rows = list(session.scalars(select(Candidate)))
    assert len(rows) == 6 and all("Отливки из чугуна" in c.raw_text for c in rows)


def test_running_job_survives_clear(session, do_import, monkeypatch):
    """Разбор шёл, когда базу очистили: воркер не падает, задач и записей не остаётся."""
    config.save({"llm_provider": "mock", "llm_fixtures": str(FIXTURES)})
    do_import(None, [RESUMES])
    parse.start_parse(session, parse.waiting_ids(session))
    real = parse._ask

    def ask_then_clear(llm, text):
        wipe.clear_base()
        return real(llm, text)

    monkeypatch.setattr(parse, "_ask", ask_then_clear)
    run_pending()  # не бросает
    assert set(counts(session).values()) == {0}
    assert jobs.next_job_id() is None


def test_night_run_on_empty_base(session):
    wipe.clear_base()
    run = night.enqueue()
    run_pending()
    session.expire_all()
    run = session.get(NightRun, run.id)
    assert run.status == "done" and run.summary["checked_resumes"] == 0
