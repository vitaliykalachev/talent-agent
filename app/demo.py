"""Демо-набор: 300 синтетических резюме, выгрузка «как из CRM» и папка с DOCX и TXT.

Запуск: `make demo` (или `TA_DATA_DIR=data/demo uv run python -m app.demo`).
Набор проходит через обычный конвейер импорта, потом разбирается на ответах модели,
записанных один раз с живой модели (`app/demo_data/llm/`, `make record-demo`),
получает смысловые отпечатки, вакансию «Оценивать каждую ночь» (`app/demo_vacancy.py`)
и один ночной прогон с отчётом на «Утре». Тесты берут ответы, которые пишет код по
фактам генератора (out/llm/answers.json), — они не зависят от записи.
"""

import csv
import json
import os
import random
import shutil
import sys
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from app import config, db, demo_record, demo_vacancy, night
from app.importer.pipeline import new_batch, start_import
from app.jobs import run_pending
from app.parse import start_parse, waiting_ids

PER_PROFESSION = 60
DUPLICATE_SHARE = 0.05
POSSIBLE = 3  # из них — возможные дубли без общих контактов
RECORDED = Path(__file__).parent / "demo_data" / "llm"  # ответы живой модели для демо
RECORDED_ON = date(2026, 9, 27)  # день записи: от него считаются даты в демо-резюме
STALE_SHARE = 0.20

MALE = [
    "Александр",
    "Дмитрий",
    "Максим",
    "Сергей",
    "Андрей",
    "Алексей",
    "Артём",
    "Илья",
    "Кирилл",
    "Михаил",
    "Никита",
    "Роман",
    "Егор",
    "Павел",
    "Владимир",
    "Денис",
    "Игорь",
    "Олег",
    "Виктор",
    "Евгений",
    "Антон",
    "Константин",
    "Юрий",
    "Григорий",
    "Василий",
]
FEMALE = [
    "Анна",
    "Мария",
    "Елена",
    "Ольга",
    "Наталья",
    "Татьяна",
    "Ирина",
    "Екатерина",
    "Светлана",
    "Юлия",
    "Анастасия",
    "Дарья",
    "Марина",
    "Людмила",
    "Ксения",
    "Алина",
    "Виктория",
    "Полина",
    "Вера",
    "Надежда",
    "Галина",
    "Евгения",
    "Софья",
    "Инна",
]
SURNAMES = [
    "Иванов",
    "Смирнов",
    "Кузнецов",
    "Попов",
    "Васильев",
    "Петров",
    "Соколов",
    "Михайлов",
    "Новиков",
    "Фёдоров",
    "Морозов",
    "Волков",
    "Алексеев",
    "Лебедев",
    "Семёнов",
    "Егоров",
    "Павлов",
    "Козлов",
    "Степанов",
    "Николаев",
    "Орлов",
    "Андреев",
    "Макаров",
    "Никитин",
    "Захаров",
    "Зайцев",
    "Соловьёв",
    "Борисов",
    "Яковлев",
    "Григорьев",
    "Романов",
    "Воробьёв",
    "Сергеев",
    "Кузьмин",
    "Фролов",
    "Александров",
    "Дмитриев",
    "Королёв",
    "Гусев",
    "Киселёв",
    "Ильин",
    "Максимов",
    "Поляков",
    "Сорокин",
    "Виноградов",
    "Ковалёв",
    "Белов",
    "Медведев",
    "Антонов",
    "Тарасов",
    "Жуков",
    "Баранов",
    "Филиппов",
    "Комаров",
    "Давыдов",
    "Беляев",
    "Герасимов",
    "Богданов",
    "Осипов",
    "Сидоров",
    "Матвеев",
    "Титов",
    "Марков",
    "Миронов",
    "Крылов",
    "Куликов",
    "Карпов",
    "Власов",
    "Мельников",
    "Денисов",
    "Гаврилов",
    "Тихонов",
]
PATRONYMICS = [
    ("Александрович", "Александровна"),
    ("Сергеевич", "Сергеевна"),
    ("Владимирович", "Владимировна"),
    ("Николаевич", "Николаевна"),
    ("Андреевич", "Андреевна"),
    ("Михайлович", "Михайловна"),
    ("Викторович", "Викторовна"),
    ("Юрьевич", "Юрьевна"),
    ("Игоревич", "Игоревна"),
    ("Олегович", "Олеговна"),
    ("Анатольевич", "Анатольевна"),
    ("Геннадьевич", "Геннадьевна"),
    ("Павлович", "Павловна"),
    ("Борисович", "Борисовна"),
    ("Евгеньевич", "Евгеньевна"),
    ("Петрович", "Петровна"),
]
CITIES = [
    "Москва",
    "Санкт-Петербург",
    "Казань",
    "Самара",
    "Нижний Новгород",
    "Екатеринбург",
    "Новосибирск",
    "Уфа",
    "Пермь",
    "Ульяновск",
    "Саратов",
    "Тольятти",
    "Челябинск",
    "Воронеж",
    "Ростов-на-Дону",
    "Краснодар",
    "Тюмень",
    "Ярославль",
]
UNIVERSITIES = [
    "КНИТУ-КАИ",
    "Самарский университет",
    "УрФУ",
    "МГТУ им. Баумана",
    "СПбПУ",
    "Финансовый университет",
    "РЭУ им. Плеханова",
    "НГТУ",
    "ВШЭ",
    "ПНИПУ",
    "КФУ",
    "УГАТУ",
    "Высшая школа экономики",
    "СГЭУ",
    "ТГУ",
]

