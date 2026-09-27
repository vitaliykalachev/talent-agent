"""Набор проверки поиска и доводов: `make eval`.

1. Собирает свежую демо-базу в data/eval (как `make demo`, с готовой вакансией).
2. По пяти вакансиям `eval/vacancies.json` строит выдачу тремя способами — только
   вектор, только BM25, гибрид (RRF) — и считает через ranx Recall@40 и Recall@200
   (подходящими считаются оценки 2 и 3) и nDCG@10 по оценкам 0–3 из `eval/qrels.txt`.
3. Проверяет доводы всех оценок в базе кодом: строки существуют, у каждого «есть»
   есть строки, плейсхолдеры обезличивания не цитируются как факты.
"""

import json
import sys
from pathlib import Path

from ranx import Qrels, Run, evaluate
from sqlalchemy import select

from app import db
from app import evaluate as ev
from app.anonymize import valid_lines
from app.demo import build
from app.models import Candidate, Match, Vacancy

HERE = Path(__file__).parent
MODES = {"vector": "Только вектор", "bm25": "Только BM25", "hybrid": "Гибрид (RRF)"}
SHORT = {"vector": "вектор", "bm25": "BM25", "hybrid": "гибрид"}
METRICS = {"recall@40-l2": "Recall@40", "recall@200-l2": "Recall@200", "ndcg@10": "nDCG@10"}


def vacancies(session) -> dict[str, Vacancy]:
    out = {}
    for item in json.loads((HERE / "vacancies.json").read_text("utf-8")):
        v = Vacancy(title=item["title"], description=item["description"], top_n=40)
        v.requirements = []
        for r in item["requirements"]:
            v.requirements.append(ev._requirement(v.requirements, r["name"], r["kind"], "user"))
        session.add(v)
        out[item["qid"]] = v
    session.commit()
    return out


def runs(session, found: dict[str, Vacancy]) -> dict[str, Run]:
    ext = dict(session.execute(select(Candidate.id, Candidate.external_id)).all())
    result = {}
    for mode in MODES:
        scores = {}
        for qid, v in found.items():
            order = ev.pool(session, v, mode)
            scores[qid] = {ext[cid]: 1.0 / place for place, cid in enumerate(order, start=1)}
        result[mode] = Run(scores, name=mode)
    return result


def check_evidence(session) -> dict[str, int]:
    """Проверка доводов кодом по всем оценкам в базе. Смотрим сырые ответы модели
    (`raw_checks`), а не уже очищенные `checks`: код превращает вердикт без строк в
    «нет данных», и по очищенным счётчики всегда были бы нулями."""
    stats = {"оценок": 0, "доводов со строками": 0, "строк нет в резюме": 0}
    stats |= {"«есть» без строк": 0, "цитата из одних плейсхолдеров": 0}
    rows = session.execute(
        select(Match, Candidate).join(Candidate, Candidate.id == Match.candidate_id)
    ).all()
    for m, c in rows:
        if m.status != "ok":
            continue
        stats["оценок"] += 1
        text = ev.model_text(c)
        lines = text.split("\n")
        for item in [*(m.raw_checks or []), *m.concerns]:
            span = item.get("evidence_lines")
            if item.get("verdict") == "met" and not span:
                stats["«есть» без строк"] += 1
            if not span:
                continue
            stats["доводов со строками"] += 1
            if len(span) != 2 or not (1 <= span[0] <= span[1] <= len(lines)):
                stats["строк нет в резюме"] += 1
            elif valid_lines(span, text) is None:
                stats["цитата из одних плейсхолдеров"] += 1
    return stats


def table(qrels: Qrels, found: dict[str, Run]) -> str:
    head = f"| Вариант | {' | '.join(METRICS.values())} |"
    rows = [head, "|" + "---|" * (len(METRICS) + 1)]
    for mode, run in found.items():
        scores = evaluate(qrels, run, list(METRICS))
        cells = " | ".join(f"{scores[m]:.3f}".replace(".", ",") for m in METRICS)
        rows.append(f"| {MODES[mode]} | {cells} |")
    return "\n".join(rows)


def main(target: str = "data/eval") -> None:
    build(Path(target))
    qrels = Qrels.from_file(str(HERE / "qrels.txt"), kind="trec")
    with db.SessionLocal() as s:
        found = vacancies(s)
        by_mode = runs(s, found)
        print(f"Набор проверки: {len(found)} вакансий, демо-база {target}\n")
        print(table(qrels, by_mode))
        print("\nПо вакансиям, Recall@40 (оценки 2–3):")
        for qid, v in found.items():
            per = {
                mode: evaluate(Qrels({qid: qrels.to_dict()[qid]}), Run({qid: run.to_dict()[qid]}),
                               "recall@40-l2")
                for mode, run in by_mode.items()
            }  # fmt: skip
            cells = " · ".join(f"{SHORT[k]} {val:.2f}".replace(".", ",") for k, val in per.items())
            print(f"- {v.title}: {cells}")
        print("\nПроверка доводов кодом:")
        for key, value in check_evidence(s).items():
            print(f"- {key}: {value}")


if __name__ == "__main__":
    main(*sys.argv[1:])
