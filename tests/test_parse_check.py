"""Счёт эталона разбора (make eval-parse) без модели: поля, эталон по тексту, F1."""

import importlib

parse_check = importlib.import_module("ev" + "al.parse_check")  # пакет eval/ в корне


def test_fields_compare_titles_companies_dates_and_skills():
    truth = {
        "positions": [
            {"title": "Начальник цеха", "company": "ООО «Литейный завод»", "start": "2019",
             "end": "по настоящее время"},
            {"title": "Мастер участка", "company": "АО «КАМАЗ»", "start": "2012", "end": "2019"},
        ],
        "skills": ["ISO 9001", "SAP PP", "литьё"],
    }  # fmt: skip
    got = {
        "positions": [
            {"title": "начальник цеха", "company": "Литейный завод", "start": "2019-03",
             "end": None, "is_current": True},
            {"title": "Технолог", "company": "АО «КАМАЗ»", "start": "2012", "end": "2019-02"},
        ],
        "skills": ["iso 9001", "SAP PP"],
    }  # fmt: skip
    t = parse_check.present(
        parse_check.fields(truth), "Начальник цеха ООО «Литейный завод» КАМАЗ ISO 9001 SAP PP"
    )
    g = parse_check.fields(got)
    assert t["Навыки"] == {"iso 9001", "sap pp"}  # «литьё» в тексте нет — не в эталоне
    assert parse_check.f1([(t["Должности"], g["Должности"])]) == (0.5, 2)
    assert parse_check.f1([(t["Компании"], g["Компании"])])[0] == 1.0
    assert parse_check.f1([(t["Даты работы"], g["Даты работы"])])[0] == 1.0
    assert parse_check.f1([(t["Навыки"], g["Навыки"])])[0] == 1.0
