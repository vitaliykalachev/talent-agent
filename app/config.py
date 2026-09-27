"""Настройки: таблица settings, запасной путь — переменные окружения и файл .env.

Файл .env ищется по пути из TA_ENV_FILE, по умолчанию — рядом с pyproject.toml.
Значение из «Настроек» важнее переменной окружения, переменная — важнее умолчания.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app import db
from app.models import Setting

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(os.environ.get("TA_ENV_FILE") or ROOT / ".env", override=False)

DEFAULTS = {
    "llm_provider": "anthropic",  # anthropic | openai | mock
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model_parse": "claude-haiku-4.5",
    "llm_model_eval": "claude-sonnet-5",
    "llm_fixtures": str(ROOT / "tests" / "fixtures" / "llm"),  # только для mock
    # Тарифы в долларах за миллион входных и выходных единиц текста.
    "price_parse_in": "1",
    "price_parse_out": "5",
    "price_eval_in": "2",
    "price_eval_out": "10",
    "usd_rub": "84",
    "embed_model": "sergeyzh/BERTA",  # запасной вариант: intfloat/multilingual-e5-base
}
ENV = {
    "llm_provider": "LLM_PROVIDER",
    "llm_base_url": "LLM_BASE_URL",
    "llm_api_key": "LLM_API_KEY",
    "llm_model_parse": "LLM_MODEL_PARSE",
    "llm_model_eval": "LLM_MODEL_EVAL",
    "llm_fixtures": "LLM_FIXTURES",
    "embed_model": "EMBED_MODEL",
}


def get(key: str) -> str:
    with db.SessionLocal() as s:
        row = s.get(Setting, key)
    if row and row.value:
        return row.value
    return os.environ.get(ENV.get(key, ""), "") or DEFAULTS[key]


def number(key: str) -> float:
    try:
        return float(get(key).replace(",", "."))
    except ValueError:
        return float(DEFAULTS[key])


def save(values: dict[str, str]) -> None:
    with db.SessionLocal() as s:
        for key, value in values.items():
            row = s.get(Setting, key) or Setting(key=key)
            row.value = value
            s.add(row)
        s.commit()
