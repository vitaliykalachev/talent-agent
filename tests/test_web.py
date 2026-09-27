import re
import time

import pytest

CSV = (
    "ID;ФИО;Телефон;Почта;Город;Должность;Дата обновления\n"
    "1;Иванов Иван Петрович;89123456789;ivan@mail.ru;Самара;Инженер-технолог;01.08.2026\n"
    "2;Петрова Ольга;89031112233;olga@ya.ru;Казань;Главный бухгалтер;01.01.2023\n"
    "3;Иванов И.П.;+7 912 345 67 89;;Самара;Технолог;01.02.2024\n"
)


def upload_and_import(client) -> int:
    r = client.post(
        "/upload",
        files={"table": ("crm.csv", CSV.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    batch_url = r.headers["location"]
    page = client.get(batch_url)
    assert page.status_code == 200
    assert 'value="phone" selected' in page.text  # догадка показана пользователю
    fields = re.findall(r'name="(col_\d+)"', page.text)
    form = {
        f: v
        for f, v in zip(
            fields,
            ["external_id", "full_name", "phone", "email", "city", "position", "resume_date"],
            strict=True,
        )
    }
    r = client.post(batch_url + "/start", data=form, follow_redirects=False)
    assert r.status_code == 303
    job_id = int(re.search(r'id="job-(\d+)"', client.get(batch_url).text).group(1))
    for _ in range(50):
        fragment = client.get(f"/jobs/{job_id}")
        if "Готово" in fragment.text:
            return job_id
        time.sleep(0.2)
    pytest.fail("фоновый импорт не дошёл до done")


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/upload",
        "/candidates",
        "/vacancies",
        "/morning",
        "/settings",
        "/static/app.css",
        "/static/htmx.min.js",
    ],
)
def test_screens_answer_200_on_empty_base(client, url):
    assert client.get(url).status_code == 200


def test_background_import_reaches_done_and_counts_rows(client):
    job_id = upload_and_import(client)
    fragment = client.get(f"/jobs/{job_id}").text
    assert "Готово: 3\xa0строки" in fragment  # аудит №21: что именно прочитали
    assert "every 2s" not in fragment  # опрос прекращается после завершения
    home = client.get("/").text
    assert re.search(r'data-stat="candidates">\s*2\s*<', home)
    assert re.search(r'data-stat="duplicates">\s*1\s*<', home)
    assert "Объединено автоматически" in home
    merged = client.get("/duplicates/merged").text
    assert "Иванов Иван Петрович" in merged and "Отменить объединение" in merged


def test_candidates_search_filters_and_card(client):
    upload_and_import(client)
    page = client.get("/candidates", params={"q": "бухгалтер"}).text
    assert "Петрова Ольга" in page and "Иванов Иван" not in page
    page = client.get("/candidates", params={"city": "Самара"}).text
    assert "Иванов Иван Петрович" in page and "Петрова" not in page
    page = client.get("/candidates", params={"fresh": "stale"}).text
    assert "Петрова Ольга" in page and "Иванов Иван Петрович" not in page
    partial = client.get("/candidates", params={"q": "иванов"}, headers={"HX-Request": "true"})
    assert "<html" not in partial.text and "Иванов Иван Петрович" in partial.text

    card_id = re.search(r'href="/candidates/(\d+)"', page).group(1)
    card = client.get(f"/candidates/{card_id}")
    assert card.status_code == 200
    assert "+79031112233" in card.text


def test_duplicate_card_links_to_main_record(client):
    upload_and_import(client)
    card = client.get("/candidates/3").text
    assert 'href="/candidates/1"' in card
    assert client.get("/candidates/999").status_code == 404
