"""Записанные ответы демо: номера строк → текстовые якоря {"__lines__": …}.

Ответы в app/demo_data/llm/ записаны с живой модели, когда строки резюме шли как есть.
Потом длинные строки стали делиться на фрагменты (app/lines.py), и номер, который
указывал на всю длинную строку, стал указывать на её первый фрагмент: цитата перестала
подтверждать вердикт. Скрипт переводит каждый номер в якорь — текст начала и конца той
же строки, по которой его ставила модель. Мок находит якорь в тексте при любой
нумерации (app/llm, _resolve_lines). Ответы модели не правятся: меняются только ссылки.

Ссылка, которую проверка строк отвергла бы и тогда (нет такой строки, одни метки
обезличивания), становится null — вердикт остаётся «в резюме не сказано», как в 0.3.

    TA_ENV_FILE=/dev/null uv run python -m eval.anchor_demo_lines <папка для сборки>
"""

import json
import re
import sys
from pathlib import Path

from sqlalchemy import select

from app import db, demo
from app.anonymize import anonymize, numbered, valid_lines
from app.importer.pipeline import new_batch, start_import
from app.jobs import run_pending
from app.lines import resume_lines
from app.llm import _line_of
from app.models import Candidate
from app.parse import MAX_CHARS, model_text

CANDIDATE_RE = re.compile(r"кандидат (\d+)\n")


def old_text(c: Candidate) -> str:
    """Текст, который видела модель при записи: строки как есть, без деления."""
    return anonymize(c.raw_text[:MAX_CHARS], c.full_name, c.phones, c.emails, c.links)


def fragments(c: Candidate) -> list[tuple[int, int]]:
    """Для каждой строки записи — её первый и последний номер в новой нумерации."""
    spans, at = [], 1
    for line in c.raw_text[:MAX_CHARS].split("\n"):
        count = resume_lines(line).count("\n") + 1
        spans.append((at, at + count - 1))
        at += count
    return spans


def anchor(ref, c: Candidate, problems: list[str]):
    lines = valid_lines(ref, old_text(c))
    if lines is None:  # отвергнута и при записи — так и остаётся
        return None
    spans = fragments(c)
    start, stop = spans[lines[0] - 1][0], spans[lines[1] - 1][1]
    new = model_text(c).split("\n")
    # пустые строки по краям в цитату ничего не дают, а пустой якорь совпал бы с любой
    while not new[start - 1].strip():
        start += 1
    while not new[stop - 1].strip():
        stop -= 1
    first, last = new[start - 1].strip(), new[stop - 1].strip()
    user = numbered(model_text(c))
    found = _line_of(first, user)
    if found != start:
        problems.append(f"{c.external_id}: {ref} → [{start}, {stop}] начало не однозначно")
    if start == stop:
        return {"__lines__": first}
    if _line_of(last, user, after=start) != stop:  # та же строка раньше внутри диапазона
        return {"__lines__": first, "lines": stop - start + 1}
    return {"__lines__": [first, last]}


def convert(folder: Path) -> None:
    db.configure(folder / "data")
    table, resumes = demo.generate(folder / "source", today=demo.RECORDED_ON)
    with db.SessionLocal() as s:
        start_import(s, batch := new_batch(s, table, [resumes]), batch.mapping)
    run_pending()
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Candidate).order_by(Candidate.id)))
    by_ext = {}
    for c in people:  # у записи «похоже на дубль» тот же ID: текст резюме тот же
        by_ext.setdefault(c.external_id, c)
    by_id = {c.id: c for c in people}
    problems: list[str] = []
    for name in ("parse", "evaluate"):
        path = demo.RECORDED / f"{name}.json"
        items = json.loads(path.read_text("utf-8"))
        for item in items:
            response = item["response"]
            if name == "parse":
                ext = item["match"].removeprefix("ID: ").strip()
                if ext not in by_ext:
                    problems.append(f"{ext}: записи с таким ID в демо нет, ответ не тронут")
                    continue
                c = by_ext[ext]
                for pos in response.get("positions") or []:
                    pos["source_lines"] = anchor(pos.get("source_lines"), c, problems)
            else:
                c = by_id[int(CANDIDATE_RE.search(item["match"]).group(1))]
                for part in (*response.get("checks", []), *response.get("concerns", [])):
                    part["evidence_lines"] = anchor(part.get("evidence_lines"), c, problems)
        path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(problems) or "все якоря однозначны")


if __name__ == "__main__":
    convert(Path(sys.argv[1]))