PROFESSIONS = {
    "производство": {
        "titles": [
            "Начальник цеха",
            "Инженер-технолог",
            "Главный инженер",
            "Мастер участка",
            "Директор по производству",
            "Инженер по качеству",
            "Технолог литейного производства",
            "Начальник участка механообработки",
            "Инженер-конструктор",
            "Главный механик",
        ],
        "companies": [
            "ПАО «КАМАЗ»",
            "АО «АвтоВАЗ»",
            "ООО «Литейный завод Поволжье»",
            "АО «Уралвагонзавод»",
            "ООО «ТехноПласт»",
            "ПАО «Северсталь»",
            "ООО «Завод металлоконструкций»",
            "АО «ОДК-Кузнецов»",
            "ООО «Хлебозавод №3»",
        ],
        "duties": [
            "организовал участок литья под давлением с нуля",
            "запустил новый цех на 120 человек",
            "внедрил бережливое производство, снизил брак на 18 %",
            "руководил сменой из 45 рабочих",
            "подготовил производство к сертификации ISO 9001",
            "разработал техпроцессы механообработки",
            "провёл модернизацию линии окраски",
            "сократил простои оборудования на 25 %",
            "вёл планирование и выполнение плана выпуска",
        ],
        "skills": [
            "бережливое производство",
            "SolidWorks",
            "КОМПАС-3D",
            "ISO 9001",
            "SAP PP",
            "техпроцессы",
            "5S",
            "TPM",
            "литьё",
            "сварка",
            "охрана труда",
        ],
    },
    "финансы": {
        "titles": [
            "Главный бухгалтер",
            "Финансовый контролёр",
            "Финансовый директор",
            "Бухгалтер по расчёту заработной платы",
            "Экономист",
            "Аналитик ФП&А",
            "Казначей",
            "Бухгалтер-калькулятор",
            "Руководитель отдела отчётности",
        ],
        "companies": [
            "ООО «Технологии учёта»",
            "АО «Сбербанк Лизинг»",
            "ПАО «Татнефть»",
            "ООО «Торговый дом Волга»",
            "Группа компаний «Эталон»",
            "АО «Альфа-Банк»",
            "ООО «Аудит-Консалт»",
            "ПАО «Россети»",
        ],
        "duties": [
            "закрытие месяца и года, сдача отчётности в ФНС",
            "подготовка отчётности по МСФО",
            "построил управленческий учёт в 1С",
            "сократил срок закрытия месяца с 12 до 5 дней",
            "бюджетирование и контроль исполнения бюджета",
            "прошёл 4 налоговые проверки без доначислений",
            "казначейство и платёжный календарь",
            "автоматизировал расчёт зарплаты на 900 человек",
        ],
        "skills": [
            "1С:Бухгалтерия",
            "1С:ЗУП",
            "МСФО",
            "РСБУ",
            "Excel",
            "Power BI",
            "налоговый учёт",
            "бюджетирование",
            "SAP FI",
            "консолидация",
        ],
    },
    "продажи": {
        "titles": [
            "Менеджер по продажам",
            "Руководитель отдела продаж",
            "Менеджер по работе с ключевыми клиентами",
            "Коммерческий директор",
            "Региональный менеджер",
            "Менеджер по развитию бизнеса",
            "Торговый представитель",
            "Менеджер по тендерам",
        ],
        "companies": [
            "ООО «ПромСнаб»",
            "АО «Мираторг»",
            "ООО «Эльдорадо»",
            "ПАО «МТС»",
            "ООО «Строительный двор»",
            "ООО «Дистрибьюция Юг»",
            "АО «Р-Фарм»",
            "ООО «Белая Дача»",
        ],
        "duties": [
            "выполнение плана продаж на 115 % три года подряд",
            "вывел продукт в 12 регионов",
            "собрал отдел продаж из 8 человек с нуля",
            "вёл 40 ключевых клиентов B2B",
            "увеличил выручку направления в 2 раза",
            "участвовал в тендерах по 44-ФЗ и 223-ФЗ",
            "открыл 3 новых дилерских центра",
            "холодные звонки и встречи с ЛПР",
        ],
        "skills": [
            "B2B-продажи",
            "amoCRM",
            "Битрикс24",
            "переговоры",
            "тендеры",
            "холодные звонки",
            "управление командой",
            "KPI",
            "дистрибуция",
        ],
    },
    "ИТ": {
        "titles": [
            "Python-разработчик",
            "Java-разработчик",
            "Руководитель группы разработки",
            "Системный аналитик",
            "DevOps-инженер",
            "Тестировщик",
            "Frontend-разработчик",
            "Администратор 1С",
            "Data Scientist",
            "Системный администратор",
        ],
        "companies": [
            "ООО «Яндекс»",
            "АО «Тинькофф»",
            "ООО «СКБ Контур»",
            "ООО «ИТ-Сервис»",
            "ООО «Лаборатория Касперского»",
            "ООО «Софтлайн»",
            "ООО «Ростелеком Информационные Технологии»",
            "ООО «ИнфоТех»",
        ],
        "duties": [
            "разработка микросервисов на Python и FastAPI",
            "перевёл монолит на микросервисы",
            "настроил CI/CD на GitLab, время выкладки сократилось с часа до 10 минут",
            "вёл команду из 6 разработчиков",
            "писал требования и постановки задач",
            "поддержка и доработка 1С:ERP",
            "построил модель прогноза спроса",
            "автоматизировал регрессионное тестирование",
        ],
        "skills": [
            "Python",
            "Java",
            "PostgreSQL",
            "Docker",
            "Kubernetes",
            "Git",
            "React",
            "SQL",
            "1С",
            "Linux",
            "Kafka",
            "pandas",
        ],
    },
    "логистика": {
        "titles": [
            "Начальник склада",
            "Логист",
            "Руководитель отдела логистики",
            "Менеджер по ВЭД",
            "Диспетчер автопарка",
            "Специалист по закупкам",
            "Директор по логистике",
            "Кладовщик",
            "Менеджер по транспортной логистике",
        ],
        "companies": [
            "ООО «Деловые Линии»",
            "ООО «ПЭК»",
            "ООО «Вайлдберриз»",
            "ООО «Озон»",
            "АО «Почта России»",
            "ООО «Магнит»",
            "ООО «Монополия»",
            "ООО «Логистик-Центр»",
        ],
        "duties": [
            "руководил складом класса А на 20 000 м²",
            "внедрил WMS-систему",
            "оптимизировал маршруты доставки, затраты ниже на 14 %",
            "организовал таможенное оформление импорта из Китая",
            "управлял автопарком из 60 машин",
            "запустил новый распределительный центр",
            "инвентаризации без расхождений",
            "вёл закупки у 50 поставщиков",
        ],
        "skills": [
            "WMS",
            "1С:УТ",
            "ВЭД",
            "таможенное оформление",
            "складская логистика",
            "маршрутизация",
            "Excel",
            "закупки",
            "SAP MM",
        ],
    },
}


