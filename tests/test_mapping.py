from app.importer.mapping import guess_mapping


def test_mapping_by_column_names():
    headers = [
        "ID",
        "ФИО",
        "Моб. телефон",
        "E-mail",
        "Город проживания",
        "Должность",
        "Компания",
        "Дата рождения",
        "Дата обновления",
        "Текст резюме",
        "Комментарий менеджера",
    ]
    assert guess_mapping(headers, []) == [
        "external_id",
        "full_name",
        "phone",
        "email",
        "city",
        "position",
        "company",
        "birth",
        "resume_date",
        "resume_text",
        "",
    ]


def test_mapping_split_name_columns():
    assert guess_mapping(["Фамилия", "Имя", "Отчество"], []) == [
        "last_name",
        "first_name",
        "middle_name",
    ]


def test_mapping_by_content_when_names_say_nothing():
    headers = ["Поле 1", "Поле 2", "Поле 3", "Поле 4", "Поле 5"]
    rows = [
        ["8 912 345-67-89", "ivan@mail.ru", "Иванов Иван Петрович", "12.03.1985", "01.06.2026"],
        ["+7 903 111 22 33", "olga@ya.ru", "Петрова Ольга", "1990-01-20", "15.08.2026"],
        ["9261234567", "", "Сидоров Пётр Ильич", "05.05.1979", "20.09.2025"],
    ]
    assert guess_mapping(headers, rows) == ["phone", "email", "full_name", "birth", "resume_date"]


def test_mapping_does_not_assign_same_field_twice_by_content():
    headers = ["А", "Б"]
    rows = [["ivan@mail.ru", "ivan@mail.ru"], ["o@ya.ru", "o@ya.ru"]]
    assert guess_mapping(headers, rows) == ["email", ""]
