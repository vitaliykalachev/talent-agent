"""Таблицы БД по разделу 5 плана. Часть таблиц заполняется на следующих этапах."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str | None] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    full_name: Mapped[str | None] = mapped_column(String(255), index=True)
    phones: Mapped[list] = mapped_column(JSON, default=list)
    emails: Mapped[list] = mapped_column(JSON, default=list)
    links: Mapped[list] = mapped_column(JSON, default=list)
    city: Mapped[str | None] = mapped_column(String(128), index=True)
    birth_year: Mapped[int | None] = mapped_column(Integer)
    resume_date: Mapped[date | None] = mapped_column(Date)
    parsed: Mapped[dict | None] = mapped_column(JSON)
    parse_status: Mapped[str] = mapped_column(String(16), default="new")  # new|parsed|failed
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(Integer)  # партия импорта, создавшая запись
    # Телефоны и почта из тела резюме (не из шапки): могут быть чужими, в автосклейке не участвуют.
    body_contacts: Mapped[list] = mapped_column(JSON, default=list)
    parse_error: Mapped[str | None] = mapped_column(Text)


class Embedding(Base):
    __tablename__ = "embeddings"

    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Vacancy(Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    hard_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    top_n: Mapped[int] = mapped_column(Integer, default=40)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # «Портрет кандидата»: [{id, name, kind: must|nice|avoid, weight, source: ai|user,
    # flag: vague|discriminatory|None, suggestion}]; каждая правка поднимает версию.
    requirements: Mapped[list] = mapped_column(JSON, default=list)
    requirements_version: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)  # начало последней оценки
    notice_seen: Mapped[bool] = mapped_column(Boolean, default=False)  # «Понятно» нажато


class Match(Base):
    __tablename__ = "matches"

    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|failed
    error: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer)  # считает код, не модель
    prev_score: Mapped[int | None] = mapped_column(Integer)
    change_reason: Mapped[str | None] = mapped_column(
        String(16)
    )  # new|resume_updated|vacancy_changed
    # [{requirement_id, name, kind, weight, verdict, evidence_lines, note}]
    checks: Mapped[list] = mapped_column(JSON, default=list)
    # ответ модели как есть, до проверки строк кодом — для набора проверки eval/
    raw_checks: Mapped[list | None] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON, default=list)  # производное от checks
    concerns: Mapped[list] = mapped_column(JSON, default=list)  # [{text, evidence_lines}]
    questions: Mapped[list] = mapped_column(JSON, default=list)
    fit_summary: Mapped[str | None] = mapped_column(Text)
    requirements_version: Mapped[int] = mapped_column(Integer, default=1)
    resume_hash: Mapped[str | None] = mapped_column(String(40))  # резюме изменилось — оценить снова
    rank: Mapped[int | None] = mapped_column(Integer)  # место в поиске при последнем прогоне
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    model: Mapped[str | None] = mapped_column(String(128))
    decision: Mapped[str | None] = mapped_column(String(16))  # invite|maybe|reject
    decision_reason: Mapped[str | None] = mapped_column(String(64))


class Feedback(Base):
    """Отметки «Неверно» у доводов; из них собирается блок «Агент запомнил»."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"), index=True)
    target: Mapped[str] = mapped_column(String(16))  # reason|concern|field
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))  # not_in_resume|misread|irrelevant|other
    scope: Mapped[str] = mapped_column(String(16), default="vacancy")  # vacancy|all
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Duplicate(Base):
    __tablename__ = "duplicates"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_a: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    candidate_b: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|merged|rejected
    # Слияние обратимо: что было у основной записи до него и какие дубли переехали к ней.
    snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)
    postponed_at: Mapped[datetime | None] = mapped_column(DateTime)  # «Отложить» — в конец


class NightRun(Base):
    """Ночной прогон: когда был запланирован, когда шёл, итог для «Утра» и письма."""

    __tablename__ = "night_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime)  # None — «Запустить сейчас»
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # queued | running | done | failed | missed (компьютер спал дольше запаса)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # import|parse|embed|evaluate|report
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mapping: Mapped[list] = mapped_column(JSON, default=list)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_ok: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
