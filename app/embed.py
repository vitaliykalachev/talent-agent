"""Смысловые отпечатки кандидатов: локальная модель, векторы в таблице embeddings.

Модель по умолчанию — sergeyzh/BERTA (MIT, 128 млн параметров, ruMTEB 69,3 против
58,3 у multilingual-e5-base); сменить — настройка `embed_model` или EMBED_MODEL.
Вес кэшируется в data/models/ (TA_MODELS_DIR). Вектор строится не по сырому тексту,
а по разобранному: summary, должности, навыки, город.

Матрица всех векторов держится в памяти (десятки тысяч × 768 float32 — сотни
мегабайт максимум), при добавлении обновляется; поиск — полный перебор.
"""

import logging
import os
import threading
import time
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
RETRY_AFTER = 30  # секунд: упавшую загрузку модели экраны запускают снова не чаще
RETRY_THREAD = "загрузка модели поиска"

log = logging.getLogger(__name__)
_lock = threading.Lock()  # матрица _index
# Веса грузятся под своим замком: при первом запуске после установки это минуты, и всё это
# время матрица и ready() отвечают сразу, а не ждут загрузку
_loading = threading.Lock()
_retry = threading.Lock()  # две вкладки не запускают две повторные загрузки
# Сколько загрузок подряд упало (нет весов, нет сети), когда и почему — для экранов
_failed: dict = {"count": 0, "at": 0.0, "reason": ""}
# На Mac torch считает на MPS, а он не выдерживает двух encode из разных потоков сразу
# (Segmentation fault в MetalShaderLibrary): поиск и фоновые задачи строят векторы по очереди
_encoding = threading.Lock()
_models: dict = {}
_index: dict = {"key": None, "ids": np.zeros(0, dtype=np.int64), "matrix": np.zeros((0, 0))}


def model_name() -> str:
    return config.get("embed_model")


def models_dir() -> Path:
    return Path(os.environ.get("TA_MODELS_DIR") or db.ROOT / "data" / "models")


def _model(name: str):
    with _loading:
        if name not in _models:
            folder = str(models_dir())
            try:
                from sentence_transformers import SentenceTransformer

                try:  # вес уже скачан — без обращений к Hugging Face (−10 с на старте)
                    _models[name] = SentenceTransformer(
                        name, cache_folder=folder, local_files_only=True
                    )
                except OSError:
                    _models[name] = SentenceTransformer(name, cache_folder=folder)
            except Exception as exc:
                _failed.update(count=_failed["count"] + 1, at=time.monotonic(), reason=_why(exc))
                raise
            _failed.update(count=0, reason="")
        return _models[name]


def _why(exc: Exception) -> str:
    """Причина сбоя загрузки коротко и по-русски: текст исключения длинный и английский."""
    if isinstance(exc, MemoryError):
        return "не хватило памяти"
    if isinstance(exc, OSError):  # весов нет на диске, а Hugging Face недоступен
        return "файлы модели не скачались"
    return "модель не запустилась на этом компьютере"


def ready() -> bool:
    """Модель поиска уже в памяти: запрос по смыслу не будет ждать загрузку весов.
    Без замка — его держит сама загрузка."""
    return model_name() in _models


def usable() -> bool:
    """Подбору по смыслу есть смысл ждать модель: она в памяти или её загрузка ещё не
    падала. После сбоя поиск и подбор кандидатов для оценки идут по словам, пока
    повторная загрузка не удастся."""
    return ready() or not _failed["count"]


def broken() -> str:
    """Загрузка упала дважды подряд: честная строка для экранов, иначе пусто."""
    if _failed["count"] < 2 or ready():
        return ""
    return (
        f"Поиск по смыслу не загрузился: {_failed['reason']}, "
        "разбор и оценка работают, поиск — по словам"
    )


def _try_load() -> None:
    try:
        _model(model_name())
    except Exception:
        log.exception("Модель поиска не загрузилась")


def ensure() -> None:
    """Экран, которому нужна модель: её нет в памяти, она не грузится, а прошлая загрузка
    упала — запускаем загрузку в фоне заново, не чаще раза в RETRY_AFTER секунд."""
    with _retry:
        if ready() or _loading.locked() or not _failed["count"]:
            return
        if time.monotonic() - _failed["at"] < RETRY_AFTER:
            return
        _failed["at"] = time.monotonic()
        threading.Thread(target=_try_load, name=RETRY_THREAD, daemon=True).start()


def encode(texts: list[str], kind: str) -> np.ndarray:
    """kind: «query» — запрос рекрутера, «passage» — карточка кандидата."""
    assert kind in ("query", "passage")
    model = _model(model_name())
    if kind in (model.prompts or {}):
        prompt = {"prompt_name": kind}
    else:
        prompt = {"prompt": FALLBACK_PREFIXES[kind]}
    with _encoding:
        vectors = model.encode(texts, **prompt, normalize_embeddings=True, batch_size=BATCH)
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


def reset() -> None:
    """База очищена: матрица пустая, следующий поиск перечитает её из БД."""
    with _lock:
        _index.update(key=None, ids=np.zeros(0, dtype=np.int64), matrix=np.zeros((0, 0)))


def _add(ids: list[int], vectors: np.ndarray) -> None:
    old_ids, old_matrix = index()
    with _lock:
        keep = ~np.isin(old_ids, ids)
        if old_matrix.size:
            vectors = np.vstack([old_matrix[keep], vectors])
        _index["ids"] = np.concatenate([old_ids[keep], np.array(ids, dtype=np.int64)])
        _index["matrix"] = vectors


def warm_up() -> None:
    """При старте: матрица и модель, если в базе уже есть векторы. Сбой загрузки — в
    журнал; экраны запустят её снова (ensure)."""
    ids, _ = index()
    if len(ids):
        _try_load()


def run_embed(job_id: int) -> None:
    """Задача отпечатков. Сбой локальной модели — не ответ сервиса ИИ: в задаче остаётся
    «Отпечатки не построены: <причина>», подробности — в журнале."""
    try:
        _embed(job_id)
    except Exception as exc:
        log.exception("Отпечатки не построены")
        with db.SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:  # базу очистили, пока задача шла
                return
            job.status, job.error = "failed", f"Отпечатки не построены: {_why(exc)}"
            job.finished_at = datetime.now()
            session.commit()


def _embed(job_id: int) -> None:
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
