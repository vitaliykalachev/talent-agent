from datetime import date

from sqlalchemy import select

from app.models import Candidate, Duplicate

HEADER = "ID,ФИО,Телефон,Email,Город,Должность,Дата обновления\n"


def csv(tmp_path, *rows):
    path = tmp_path / "crm.csv"
    path.write_text(HEADER + "".join(",".join(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_same_phone_in_other_format_is_merged_into_fresher_record(tmp_path, do_import, session):
    do_import(
        csv(
            tmp_path,
            [
                "1",
                "Иванов Иван",
                "8 (912) 345-67-89",
                "old@mail.ru",
                "Самара",
                "Мастер",
                "01.02.2023",
            ],
            [
                "2",
                "Иванов Иван Петрович",
                "+7 912 345 67 89",
                "new@mail.ru",
                "",
                "Начальник цеха",
                "01.08.2026",
            ],
        )
    )
    old, new = session.scalars(select(Candidate).order_by(Candidate.id))
    assert old.duplicate_of == new.id
    assert new.duplicate_of is None
    assert new.full_name == "Иванов Иван Петрович"
    assert new.resume_date == date(2026, 8, 1)
    assert new.city == "Самара"  # пустое поле свежей записи дополнено из старой
    assert new.phones == ["+79123456789"]
    assert sorted(new.emails) == ["new@mail.ru", "old@mail.ru"]
    dup = session.scalar(select(Duplicate))
    assert (dup.candidate_a, dup.candidate_b, dup.status, dup.confidence) == (
        new.id,
        old.id,
        "merged",
        1.0,
    )


def test_same_email_different_case_is_merged(tmp_path, do_import, active):
    do_import(
        csv(
            tmp_path,
            ["1", "Орлова Анна", "", "Anna.Orlova@Mail.RU", "Пермь", "", "01.01.2026"],
            ["2", "Орлова Анна", "89001234567", "anna.orlova@mail.ru", "Пермь", "", "01.03.2026"],
        )
    )
    (anna,) = active()
    assert anna.external_id == "2"
    assert anna.phones == ["+79001234567"]


def test_namesakes_with_different_contacts_stay_separate(tmp_path, do_import, active, session):
    do_import(
        csv(
            tmp_path,
            ["1", "Смирнов Алексей", "89000000001", "a1@mail.ru", "Омск", "", ""],
            ["2", "Смирнов Алексей", "89000000002", "a2@mail.ru", "Омск", "", ""],
            ["3", "Козлова Вера", "89000000003", "", "Тула", "", ""],
        )
    )
    assert len(active()) == 3
    assert session.scalar(select(Duplicate)) is None


def test_duplicate_across_two_imports(tmp_path, do_import, active):
    do_import(csv(tmp_path, ["1", "Лисина Ольга", "89005556677", "", "Уфа", "", "01.01.2025"]))
    do_import(csv(tmp_path, ["9", "Лисина Ольга", "+79005556677", "", "Уфа", "", "01.09.2026"]))
    (olga,) = active()
    assert olga.external_id == "9"


def test_record_without_patronymic_initials_latin_and_yo_merged(tmp_path, do_import, active):
    do_import(
        csv(
            tmp_path,
            ["1", "Королёв Пётр Семёнович", "89001112233", "", "Тула", "", "01.01.2025"],
            ["2", "Королев Петр", "+7 900 111-22-33", "", "", "", "01.02.2025"],
            ["3", "Королев П. С.", "", "p.korolev+hh@gmail.com", "", "", "01.03.2025"],
            [
                "4",
                "Petr Korolev",
                "8 900 111 22 33",
                "pkorolev@googlemail.com",
                "",
                "",
                "01.04.2025",
            ],
        )
    )
    (petr,) = active()
    assert petr.external_id == "4"


def test_different_patronymics_with_same_phone_not_merged(tmp_path, do_import, active, session):
    do_import(
        csv(
            tmp_path,
            ["1", "Иванов Иван Петрович", "89001112233", "", "Тула", "", ""],
            ["2", "Иванов Иван Сергеевич", "89001112233", "", "Тула", "", ""],
        )
    )
    assert len(active()) == 2
    dup = session.scalar(select(Duplicate))
    assert dup.status == "open"  # общий контакт, ФИО противоречат — решает человек


def test_shared_phone_of_different_people_not_merged(tmp_path, do_import, active, session):
    """Общий телефон у мужа и жены: не сливаем, кладём в «нужно проверить»."""
    do_import(
        csv(
            tmp_path,
            ["1", "Смирнов Олег", "89002223344", "", "Омск", "", ""],
            ["2", "Смирнова Анна", "89002223344", "", "Омск", "", ""],
        )
    )
    assert len(active()) == 2
    assert [d.status for d in session.scalars(select(Duplicate))] == ["open"]


def test_contact_of_three_names_is_not_identifying(tmp_path, do_import, active, session):
    """Телефон отдела кадров у трёх разных людей: ни слияния, ни пар в очереди."""
    do_import(
        csv(
            tmp_path,
            ["1", "Смирнов Олег", "84951234567", "", "", "", ""],
            ["2", "Петров Иван", "84951234567", "", "", "", ""],
            ["3", "Сидорова Анна", "84951234567", "", "", "", ""],
            ["4", "Смирнов Олег", "84951234567", "o.smirnov@mail.ru", "", "", ""],
        )
    )
    assert len(active()) == 4
    assert session.scalar(select(Duplicate)) is None


def test_chain_through_short_name_does_not_merge_incompatible(tmp_path, do_import, session):
    """Правка ревью этапа 3, №2: «Иванов Иван Петрович» и «Иванов Иван Павлович» с общим
    телефоном не склеиваются транзитивно через «Иванов Иван»."""
    do_import(
        csv(
            tmp_path,
            ["1", "Иванов Иван Петрович", "8 912 345-67-89", "", "", "", "01.08.2026"],
            ["2", "Иванов Иван", "8 912 345-67-89", "", "", "", "01.05.2026"],
            ["3", "Иванов Иван Павлович", "8 912 345-67-89", "", "", "", "01.02.2026"],
        )
    )
    active = list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))
    names = sorted(c.full_name for c in active)
    assert names == ["Иванов Иван Павлович", "Иванов Иван Петрович"]
    pair = session.scalar(select(Duplicate).where(Duplicate.status == "open"))
    assert {pair.candidate_a, pair.candidate_b} == {c.id for c in active}
