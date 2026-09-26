"""Смысловые отпечатки кандидатов: локальная модель, векторы в таблице embeddings.

Модель по умолчанию — sergeyzh/BERTA (MIT, 128 млн параметров, ruMTEB 69,3 против
58,3 у multilingual-e5-base); сменить — настройка `embed_model` или EMBED_MODEL.
Вес кэшируется в data/models/ (TA_MODELS_DIR). Вектор строится не по сырому тексту,
а по разобранному: summary, должности, навыки, город.

Матрица всех векторов держится в памяти (десятки тысяч × 768 float32 — сотни
мегабайт максимум), при добавлении обновляется; поиск — полный перебор.
"""

import os
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select

from app import config, db
from app.jobs import stopping
from app.models import Candidate, Embedding, Job

# Префиксы задачи поиска. У BERTA они записаны в настройках модели как prompts
# «query» → «search_query: » и «passage» → «search_document: »
# (https://huggingface.co/sergeyzh/BERTA), а по умолчанию там стоит префикс
# классификации — поэтому encode() без явного prompt_name/prompt запрещён.
# У e5 настроек нет, префиксы из карточки https://huggingface.co/intfloat/multilingual-e5-base
FALLBACK_PREFIXES = {"query": "query: ", "passage": "passage: "}
BATCH = 64
RAW_CHARS = 1500  # ≈ 512 токенов окна модели — для резюме, которые не удалось разобрать

_lock = threading.Lock()
_models: dict = {}
_index: dict = {"key": None, "ids": np.zeros(0, dtype=np.int64), "matrix": np.zeros((0, 0))}


def model_name() -> str:
    return config.get("embed_model")


def models_dir() -> Path:
    return Path(os.environ.get("TA_MODELS_DIR") or db.ROOT / "data" / "models")


def _model(name: str):
    with _lock:
        if name not in _models:
            from sentence_transformers import SentenceTransformer

            folder = str(models_dir())
            try:  # вес уже скачан — без обращений к Hugging Face (−10 с на старте)
                _models[name] = SentenceTransformer(
                    name, cache_folder=folder, local_files_only=True
                )
            except OSError:
                _models[name] = SentenceTransformer(name, cache_folder=folder)
        return _models[name]


def encode(texts: list[str], kind: str) -> np.ndarray:
    """kind: «query» — запрос рекрутера, «passage» — карточка кандидата."""
    assert kind in ("query", "passage")
    model = _model(model_name())
    if kind in (model.prompts or {}):
        vectors = model.encode(texts, prompt_name=kind, normalize_embeddings=True, batch_size=BATCH)
    else:
        vectors = model.encode(
            texts, prompt=FALLBACK_PREFIXES[kind], normalize_embeddings=True, batch_size=BATCH
        )
    return np.asarray(vectors, dtype=np.float32)


def passage(c: Candidate) -> str:
    """Поисковая карточка из разобранного резюме: окно модели — 512 токенов, поэтому
    не сырой текст (он обрезался бы на первой странице), а главное из него.
    Резюме, которое разобрать не удалось, идёт началом сырого текста."""
    parsed = c.parsed or {}
    if c.parse_status != "parsed":
        return c.raw_text[:RAW_CHARS]
    jobs = "; ".join(
        ", ".join(filter(None, (p.get("title"), p.get("company"), p.get("industry"))))
        for p in parsed.get("positions", [])[:4]
    )
    parts = [
        f"Желаемая должность: {parsed['desired_position']}"
        if parsed.get("desired_position")
        else "",
        f"Опыт: {jobs}" if jobs else "",
        f"Навыки: {', '.join(parsed.get('skills') or [])}" if parsed.get("skills") else "",
        parsed.get("summary") or "",
        f"Город: {parsed['city']}" if parsed.get("city") else "",
    ]
    return "\n".join(p for p in parts if p)


def index() -> tuple[np.ndarray, np.ndarray]:
    """Матрица текущей базы; при смене базы или модели перечитывается из БД."""
    key = (str(db.data_dir), model_name())
    with _lock:
        if _index["key"] != key:
            with db.SessionLocal() as s:
                rows = s.execute(
                    select(Embedding.candidate_id, Embedding.vector).where(
                        Embedding.model == key[1]
                    )
                ).all()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            matrix = (
                np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                if rows
                else np.zeros((0, 0), dtype=np.float32)
            )
            _index.update(key=key, ids=ids, matrix=matrix)
        return _index["ids"], _index["matrix"]


def _add(ids: list[int], vectors: np.ndarray) -> None:
    old_ids, old_matrix = index()
    with _lock:
        keep = ~np.isin(old_ids, ids)
        if old_matrix.size:
            vectors = np.vstack([old_matrix[keep], vectors])
        _index["ids"] = np.concatenate([old_ids[keep], np.array(ids, dtype=np.int64)])
        _index["matrix"] = vectors


def nearest(query: str, k: int = 200) -> list[tuple[int, float]]:
    ids, matrix = index()
    if not len(ids):
        return []
    scores = matrix @ encode([query], "query")[0]
    top = np.argsort(-scores)[:k]
    return [(int(ids[i]), float(scores[i])) for i in top]


def warm_up() -> None:
    """При старте: матрица и модель, если в базе уже есть векторы."""
    ids, _ = index()
    if len(ids):
        _model(model_name())


def run_embed(job_id: int) -> None:
    name = model_name()
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        have = select(Embedding.candidate_id).where(Embedding.model == name)
        todo = list(
            session.scalars(
                select(Candidate.id).where(
                    Candidate.duplicate_of.is_(None),
                    Candidate.parse_status.in_(("parsed", "failed")),
                    Candidate.id.not_in(have),
                )
            )
        )
        job.status, job.total, job.progress = "running", job.progress + len(todo), job.progress
        session.commit()
        for i in range(0, len(todo), BATCH):
            session.refresh(job)
            if stopping.is_set() or job.status == "paused":
                return
            batch = [session.get(Candidate, cid) for cid in todo[i : i + BATCH]]
            vectors = encode([passage(c) for c in batch], "passage")
            for c, v in zip(batch, vectors, strict=True):
                session.merge(Embedding(candidate_id=c.id, model=name, vector=v.tobytes()))
            job.progress += len(batch)
            session.commit()
            _add([c.id for c in batch], vectors)
        job.status, job.finished_at = "done", datetime.now()
        session.commit()
