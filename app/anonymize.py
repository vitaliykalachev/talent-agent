"""Обезличивание текста перед отправкой в ИИ-модель (раздел 6 плана).

Слои по порядку:
1. Известные значения записи: её почта и телефоны в любом оформлении.
2. Регулярные выражения: почта, ссылки на соцсети и мессенджеры, ники с @, номера
   документов (ИНН, ОГРН, СНИЛС, паспорт — до телефонов, чтобы их цифры не приняли
   за номер), даты рождения и возраст, телефоны РФ и СНГ.
3. ФИО записи во всех формах: полностью и по частям во всех падежах (pymorphy2 из natasha),
   с инициалами до и после фамилии, латиницей в распространённых транслитерациях.
4. NER natasha построчно; строки капсом перед распознаванием переводятся в обычный
   регистр, иначе имена в них не находятся.
В БД остаётся оригинал; модели уходит только результат `anonymize`.
"""

import re
from functools import lru_cache

from app.importer.normalize import EMAIL_RE, PHONE_RE, find_name, latin_name, to_cyrillic

NAME, PHONE, EMAIL, LINK = "[ИМЯ]", "[ТЕЛЕФОН]", "[ПОЧТА]", "[ССЫЛКА]"
BIRTH, DOCUMENT = "[ДАТА РОЖДЕНИЯ]", "[ДОКУМЕНТ]"
PLACEHOLDER_RE = re.compile(r"\[(?:ИМЯ|ТЕЛЕФОН|ПОЧТА|ССЫЛКА|ДАТА РОЖДЕНИЯ|ДОКУМЕНТ)\]")

LINK_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:vk\.com|t\.me|telegram\.me|wa\.me|linkedin\.com|facebook\.com|fb\.com"
    r"|instagram\.com|ok\.ru|github\.com|hh\.ru|habr\.com|twitter\.com|x\.com)/\S*"
    r"|(?<![\w.@])@[A-Za-z][\w.]{3,}"
    r"|(?:skype|telegram|телеграм|tg)\s*:\s*[\w.\-]{3,}",
    re.IGNORECASE,
)
DOCUMENT_RE = re.compile(
    r"\b(?:ИНН|ОГРНИП|ОГРН|СНИЛС|паспорт\w*|серия|в/у|водительское удостоверение)"
    r"\s*[:№#]?\s*(?:серия\s*)?\d[\d\s\-№]{5,20}\d",
    re.IGNORECASE,
)
MONTHS = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
DATE_VALUE = rf"(?:\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}|\d{{1,2}}\s+{MONTHS}\s+\d{{4}}|\d{{4}})"
BIRTH_RE = re.compile(
    rf"((?:дата\s+рожд(?:ения|\.)?|д\.\s?р\.|г\.\s?р\.|родил(?:ся|ась)|год\s+рождения)\s*:?\s*)"
    rf"{DATE_VALUE}(?:\s*г(?:ода|\.)?)?"
    r"|\b\d{4}\s*г\.?\s*р\.?"
    r"|((?:возраст|мужчина|женщина|муж\.|жен\.)\s*[:,]?\s*)\d{2}\s*(?:год|года|лет)\b",
    re.IGNORECASE,
)
SAME_RUN_RE = re.compile(r"\[ИМЯ\](?:[\s,]*\[ИМЯ\])+")
INITIALS_AFTER_RE = re.compile(r"(\[ИМЯ\])\s*(?:[А-ЯЁA-Z]\.\s?){1,2}")
INITIALS_BEFORE_RE = re.compile(r"(?:\b[А-ЯЁA-Z]\.\s?){1,2}\s*(\[ИМЯ\])")
LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "(?:e|ye)", "ё": "(?:e|yo|jo)",
    "ж": "(?:zh|j)", "з": "z", "и": "i", "й": "(?:y|i|j)", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "(?:kh|h|x)", "ц": "(?:ts|tc|c|cz)", "ч": "ch", "ш": "sh", "щ": "(?:shch|sch)",
    "ъ": "", "ы": "(?:y|i)", "ь": "y?", "э": "e", "ю": "(?:yu|iu|ju)", "я": "(?:ya|ia|ja|a)",
}  # fmt: skip


@lru_cache(maxsize=1)
def _ner():
    from natasha import NewsEmbedding, NewsNERTagger, Segmenter

    return Segmenter(), NewsNERTagger(NewsEmbedding())


@lru_cache(maxsize=1)
def _morph():
    from natasha import MorphVocab  # pymorphy2 с поправкой natasha под Python 3.12

    return MorphVocab()


def _line_person_spans(line: str) -> list[tuple[int, int]]:
    from natasha import Doc

    letters = [ch for ch in line if ch.isalpha()]
    if not letters:
        return []
    segmenter, tagger = _ner()
    caps = sum(ch.isupper() for ch in letters) / len(letters) > 0.7
    doc = Doc(line.title() if caps else line)  # title() сохраняет длину и смещения
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    # Одиночные слова NER путает с должностями и аббревиатурами («Казначей», «ЛПР»,
    # «им. Баумана»), поэтому берём только имена из двух слов и больше.
    return [
        (s.start, s.stop)
        for s in doc.spans
        if s.type == "PER"
        and len(s.text.split()) >= 2
        and not PLACEHOLDER_RE.search(line[max(s.start - 1, 0) : s.stop + 1])
    ]


