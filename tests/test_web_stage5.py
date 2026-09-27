"""Этап 5, полировка по аудиту интерфейса (docs/AUDIT-ui.md): номера в именах тестов."""

import re

from sqlalchemy import func, select

from app.importer.mapping import guess_mapping
from app.jobs import run_pending
from app.models import Candidate, Job

CSV = (
    "ID;ФИО;Телефон;Почта;Город;Ожидания, руб;Комментарий рекрутера\n"
    "1;Иванов Иван Петрович;89123456789;ivan@mail.ru;Самара;150000;хороший, но дорогой\n"
    "2;Петрова Ольга;89031112233;olga@ya.ru;Казань;;\n"
    "3;Иванов И.П.;+7 912 345 67 89;;Самара;;\n"
)


def upload(client, name: str, data: bytes, field: str = "table"):
    return client.post("/upload", files={field: (name, data)}, follow_redirects=False)


# ── №1: битый файл ──────────────────────────────────────────────────────────


def test_1_broken_xlsx_answers_400_on_same_form(client):
    r = upload(client, "crm.xlsx", b"PK\x03\x04 not really a workbook")
    assert r.status_code == 400
    assert "Файл «crm.xlsx» не открылся: он повреждён или это не Excel." in r.text
    assert "Сохраните выгрузку заново и загрузите ещё раз." in r.text
    assert 'action="/upload"' in r.text  # та же форма, можно выбрать другой файл


def test_1_broken_zip_answers_400(client):
    r = upload(client, "resumes.zip", b"not a zip", field="resumes")
    assert r.status_code == 400 and "Архив «resumes.zip» не открылся" in r.text


# ── №2 и №11: колонки, метки, «Что получится» ──────────────────────────────


def test_11_salary_is_a_field_and_guessed_by_name():
    fields = guess_mapping(["ФИО", "Ожидания, руб", "Комментарий рекрутера"], [])
    assert fields == ["full_name", "salary", ""]


def test_11_columns_step_labels_outcome_and_button(client):
    r = upload(client, "crm.csv", CSV.encode("utf-8"))
    page = client.get(r.headers["location"]).text
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    assert "совпадает" in text and "не понял — выберите" in text
    assert "Что получится" in text
    assert "Загрузим 3 кандидатов" in text  # винительный: «трёх кандидатов»
    assert "Точных дублей объединим: 1" in text  # Иванов И.П. с тем же телефоном
    assert "Загрузить 3 кандидатов" in text
    assert "Начать разбор" not in text  # кнопка обещает ровно то, что случится


def test_11_outcome_recounts_when_column_changes(client):
    r = upload(client, "crm.csv", CSV.encode("utf-8"))
    batch = r.headers["location"]
    # ФИО, телефон и почту «не использовать» — связать строки не с кем
    form = {f"col_{i}": "" for i in range(7)}
    part = client.post(batch + "/preview", data=form).text
    assert "Пропустим 3\xa0строки" in part and "Загружать нечего" in part


def test_11_empty_csv_is_an_error_not_zero_rows(client):
    r = upload(client, "empty.csv", "ФИО;Телефон\n".encode())
    assert r.status_code == 400 and "В файле «empty.csv» нет строк с данными" in r.text


def test_11_nameless_rows_warned_and_can_be_skipped(client):
    csv = "Телефон;Город\n89120000001;Самара\n89120000002;Казань\n".encode()
    r = upload(client, "noname.csv", csv)
    batch = r.headers["location"]
    page = client.get(batch).text
    assert "В 2\xa0строках нет ФИО — загрузим как «Без имени»?" in page
    fields = re.findall(r'name="(col_\d+)"', page)
    form = dict(zip(fields, ["phone", "city"], strict=True))
    client.post(batch + "/start", data={**form, "nameless": "skip"})
    run_pending()
    from app import db

    with db.SessionLocal() as s:
        assert s.scalar(select(func.count(Candidate.id))) == 0


def test_2_unused_column_not_sent_to_model_screen(client):
    r = upload(client, "crm.csv", CSV.encode("utf-8"))
    batch = r.headers["location"]
    page = client.get(batch).text
    fields = re.findall(r'name="(col_\d+)"', page)
    mapping = ["external_id", "full_name", "phone", "email", "city", "salary", ""]
    client.post(batch + "/start", data=dict(zip(fields, mapping, strict=True)))
    run_pending()
    from app import db
    from app.parse import model_input

    with db.SessionLocal() as s:
        ivan = s.scalar(select(Candidate).where(Candidate.external_id == "1"))
        sent = model_input(ivan)
    assert "Ожидания, руб: 150000" in sent  # зарплата теперь поле, а не потеря
    assert "дорогой" not in sent and "Комментарий" not in sent


# ── №10 и №21: итоги загрузки ───────────────────────────────────────────────


def test_10_all_files_broken_is_not_done(client, tmp_path):
    r = client.post(
        "/upload",
        files=[("resumes", ("a.docx", b"broken")), ("resumes", ("b.pdf", b"broken"))],
        follow_redirects=False,
    )
    batch = r.headers["location"]
    client.post(batch + "/start", data={})
    run_pending()
    page = client.get(batch).text
    assert "Не получилось" in page and "Готово: " not in page
    from app import db

    with db.SessionLocal() as s:
        job = s.scalar(select(Job).where(Job.kind == "import"))
    assert "Не получилось" in client.get("/upload").text
    assert job.status == "done"
