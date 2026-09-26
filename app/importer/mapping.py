"""Догадка «какая колонка что значит»: сначала по названию, потом по содержимому."""

import re
from datetime import date
from statistics import median

from app.importer.normalize import EMAIL_RE, looks_like_name, normalize_phone, parse_date

FIELDS = {
    "": "не использовать",
    "external_id": "ID в CRM",
    "full_name": "ФИО",
    "last_name": "Фамилия",
    "first_name": "Имя",
    "middle_name": "Отчество",
    "phone": "Телефон",
    "email": "Почта",
    "city": "Город",
    "position": "Должность",
    "company": "Компания",
    "birth": "Дата или год рождения",
    "resume_date": "Дата резюме",
    "resume_text": "Текст резюме",
    "resume_file": "Имя файла резюме",
}

# Порядок важен: более узкие правила раньше общих («имя кандидата» — это ФИО, а не имя).
NAME_RULES = [
    ("birth", ["дата рождения", "год рождения", "др", "birth", "birthday"]),
    ("resume_date", ["дата обновления", "обновлено", "дата резюме", "дата изменения", "updated"]),
    ("resume_file", ["файл", "file"]),
    ("resume_text", ["текст резюме", "резюме", "опыт работы", "resume", "cv"]),
    ("full_name", ["фио", "ф и о", "имя кандидата", "кандидат", "full name"]),
    ("last_name", ["фамилия", "last name", "surname"]),
    ("middle_name", ["отчество", "middle name"]),
    ("first_name", ["имя", "first name"]),
    ("phone", ["телефон", "тел", "мобильный", "моб", "phone", "mobile"]),
    ("email", ["email", "e mail", "почта", "эл почта", "mail"]),
    ("external_id", ["id", "ид", "код", "номер", "no"]),
    ("city", ["город", "населенный пункт", "city", "место жительства"]),
    ("position", ["должность", "позиция", "position", "title"]),
    ("company", ["компания", "работодатель", "место работы", "организация", "company"]),
]


def _clean(header: str) -> str:
    text = str(header or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _by_name(header: str) -> str:
    text = _clean(header)
    for field, patterns in NAME_RULES:
        if any(re.search(rf"\b{re.escape(p)}\b", text) for p in patterns):
            return field
    return ""


def _share(values: list, check) -> float:
    return sum(1 for v in values if check(v)) / len(values) if values else 0.0


def _by_content(values: list) -> str:
    values = [v for v in values if str(v or "").strip()]
    if not values:
        return ""
    if _share(values, lambda v: EMAIL_RE.fullmatch(str(v).strip())) >= 0.6:
        return "email"
    if _share(values, lambda v: normalize_phone(v)) >= 0.6:
        return "phone"
    dates = [parse_date(v) for v in values]
    if _share(dates, bool) >= 0.6:
        years = median(d.year for d in dates if d)
        return "birth" if years < date.today().year - 16 else "resume_date"
    if _share(values, looks_like_name) >= 0.6:
        return "full_name"
    if median(len(str(v)) for v in values) > 200:
        return "resume_text"
    return ""


def guess_mapping(headers: list[str], rows: list[list]) -> list[str]:
    """Возвращает поле для каждой колонки; пустая строка — колонку не использовать."""
    fields = [_by_name(h) for h in headers]
    taken = {f for f in fields if f}
    sample = rows[:50]
    for i, field in enumerate(fields):
        if field:
            continue
        guess = _by_content([r[i] if i < len(r) else None for r in sample])
        if guess and guess not in taken:
            fields[i] = guess
            taken.add(guess)
    return fields
