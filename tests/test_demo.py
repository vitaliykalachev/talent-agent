from sqlalchemy import func, select

from app.demo import generate
from app.models import Candidate, Duplicate


def test_demo_set_imports_as_300_candidates_with_duplicates_and_stale(tmp_path, do_import, session):
    table, resumes = generate(tmp_path / "demo")
    assert len(list(resumes.glob("*.docx"))) > 0 and len(list(resumes.glob("*.txt"))) > 0
    job = do_import(table, [resumes])
    assert job.status == "done" and job.error is None

    def count(*where):
        return session.scalar(select(func.count(Candidate.id)).where(*where))

    active = Candidate.duplicate_of.is_(None)
    assert count(active) == 300
    assert count() == 315  # 5 % намеренных дублей
    assert session.scalar(select(func.count(Duplicate.id))) == 15
    assert count(active, Candidate.stale.is_(True)) == 60  # 20 % старше полутора лет
    assert count(active, Candidate.external_id.is_(None)) == 0  # все файлы связаны со строками


def test_key_file_not_copied_into_image():
    """COPY . . в Dockerfile не должен класть .env с ключом в образ."""
    from pathlib import Path

    ignored = (Path(__file__).parent.parent / ".dockerignore").read_text().split()
    assert ".env" in ignored
