"""Как показывать данные на экране: числа, сроки, деньги, статусы, подсветка исходника."""

import time
from collections import Counter

from markupsafe import Markup, escape
from sqlalchemy import select

from app.anonymize import quote_span
from app.models import Candidate, ImportBatch, Job

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
NBSP = " "
PARSE_STATUS = {"new": "Ждёт разбора", "parsed": "Разобрано", "failed": "Не удалось разобрать"}


def num(n) -> str:
    return f"{int(n):,}".replace(",", NBSP)


def short_date(d) -> str:
    if not d:
        return "—"
    return f"{d.day}{NBSP}{MONTHS[d.month - 1]} {d.year}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def duration(low: float, high: float, after_verb: bool = False) -> str:
    """«3–4 часа», «10–20 минут», «меньше минуты»; after_verb — после «займёт»:
    «займёт 1 минуту», а не «1 минута»."""
    if high < 60:
        return "меньше минуты"
    if high < 3600:
        lo, hi = max(1, round(low / 60)), max(1, round(high / 60))
        unit = plural(hi, "минуту" if after_verb else "минута", "минуты", "минут")
    else:
        lo, hi = max(1, round(low / 3600)), max(1, round(high / 3600))
        unit = plural(hi, "час", "часа", "часов")
    return f"{lo} {unit}" if lo == hi else f"{lo}–{hi} {unit}"


def money(value: float) -> str:
    if value < 1:
        return f"{value:.2f}".replace(".", ",")
    if value < 10:
        return f"{value:.1f}".replace(".", ",")
    return num(round(value))


def rub_range(low: float, high: float) -> str:
    return f"{money(low)}–{money(high)}{NBSP}₽"


def job_status(job: Job) -> str:
    if job.status == "done":
        return "Готово, есть проблемы" if job.error else "Готово"
    return {
        "queued": "В очереди",
        "running": "Идёт",
        "paused": "Приостановлено",
        "failed": "Не получилось",
    }.get(job.status, job.status)


def problems(job: Job) -> list[tuple[str, str]]:
    """Строки ошибок задачи: (что, почему)."""
    lines = (job.error or "").splitlines()
    return [tuple(line.rsplit(": ", 1)) if ": " in line else (line, "") for line in lines]


def problem_summary(job: Job) -> str:
    """«312 не получилось — 210 сканов без текста, 80 пустых файлов»."""
    items = problems(job)
    reasons = Counter(reason for _, reason in items)
    parts = ", ".join(f"{num(n)} — {reason}" for reason, n in reasons.most_common())
    return f"{num(len(items))} не получилось: {parts}"


def remaining(job: Job) -> str:
    """Остаток по скорости за последние 10–15 минут; первые две минуты — «считаем»."""
    ticks = [t for t in job.payload.get("ticks", []) if time.time() - t[0] <= 15 * 60]
    if len(ticks) < 2 or ticks[-1][0] - ticks[0][0] < 120 or ticks[-1][1] <= ticks[0][1]:
        return "Считаем, сколько займёт…"
    rate = (ticks[-1][1] - ticks[0][1]) / (ticks[-1][0] - ticks[0][0])
    left = (job.total - job.progress) / rate
    return f"Осталось {duration(left * 0.85, left * 1.2)}"


def batch_label(b: ImportBatch) -> str:
    """«Загрузка 26 сен, 14:05 · crm_export.xlsx»."""
    when = b.created_at
    return (
        f"Загрузка {when.day}{NBSP}{MONTHS[when.month - 1]}, {when:%H:%M} · "
        f"{b.file_name or 'файлы резюме'}"
    )


def batch_labels(s, ids) -> dict[int, str]:
    rows = s.scalars(select(ImportBatch).where(ImportBatch.id.in_([i for i in ids if i])))
    return {b.id: batch_label(b) for b in rows}


def stale_label(c: Candidate) -> str:
    if c.resume_date:
        return f"Нужно проверить: резюме от {short_date(c.resume_date)}"
    return "Нужно проверить: дата резюме не указана"


def line_span(text: str, lines: list[int]) -> tuple[int, int]:
    """Строки [от, до] (с единицы) → смещения символов в тексте."""
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    return starts[lines[0] - 1], starts[lines[1]] - 1


def stale_note(c: Candidate) -> str:
    """Пометка устаревшего резюме в результатах по вакансии (раздел 7 плана)."""
    if c.resume_date:
        return f"Резюме от {short_date(c.resume_date)} — старше полутора лет, уточните на созвоне"
    return "Дата резюме не указана — уточните на созвоне, актуально ли оно"


def marked_source(c: Candidate, extra: list[tuple[str, list[int]]] = ()) -> tuple[Markup, set[str]]:
    """Исходник с метками <mark id> на строках и цитатах, на которых основаны поля слева;
    `extra` — ещё строки для подсветки (довод оценки по ссылке «Показать в резюме»)."""
    parsed = c.parsed or {}
    spans = []
    ranges = [(f"q-p{i}", p.get("source_lines")) for i, p in enumerate(parsed.get("positions", []))]
    for anchor, lines in [*ranges, *extra]:
        if lines:
            spans.append((*line_span(c.raw_text, lines), anchor))
    quote = parsed.get("summary_quote")
    if quote and (span := quote_span(quote, c.raw_text)):
        spans.append((*span, "q-s"))
    spans.sort()
    out, pos, anchors = [], 0, set()
    for start, stop, anchor in spans:
        if start < pos:
            continue
        out += [escape(c.raw_text[pos:start]), Markup(f'<mark id="{anchor}">')]
        out += [escape(c.raw_text[start:stop]), Markup("</mark>")]
        pos, anchors = stop, anchors | {anchor}
    out.append(escape(c.raw_text[pos:]))
    return Markup("").join(out), anchors
