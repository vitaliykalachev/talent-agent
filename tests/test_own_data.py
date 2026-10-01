"""Версия 0.3, «свои данные»: очистка базы с сохранением настроек и лимит разбора."""

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
    "parse_limit": "150",
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


def test_parse_limit_per_run(web, session, do_import):
    config.save({"llm_provider": "mock", "llm_fixtures": str(FIXTURES), "parse_limit": "2"})
    do_import(None, [RESUMES])
    confirm = web.get("/parse", params={"scope": "all"}).text
    assert "Агент разберёт 2 из 5 резюме" in confirm and "следующим запуском" in confirm
    web.post("/parse", data={"scope": "all"})
    job = session.scalar(select(Job).where(Job.kind == "parse"))
    assert len(job.payload["ids"]) == 2
    run_pending()
    assert "Разобрано 2 из 5, остальные — следующим запуском." in web.get("/").text
    assert len(parse.waiting_ids(session)) == 3


def test_parse_limit_zero_means_all(web, session, do_import):
    config.save({"llm_provider": "mock", "llm_fixtures": str(FIXTURES), "parse_limit": "0"})
    do_import(None, [RESUMES])
    confirm = web.get("/parse", params={"scope": "all"}).text
    assert "Агент разберёт 5 резюме" in confirm and "следующим запуском" not in confirm
    web.post("/parse", data={"scope": "all"})
    run_pending()
    assert parse.waiting_ids(session) == []
    assert "Разобрано 5\xa0резюме" in web.get("/").text


def test_parse_limit_setting_validated(web):
    assert config.get("parse_limit") == "200"
    for bad in ("двести", "²", "99999999999999999999", "1000001", "-5", "1.5"):
        page = web.post("/settings", data={"parse_limit": bad})
        assert "Лимит разбора — целое число" in page.text, bad
        assert config.get("parse_limit") == "200", bad
    assert web.get("/parse", params={"scope": "all"}).status_code == 200
    web.post("/settings", data={"parse_limit": "50"})
    assert config.get("parse_limit") == "50"
    web.post("/settings", data={"parse_limit": ""})  # пустое поле — значение по умолчанию
    assert config.get("parse_limit") == "200"


def test_old_base_upgraded_on_start(tmp_path):
    """Обновление переносит базу прежней версии: при старте она догоняет схему."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    folder = tmp_path / "old"
    folder.mkdir()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{folder / 'app.db'}")
    command.upgrade(cfg, "0001")
    with TestClient(main.create_app(folder)) as c:
        assert c.get("/settings").status_code == 200
    with create_engine(f"sqlite:///{folder / 'app.db'}").connect() as conn:
        assert conn.scalar(text("select version_num from alembic_version")) == "0004"


def test_clear_resets_word_index(session, do_import):
    """Очистка → столько же записей с теми же id, но другим текстом: поиск по словам
    ищет по новому тексту, а не по индексу прежней базы."""
    from app import search

    def load(word: str) -> None:
        for i in range(3):
            session.add(Candidate(full_name=f"Иванов{i} Иван", raw_text=f"{word} стаж {i} лет"))
        session.commit()

    load("Сварщик")
    assert len(search.lexical(session, "сварщик")) == 3
    wipe.clear_base()
    load("Кровельщик")
    assert search.lexical(session, "сварщик") == []
    assert len(search.lexical(session, "кровельщик")) == 3


@pytest.mark.parametrize("path", ["/settings/clear", "/settings/key-delete"])
def test_irreversible_buttons_refuse_other_sites(web, session, do_import, path):
    config.save({"llm_api_key": "ключ-клиента"})
    do_import(None, [RESUMES])
    data = {"confirm": "1"}
    for headers in (
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"},
    ):
        r = web.post(path, data=data, headers=headers)
        assert r.status_code == 403 and "Запрос пришёл не из программы" in r.text, headers
    assert counts(session)["Candidate"] == 5 and config.get("llm_api_key") == "ключ-клиента"
    own = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert web.post(path, data=data, headers=own).status_code == 200
    if path == "/settings/clear":
        assert counts(session)["Candidate"] == 0
    else:
        assert config.get("llm_api_key") == ""


def test_form_without_origin_still_works(web, session, do_import):
    do_import(None, [RESUMES])
    assert web.post("/settings/clear", data={"confirm": "1"}).status_code == 200
    assert counts(session)["Candidate"] == 0


def test_demo_with_own_data_is_not_called_fictional(web, session, do_import, tmp_path):
    """После обновления с 0.2 в демо-базе бывают и свои кандидаты: кнопка не называет
    всё вымышленным."""
    config.save({"llm_provider": "mock", "llm_fixtures": config.RECORDED})
    do_import(None, [RESUMES])  # демо
    do_import(resume_xlsx(tmp_path / "crm.xlsx"))  # своя выгрузка
    page = web.get("/settings").text
    assert "Удалить все данные: 11\xa0кандидатов, среди них вымышленные из демо" in page
    assert "Удалить вымышленные данные" not in page
