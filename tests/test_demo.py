import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, db, demo, morning
from app.demo import generate
from app.main import create_app
from app.models import Candidate, Duplicate, Embedding, Match, NightRun, Vacancy


def test_demo_set_imports_as_300_candidates_with_duplicates_and_stale(tmp_path, do_import, session):
    table, resumes = generate(tmp_path / "demo")
    assert len(list(resumes.glob("*.docx"))) > 0 and len(list(resumes.glob("*.txt"))) > 0
    job = do_import(table, [resumes])
    assert job.status == "done" and job.error is None

    def count(*where):
        return session.scalar(select(func.count(Candidate.id)).where(*where))

    active = Candidate.duplicate_of.is_(None)
    assert count(active) == 303  # 300 человек и 3 записи «похоже на дубль»
    assert count() == 315  # 5 % намеренных дублей: 12 точных и 3 возможных
    status = dict(
        session.execute(select(Duplicate.status, func.count()).group_by(Duplicate.status)).all()
    )
    assert status["merged"] == 12  # общий телефон или почта — склеены сами
    assert status["open"] >= 3  # то же ФИО, город и дата рождения, контакты разные
    assert 60 <= count(active, Candidate.stale.is_(True)) <= 63  # 20 % старше полутора лет
    assert count(active, Candidate.external_id.is_(None)) == 0  # все файлы связаны со строками


@pytest.mark.slow
def test_make_demo_works_without_key(tmp_path, monkeypatch):
    """`make demo` без LLM_API_KEY: записанные с живой модели ответы, разобранная база,
    отпечатки, вакансия с 40 оценками и один ночной прогон на «Утре»."""
    for name in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    started = time.monotonic()
    demo.build(tmp_path / "demo")
    assert time.monotonic() - started < 300  # «за несколько минут»
    with db.SessionLocal() as s:
        assert config.get("llm_provider") == "mock" and not config.get("llm_api_key")
        active = Candidate.duplicate_of.is_(None)
        parsed = s.scalar(
            select(func.count(Candidate.id)).where(active, Candidate.parse_status == "parsed")
        )
        assert parsed >= 300
        assert s.scalar(select(func.count(Embedding.candidate_id))) >= 300
        [run] = s.scalars(select(NightRun))
        assert run.status == "done"
        v = s.scalar(select(Vacancy))
        assert v.schedule_enabled
        rows = list(s.scalars(select(Match).where(Match.vacancy_id == v.id)))
        assert len(rows) == 40 and all(m.status == "ok" for m in rows)
        view = morning.view(s)
    first = view["sections"][0]
    assert first["title"] == "Новые кандидаты по вакансиям"
    assert first["rows"][0]["link"] == f"/vacancies/{v.id}/results"
    assert "подход" in first["rows"][0]["text"]
    with TestClient(create_app(tmp_path / "demo")) as web:
        for url in ("/", "/morning", "/vacancies", f"/vacancies/{v.id}/results", "/duplicates"):
            assert web.get(url).status_code == 200, url


def test_key_file_not_copied_into_image():
    """COPY . . в Dockerfile не должен класть .env с ключом в образ."""
    from pathlib import Path

    ignored = (Path(__file__).parent.parent / ".dockerignore").read_text().split()
    assert ".env" in ignored
