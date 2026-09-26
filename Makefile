.PHONY: run test test-fast lint demo eval eval-parse

run:
	uv run app

test:
	uv run pytest -q

# Без тестов с моделью поиска: секунды вместо минут.
test-fast:
	uv run pytest -q -m "not slow"

lint:
	uv run ruff check .
	uv run ruff format --check .

# Демо живёт в отдельной папке data/demo и пересоздаётся при каждом запуске.
demo:
	TA_DATA_DIR=data/demo uv run python -m app.demo
	TA_DATA_DIR=data/demo uv run app

# Набор проверки: Recall@40, Recall@200, nDCG@10 для вектора, BM25 и гибрида.
eval:
	uv run python -m eval.run data/eval

# Эталон разбора: 30 демо-резюме через модель из настроек, F1 по полям. Нужен ключ.
eval-parse:
	rm -rf data/parse-check
	uv run python -m eval.parse_check data/parse-check
