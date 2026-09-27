"""Как показывать данные на экране: числа, сроки, деньги, статусы, подсветка исходника."""

import re
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


def count(n: int, one: str, few: str, many: str) -> str:
    """Число со словом в нужной форме: «1 кандидат», «3 записи», «35 000 файлов»."""
    return f"{num(n)}{NBSP}{plural(int(n), one, few, many)}"


def short_name(full: str | None) -> str:
    """«Давыдов Андрей Михайлович» → «Давыдов А. М.»."""
    parts = (full or "").split()
    if not parts:
        return "Без имени"
    return " ".join([parts[0], *(f"{w[0]}." for w in parts[1:3])])


def years(value) -> str:
    """Стаж словами: «1 год», «3 года», «11,5 года», «20 лет»."""
    if value is None:
        return "—"
    text = f"{value:g}".replace(".", ",")
    if float(value) != int(value):
        return f"{text}{NBSP}года"
    return f"{text}{NBSP}{plural(int(value), 'год', 'года', 'лет')}"


def phone(value: str) -> str:
    """«+79120000000» → «+7 912 000-00-00»; другие страны — как есть."""
    if value and len(value) == 12 and value.startswith("+7"):
        d = value[2:]
        return f"+7{NBSP}{d[:3]}{NBSP}{d[3:6]}-{d[6:8]}-{d[8:]}"
    return value


def cut(text: str | None, limit: int) -> str:
    """Обрезка по слову с многоточием: не «техническому перев», а «техническому…»."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = text[: limit + 1].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return head.rstrip(" ,.;:—-") + "…"


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


def failed_all(job: Job) -> bool:
    """Задача дошла до конца, но ничего не получилось: каждый элемент — в списке проблем."""
    return job.status == "done" and bool(job.error) and len(problems(job)) >= job.total


def job_status(job: Job) -> str:
    if failed_all(job):
        return "Не получилось"
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


def reasons(job: Job) -> str:
    """Причины сбоев словами, частые первыми: «ответ модели не получен, пустой файл»."""
    found = Counter(reason for _, reason in problems(job) if reason)
    return ", ".join(reason for reason, _ in found.most_common())


def settings_hint(text: str | None) -> Markup:
    """Текст ошибки; если дело в ключе — со ссылкой «Открыть настройки»."""
    from app.llm import AUTH_MESSAGE

    out = escape(text or "")
    if AUTH_MESSAGE in (text or ""):
        out += Markup(' <a href="/settings">Открыть настройки</a>')
    return out


def import_done(job: Job) -> str:
    """Итог загрузки: «30 строк и 5 файлов» — что именно прочитали."""
    rows, files = job.payload.get("rows"), job.payload.get("files")
    if rows is None:
        return f"{num(job.progress)} из {num(job.total)}"
    ok_files = files - len(problems(job))
    parts = []
    if rows:
        parts.append(count(rows, "строка", "строки", "строк"))
    if files:
        parts.append(count(ok_files, "файл", "файла", "файлов"))
    return " и ".join(parts) or "файл пустой"


def network_errors(job: Job) -> bool:
    """Есть ли среди причин сетевые сбои: только тогда пишем, что агент их повторил."""
    from app.llm import BUSY_MESSAGE

    return any(reason == BUSY_MESSAGE for _, reason in problems(job))


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


RELOCATION_RE = re.compile(r"(?i)(?:пере|от)езд\w*|релокац\w*|командировк\w*")


def field_spans(text: str, parsed: dict) -> list[tuple[int, int, str]]:
    """Опора для навыков, города, переезда и языков: первое место в резюме, где стоит
    значение поля (для навыков и языков — первое из найденных)."""
    lower = text.lower()
    found = []
    for field in ("city", "skills", "languages"):
        values = parsed.get(field) or []
        for value in [values] if isinstance(values, str) else values:
            at = lower.find(str(value).lower()) if value else -1
            if at >= 0:
                found.append((at, at + len(value), f"q-{field}"))
                break
    if (parsed.get("relocation") or "unknown") != "unknown" and (m := RELOCATION_RE.search(text)):
        found.append((m.start(), m.end(), "q-relocation"))
    return found


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
    spans += field_spans(c.raw_text, parsed)
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
