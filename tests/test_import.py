import zipfile
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app import jobs
from app.demo import write_docx
from app.importer import pipeline
from app.importer.pipeline import new_batch, start_import
from app.models import Candidate, ImportBatch, Job

CSV_HEADER = "Код;ФИО;Телефон;Почта;Город;Должность;Дата рождения;Дата обновления\n"


def write_csv(path, rows, encoding="cp1251"):
    path.write_text(CSV_HEADER + "".join(";".join(r) + "\n" for r in rows), encoding=encoding)
    return path


def minimal_pdf(text: str) -> bytes:
    """Одностраничный PDF со строкой латиницы — без сторонних библиотек."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_csv_import_normalizes_rows(tmp_path, do_import, active):
    table = write_csv(
        tmp_path / "crm.csv",
        [
            [
                "101",
                "иванов иван петрович",
                "8 (912) 345-67-89",
                "Ivan@Mail.ru",
                "Самара",
                "Инженер-технолог",
                "12.03.1985",
                "01.06.2026",
            ],
            [
                "102",
                "Ольга Сергеевна Петрова",
                "+7 903 111-22-33",
                "",
                "г. Казань",
                "Главный бухгалтер",
                "1990",
                "10.01.2024",
            ],
        ],
    )
    job = do_import(table)
    assert job.status == "done"
    assert job.progress == job.total == 2
    ivan, olga = active()
    assert ivan.external_id == "101"
    assert ivan.full_name == "Иванов Иван Петрович"
    assert ivan.phones == ["+79123456789"]
    assert ivan.emails == ["ivan@mail.ru"]
    assert ivan.city == "Самара"
    assert ivan.birth_year == 1985
    assert ivan.resume_date == date(2026, 6, 1)
    assert "Инженер-технолог" in ivan.raw_text
    assert olga.full_name == "Петрова Ольга Сергеевна"
    assert olga.city == "Казань"
    assert olga.stale is True


def test_xlsx_import(tmp_path, do_import, active):
    wb = Workbook()
    ws = wb.active
    ws.append(["ID кандидата", "Фамилия", "Имя", "Отчество", "Мобильный", "Email", "Город"])
    ws.append([7, "Сидоров", "Пётр", "Ильич", 79261234567, "PETR@YA.RU", "Москва"])
    path = tmp_path / "crm.xlsx"
    wb.save(path)
    job = do_import(path)
    assert job.status == "done"
    (c,) = active()
    assert (c.external_id, c.full_name, c.phones, c.emails, c.city) == (
        "7",
        "Сидоров Пётр Ильич",
        ["+79261234567"],
        ["petr@ya.ru"],
        "Москва",
    )


def test_folder_with_docx_and_txt_links_to_rows(tmp_path, do_import, active):
    table = write_csv(
        tmp_path / "crm.csv",
        [
            ["201", "Кузнецова Анна", "89001112233", "", "Пермь", "Логист", "", "01.08.2026"],
            ["202", "Орлов Максим Петрович", "89004445566", "", "Уфа", "Аналитик", "", ""],
        ],
    )
    folder = tmp_path / "resumes"
    folder.mkdir()
    write_docx(folder / "201.docx", ["Кузнецова Анна", "Опыт: диспетчер автопарка, 5 лет"])
    (folder / "Орлов Максим.txt").write_text(
        "Орлов Максим Петрович\nАналитик данных, SQL, Python\nmax@orlov.ru\n"
        "Резюме обновлено 15.07.2026",
        encoding="utf-8",
    )
    (folder / "новый.txt").write_text(
        "Белова Мария Андреевна\nМенеджер по продажам\n+7 999 000-11-22", encoding="utf-8"
    )
    job = do_import(table, [folder])
    assert job.status == "done"
    anna, max_, maria = active()
    assert "диспетчер автопарка" in anna.raw_text
    assert anna.source_file == "201.docx"
    assert "Python" in max_.raw_text
    assert max_.emails == ["max@orlov.ru"]
    assert max_.resume_date == date(2026, 7, 15)
    assert maria.full_name == "Белова Мария Андреевна"
    assert maria.phones == ["+79990001122"]
    assert maria.external_id is None


def test_zip_with_documents_and_pdf(tmp_path, do_import, active):
    archive = tmp_path / "resumes.zip"
    docx = tmp_path / "a.docx"
    write_docx(docx, ["Громов Илья Сергеевич", "Сварщик 6 разряда", "8 912 000 00 01"])
    with zipfile.ZipFile(archive, "w") as z:
        z.write(docx, "папка/Громов.docx")
        z.writestr("Лебедева.txt", "Лебедева Нина Павловна\nкладовщик\nnina@mail.ru")
        z.writestr("cv.pdf", minimal_pdf("Engineer resume, phone +7 912 000 00 02"))
    job = do_import(None, [archive])
    assert job.status == "done"
    names = {c.full_name for c in active()}
    assert {"Громов Илья Сергеевич", "Лебедева Нина Павловна"} <= names
    pdf = next(c for c in active() if c.source_file == "cv.pdf")
    assert pdf.phones == ["+79120000002"]


def test_interrupted_import_resumes_without_duplicating_rows(tmp_path, session, monkeypatch):
    rows = [
        [str(i), f"Тестов{i} Тест", f"8912000{i:04d}", "", "Омск", "", "", ""] for i in range(120)
    ]
    table = write_csv(tmp_path / "big.csv", rows)
    batch = new_batch(session, table, [])
    job = start_import(session, batch, batch.mapping)

    original = pipeline.process_row
    calls = {"n": 0}

    class Crash(Exception):
        pass

    def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 75:
            raise Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "process_row", crashing)
    with pytest.raises(Crash):
        pipeline.run_import(job.id)
    session.expire_all()
    interrupted = session.get(Job, job.id)
    assert interrupted.status == "running"
    assert 0 < interrupted.progress < 120

    monkeypatch.setattr(pipeline, "process_row", original)
    jobs.run_pending()
    session.expire_all()
    done = session.get(Job, job.id)
    assert done.status == "done"
    assert session.scalar(select(func.count(Candidate.id))) == 120
    assert session.get(ImportBatch, batch.id).rows_ok == 120


def test_csv_with_multiline_resume_cells(tmp_path, do_import, active):
    path = tmp_path / "crm.csv"
    path.write_text(
        'ID;ФИО;Резюме\n1;Орлов Пётр;"Опыт работы\n2019 — 2024: ООО «Ромашка», логист; склад"\n'
        '2;Белова Анна;"Навыки: Excel, 1С"\n',
        encoding="utf-8",
    )
    job = do_import(path)
    assert job.total == 2
    petr, anna = active()
    assert "ООО «Ромашка», логист; склад" in petr.raw_text
    assert anna.full_name == "Белова Анна"
