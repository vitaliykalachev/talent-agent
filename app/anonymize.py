"""Обезличивание текста перед отправкой в ИИ-модель (раздел 6 плана).

Вырезаются ФИО (natasha, PER), телефоны РФ и СНГ, почта, ссылки и ники, даты
рождения. Сверх распознавания из текста убираются ФИО, телефоны и почта самой
записи — даже если распознавание их пропустило (капс, опечатки, склонения).
В БД остаётся оригинал; модели уходит только результат `anonymize`.
"""

import re
from functools import lru_cache

from app.importer.normalize import EMAIL_RE, PHONE_RE

NAME, PHONE, EMAIL, LINK, BIRTH = "[ИМЯ]", "[ТЕЛЕФОН]", "[ПОЧТА]", "[ССЫЛКА]", "[ДАТА РОЖДЕНИЯ]"
PLACEHOLDER_RE = re.compile(r"\[(?:ИМЯ|ТЕЛЕФОН|ПОЧТА|ССЫЛКА|ДАТА РОЖДЕНИЯ)\]")

LINK_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:vk\.com|t\.me|telegram\.me|linkedin\.com|facebook\.com|fb\.com|instagram\.com"
    r"|ok\.ru|github\.com|hh\.ru|habr\.com|twitter\.com|x\.com)/\S*"
    r"|(?<![\w.@])@[A-Za-z][\w.]{3,}",
    re.IGNORECASE,
)
MONTHS = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
DATE_VALUE = rf"(?:\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}|\d{{1,2}}\s+{MONTHS}\s+\d{{4}}|\d{{4}})"
BIRTH_RE = re.compile(
    rf"((?:дата\s+рождения|д\.\s?р\.|родил(?:ся|ась)|год\s+рождения)\s*:?\s*)"
    rf"{DATE_VALUE}(?:\s*г(?:ода|\.)?)?"
    rf"|\b\d{{4}}\s*г\.?\s*р\.?",
    re.IGNORECASE,
)
CAPS_WORD_RE = re.compile(r"\b[А-ЯЁ]{2,}\b")
SAME_RUN_RE = re.compile(r"\[ИМЯ\](?:[\s,]*\[ИМЯ\])+")
INITIALS_RE = re.compile(r"(\[ИМЯ\])\s*(?:[А-ЯЁ]\.\s?){1,2}")


@lru_cache(maxsize=1)
def _ner():
    from natasha import NewsEmbedding, NewsNERTagger, Segmenter

    return Segmenter(), NewsNERTagger(NewsEmbedding())


def _person_spans(text: str) -> list[tuple[int, int]]:
    from natasha import Doc

    segmenter, tagger = _ner()
    # NER не узнаёт имена капсом; «ИВАНОВ» → «Иванов» той же длины, смещения совпадают.
    doc = Doc(CAPS_WORD_RE.sub(lambda m: m.group().capitalize(), text))
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    # Одиночные слова NER путает с должностями и аббревиатурами («Казначей», «ЛПР»,
    # «им. Баумана»), поэтому берём только имена из двух слов и больше в одной строке.
    return [
        (s.start, s.stop)
        for s in doc.spans
        if s.type == "PER"
        and len(s.text.split()) >= 2
        and "\n" not in s.text
        and not PLACEHOLDER_RE.search(text[max(s.start - 1, 0) : s.stop + 1])
    ]


def _digits_pattern(phone: str) -> str:
    """Номер записи в любом оформлении: последние 10 цифр с любыми разделителями."""
    digits = re.sub(r"\D", "", phone)[-10:]
    return r"[\s\-()]*".join(digits)


def _name_part_pattern(part: str) -> str:
    stem = part[:-1] if len(part) > 4 else part  # Петров → Петрова, Петрову, Петровым
    return rf"(?<![\w]){re.escape(stem)}[а-яё]{{0,4}}(?![\w])"


def anonymize(text: str, full_name: str | None = None, phones=(), emails=(), links=()) -> str:
    text = text or ""
    for email in emails:
        text = re.sub(re.escape(email), EMAIL, text, flags=re.IGNORECASE)
    text = EMAIL_RE.sub(EMAIL, text)
    for link in links:
        text = text.replace(link, LINK)
    text = LINK_RE.sub(LINK, text)
    text = BIRTH_RE.sub(lambda m: (m.group(1) or "") + BIRTH, text)
    text = PHONE_RE.sub(
        lambda m: PHONE if len(re.sub(r"\D", "", m.group())) >= 10 else m.group(), text
    )
    for phone in phones:
        text = re.sub(_digits_pattern(phone), PHONE, text)
    for part in (full_name or "").split():
        if len(part) >= 3:
            text = re.sub(_name_part_pattern(part), NAME, text, flags=re.IGNORECASE)
    for start, stop in reversed(_person_spans(text)):
        text = text[:start] + NAME + text[stop:]
    text = SAME_RUN_RE.sub(NAME, text)
    return INITIALS_RE.sub(r"\1 ", text)


def quote_span(quote: str, original: str) -> tuple[int, int] | None:
    """Где цитата из обезличенного текста стоит в оригинале.

    Плейсхолдеры в цитате подставляются обратно как «любой короткий фрагмент»,
    пробелы и регистр не важны. None — цитаты в оригинале нет.
    """
    parts = [p for p in PLACEHOLDER_RE.split(quote or "") if p.strip()]
    if not parts:
        return None
    words = [r"\s+".join(map(re.escape, p.split())) for p in parts]
    gaps = PLACEHOLDER_RE.findall(quote)
    pattern = r"\s*.{1,80}?\s*".join(words) if gaps else words[0]
    match = re.search(pattern, original or "", flags=re.IGNORECASE | re.DOTALL)
    return match.span() if match else None