def write_docx(path: Path, paragraphs: list[str]) -> None:
    """Минимальный DOCX без сторонних библиотек: хватает для markitdown и Word."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>' for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def _person(rnd: random.Random) -> dict:
    female = rnd.random() < 0.45
    first = rnd.choice(FEMALE if female else MALE)
    last = rnd.choice(SURNAMES) + ("а" if female else "")
    middle = rnd.choice(PATRONYMICS)[1 if female else 0]
    return {"last": last, "first": first, "middle": middle, "female": female}


def _phone(rnd: random.Random, taken: set) -> str:
    while True:
        phone = "9" + "".join(rnd.choice("0123456789") for _ in range(9))
        if phone not in taken:
            taken.add(phone)
            return phone


def _fmt_phone(digits10: str, style: int) -> str:
    d = digits10
    return [
        f"+7 ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:]}",
        f"8{d}",
        f"8 {d[:3]} {d[3:6]} {d[6:8]} {d[8:]}",
        f"+7{d}",
        f"8-{d[:3]}-{d[3:6]}-{d[6:8]}-{d[8:]}",
    ][style]


TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def _email(rnd: random.Random, p: dict, taken: set) -> str:
    base = f"{p['first'][0]}.{p['last']}".lower().translate(TRANSLIT)
    while True:
        domain = rnd.choice(["mail.ru", "yandex.ru", "gmail.com", "bk.ru", "inbox.ru"])
        email = f"{base}{rnd.choice(['', str(rnd.randint(1, 99))])}@{domain}"
        if email not in taken:
            taken.add(email)
            return email


def _resume_text(
    rnd: random.Random,
    person: dict,
    prof: str,
    title: str,
    company: str,
    city: str,
    birth: date,
    updated: date,
    style: str,
    facts: dict,
    duties: list[str] | None = None,
) -> str:
    """Текст резюме; в facts — то, что из него должна понять модель (для записанных ответов)."""
    spec = PROFESSIONS[prof]
    name = f"{person['last']} {person['first']} {person['middle']}"
    years = rnd.randint(2, 22)
    sampled = rnd.sample(spec["duties"], k=min(len(spec["duties"]), rnd.randint(2, 5)))
    duties = duties or sampled
    skills = rnd.sample(spec["skills"], k=rnd.randint(3, 6))
    facts.update(years=years, duties=duties, skills=skills, positions=[], education=None)
    if style == "short":
        facts["positions"] = [
            {"title": title, "source_lines": {"__lines__": f"{title}, опыт {years} лет"}}
        ]
        return f"{name}\n{title}, опыт {years} лет, {city}. {duties[0].capitalize()}."
    if style == "messy":
        facts["positions"] = [
            {
                "title": title,
                "company": company,
                "source_lines": {"__lines__": f"{title.lower()} {company}"},
            }
        ]
        return (
            f"{name.upper()}\n{title.lower()} {company} стаж {years} лет "
            f"{', '.join(duties)} навыки {' '.join(skills)} г {city}"
        )
    prev_company = rnd.choice([c for c in spec["companies"] if c != company])
    start = updated.year - rnd.randint(1, min(years, 8))
    prev_start, prev_title = start - rnd.randint(2, 6), rnd.choice(spec["titles"])
    university = rnd.choice(UNIVERSITIES)
    facts["positions"] = [
        {
            "title": title,
            "company": company,
            "start": str(start),
            "end": "по настоящее время",
            "source_lines": {"__lines__": f"{start} — по настоящее время: {company}, {title}"},
        },
        {
            "title": prev_title,
            "company": prev_company,
            "start": str(prev_start),
            "end": str(start),
            "source_lines": {"__lines__": f"{prev_start} — {start}: {prev_company}, {prev_title}"},
        },
    ]
    facts["education"] = {"institution": university, "year": birth.year + 22}
    facts["relocation"] = "relocation_possible" if style == "long" else "unknown"
    lines = [
        name,
        f"Желаемая должность: {title}",
        f"Город: {city}",
        f"Дата рождения: {birth.strftime('%d.%m.%Y')}",
        "",
        "Опыт работы",
        f"{start} — по настоящее время: {company}, {title}",
        *[f"— {d}" for d in duties],
        f"{prev_start} — {start}: {prev_company}, {prev_title}",
        f"— {rnd.choice(spec['duties'])}",
        "",
        f"Навыки: {', '.join(skills)}",
        f"Образование: {university}, {birth.year + 22}",
        f"Общий стаж: {years} лет",
    ]
    if style == "long":
        lines += [
            "",
            "О себе",
            "Ответственный, умею работать в команде и доводить проекты до результата. "
            "Готов к командировкам, рассматриваю переезд.",
        ]
    lines.append(f"Резюме обновлено {updated.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


# Витринный кандидат для критерия приёмки из раздела 11 плана: запрос «запуск цеха
# с нуля» находит резюме с «организовал участок литья» — слов «запуск» и «цех» в нём нет.
SHOWCASE_TITLE = "Начальник литейного участка"
SHOWCASE_DUTIES = [
    "организовал участок литья алюминиевых корпусов с нуля: подобрал оборудование, набрал бригаду",
    "вывел участок на плановую мощность за восемь месяцев",
]


# Кандидат с длинным резюме: редкий термин (марка станка) стоит на второй странице,
# за пределами поисковой карточки. Его находит BM25 по полному тексту (раздел 8 плана).
RARE_TERM = "Hermle C42U"
SECOND_PAGE = [
    "",
    "Ранний опыт",
    *[
        f"— {year}: {what}"
        for year, what in [
            (2004, "технолог механического участка, подготовка управляющих программ"),
            (2005, "освоение токарной группы, наладка станков с ЧПУ"),
            (2006, "расчёт норм времени на операции механообработки"),
            (2007, "подбор режущего инструмента, снижение расхода пластин"),
            (2008, "внедрение карт наладки на участке фрезерной обработки"),
            (2009, "обучение операторов, аттестация рабочих мест"),
        ]
    ],
    "",
    "Проекты",
    *[
        f"— проект {n}: перенос номенклатуры деталей на новое оборудование, партия {n * 40} шт."
        for n in range(1, 25)
    ],
    "",
    "Оборудование, с которым работал",
    f"— пятиосевой обрабатывающий центр {RARE_TERM}, токарные станки с ЧПУ, координатно-"
    "измерительная машина",
]


def recorded_answer(p: dict, facts: dict) -> dict:
    """Ответ модели для записи в фикстуру: то, что честный разбор нашёл бы в тексте."""
    duties = facts["duties"]
    return {
        "desired_position": p["title"],
        "positions": facts["positions"],
        "skills": facts["skills"],
        "total_years": facts["years"],
        "city": p["city"],
        "relocation": facts.get("relocation", "unknown"),
        "salary_expect": None,
        "languages": [],
        "education": [facts["education"]] if facts["education"] else [],
        "summary": (
            f"{p['title']} с опытом {facts['years']} лет. "
            f"{duties[0][0].upper()}{duties[0][1:]}"
            f"{'; ' + duties[1] if len(duties) > 1 else ''}. "
            f"Сильные стороны: {', '.join(facts['skills'][:3])}."
        ),
        "summary_quote": duties[0],
        "resume_date": p["updated"].isoformat(),
    }


def generate(out: Path, seed: int = 42, today: date | None = None) -> tuple[Path, Path]:
    """Создаёт out/crm_export.csv, out/resumes/ (DOCX и TXT) и out/llm/answers.json —
    записанные ответы модели для разбора без ключа; возвращает пути к таблице и резюме."""
    rnd = random.Random(seed)
    today = today or date.today()
    if out.exists():
        shutil.rmtree(out)
    resumes = out / "resumes"
    resumes.mkdir(parents=True)

    people = []
    ext_id = 10000
    for prof in PROFESSIONS:
        for _ in range(PER_PROFESSION):
            ext_id += rnd.randint(1, 7)
            people.append({"prof": prof, "ext_id": str(ext_id), **_person(rnd)})
    rnd.shuffle(people)
    stale_ids = {id(p) for p in rnd.sample(people, int(len(people) * STALE_SHARE))}

    showcase = next(p for p in people if p["prof"] == "производство" and id(p) not in stale_ids)
    long_one = None
    short_names = Counter((p["last"], p["first"]) for p in people)
    answers = []
    phones: set[str] = set()
    emails: set[str] = set()
    rows = []
    for p in people:
        spec = PROFESSIONS[p["prof"]]
        p.update(
            title=rnd.choice(spec["titles"]),
            company=rnd.choice(spec["companies"]),
            city=rnd.choice(CITIES),
            phone=_phone(rnd, phones),
            email=_email(rnd, p, emails),
            birth=date(rnd.randint(1965, 2001), rnd.randint(1, 12), rnd.randint(1, 28)),
        )
        if id(p) in stale_ids:
            p["updated"] = today - timedelta(days=rnd.randint(560, 1500))
        else:
            p["updated"] = today - timedelta(days=rnd.randint(1, 500))
        style = rnd.choices(["short", "messy", "normal", "long"], weights=[2, 1, 5, 2])[0]
        duties = None
        if p is showcase:
            p["title"], style, duties = SHOWCASE_TITLE, "normal", SHOWCASE_DUTIES
        facts: dict = {}
        text = _resume_text(
            rnd,
            p,
            p["prof"],
            p["title"],
            p["company"],
            p["city"],
            p["birth"],
            p["updated"],
            style,
            facts,
            duties,
        )
        if (
            long_one is None
            and p is not showcase
            and p["prof"] == "производство"
            and id(p) not in stale_ids
            and style in ("normal", "long")
        ):
            long_one, text = p, text + "\n" + "\n".join(SECOND_PAGE)
        answers.append({"match": f"ID: {p['ext_id']}\n", "response": recorded_answer(p, facts)})
        p["text"], p["facts"] = text, facts
        where = rnd.choices(["csv", "docx", "txt"], weights=[6, 2, 2])[0]
        if where == "txt" and short_names[(p["last"], p["first"])] > 1:
            where = "docx"  # по неоднозначному имени файл не связать — называем по ID
        p["where"] = where
        full = f"{p['last']} {p['first']} {p['middle']}"
        if where == "docx":
            write_docx(resumes / f"{p['ext_id']}.docx", text.splitlines())
            text = ""
        elif where == "txt":  # файл назван по ФИО — связь со строкой по имени
            (resumes / f"{p['last']} {p['first']}.txt").write_text(text, encoding="utf-8")
            text = ""
        rows.append(
            {
                "ID": p["ext_id"],
                "ФИО": full if rnd.random() < 0.8 else f"{p['first']} {p['middle']} {p['last']}",
                "Телефон": _fmt_phone(p["phone"], rnd.randint(0, 4)),
                "E-mail": p["email"],
                "Город": p["city"],
                "Должность": p["title"],
                "Компания": p["company"],
                "Дата рождения": p["birth"].strftime("%d.%m.%Y"),
                "Дата обновления": p["updated"].strftime("%d.%m.%Y"),
                "Резюме": text,
            }
        )

    # 5 % дублей: тот же человек, другое оформление телефона или почты, более старая запись.
    # Первые POSSIBLE из них — без общих контактов: то же ФИО, город и дата рождения, но
    # телефон и почта другие. Сами не склеиваются, попадают в очередь «Похоже на дубль».
    fresh_people = [p for p in people if id(p) not in stale_ids]
    doubled = rnd.sample(fresh_people, int(len(people) * DUPLICATE_SHARE))
    # файл, названный по ФИО, при однофамильце в выгрузке не связать — таких не берём
    possible = {id(p) for p in [p for p in doubled if p["where"] != "txt"][:POSSIBLE]}
    for p in doubled:
        ext_id += rnd.randint(1, 7)
        if id(p) in possible:
            rows.append(
                {
                    "ID": str(ext_id),
                    "ФИО": f"{p['last']} {p['first']} {p['middle']}",
                    "Телефон": _fmt_phone(_phone(rnd, phones), 0),
                    "E-mail": _email(rnd, p, emails),
                    "Город": p["city"],
                    "Должность": p["title"],
                    "Компания": p["company"],
                    "Дата рождения": p["birth"].strftime("%d.%m.%Y"),
                    "Дата обновления": (p["updated"] - timedelta(days=200)).strftime("%d.%m.%Y"),
                    "Резюме": p["text"],
                }
            )
            answers.append({"match": f"ID: {ext_id}\n", "response": recorded_answer(p, p["facts"])})
            continue
        by_phone = rnd.random() < 0.6
        rows.append(
            {
                "ID": str(ext_id),
                "ФИО": f"{p['last'].upper()} {p['first']}",
                "Телефон": _fmt_phone(p["phone"], rnd.randint(0, 4)) if by_phone else "",
                "E-mail": p["email"].upper() if not by_phone else _email(rnd, p, emails),
                "Город": p["city"].lower(),
                "Должность": rnd.choice(PROFESSIONS[p["prof"]]["titles"]),
                "Компания": rnd.choice(PROFESSIONS[p["prof"]]["companies"]),
                "Дата рождения": "",
                "Дата обновления": (p["updated"] - timedelta(days=rnd.randint(30, 400))).strftime(
                    "%d.%m.%Y"
                ),
                "Резюме": "",
            }
        )
    rnd.shuffle(rows)

    table = out / "crm_export.csv"
    with table.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    (out / "llm").mkdir()
    (out / "llm" / "answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return table, resumes


def build(target: Path, record: bool = False) -> None:
    """Чистая демо-база в `target` (раздел 9 плана): импорт, разбор, отпечатки, вакансия
    «Оценивать каждую ночь» и один ночной прогон, который её оценивает и оставляет отчёт
    на «Утре».

    Ответы модели — записанные с живой модели (`app/demo_data/llm/`), ключ не нужен.
    `record=True` — один раз прогнать то же самое через настоящий сервис из настроек и
    окружения и записать ответы туда (`make record-demo`)."""
    db.configure(target)
    if (db.data_dir / "app.db").exists():  # демо всегда начинается с чистой базы
        db.engine.dispose()
        for name in ("app.db", "app.db-wal", "app.db-shm"):
            (db.data_dir / name).unlink(missing_ok=True)
        shutil.rmtree(db.data_dir / "uploads", ignore_errors=True)
        db.configure(db.data_dir)
    # Дата фиксирована: записанные ответы совпадают с текстом резюме слово в слово.
    table, resumes = generate(db.data_dir / "source", today=RECORDED_ON)
    with db.SessionLocal() as session:
        batch = new_batch(session, table, [resumes])
        start_import(session, batch, batch.mapping)
    run_pending()
    recorder = demo_record.Recorder(RECORDED) if record else None
    if recorder:
        recorder.attach()
    else:  # записанные ответы; в «Настройках» можно переключиться на настоящий сервис
        config.save({"llm_provider": "mock", "llm_fixtures": str(RECORDED)})
    with db.SessionLocal() as session:
        start_parse(session, waiting_ids(session))
    run_pending()
    vacancy = demo_vacancy.create()
    night.enqueue()
    run_pending()
    if recorder:
        recorder.extra(vacancy)  # запас на случай другого порядка поиска на другой машине
        recorder.save(RECORDED)


def main() -> None:
    target = Path(os.environ.get("TA_DATA_DIR") or "data/demo")
    if target.name != "demo" and (target / "app.db").exists():
        sys.exit(f"Демо пересоздаёт базу; {target} не похожа на демо-папку, не трогаю.")
    build(target, record="--record" in sys.argv)
    print(f"Демо готово: {db.data_dir / 'app.db'}")


if __name__ == "__main__":
    main()
