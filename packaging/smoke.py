"""Дымовой тест распакованной сборки: `python packaging/smoke.py <папка KadrovyAgent> <лог>`.

Сервер уже запущен через «Запустить.bat», вывод окна идёт в лог. Тест ждёт строку
с адресом и /health, обходит экраны, создаёт вакансию с текстом демо (кандидаты
нашлись — значит, модель поиска загрузилась) и вакансию со своим текстом (подсказка
демо-режима вместо ошибки). Итог — строка JSON с замерами.
"""

import ast
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BANNER = "Демо-режим: данные вымышленные, ИИ работает на записанных ответах"
DEMO_MISS = "В демо-версии ИИ отключён — новые вакансии и резюме не разбираются"
PAGES = ("/", "/vacancies", "/vacancies/1", "/duplicates", "/morning", "/settings")
SHOWCASE = ("/", "/morning", "/vacancies", "/vacancies/1/results")
FAILURES = ("не получилось", "не удалось оценить", "есть проблемы")


def get(url: str, data: dict | None = None) -> tuple[int, str, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(url, body, timeout=300) as r:  # 303 после POST — GET
        return r.status, r.geturl(), r.read().decode("utf-8")


def description(pkg: Path) -> str:
    tree = ast.parse((pkg / "app" / "demo_vacancy.py").read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "DESCRIPTION":
            return ast.literal_eval(node.value)
    raise LookupError("DESCRIPTION")


def main() -> None:
    pkg, log = Path(sys.argv[1]), Path(sys.argv[2])
    started = time.monotonic()
    base = None
    while time.monotonic() - started < 120:
        text = log.read_text("utf-8", errors="replace") if log.exists() else ""
        if found := re.search(r"работает: (http://127\.0\.0\.1:\d+)/ — не закрывайте", text):
            base = found.group(1)
            try:
                if get(base + "/health")[2] == "ok":
                    break
            except OSError:
                pass
        time.sleep(0.5)
    else:
        sys.exit(f"/health не ответил за 120 с (адрес: {base})")
    to_health = time.monotonic() - started

    for path in PAGES:
        status, _, text = get(base + path)
        assert status == 200, (path, status)
    assert BANNER in get(base + "/")[2], "нет плашки демо-режима"
    for path in SHOWCASE:
        text = get(base + path)[2].lower()
        for word in FAILURES:
            assert word not in text, (path, word)

    started_search = time.monotonic()
    # Тот же текст демо, но не повтор: повтор за минуту ведёт на готовую вакансию
    own = description(pkg) + " Вакансия для проверки сборки."
    _, url, text = get(base + "/vacancies/new", {"description": own})
    first_search = time.monotonic() - started_search
    assert re.search(r"/vacancies/\d+$", url) and url != base + "/vacancies/1", url
    counts = [int(n) for n in re.findall(r"<small>(\d+) кандидат", text)]
    assert counts and max(counts) > 0, "по демо-вакансии кандидаты не нашлись"
    text = get(base + "/candidates?" + urllib.parse.urlencode({"q": "литейное производство"}))[2]
    assert "Агент ищет по смыслу" in text

    status, _, text = get(
        base + "/vacancies/new", {"description": "Нужен бариста в кофейню у метро, опыт от года."}
    )
    assert status == 200 and DEMO_MISS in text and "Traceback" not in text

    print(json.dumps({
        "seconds_to_health": round(to_health, 1),
        "seconds_first_search": round(first_search, 1),
        "candidates": max(counts),
        "pages": [*PAGES, *SHOWCASE[3:], "/vacancies/new", "/candidates?q=…"],
    }, ensure_ascii=False))  # fmt: skip


if __name__ == "__main__":
    main()
