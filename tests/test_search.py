import numpy as np
import pytest
from sqlalchemy import select

from app import db, embed
from app.demo import RARE_TERM, SHOWCASE_DUTIES
from app.models import Candidate, Embedding
from app.search import Filters, hybrid, lexical, rrf, search

pytestmark = pytest.mark.slow  # демо-база с моделью поиска


@pytest.fixture
def demo(demo_base):
    demo_base()
    with db.SessionLocal() as s:
        yield s


def showcase(s) -> Candidate:
    return s.scalar(select(Candidate).where(Candidate.raw_text.contains(SHOWCASE_DUTIES[0][:40])))


def test_query_and_passage_vectors_differ(demo):
    """Сторож префиксов: без prompt_name BERTA подставила бы префикс классификации."""
    text = "Начальник цеха, запуск производства"
    q, p = embed.encode([text], "query")[0], embed.encode([text], "passage")[0]
    assert not np.allclose(q, p)
    assert abs(np.linalg.norm(q) - 1) < 1e-3  # векторы нормализованы — близость = произведение
    with pytest.raises(AssertionError):
        embed.encode([text], "document")


def test_every_parsed_candidate_has_vector(demo):
    parsed = demo.scalar(select(Candidate.id).where(Candidate.parse_status == "parsed").limit(1))
    assert parsed is not None
    count = len(list(demo.scalars(select(Embedding.candidate_id))))
    assert count == 300
    ids, matrix = embed.index()
    assert len(ids) == 300 and matrix.shape == (300, 768)


def test_launch_query_finds_casting_section(demo):
    """Критерий приёмки: «запуск цеха с нуля» → «организовал участок литья».

    Вектор ставит его в первую десятку. В гибриде BM25 поднимает выше тех, кто
    дословно «запустил новый цех», поэтому витрина уходит на вторую страницу, но
    остаётся среди 40, которые идут в оценку под вакансию."""
    target = showcase(demo)
    assert "организовал участок литья" in target.raw_text
    assert "запуск" not in target.raw_text.lower() and "цех" not in target.raw_text.lower()
    by_vector, _ = hybrid(demo, "запуск цеха с нуля", "vector")
    assert target.id in by_vector[:10]
    pool, _ = hybrid(demo, "запуск цеха с нуля")
    assert target.id in pool[:40]
    r = search(demo, "запуск цеха с нуля", Filters())
    assert r.mode == "meaning"
    labels = {near for _, near, _ in r.hits}
    assert labels <= {"очень близко", "близко", "возможно"}
    assert all(frag for _, _, frag in r.hits)


def test_filters_cut_and_report_how_many_they_remove(demo):
    target = showcase(demo)
    everyone = search(demo, "запуск цеха с нуля", Filters())
    other_city = "Москва" if target.city != "Москва" else "Казань"
    r = search(demo, "запуск цеха с нуля", Filters(city=other_city))
    assert target.id not in [c.id for c, _, _ in r.hits]
    assert all((c.city or c.parsed.get("city")) == other_city for c, _, _ in r.hits)
    assert r.removed["city"] == everyone.total - r.total > 0

    years = target.parsed["total_years"]
    assert target.id not in [
        c.id for c, _, _ in search(demo, "литьё", Filters(min_years=years + 1)).hits
    ]
    fresh = search(demo, "бухгалтер", Filters(fresh="stale"))
    assert fresh.hits and all(c.stale for c, _, _ in fresh.hits)
    # зарплата в демо не указана — потолок никого не убирает
    assert (
        search(demo, "бухгалтер", Filters(max_salary=1)).total
        == search(demo, "бухгалтер", Filters()).total
    )


def test_empty_result_suggests_which_condition_to_drop(demo):
    r = search(demo, "бухгалтер МСФО", Filters(city="Казань", min_years=99))
    assert r.total == 0
    key, n = r.relax
    assert key == "min_years" and n > 0


def test_contact_like_query_searches_contacts(demo):
    c = demo.scalar(select(Candidate).where(Candidate.duplicate_of.is_(None)).limit(1))
    for query in (c.phones[0], c.emails[0].upper(), c.full_name):
        r = search(demo, query, Filters())
        assert r.mode == "contacts" and c.id in [h[0].id for h in r.hits], query


def test_rrf_merges_by_places():
    """RRF с k = 60: второй в обоих списках обходит первого в одном."""
    assert rrf([1, 2, 3], [4, 2, 5])[0] == 2
    assert rrf([], [7, 8]) == [7, 8]


def test_rare_term_on_second_page_is_found(demo):
    """Марка станка стоит на второй странице резюме и в поисковую карточку не входит:
    находит её BM25 по полному тексту, и гибрид поднимает кандидата в первую десятку."""
    target = demo.scalar(select(Candidate).where(Candidate.raw_text.contains(RARE_TERM)))
    assert target.raw_text.index(RARE_TERM) > 3000  # вторая страница
    assert RARE_TERM not in embed.passage(target)
    assert lexical(demo, RARE_TERM)[0] == target.id
    pool, _ = hybrid(demo, RARE_TERM)
    assert target.id in pool[:10]
    r = search(demo, RARE_TERM, Filters())
    assert target.id in [c.id for c, _, _ in r.hits[:10]]
    hit = next(frag for c, _, frag in r.hits if c.id == target.id)
    assert "<mark>Hermle</mark>" in hit  # фрагмент — тот абзац, где найден термин
