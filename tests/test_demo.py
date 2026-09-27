import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, db, demo, morning
from app.demo import generate
from app.main import create_app
from app.models import Candidate, Duplicate, Embedding, Match, NightRun, Vacancy
from app.parse import CandidateProfile, to_parsed


def test_demo_set_imports_as_300_candidates_with_duplicates_and_stale(tmp_path, do_import, session):
    table, resumes = generate(tmp_path / "demo")
    assert len(list(resumes.glob("*.docx"))) > 0 and len(list(resumes.glob("*.txt"))) > 0
    job = do_import(table, [resumes])
    assert job.status == "done" and job.error is None

    def count(*where):
        return session.scalar(select(func.count(Candidate.id)).where(*where))

    active = Candidate.duplicate_of.is_(None)
    assert count(active) == 303  # 300 человек и 3 записи «похоже на дубль»
    assert count() == 318  # 15 точных дублей (5 %) и 3 возможных
    status = dict(
        session.execute(select(Duplicate.status, func.count()).group_by(Duplicate.status)).all()
    )
    assert status["merged"] == 15  # общий телефон или почта — склеены сами
    assert status["open"] >= 3  # то же ФИО, город и дата рождения, контакты разные
    assert count(active, Candidate.stale.is_(True)) == 60  # 20 % старше полутора лет
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
    assert parsed == 303  # демо к показу: у каждой записи есть записанный разбор
    with TestClient(create_app(tmp_path / "demo")) as web:
        for url in ("/", "/morning", "/vacancies", f"/vacancies/{v.id}/results"):
            text = web.get(url).text.lower()
            for word in ("не получилось", "не удалось оценить", "есть проблемы"):
                assert word not in text, (url, word)
        queue = web.get("/duplicates").text
        assert "Похоже на дубль: " in queue and "Похоже на дубль: 0" not in queue


YEARS_RE = re.compile(r"(?<![\d.,])(\d{1,2})(?:[.,]\d)?(?:\s*-\s*|\s+)(?:летн\w*|лет|года?)\b")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")
# Названия продуктов и принятые сокращения, которые и по-русски пишут латиницей
PRODUCTS = {"ERP", "CRM", "WMS", "CAD", "IT", "B2B", "FMCG", "DevOps", "Data", "Science", "TPM"}


@pytest.fixture
def recorded_profiles(tmp_path, do_import, session):
    """Демо-база и то, что из записанного ответа модели сделает разбор: (кандидат, parsed).
    Каждой активной записи демо есть записанный разбор — демо не показывает сбоев."""
    table, resumes = generate(tmp_path / "demo", today=demo.RECORDED_ON)
    do_import(table, [resumes])
    answers = {
        a["match"]: a["response"]
        for a in json.loads((demo.RECORDED / "parse.json").read_text("utf-8"))
    }
    found = []
    for c in session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))):
        response = answers[f"ID: {c.external_id}\n"]
        found.append((c, to_parsed(CandidateProfile.model_validate(response), c)))
    return found


def test_demo_summary_years_match_dates(recorded_profiles):
    """«Кратко»: число лет не дальше 2 от стажа, который код считает по датам мест работы."""
    for c, p in recorded_profiles:
        for m in YEARS_RE.finditer(p["summary"] or ""):
            assert abs(int(m.group(1)) - p["total_years"]) <= 2, (c.external_id, p["summary"])


def test_demo_summary_has_no_latin_except_products(recorded_profiles):
    for c, p in recorded_profiles:
        text = c.raw_text.lower()
        for word in LATIN_RE.findall(p["summary"] or ""):
            assert word in PRODUCTS or word.lower() in text, (c.external_id, word, p["summary"])


def test_key_file_not_copied_into_image():
    """COPY . . в Dockerfile не должен класть .env с ключом в образ."""
    from pathlib import Path

    ignored = (Path(__file__).parent.parent / ".dockerignore").read_text().split()
    assert ".env" in ignored
