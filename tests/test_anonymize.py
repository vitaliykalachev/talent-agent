import re

import pytest
from sqlalchemy import select

from app import db
from app.anonymize import anonymize, quote_span
from app.importer.normalize import EMAIL_RE, extract_phones
from app.models import Candidate


def leaks(text: str, c: Candidate) -> list[str]:
    """Что из контактов и ФИО записи осталось в тексте."""
    found = []
    digits = re.sub(r"\D", "", text)
    found += [p for p in c.phones if p[-10:] in digits]
    found += [e for e in c.emails if e.lower() in text.lower()]
    for part in (c.full_name or "").split():
        if len(part) > 2 and re.search(rf"(?<!\w){re.escape(part)}(?!\w)", text, re.I):
            found.append(part)
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
