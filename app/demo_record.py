"""Запись ответов живой модели для демо (`make record-demo`, один раз).

Демо прогоняется через настоящий сервис из «Настроек» и окружения, каждый ответ
сохраняется в формате мок-фикстуры: разбор — по строке «ID: …» резюме, оценка — по
строке «Вакансия N, кандидат M», разбор вакансии — по началу описания. Демо-резюме
синтетические, персональных данных в записи нет.
"""

import json
import re
from collections import Counter
from pathlib import Path

from app import config, db
from app import evaluate as ev
from app import parse as parse_module
from app.demo_vacancy import DESCRIPTION
from app.jobs import run_pending
from app.models import Vacancy

EXTRA = 8  # оценок сверх top_n: другой компьютер может чуть иначе упорядочить поиск


KEEP = ("CandidateProfile", "VacancyDraft")  # их можно взять из прошлой записи


class Recorder:
    """Записывает ответы. Разбор резюме и вакансии, уже записанный в прошлый раз, не
    оплачивается снова: запись можно продолжить после сбоя. Оценки пишутся заново —
    они зависят от того, как разобрана вся база."""

    def __init__(self, folder: Path):
        self.answers: dict[str, dict] = {}
        self.usage: Counter = Counter()
        for path in folder.glob("*.json"):
            for item in json.loads(path.read_text("utf-8")):
                if item["schema"] in KEEP:
                    self.answers[f"{item['schema']}:{item['match']}"] = item

    def _key(self, schema, user: str) -> str:
        if schema.__name__ == "Evaluation":
            return re.match(r"(Вакансия \d+, кандидат \d+)\n", user).group(1) + "\n"
        if schema.__name__ == "VacancyDraft":
            return DESCRIPTION[:60]
        found = re.search(r"ID: \d+\n", user) or re.search(r"^1\| .+\n", user, re.MULTILINE)
        return found.group(0)  # резюме без строки выгрузки — по первой строке

    def wrap(self, llm, purpose: str):
        call, count = llm._call, llm._count

        def recorded_call(schema, system, user):
            key = self._key(schema, user)
            name = f"{schema.__name__}:{key}"
            if name in self.answers and "Прошлый ответ не прошёл проверку" not in user:
                return self.answers[name]["response"]
            raw = call(schema, system, user)
            try:  # ответ текстом, а не по схеме, пишется как есть: мок отдаст его так же
                response = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                response = raw
            self.answers[name] = {"match": key, "schema": schema.__name__, "response": response}
            return raw

        def counted(tokens_in, tokens_out):
            self.usage[f"{purpose}_in"] += tokens_in
            self.usage[f"{purpose}_out"] += tokens_out
            self.usage[f"{purpose}_calls"] += 1
            count(tokens_in, tokens_out)

        llm._call, llm._count = recorded_call, counted
        return llm

    def attach(self) -> None:
        """Разбор и оценка идут через записывающую обёртку настоящей модели."""
        original = parse_module.get_llm
        for module in (parse_module, ev):
            module.get_llm = lambda purpose="parse", _o=original: self.wrap(_o(purpose), purpose)

    def extra(self, v: Vacancy) -> None:
        with db.SessionLocal() as s:
            v = s.get(Vacancy, v.id)
            ev.start(s, v, v.top_n + EXTRA)
        run_pending()

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.json"):
            old.unlink()
        groups = {"CandidateProfile": "parse", "VacancyDraft": "vacancy", "Evaluation": "evaluate"}
        for schema, name in groups.items():
            items = [a for a in self.answers.values() if a["schema"] == schema]
            (folder / f"{name}.json").write_text(
                json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        rub = 0.0
        for purpose in ("parse", "eval"):
            usd = (
                self.usage[f"{purpose}_in"] * config.number(f"price_{purpose}_in")
                + self.usage[f"{purpose}_out"] * config.number(f"price_{purpose}_out")
            ) / 1e6
            rub += usd * config.number("usd_rub")
            calls, sent, got = (self.usage[f"{purpose}_{k}"] for k in ("calls", "in", "out"))
            print(
                f"{purpose}: {calls} запросов, {sent} на входе, {got} на выходе, "
                f"≈ {usd * config.number('usd_rub'):.1f} ₽"
            )
        print(f"Записано ответов: {len(self.answers)}; всего ≈ {rub:.1f} ₽ по тарифам из настроек")
