import re

import pytest
from sqlalchemy import select

from app import db
from app.anonymize import anonymize, numbered, quote_span, valid_lines
from app.importer.normalize import EMAIL_RE, extract_phones
from app.models import Candidate

# Своя таблица латиницы для сторожа, независимая от обезличивания: у буквы несколько
# распространённых написаний, основа слова без последней буквы ловит падежи.
GUARD_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "(?:e|ye)", "ё": "(?:e|yo|jo)",
    "ж": "(?:zh|j)", "з": "z", "и": "i", "й": "(?:y|i|j)", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "(?:kh|h)", "ц": "(?:ts|c)", "ч": "ch", "ш": "sh", "щ": "(?:shch|sch)", "ъ": "",
    "ы": "(?:y|i)", "ь": "y?", "э": "e", "ю": "(?:yu|ju|iu)", "я": "(?:ya|ja|ia|a)",
}  # fmt: skip


def leaks(text: str, c: Candidate) -> list[str]:
    """Что из контактов и ФИО записи осталось в тексте: телефоны — по цифрам (с любыми
    разделителями, в том числе точками), ФИО — по основе слова в любом падеже, с ё и
    латиницей."""
    found = []
    digits = re.sub(r"\D", "", text)
    found += [p for p in c.phones if p[-10:] in digits]
    found += [e for e in c.emails if e.lower() in text.lower()]
    plain = text.lower().replace("ё", "е")
    for part in (c.full_name or "").split():
        word = part.lower().replace("ё", "е")
        if len(word) < 3 or "." in word:
            continue
        stem = word[:-1] if len(word) > 4 else word
        if re.search(rf"(?<!\w){re.escape(stem)}", plain):
            found.append(part)
        latin = "".join(GUARD_LATIN.get(ch, re.escape(ch)) for ch in part.lower()[: len(stem)])
        if re.search(rf"(?<![a-z]){latin}", plain):
            found.append(f"{part} латиницей")
    return found


DIRTY = """ИВАНОВ ИВАН ПЕТРОВИЧ
инженер-технолог ООО «ТехноПласт», 8 (912) 345-67-89, IVAN@MAIL.RU
Дата рождения: 12.03.1985. Родился 5 мая 1985 года, 1985 г.р.
vk.com/ivanov https://t.me/ivan @ivan_petrov www.ivanov.ru
Рекомендации: Петров Сергей Николаевич, директор, +375 29 123-45-67
Иванова И.П. в ПАО «КАМАЗ» с 2015 по н.в., зарплата от 150 000 руб, тел. 8-912-3456789"""


def test_dirty_resume_has_no_contacts_names_links_or_birth_date():
    out = anonymize(DIRTY, "Иванов Иван Петрович", ["+79123456789"], ["ivan@mail.ru"])
    assert extract_phones(out) == [] and not EMAIL_RE.search(out)
    for word in ("Иван", "ИВАН", "Петров", "Сергей", "ivanov", "ivan_petrov", "12.03", "1985"):
        assert word not in out
    assert out.count("[ТЕЛЕФОН]") == 3 and "[ПОЧТА]" in out and "[ССЫЛКА]" in out
    assert "[ДАТА РОЖДЕНИЯ]" in out and "[ИМЯ]" in out
    # профессиональное остаётся
    for kept in ("инженер-технолог", "ООО «ТехноПласт»", "ПАО «КАМАЗ»", "2015 по н.в.", "150 000"):
        assert kept in out


@pytest.mark.parametrize(
    "phone",
    ["+7 701 234 56 78", "+375 (29) 123-45-67", "380 44 123 45 67", "+998 90 123 45 67"],
)
def test_cis_phones_removed(phone):
    assert anonymize(f"Тел.: {phone}; опыт 5 лет") == "Тел.: [ТЕЛЕФОН]; опыт 5 лет"


@pytest.mark.parametrize(
    "text,name,phones",
    [
        ("Тел. 8.999.123.45.67, токарь", None, ()),  # телефон с точками по шаблону
        ("Звонить: 8.999.123.45.67", "Сидоров Пётр", ["+79991234567"]),  # номер записи
        ("+7.999.123.45.67 — рабочий", None, ()),
    ],
)
def test_phone_with_dots_removed(text, name, phones):
    out = anonymize(text, name, phones)
    assert "[ТЕЛЕФОН]" in out and "123" not in out, out


