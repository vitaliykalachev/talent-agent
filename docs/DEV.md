# Разработчику

Требования — `PLAN.md`, что сделано по этапам — `docs/REPORT-stage-*.md`.

## Структура

```
app/
  main.py            FastAPI, воркер задач и расписание стартуют вместе с приложением
  models.py, db.py   таблицы (раздел 5 плана), SQLite в TA_DATA_DIR, миграции alembic при старте
  config.py          настройки: таблица settings → переменные окружения → умолчания
  jobs.py            фоновые задачи: таблица jobs, воркер в том же процессе
  importer/          CSV/XLSX и файлы резюме, маппинг колонок, нормализация, дубли
  anonymize.py       обезличивание перед любым запросом к модели
  llm/               адаптер модели: Anthropic, OpenAI-совместимый, мок на фикстурах
  parse.py           разбор резюме по схеме CandidateProfile
  embed.py, search.py  смысловые отпечатки (BERTA) и гибридный поиск BM25 + вектор
  evaluate.py        портрет вакансии и оценка кандидатов, балл и категория считает код
  schedule.py        APScheduler: время и дни из настроек → cron, coalesce, запас 6 часов
  night.py           ночной прогон (задача night), итог в night_runs
  morning.py, mail.py  «Утро», строка о ближайшем прогоне, письмо без персональных данных
  demo.py, demo_vacancy.py, demo_record.py  демо-база и запись ответов модели
  demo_data/llm/     ответы живой модели для демо (make record-demo)
  web/               маршруты и шаблоны Jinja2, HTMX для обновления фрагментов
migrations/          alembic
tests/               pytest; модель — мок с записанными ответами (tests/fixtures/llm)
eval/                набор проверки поиска и эталон разбора
```

## Команды

| Команда | Что делает |
|---|---|
| `make run` | приложение на http://127.0.0.1:8000 с базой в `data/` |
| `make demo` | пересоздаёт `data/demo` и запускает приложение на ней, без ключа |
| `make record-demo` | прогоняет демо через настоящий сервис и докупает недостающие ответы в `app/demo_data/llm/`, записанные не трогает; нужен ключ; полная запись с нуля стоила около 206 ₽ |
| `make test` | все тесты |
| `make test-fast` | без тестов с моделью поиска (`-m "not slow"`) |
| `make lint` | `ruff check` и `ruff format --check` |
| `make eval` | Recall@40, Recall@200, nDCG@10 на наборе проверки |
| `make eval-parse` | F1 разбора на 30 демо-резюме через модель из настроек; нужен ключ |

Ключ для записи и живых проверок лежит в `.env` (образец — `.env.example`) или в файле, путь к которому задан в `TA_ENV_FILE`. Тесты `.env` не читают.

## Тесты

Каждое новое поведение — сначала тест. Модель в тестах — `MockLLM`: ответы в JSON-фикстурах ищутся по подстроке запроса, номера строк можно задать фрагментом текста (`{"__lines__": "…"}`). Тест-сторож в `tests/test_anonymize.py` проверяет, что в запросах к модели нет телефонов, почты и ФИО. Расписание проверяется с подменой времени: настоящий планировщик APScheduler без своего потока, часы двигает тест (`tests/test_night.py`).

## Как добавить сервис ИИ

1. В `app/llm/__init__.py` унаследуйте класс от `LLM` и реализуйте `_call(schema, system, user)`: запрос с принудительным вызовом инструмента `answer`, JSON-схема инструмента — `schema.model_json_schema()`; вернуть аргументы вызова (dict или строку JSON) и посчитать объём через `self._count(вход, выход)`.
2. Заполните `network_errors` (временные сбои — повторяются), `auth_errors` (неверный ключ) и `status_errors` (прочие ответы с кодом: 5xx повторяются, остальное — понятная ошибка).
3. Добавьте ветку в `get_llm()` и вариант в поле «Сервис» на экране «Настройки» (`app/web/templates/settings.html`).
4. Тест: адаптер на подменённом клиенте — схема ответа, повтор при сбое сети, неверный ключ (образец — `tests/test_llm.py`).

## Портативные сборки

Обе цели собирает `packaging/build.py`: свой Python из python-build-standalone, зависимости из `uv.lock` без dev-группы, код, веса модели поиска в `data/models` и демо-база. Ключа ИИ в сборках нет.

- **Windows** — `python packaging/build.py --target win64` на раннере `windows-latest`, workflow `portable-win` (вручную или тегом `portable-*`). Итог — `dist/kadrovyi-agent-win64.zip` с «Запустить.bat»; отчёт — [`docs/REPORT-portable.md`](REPORT-portable.md).
- **Mac с Apple Silicon** — на таком же маке, нужен только uv:

  ```bash
  uv run python packaging/build.py --target mac-arm64
  packaging/smoke-mac.sh dist/KadrovyAgent /tmp/kadr-home   # дымовой тест в чистом окружении
  ```

  Итог — `dist/kadrovyi-agent-mac.zip` (около 900 МБ) и установщик `dist/install-mac.sh`. Перед выкладкой в `install-mac.sh` подставляют адрес ZIP в строку `URL="__ПОДСТАВИТЬ__"`, а в `packaging/message-client-mac.txt` — адрес установщика. Нужна macOS 14 или новее: с этой версии собраны колёса torch. Пробы и замеры — [`docs/REPORT-portable-mac.md`](REPORT-portable-mac.md).
