"""Нормализация контактов, ФИО и дат (раздел 7 плана)."""

import re
from datetime import date, datetime, timedelta
from functools import lru_cache

import phonenumbers
from rapidfuzz.distance import Levenshtein

STALE_AFTER = timedelta(days=548)  # 18 месяцев

CIS_CODES = ("375", "380", "998", "996", "994", "995", "992", "993", "374", "373")
PHONE_RE = re.compile(
    # международный с «+»: +44 20 7946 0958, +375 (29) 123-45-67, +7(495)1234567
    r"\+\d{1,3}(?:[ \-.]?\(?\d{2,7}\)?){2,5}(?!\d)"
    # СНГ без «+»: 375 29 123 45 67
    rf"|(?<![\d\w])(?:{'|'.join(CIS_CODES)})"
    r"[ \-.]?\(?\d{2,3}\)?[ \-.]?\d{3}[ \-.]?\d{2}[ \-.]?\d{2}(?!\d)"
    # РФ и Казахстан: 8 (912) 345-67-89, 9123456789, 8.999.123.45.67
    r"|(?<![\d\w])(?:8|7)?[\s\-(.]*\d{3}[\s\-).]*\d{3}[\s\-.]*\d{2}[\s\-.]*\d{2}(?!\d)"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zа-я]{2,}", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")
