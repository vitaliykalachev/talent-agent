"""Экраны этапа 4: «Настройки», метки и история загрузок, экраны без запрещённых слов."""

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.main import create_app
from app.models import Candidate, Feedback, ImportBatch, Vacancy
from app.web.present import batch_label

BANNED = ("провайдер", "токен", "эмбеддинг", "парсинг", "cron", "prompt", "llm", "smtp")


@pytest.fixture
def web(session):
    return TestClient(create_app())


def screen(web, url) -> str:
    r = web.get(url)
    assert r.status_code == 200, url
    text = re.sub(r"<[^>]+>", " ", r.text).lower()
    for word in BANNED:
        assert word not in text, (url, word)
    return r.text


def test_settings_blocks_and_night_schedule_saved(web, session):
    text = screen(web, "/settings")
    for words in (
        "Каждую ночь в 02:00",
        "По будням в 02:00",
        "Компьютер должен быть включён",
        "Почта для отчёта",
        "Отправить пробное письмо",
        "Что уходит модели",
        "[ИМЯ]",
        "[ТЕЛЕФОН]",
        "Агент запомнил",
        "Подключение к ИИ",
    ):
        assert words in text, words
    assert "Петров" not in text and "912 345" not in text  # образец обезличен
    r = web.post("/settings", data={"night_time": "03:30", "night_days": "weekdays"})
    assert r.status_code == 200 and "Сохранено." in r.text
    assert (config.get("night_time"), config.get("night_days")) == ("03:30", "weekdays")
    assert "По будням в 03:30" in r.text
    r = web.post("/settings", data={"night_time": "3 часа ночи", "night_days": "daily"})
    assert "Время укажите как 02:00." in r.text and config.get("night_time") == "03:30"


def test_mail_password_kept_and_never_shown(web, session):
    web.post(
        "/settings",
        data={
            "smtp_host": "smtp.yandex.ru",
            "smtp_port": "465",
            "smtp_user": "hr@example.ru",
            "smtp_password": "секретный-пароль",
            "smtp_to": "boss@example.ru",
        },
    )
    assert config.get("smtp_password") == "секретный-пароль"
    text = screen(web, "/settings")
    assert "секретный-пароль" not in text and "Пароль задан" in text
    web.post("/settings", data={"smtp_host": "smtp.yandex.ru", "smtp_password": ""})
    assert config.get("smtp_password") == "секретный-пароль"  # пустое поле не стирает


def test_test_mail_without_server_says_what_to_fill(web):
    r = web.post("/settings/mail-test")
    assert "Заполните адрес почтового сервера и адрес получателя." in r.text


def test_agent_memory_in_settings_delete_and_forget_all(web, session):
    v = Vacancy(title="Технолог", description="технолог")
    session.add(v)
    session.commit()
    session.add_all(
        [
            Feedback(vacancy_id=v.id, target="reason", text="Литьё", kind="irrelevant"),
            Feedback(target="reason", text="ISO 9001", kind="not_in_resume", scope="all"),
        ]
    )
    session.commit()
    text = screen(web, "/settings")
    assert "«Литьё» — для этой вакансии неважно, вакансия «Технолог»" in text
    assert "«ISO 9001» — этого нет в резюме, для всех вакансий" in text
    first = session.scalar(select(Feedback).order_by(Feedback.id))
    web.post(f"/feedback/{first.id}/delete", data={"back": "/settings#memory"})
    assert "«Литьё»" not in screen(web, "/settings")
    r = web.post("/feedback/clear", follow_redirects=False)
    assert r.status_code == 303
    assert session.scalar(select(Feedback)) is None


def test_batch_label_filter_and_history(web, session):
    b1 = ImportBatch(file_name="crm_export.xlsx", created_at=datetime(2026, 9, 26, 14, 5))
    b2 = ImportBatch(file_name=None, created_at=datetime(2026, 9, 27, 9, 0))
    session.add_all([b1, b2])
    session.commit()
    assert batch_label(b1) == "Загрузка 26 сен, 14:05 · crm_export.xlsx"
    assert batch_label(b2) == "Загрузка 27 сен, 09:00 · файлы резюме"
    session.add_all(
        [
            Candidate(full_name="Первый Из Выгрузки", raw_text="технолог", batch_id=b1.id),
            Candidate(full_name="Второй Из Папки", raw_text="технолог", batch_id=b2.id),
        ]
    )
    session.commit()
    text = screen(web, f"/candidates?batch={b1.id}")
    assert "Из загрузки…" in text
    assert "Первый Из Выгрузки" in text and "Второй Из Папки" not in text
    assert "загрузка 26 сен, 14:05 · crm_export.xlsx" in text  # плашка фильтра
    history = screen(web, "/upload")
    assert "История загрузок" in history
    assert "Загрузка 26 сен, 14:05 · crm_export.xlsx" in history
    assert f'href="/candidates?batch={b1.id}"' in history


def test_vacancy_can_be_evaluated_every_night(web, session):
    v = Vacancy(title="Технолог", description="технолог", requirements=[])
    session.add(v)
    session.commit()
    assert "Оценивать каждую ночь" in screen(web, f"/vacancies/{v.id}")
    web.post(f"/vacancies/{v.id}/conditions", data={"top_n": "40", "schedule": "1"})
    session.expire_all()
    assert session.get(Vacancy, v.id).schedule_enabled is True
    web.post(f"/vacancies/{v.id}/conditions", data={"top_n": "40"})
    session.expire_all()
    assert session.get(Vacancy, v.id).schedule_enabled is False


@pytest.mark.parametrize(
    "url", ["/", "/morning", "/settings", "/upload", "/duplicates", "/duplicates/merged"]
)
def test_screens_answer_without_banned_words(web, url):
    screen(web, url)
