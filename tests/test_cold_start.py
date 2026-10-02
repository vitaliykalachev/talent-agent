"""Холодный старт: модель поиска ещё грузится (первый запуск после установки — до двух
минут), а карточка вакансии, результат и поиск отвечают сразу и её не ждут."""

import sys
import threading
import time
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evaluate import base, mock, run, standard  # noqa: F401 — фикстуры

from app import embed
from app.main import create_app
from app.models import Candidate, Embedding

LOADING = "Поиск по смыслу ещё загружается, через минуту обновите страницу"
DIM = 4


@pytest.fixture
def slow_model(monkeypatch):
    """Модель поиска грузится, пока тест не отпустит `gate`, но не дольше 5 с: так веса
    BERTA грузятся при первом запуске после установки, пока macOS проверяет новые файлы."""
    gate, loading = threading.Event(), threading.Event()

    class Model:
        prompts = {"query": "search_query: ", "passage": "search_document: "}

        def __init__(self, *_args, **_kwargs):
            loading.set()
            gate.wait(5)

        def encode(self, texts, **_kwargs):
            return np.full((len(texts), DIM), 0.5, dtype=np.float32)

    fake = types.SimpleNamespace(SentenceTransformer=Model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(embed, "_models", {})  # настоящая модель медленных тестов не в счёт
    yield gate, loading
    gate.set()


def with_vectors(session) -> None:
    """Отпечатки у всех кандидатов: без модели по смыслу больше не искать."""
    for cid in session.scalars(select(Candidate.id)):
        vector = np.full(DIM, 0.5, dtype=np.float32).tobytes()
        session.add(Embedding(candidate_id=cid, model=embed.model_name(), vector=vector))
    session.commit()
    embed.reset()


def loaded() -> bool:
    deadline = time.monotonic() + 5
    while not embed.ready() and time.monotonic() < deadline:
        time.sleep(0.05)
    return embed.ready()


def timed(web, url, **kw):
    started = time.monotonic()
    r = web.get(url, **kw)
    assert r.status_code == 200, url
    return r.text, time.monotonic() - started


def test_vacancy_card_and_results_do_not_wait_for_model(session, base, mock, slow_model, tmp_path):  # noqa: F811
    gate, loading = slow_model
    v, p = base
    mock(standard(v, p))
    run(session, v)  # оценки сохранены, пока отпечатков ещё нет
    with_vectors(session)
    with TestClient(create_app(tmp_path / "data")) as web:
        try:
            assert loading.wait(5)  # при старте модель грузится в фоне
            card, card_s = timed(web, f"/vacancies/{v.id}")
            results, results_s = timed(web, f"/vacancies/{v.id}/results")
            assert not embed.ready()
        finally:
            gate.set()
        assert card_s < 2 and results_s < 2
        # карточка из сохранённого: портрет и ссылка на результат; запуск — после загрузки
        assert "Руководил цехом литья от 3 лет" in card and "Результат по вакансии" in card
        assert LOADING in card and 'hx-trigger="every 10s"' in card
        assert "Подходят" in results and "Громов Илья Сергеевич" in results
        assert loaded()
        card, _ = timed(web, f"/vacancies/{v.id}")
        assert LOADING not in card and "Все найденные кандидаты уже оценены" in card


def test_search_answers_by_words_while_model_loads(session, base, slow_model, tmp_path):  # noqa: F811
    gate, loading = slow_model
    with_vectors(session)
    query = {"q": "литьё под давлением, бережливое производство"}
    with TestClient(create_app(tmp_path / "data")) as web:
        try:
            assert loading.wait(5)
            text, seconds = timed(web, "/candidates", params=query)
            assert not embed.ready()
        finally:
            gate.set()
        assert seconds < 2
        assert LOADING in text and "Громов Илья Сергеевич" in text  # по словам, BM25
        assert "Близость" not in text  # близости по смыслу без модели нет
        assert loaded()
        text, _ = timed(web, "/candidates", params=query)
        assert LOADING not in text and "Агент ищет по смыслу" in text