BIRTH_RE = re.compile(
    r"(?:дата рождения|родил(?:ся|ась)|д\.\s?р\.)\s*:?\s*(\d{1,2}[./]\d{1,2}[./](\d{4})|(\d{4}))"
    r"|\b(\d{4})\s*г\.?\s*р\.?",
    re.IGNORECASE,
)
RESUME_DATE_RE = re.compile(
    r"(?:резюме обновлено|обновлено|дата резюме|дата обновления|актуально на)\s*:?\s*"
    r"(\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
FEMALE_SURNAME_ENDINGS = ("ова", "ева", "ёва", "ская", "цкая")
PATRONYMIC_ENDINGS = ("вич", "вна", "ична", "инична", "оглы", "кызы")
NAME_WORD_RE = re.compile(r"^[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?$")


def normalize_phone(value) -> str | None:
    """Номер в E.164 через `phonenumbers` (регион RU); РФ и Казахстан — +7XXXXXXXXXX.
    8-800, короткие номера и шум — None."""
    text = str(value or "").strip()
    if text.endswith(".0"):  # число из Excel
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        text = "+" + digits
    else:
        text = digits
        if len(digits) == 12 and digits.startswith(CIS_CODES):  # СНГ без «+»
            text = "+" + digits
        elif len(digits) == 11 and digits[0] in "78":
            text = "+7" + digits[1:]
        elif len(digits) == 10:
            text = "+7" + digits
    try:
        number = phonenumbers.parse(text, "RU")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    if e164.startswith("+7") and (e164[2] not in "34789" or e164[2:5] == "800"):
        return None
    return e164


def extract_phones(text: str) -> list[str]:
    found = (normalize_phone(m.group()) for m in PHONE_RE.finditer(text or ""))
    return list(dict.fromkeys(p for p in found if p))


def normalize_email(value) -> str | None:
    match = EMAIL_RE.search(str(value or ""))
    return match.group().lower() if match else None


def email_key(email: str) -> str:
    """Ключ почты для поиска дублей: у gmail.com и googlemail.com точки в имени и
    «+метка» не значат ничего — ivan.petrov+hh@googlemail.com = ivanpetrov@gmail.com."""
    local, _, domain = email.lower().partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        return f"{local.split('+')[0].replace('.', '')}@gmail.com"
    return f"{local}@{domain}"


def extract_emails(text: str) -> list[str]:
    return list(dict.fromkeys(m.group().lower() for m in EMAIL_RE.finditer(text or "")))


@lru_cache(maxsize=1)
def _names_extractor():
    from natasha import MorphVocab, NamesExtractor

    return NamesExtractor(MorphVocab())


def _title(word: str) -> str:
    return "-".join(p.upper() if "." in p else p.capitalize() for p in word.split("-"))


def normalize_name(value) -> str | None:
    """ФИО в порядке «Фамилия Имя Отчество» с правильным регистром.

    Два слова переставляются, только когда фамилия видна явно: вторая стоит капсом
    («Сергей СИДОРОВ») или у неё женская фамильная форма («Анна Петрова»). Мужские
    пары вроде «Александр Михайлов» / «Михайлов Александр» надёжно не различить —
    порядок остаётся как в источнике.
    """
    raw = str(value or "").split()
    words = [_title(w) for w in raw]
    if not words:
        return None
    text = " ".join(words)
    if len(words) == 2:
        first_caps, second_caps = (w.isupper() and len(w) > 1 for w in raw)
        second_surname = second_caps and not first_caps
        female = words[1].endswith(FEMALE_SURNAME_ENDINGS)
        if second_surname or (female and not words[0].endswith(FEMALE_SURNAME_ENDINGS)):
            return f"{words[1]} {words[0]}"
        return text
    # Отчество узнаётся по окончанию — порядок ясен и без natasha (она ~30 мс на имя).
    if len(words) == 3 and words[2].endswith(PATRONYMIC_ENDINGS):
        return text
    if len(words) == 3 and words[1].endswith(PATRONYMIC_ENDINGS):
        return " ".join([words[2], words[0], words[1]])
    if len(words) == 3 and all(NAME_WORD_RE.match(w) for w in words):
        match = _names_extractor().find(text)
        fact = match.fact if match else None
        if fact and fact.first and fact.last:
            parts = [fact.last, fact.first, fact.middle]
            ordered = [p for p in parts if p]
            if sorted(ordered) == sorted(words):
                return " ".join(ordered)
    return text


def looks_like_name(value) -> bool:
    words = str(value or "").split()
    return 2 <= len(words) <= 3 and all(NAME_WORD_RE.match(w) and w[0].isupper() for w in words)


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    if match.group(1):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_year(value) -> int | None:
    parsed = parse_date(value)
    if parsed:
        return parsed.year
    match = re.fullmatch(r"\s*(\d{4})(?:\.0)?\s*", str(value or ""))
    return int(match.group(1)) if match else None


def extract_birth_year(text: str) -> int | None:
    match = BIRTH_RE.search(text or "")
    if not match:
        return None
    year = match.group(2) or match.group(3) or match.group(4)
    return int(year) if 1930 <= int(year) <= date.today().year - 14 else None


def extract_resume_date(text: str) -> date | None:
    match = RESUME_DATE_RE.search(text or "")
    return parse_date(match.group(1)) if match else None


def normalize_city(value) -> str | None:
    text = re.sub(r"^\s*(?:г\.|город)\s*", "", str(value or ""), flags=re.IGNORECASE).strip()
    if text and (text.islower() or text.isupper()):
        text = " ".join(_title(w) for w in text.split())
    return text or None


def is_stale(resume_date: date | None, today: date | None = None) -> bool:
    return resume_date is None or (today or date.today()) - resume_date > STALE_AFTER


CITY_RE = re.compile(
    r"(?i:город|проживает|место жительства|проживание)[ \t]*:?[ \t]*(?:в[ \t]+)?(?:г\.[ \t]*)?"
    r"([А-ЯЁ][а-яё]+(?:[ -](?:на-)?[А-ЯЁ][а-яё]+)?)"
)


def extract_city(text: str) -> str | None:
    match = CITY_RE.search(text or "")
    return normalize_city(match.group(1)) if match else None


LATIN_NAME_RE = re.compile(r"^[A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2}$|^[A-Z]+(?: [A-Z]+){1,2}$")
LATIN_TO_CYR = [
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"),
    ("sh", "ш"), ("ts", "ц"), ("tc", "ц"), ("yu", "ю"), ("ju", "ю"), ("iu", "ю"),
    ("ya", "я"), ("ja", "я"), ("ia", "ия"), ("ye", "е"), ("x", "кс"), ("a", "а"),
    ("b", "б"), ("v", "в"), ("w", "в"), ("g", "г"), ("d", "д"), ("e", "е"), ("z", "з"),
    ("i", "и"), ("j", "й"), ("k", "к"), ("q", "к"), ("c", "к"), ("l", "л"), ("m", "м"),
    ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("f", "ф"), ("h", "х"),
]  # fmt: skip


def to_cyrillic(word: str) -> str:
    """Латиница → кириллица в нижнем регистре: Sergey → сергей, Ilyich → ильич."""
    word = word.lower()
    word = re.sub(
        r"(?<=[aeiou])y\b|iy\b|ii\b", lambda m: "ий" if len(m.group()) == 2 else "й", word
    )
    soft = {"a": "ья", "u": "ью", "o": "ьё", "e": "ье", "i": "ьи"}  # Ilyich, Natalya
    word = re.sub(r"(?<=[bcdfghklmnprstvz])y([aeiou])", lambda m: soft[m.group(1)], word)
    for lat, cyr in LATIN_TO_CYR:
        word = word.replace(lat, cyr)
    return word.replace("y", "ы")


def latin_name(text: str, lines: int = 5) -> str | None:
    """ФИО латиницей из шапки резюме («Sidorov Petr Ilyich»), если это отдельная строка."""
    for line in [ln.strip(" #*_|-:\t") for ln in (text or "").splitlines() if ln.strip()][:lines]:
        if LATIN_NAME_RE.match(line):
            return line
    return None


def find_name(text: str, lines: int = 5) -> str | None:
    """ФИО из первых строк резюме, если natasha узнаёт в строке имя и фамилию."""
    candidates = [ln.strip(" #*_|-:\t") for ln in (text or "").splitlines() if ln.strip()]
    for line in candidates[:lines]:
        titled = " ".join(_title(w) for w in line.split())
        if not looks_like_name(titled):
            continue
        match = _names_extractor().find(titled)
        if match and match.fact.first and match.fact.last:
            return normalize_name(titled)
    return None


# ── Совместимость ФИО для автослияния (раздел 7 плана) ─────────────────────────

# Уменьшительные и разговорные формы → полные имена. Неоднозначные («Женя», «Саша»,
# «Слава») ведут к нескольким.
DIMINUTIVES = {
    "саша": "александр александра", "шура": "александр александра", "саня": "александр",
    "алеша": "алексей", "леша": "алексей", "толя": "анатолий", "андрюша": "андрей",
    "аня": "анна", "анюта": "анна", "боря": "борис", "валя": "валентина валентин",
    "вася": "василий", "витя": "виктор виталий", "вова": "владимир", "володя": "владимир",
    "слава": "вячеслав ярослав владислав станислав", "галя": "галина", "дима": "дмитрий",
    "митя": "дмитрий", "женя": "евгений евгения", "катя": "екатерина", "лена": "елена",
    "ваня": "иван", "костя": "константин", "люда": "людмила", "мила": "людмила",
    "миша": "михаил", "маша": "мария", "наташа": "наталья наталия", "наталия": "наталья",
    "коля": "николай", "оля": "ольга", "паша": "павел", "петя": "петр", "сережа": "сергей",
    "света": "светлана", "стас": "станислав", "таня": "татьяна", "юра": "юрий", "юля": "юлия",
    "макс": "максим", "жора": "георгий", "гоша": "георгий", "гриша": "григорий",
    "рома": "роман", "тема": "артем", "валера": "валерий", "гена": "геннадий",
    "леня": "леонид", "ира": "ирина", "надя": "надежда", "люба": "любовь",
    "ксюша": "ксения", "настя": "анастасия", "даша": "дарья", "соня": "софья софия",
    "софия": "софья", "лиза": "елизавета", "рита": "маргарита", "лера": "валерия",
    "вика": "виктория", "влад": "владислав", "лида": "лидия", "тоня": "антонина",
    "зина": "зинаида", "нина": "нина", "федя": "федор", "сеня": "семен", "яша": "яков",
    "эдик": "эдуард", "тимоша": "тимофей", "егорка": "егор", "кира": "кирилл кира",
}  # fmt: skip


def _cyrillic(word: str) -> str:
    return to_cyrillic(word).replace("ё", "е")


def _name_words(name: str) -> tuple[list[str], list[str]]:
    """Полные слова и инициалы ФИО в нижнем регистре кириллицей: «Иванов И.П.» →
    (["иванов"], ["и", "п"])."""
    words, initials = [], []
    for token in re.findall(r"[^\s.]+\.?", str(name or "")):
        letters = token.rstrip(".")
        if token.endswith(".") or len(letters) == 1:
            initials.append(_cyrillic(letters)[:1])
        else:
            words.append(_cyrillic(letters))
    return words, initials


def _readings(name: str) -> list[tuple[str, str | None, str | None]]:
    """Возможные прочтения (фамилия, имя, отчество); инициал — одна буква."""
    words, initials = _name_words(name)
    tail = [*initials, None, None]
    if len(words) >= 3:
        return [(words[0], words[1], words[2])]
    if len(words) == 2:
        if initials:
            return [(words[0], words[1], initials[0]), (words[1], words[0], initials[0])]
        return [(words[0], words[1], None), (words[1], words[0], None)]
    if len(words) == 1:
        return [(words[0], tail[0], tail[1])]
    return []


def _full_names(first: str) -> set[str]:
    return {first, *DIMINUTIVES.get(first, "").split()}


def _part_ok(a: str | None, b: str | None, fuzzy: bool = False) -> bool:
    if a is None or b is None:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    if a == b:
        return True
    return fuzzy and min(len(a), len(b)) >= 5 and Levenshtein.distance(a, b) <= 1


def _first_ok(a: str | None, b: str | None, latin: bool) -> bool:
    if a is None or b is None or len(a) == 1 or len(b) == 1:
        return _part_ok(a, b)
    if _full_names(a) & _full_names(b):
        return True
    return latin and min(len(a), len(b)) >= 5 and Levenshtein.distance(a, b) <= 1


def names_compatible(a: str | None, b: str | None) -> bool:
    """Одно ли это лицо по ФИО: фамилия совпадает или отличается одной правкой при
    длине от 5 букв, имя совпадает с учётом уменьшительных, отчества не противоречат,
    инициалы совместимы; ё = е, латиница переводится в кириллицу. Нет ФИО — не мешает."""
    if not a or not b:
        return True
    latin = bool(re.search(r"[A-Za-z]", f"{a}{b}"))
    for last_a, first_a, middle_a in _readings(a):
        for last_b, first_b, middle_b in _readings(b):
            if (
                _part_ok(last_a, last_b, fuzzy=True)
                and len(last_a) > 1
                and len(last_b) > 1
                and _first_ok(first_a, first_b, latin)
                and _part_ok(middle_a, middle_b)
            ):
                return True
    return False