def _digits_pattern(phone: str) -> str:
    """Номер записи в любом оформлении: последние 10 цифр с любыми разделителями."""
    digits = re.sub(r"\D", "", phone)[-10:]
    return r"[\s\-().]*".join(digits)


def _forms(part: str) -> set[str]:
    """Слово ФИО во всех падежах: Лев → Льва, Льву; Пётр → Петра."""
    forms = {part.lower()}
    for parse in _morph().parse(part)[:3]:
        if {"Name", "Surn", "Patr"} & set(parse.tag.grammemes):
            forms |= {f.word for f in parse.lexeme}
    return {f.replace("ё", "е") for f in forms}


def _cyr_pattern(part: str) -> str:
    stems = {re.escape(f[:-1] if len(f) > 4 else f) for f in _forms(part)}
    body = "|".join(sorted(stems, key=len, reverse=True)).replace("е", "[её]")
    return rf"(?<!\w)(?:{body})[а-яё]{{0,4}}(?!\w)"


def _latin_pattern(part: str) -> str:
    word = part.lower()
    stem = word[:-1] if len(word) > 4 else word
    body = "".join(LATIN.get(ch, re.escape(ch)) for ch in stem)
    return rf"(?<![A-Za-z]){body}[a-z]{{0,4}}(?![A-Za-z])"


def _remove_name(text: str, full_name: str | None) -> str:
    for part in (full_name or "").split():
        if len(part) < 3 or "." in part:
            continue
        text = re.sub(_cyr_pattern(part), NAME, text, flags=re.IGNORECASE)
        text = re.sub(_latin_pattern(part), NAME, text, flags=re.IGNORECASE)
    return text


def _phone_or_keep(match: re.Match) -> str:
    return PHONE if len(re.sub(r"\D", "", match.group())) >= 10 else match.group()


def anonymize(text: str, full_name: str | None = None, phones=(), emails=(), links=()) -> str:
    """Обезличенная копия с тем же числом строк, что и оригинал: номера строк, на которые
    ссылается модель, совпадают с `raw_text`. Если замена склеила строки (телефон или ФИО
    с переносом), текст обезличивается построчно."""
    result = _anonymize(text or "", full_name, phones, emails, links)
    lines = (text or "").split("\n")
    if result.count("\n") == len(lines) - 1:
        return result
    return "\n".join(_anonymize(line, full_name, phones, emails, links) for line in lines)


def numbered(text: str) -> str:
    """Строки с номерами «12| текст» — так модель указывает, где что написано."""
    return "\n".join(f"{i}| {line}" for i, line in enumerate(text.split("\n"), start=1))


def valid_lines(value, text: str) -> list[int] | None:
    """Диапазон [от, до] строк `text`, если он существует и опирается не только на
    плейсхолдеры обезличивания; иначе None («в резюме не сказано»)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        start, stop = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    lines = text.split("\n")
    if not 1 <= start <= stop <= len(lines):
        return None
    cited = PLACEHOLDER_RE.sub("", "\n".join(lines[start - 1 : stop]))
    if not re.search(r"\w{2,}", cited):
        return None
    return [start, stop]


def _anonymize(text: str, full_name, phones, emails, links) -> str:
    # 1. известные значения записи
    for email in emails:
        text = re.sub(re.escape(email), EMAIL, text, flags=re.IGNORECASE)
    for link in links:
        text = text.replace(link, LINK)
    # 2. регулярные выражения
    text = EMAIL_RE.sub(EMAIL, text)
    text = LINK_RE.sub(LINK, text)
    text = DOCUMENT_RE.sub(DOCUMENT, text)
    text = BIRTH_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + BIRTH, text)
    text = PHONE_RE.sub(_phone_or_keep, text)
    for phone in phones:
        text = re.sub(_digits_pattern(phone), PHONE, text)
    # 3. ФИО записи во всех формах; нет ФИО у записи — берём из шапки резюме
    latin = latin_name(text)
    if latin:
        text = _remove_name(text, " ".join(to_cyrillic(w) for w in latin.split()))
        for word in latin.split():
            text = re.sub(
                rf"(?<![A-Za-z]){re.escape(word)}[a-z]{{0,4}}(?![A-Za-z])",
                NAME,
                text,
                flags=re.IGNORECASE,
            )
    text = _remove_name(text, full_name or find_name(text))
    # 4. NER построчно
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for start, stop in reversed(_line_person_spans(line)):
            line = line[:start] + NAME + line[stop:]
        lines[i] = line
    text = SAME_RUN_RE.sub(NAME, "\n".join(lines))
    text = INITIALS_BEFORE_RE.sub(r"\1", text)
    return INITIALS_AFTER_RE.sub(r"\1 ", text)


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
