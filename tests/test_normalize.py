from datetime import date, datetime

import pytest

from app.importer.normalize import (
    extract_birth_year,
    extract_emails,
    extract_phones,
    extract_resume_date,
    is_stale,
    normalize_email,
    normalize_name,
    normalize_phone,
    parse_date,
)


@pytest.mark.parametrize(
    "raw",
    ["8 (912) 345-67-89", "+7 912 345 67 89", "9123456789", "7-912-345-6789", "79123456789.0"],
)
def test_phone_to_plus7(raw):
    assert normalize_phone(raw) == "+79123456789"


@pytest.mark.parametrize("raw", ["123", "", "2015-2019", "8 800 555-35-35", "+7 800 100 00 00"])
def test_phone_rejects_noise_and_toll_free(raw):
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 701 234 56 78", "+77012345678"),  # Казахстан
        ("8 (727) 123-45-67", "+77271234567"),
        ("+375 (29) 123-45-67", "+375291234567"),
        ("375 29 123 45 67", "+375291234567"),
        ("+998 90 123 45 67", "+998901234567"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_phone_non_russian_to_e164(raw, expected):
    assert normalize_phone(raw) == expected
    assert extract_phones(f"Тел.: {raw}, пишите") == [expected]


def test_extract_phones_from_text_skips_years_and_salary():
    text = "Тел: 8-912-345-67-89, доп. +7(495)1234567; работал 2015-2019, зарплата 150 000"
    assert extract_phones(text) == ["+79123456789", "+74951234567"]


def test_email_lowercase_and_extract():
    assert normalize_email(" Ivan.Petrov@Mail.RU ") == "ivan.petrov@mail.ru"
    assert normalize_email("не почта") is None
    assert extract_emails("пишите: A.B@Yandex.ru или a.b@yandex.ru") == ["a.b@yandex.ru"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("иванов иван петрович", "Иванов Иван Петрович"),
        ("Иван Петрович Иванов", "Иванов Иван Петрович"),
        ("ПЕТРОВА анна", "Петрова Анна"),
        ("Анна Петрова", "Петрова Анна"),
        ("  смирнова-щедрина   ольга ", "Смирнова-Щедрина Ольга"),
        # мужские пары без явной фамилии остаются в порядке источника
        ("Сидоров Сергей", "Сидоров Сергей"),
        ("СИДОРОВ Сергей", "Сидоров Сергей"),
        ("Александр Михайлов", "Александр Михайлов"),
        ("Михайлов Александр", "Михайлов Александр"),
        ("Сергей СИДОРОВ", "Сидоров Сергей"),
        ("", None),
    ],
)
def test_name_order_and_case(raw, expected):
    assert normalize_name(raw) == expected


def test_parse_date_variants():
    assert parse_date("05.02.2025") == date(2025, 2, 5)
    assert parse_date("2025-02-05") == date(2025, 2, 5)
    assert parse_date(datetime(2025, 2, 5, 10, 0)) == date(2025, 2, 5)
    assert parse_date("вчера") is None


def test_birth_year_and_resume_date_from_text():
    assert extract_birth_year("Дата рождения: 12.03.1985\nГород: Самара") == 1985
    assert extract_birth_year("Иванов, 1990 г.р., Казань") == 1990
    assert extract_birth_year("Опыт с 2010 года") is None
    assert extract_resume_date("Резюме обновлено 05.02.2025") == date(2025, 2, 5)
    assert extract_resume_date("Дата резюме: 2024-11-30") == date(2024, 11, 30)


def test_stale_after_18_months():
    today = date(2026, 9, 26)
    assert is_stale(date(2025, 2, 1), today) is True
    assert is_stale(date(2025, 6, 1), today) is False
    assert is_stale(None, today) is True
