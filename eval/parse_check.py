"""Эталон разбора: `make eval-parse`.

30 демо-резюме разбираются моделью из текущих настроек (сервис и ключ — «Настройки»
или .env) и сравниваются с истинными полями из генератора демо (`app/demo.py` пишет их
в source/llm/answers.json). Печатается F1 по должностям, компаниям, датам и навыкам,
время и объём запросов. Порога нет: числа идут в отчёт этапа.
"""

import json
import re
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app import config, db
from app.demo import generate
from app.importer.pipeline import new_batch, start_import
from app.jobs import run_pending
from app.models import Candidate, Job
from app.parse import company_key, estimate, run_parse, start_parse

SIZE = 30
NOW = "сейчас"


def _norm(text) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _year(value) -> str | None:
    match = re.search(r"\d{4}", str(value or ""))
    return match.group() if match else None


def fields(answer: dict) -> dict[str, set]:
    """Поля для сравнения: множества значений одного резюме."""
    positions = answer.get("positions") or []
    dates = set()
    for p in positions:
        current = p.get("is_current") or re.search(r"настоящ|н\.\s*в", str(p.get("end") or ""))
        start = _year(p.get("start"))
        if start:
            dates.add((start, NOW if current else _year(p.get("end"))))
    return {
        "Должности": {_norm(p.get("title")) for p in positions if p.get("title")},
        "Компании": {company_key(p.get("company")) for p in positions if p.get("company")},
        "Даты работы": dates,
        "Навыки": {_norm(s) for s in answer.get("skills") or []},
    }


def present(truth: dict[str, set], raw_text: str) -> dict[str, set]:
    """Эталон — только то, что есть в тексте: у коротких резюме генератор знает навыки,
    которых в тексте нет, и честный разбор их не найдёт."""
    text = _norm(raw_text)
    truth["Навыки"] = {s for s in truth["Навыки"] if s in text}
    truth["Компании"] = {k for k in truth["Компании"] if k and k in text}
    return truth


def f1(pairs: list[tuple[set, set]]) -> tuple[float, int]:
    tp = sum(len(truth & got) for truth, got in pairs)
    fp = sum(len(got - truth) for truth, got in pairs)
    fn = sum(len(truth - got) for truth, got in pairs)
    return (2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0), tp + fn


def main(target: str = "data/parse-check") -> None:
    root = Path(target)
    db.configure(root)
    if db.SessionLocal().scalar(select(Candidate.id).limit(1)):
        sys.exit(f"{root} уже занята; удалите папку, чтобы собрать эталон заново.")
    table, resumes = generate(root / "source")
    truth = {
        item["match"].removeprefix("ID: ").strip(): item["response"]
        for item in json.loads((root / "source" / "llm" / "answers.json").read_text("utf-8"))
    }
    with db.SessionLocal() as s:
        start_import(s, batch := new_batch(s, table, [resumes]), batch.mapping)
    run_pending()
    with db.SessionLocal() as s:
        picked = [
            c
            for c in s.scalars(
                select(Candidate).where(Candidate.duplicate_of.is_(None)).order_by(Candidate.id)
            )
            if c.external_id in truth
        ][:SIZE]
        job = start_parse(s, [c.id for c in picked])
        est = estimate(s, [c.id for c in picked])
    started = time.monotonic()
    run_parse(job.id)  # только разбор: отпечатки для эталона не нужны
    seconds = time.monotonic() - started
    with db.SessionLocal() as s:
        job = s.get(Job, job.id)
        rows = [s.get(Candidate, c.id) for c in picked]
    parsed = [c for c in rows if c.parse_status == "parsed"]
    print(f"Сервис: {config.get('llm_provider')}, модель {config.get('llm_model_parse')}")
    print(f"Разобрано {len(parsed)} из {len(rows)} за {seconds:.0f} с".replace(".", ","))
    tokens = job.payload.get("tokens_in", 0), job.payload.get("tokens_out", 0)
    print(f"Объём запросов: {tokens[0]} на вход, {tokens[1]} на выход")
    fact = (
        (tokens[0] * config.number("price_parse_in") + tokens[1] * config.number("price_parse_out"))
        / 1e6
        * config.number("usd_rub")
    )
    print(
        f"Цена по тарифу из настроек: {fact:.1f} ₽, до старта обещали "
        f"{est['rub_low']:.1f}–{est['rub_high']:.1f} ₽; время обещали "
        f"{est['seconds_low']:.0f}–{est['seconds_high']:.0f} с\n".replace(".", ",")
    )
    print("| Поле | F1 | Значений в эталоне |\n|---|---|---|")
    pairs = [(present(fields(truth[c.external_id]), c.raw_text), fields(c.parsed)) for c in parsed]
    for name in ("Должности", "Компании", "Даты работы", "Навыки"):
        score, total = f1([(t[name], g[name]) for t, g in pairs])
        print(f"| {name} | {score:.2f} | {total} |".replace(".", ","))
    if job.error:
        print("\nНе разобрано:\n" + job.error)


if __name__ == "__main__":
    main(*sys.argv[1:])
