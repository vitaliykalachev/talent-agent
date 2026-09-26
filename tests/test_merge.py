"""Возможные дубли: очередь, экран пары, объединение, отмена, «Нет, это разные люди»."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.importer.dedup import find_possible, merge_exact_duplicates
from app.main import create_app
from app.models import Candidate, Duplicate

HEADER = "ID,ФИО,Телефон,Email,Город,Дата рождения,Дата обновления\n"


def csv(tmp_path, *rows, name="crm.csv"):
    path = tmp_path / name
    path.write_text(HEADER + "".join(",".join(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def web(session):
    return TestClient(create_app())


@pytest.fixture
def namesakes(tmp_path, do_import, session):
    """То же ФИО, тот же город и год рождения, телефоны и почта разные."""
    do_import(
        csv(
            tmp_path,
            [
                "1",
                "Кузнецов Андрей Петрович",
                "8 912 111-22-33",
                "",
                "Самара",
                "05.04.1984",
                "01.02.2024",
            ],
            [
                "2",
                "Кузнецов Андрей Петрович",
                "8 912 999-88-77",
                "ak@mail.ru",
                "Самара",
                "05.04.1984",
                "01.08.2026",
            ],
        )  # fmt: skip
    )
    dup = session.scalar(select(Duplicate))
    return dup


def test_same_name_and_birth_year_is_a_possible_duplicate(namesakes, session):
    assert (namesakes.status, namesakes.confidence) == ("open", 0.7)
    assert (
        len(list(session.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))))) == 2
    )


def test_pair_screen_shows_cards_matches_and_reason(web, namesakes, session):
    r = web.get("/")
    assert "Похоже на дубль: 1" in r.text and "Проверить дубли" in r.text
    r = web.get(f"/duplicates/{namesakes.id}")
    assert r.status_code == 200
    assert "Почему похожи: то же ФИО, тот же город, год рождения 1984 у обоих, телефоны разные" in (
        r.text
    )
    assert r.text.count('class="same"') == 6  # ФИО, город, год — в обеих карточках
    for button in ("Объединить", "Нет, это разные люди", "Отложить"):
        assert button in r.text


def test_merge_takes_fresher_values_and_manual_choice_then_undo_restores(web, namesakes, session):
    old, new = session.scalars(select(Candidate).order_by(Candidate.id))
    old.city = "Тольятти"  # разное значение — можно выбрать руками
    session.commit()
    r = web.post(
        f"/duplicates/{namesakes.id}/merge", data={"city": str(old.id)}, follow_redirects=False
    )
    assert r.status_code == 303
    session.expire_all()
    assert old.duplicate_of == new.id and new.duplicate_of is None
    assert new.city == "Тольятти"  # выбрано руками из старой записи
    assert new.resume_date.year == 2026  # остальное — из более свежей
    assert set(new.phones) == {"+79121112233", "+79129998877"}
    card = web.get(f"/candidates/{new.id}").text
    assert "Отменить объединение" in card

    r = web.post(f"/duplicates/{namesakes.id}/unmerge", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/candidates/{new.id}"
    session.expire_all()
    assert old.duplicate_of is None and new.duplicate_of is None
    assert new.city == "Самара" and new.phones == ["+79129998877"]
    assert old.phones == ["+79121112233"] and old.city == "Тольятти"
    assert session.get(Duplicate, namesakes.id).status == "open"


def test_different_people_are_not_offered_again(web, namesakes, session, tmp_path, do_import):
    r = web.post(f"/duplicates/{namesakes.id}/reject", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/duplicates"
    assert find_possible(session) == 0
    assert "Непроверенных пар нет" in web.get("/duplicates").text
    assert "Похоже на дубль" not in web.get("/").text
    session.expire_all()
    assert session.get(Duplicate, namesakes.id).status == "rejected"
    assert session.scalar(select(Duplicate.id).where(Duplicate.id != namesakes.id)) is None


def test_postpone_moves_pair_to_the_end(web, namesakes, session):
    r = web.post(f"/duplicates/{namesakes.id}/postpone", follow_redirects=False)
    assert r.headers["location"] == f"/duplicates/{namesakes.id}"  # других пар нет
    session.expire_all()
    assert session.get(Duplicate, namesakes.id).postponed_at is not None
    assert "отложено" in web.get("/duplicates").text


def test_auto_merge_undo_restores_both_and_is_not_merged_again(web, tmp_path, do_import, session):
    do_import(
        csv(
            tmp_path,
            ["1", "Иванов Иван", "8 (912) 345-67-89", "old@mail.ru", "", "", "01.02.2023"],
            [
                "2",
                "Иванов Иван Петрович",
                "+7 912 345 67 89",
                "new@mail.ru",
                "Самара",
                "",
                "01.08.2026",
            ],
        )  # fmt: skip
    )
    old, new = session.scalars(select(Candidate).order_by(Candidate.id))
    dup = session.scalar(select(Duplicate))
    assert dup.status == "merged" and old.duplicate_of == new.id
    assert "Объединено автоматически" in web.get("/").text
    listing = web.get("/duplicates/merged").text
    assert "Иванов Иван Петрович" in listing and "Отменить объединение" in listing

    web.post(f"/duplicates/{dup.id}/unmerge")
    session.expire_all()
    assert old.duplicate_of is None
    assert new.emails == ["new@mail.ru"] and old.emails == ["old@mail.ru"]
    assert dup.status == "open"
    merge_exact_duplicates(session)  # следующая загрузка не склеивает их снова
    session.expire_all()
    assert old.duplicate_of is None