@pytest.mark.parametrize(
    "text", ["Дата рожд.: 01.01.1980, токарь", "г.р. 1980, токарь", "Г. р.: 1980, токарь"]
)
def test_birth_date_short_forms_removed(text):
    out = anonymize(text)
    assert "1980" not in out and "[ДАТА РОЖДЕНИЯ]" in out and "токарь" in out, out


def test_latin_name_from_header_removed_without_record_name():
    """У записи нет ФИО (файл без строки выгрузки): имя берётся из шапки резюме."""
    text = "Sidorov Petr Ilyich\nТокарь 6 разряда\nРекомендую: Сидоров П. И. — Sidorova team"
    out = anonymize(text)
    assert not re.search(r"(?i)sidorov|petr|ilyich|сидоров", out), out
    assert "Токарь 6 разряда" in out


def test_record_first_name_caught_in_latin():
    out = anonymize("Julia, главный бухгалтер; Юлии позвонить, Yulia на связи", "Юлия")
    assert not re.search(r"(?i)julia|yulia|юли", out) and "главный бухгалтер" in out, out


def test_guard_catches_what_it_should():
    """Сторож не пустой: падеж, латиница и телефон с точками — это утечки."""
    c = Candidate(full_name="Сидоров Пётр", phones=["+79991234567"], emails=[])
    assert leaks("Резюме Сидорова", c) == ["Сидоров"]
    assert leaks("Petr S., токарь", c) == ["Пётр латиницей"]
    assert leaks("тел 8.999.123.45.67", c) == ["+79991234567"]
    assert leaks("Токарь, опыт 12 лет", c) == []


def test_record_name_removed_even_if_ner_misses_it():
    # Фамилия капсом и в косвенном падеже, имя с маленькой буквы — NER такое пропускает.
    out = anonymize("резюме СИДОРОВОЙ, ольга — главный бухгалтер", "Сидорова Ольга Ильинична")
    assert "СИДОРОВ" not in out and "ольга" not in out
    assert "главный бухгалтер" in out


def test_quote_found_in_original_after_placeholder_backfill():
    original = "Иванов Иван\nОрганизовал участок литья,\n  руководил сменой Петрова С."
    assert quote_span("организовал участок литья", original)
    assert quote_span("руководил сменой [ИМЯ]", original)
    assert quote_span("[ИМЯ] Организовал участок", original)
    assert quote_span("запустил цех с нуля", original) is None


