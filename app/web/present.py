"""Как показывать данные на экране: числа, сроки, деньги, статусы, подсветка исходника."""

import time
from collections import Counter

from markupsafe import Markup, escape

from app.anonymize import quote_span
from app.models import Candidate, Job

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


def duration(low: float, high: float) -> str:
    """«3–4 часа», «10–20 минут», «меньше минуты»."""
    if high < 60:
        return "меньше минуты"
    if high < 3600:
        lo, hi = max(1, round(low / 60)), max(1, round(high / 60))
        unit = plural(hi, "минута", "минуты", "минут")
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


def stale_label(c: Candidate) -> str:
    if c.resume_date:
        return f"Нужно проверить: резюме от {short_date(c.resume_date)}"
    return "Нужно проверить: дата резюме не указана"


def marked_source(c: Candidate) -> tuple[Markup, set[str]]:
    """Исходник с метками <mark id> на цитатах, на которых основаны поля слева."""
    parsed = c.parsed or {}
    quotes = [(f"q-p{i}", p.get("quote")) for i, p in enumerate(parsed.get("positions", []))]
    quotes.append(("q-s", parsed.get("summary_quote")))
    spans = []
    for anchor, quote in quotes:
        if quote and (span := quote_span(quote, c.raw_text)):
            spans.append((*span, anchor))
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