@pytest.fixture(scope="module")
def demo_records(tmp_path_factory):
    """20 записей демо-набора всех видов: обычные, короткие, длинные, «грязные» капсом."""
    from app.demo import generate
    from app.importer.pipeline import new_batch, start_import
    from app.jobs import run_pending

    root = tmp_path_factory.mktemp("anon")
    db.configure(root / "data")
    table, resumes = generate(root / "demo")
    with db.SessionLocal() as s:
        start_import(s, batch := new_batch(s, table, [resumes]), batch.mapping)
    run_pending()
    with db.SessionLocal() as s:
        items = list(s.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    caps = [c for c in items if (c.full_name or "").upper() in c.raw_text]
    files = [c for c in items if c.source_file]
    rows = [c for c in items if not c.source_file]
    picked: dict = {}
    for group, n in ((caps, 7), (files, 14), (rows, 20)):
        for c in group:
            if len(picked) < n:
                picked.setdefault(c.id, c)
    assert len(caps) >= 7 and len(picked) == 20
    return list(picked.values())


def test_twenty_demo_resumes_leak_nothing(demo_records):
    for c in demo_records:
        out = anonymize(c.raw_text, c.full_name, c.phones, c.emails, c.links)
        assert leaks(out, c) == [], (c.id, out[:300])
        assert extract_phones(out) == [] and not EMAIL_RE.search(out)
        assert "Дата рождения: [ДАТА РОЖДЕНИЯ]" in out or "Дата рождения" not in out


@pytest.mark.parametrize(
    "text",
    [
        "СИДОРОВ ПЁТР ИЛЬИЧ\nтокарь",  # капсом в шапке
        "Резюме Сидорова Петра Ильича, токарь",  # родительный падеж, ё → е
        "Sidorov Petr, turner; Sidorova team",  # латиница
        "Pyotr Sidorov, токарь",
        "Контакты: p.sidorov@mail.ru, @petr_sidorov, t.me/sidorov_p",  # логин почты и ник
        "Отзыв: П. И. Сидоров — отличный токарь; Сидоров П.И. — ответственный",  # инициалы
    ],
)
def test_record_name_removed_in_all_forms(text):
    out = anonymize(text, "Сидоров Пётр Ильич")
    assert not re.search(r"(?i)sidorov|сидоров|п[её]тр|pyotr|petr|ильич|\bП\.\s?И\.", out), out


def test_documents_and_age_removed_but_experience_kept():
    text = (
        "ИНН 770708389312, СНИЛС 123-456-789 01, паспорт 45 12 345678. Мужчина, 35 лет. Стаж 12 лет"
    )
    out = anonymize(text)
    assert out.count("[ДОКУМЕНТ]") == 3 and "[ТЕЛЕФОН]" not in out
    assert "35" not in out and "Стаж 12 лет" in out


def test_docx_author_and_file_name_not_in_text(tmp_path):
    import zipfile

    from app.demo import write_docx
    from app.importer.readers import document_text

    path = tmp_path / "Сидоров Пётр.docx"
    write_docx(path, ["Токарь 6 разряда"])
    core = (
        '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties xmlns:cp="http://schemas.'
        'openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/'
        'dc/elements/1.1/"><dc:creator>Сидоров Пётр</dc:creator></cp:coreProperties>'
    )
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("docProps/core.xml", core)
    text = document_text(path)
    assert "Токарь" in text and "Сидоров" not in text


def test_anonymized_copy_keeps_line_numbers():
    """Номера строк обезличенной копии совпадают с оригиналом, даже если телефон или
    два имени подряд разбиты переносом."""
    text = "Иванов Иван\nПетров Сергей\nтел. 8 (912)\n345-67-89\nИнженер-технолог\n\nКАМАЗ"
    out = anonymize(text, "Иванов Иван", ["+79123456789"])
    assert out.count("\n") == text.count("\n")
    assert out.split("\n")[4] == "Инженер-технолог" and out.split("\n")[6] == "КАМАЗ"
    assert numbered("а\nб").split("\n") == ["1| а", "2| б"]


def test_line_range_checked_against_text():
    text = "[ИМЯ]\nИнженер-технолог\n[ТЕЛЕФОН], [ПОЧТА]\nКАМАЗ, 2015–2020"
    assert valid_lines([2, 4], text) == [2, 4]
    assert valid_lines([4, 5], text) is None  # строки 5 нет
    assert valid_lines([3, 2], text) is None  # перевёрнутый диапазон
    assert valid_lines([0, 1], text) is None
    assert valid_lines([1, 1], text) is None  # одни плейсхолдеры — не факт
    assert valid_lines([3, 3], text) is None
    assert valid_lines(None, text) is None and valid_lines(["x", 2], text) is None


def test_contact_line_is_not_evidence():
    """Правка ревью этапа 3, №3: строка из меток и подписей контактов — не опора."""
    text = "[ИМЯ]\nТел.: [ТЕЛЕФОН], [ПОЧТА]\nНачальник цеха литья, 2019 — 2024"
    assert valid_lines([2, 2], text) is None
    assert valid_lines([3, 3], text) == [3, 3]
    assert valid_lines([2, 3], text) == [2, 3]  # в диапазоне есть настоящая опора


def test_phone_split_by_line_break_and_age_are_masked():
    """Правка ревью этапа 3, №11: номер, разорванный переносом, и «Мне 45 лет»."""
    text = "Иванов Иван\nТел.: +7 (912)\n345-67-89\nМне 45 лет, опыт 10 лет"
    for phones in (["+79123456789"], []):
        result = anonymize(text, "Иванов Иван", phones, [])
        assert result.count("\n") == text.count("\n")  # строки на месте
        assert "912" not in result and "345-67" not in result, result
        assert "45 лет" not in result and "опыт 10 лет" in result
