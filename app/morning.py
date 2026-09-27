"""Отчёт за ночь: «Утро», письмо и строка о ближайшем прогоне на «Главной».

Разделы строятся только из `night_runs.summary` — чисел и названий вакансий, поэтому
письмо, собранное из тех же разделов, не содержит ни имён, ни контактов кандидатов.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import night, schedule
from app.models import Candidate, NightRun, Vacancy
from app.web.present import MONTHS, NBSP, num, plural

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _n(n: int, one: str, few: str, many: str) -> str:
    return f"{num(n)}{NBSP}{plural(n, one, few, many)}"


def day(moment: datetime) -> str:
    return f"{moment.day}{NBSP}{MONTHS[moment.month - 1]}"


def sections(summary: dict) -> list[dict]:
    """[{title, rows: [{text, link}]}] по срочности; пустые разделы не показываются."""
    fresh = []
    for v in summary.get("vacancies", []):
        parts = []
        if v["fit"]:
            parts.append(_n(v["fit"], "подходит", "подходят", "подходят"))
        if v["maybe"]:
            parts.append(f"{num(v['maybe'])} можно рассмотреть")
        if v.get("changed"):
            parts.append(f"у {num(v['changed'])} изменилась оценка")
        if parts:
            fresh.append(
                {
                    "text": f"{v['title']}: {', '.join(parts)}",
                    "link": f"/vacancies/{v['id']}/results",
                }
            )
    decide = summary.get("decide", {})
    todo = []
    if decide.get("duplicates"):
        todo.append(
            {"text": f"Похоже на дубль: {num(decide['duplicates'])}", "link": "/duplicates"}
        )
    if decide.get("stale"):
        todo.append(
            {
                "text": f"Резюме старше полутора лет: {num(decide['stale'])}",
                "link": "/candidates?fresh=stale",
            }
        )
    base = summary.get("base", {})
    changed = []
    if base.get("new"):
        changed.append({"text": f"Новых кандидатов: {num(base['new'])}", "link": "/candidates"})
    if base.get("merged"):
        changed.append(
            {"text": f"Объединено дублей: {num(base['merged'])}", "link": "/duplicates/merged"}
        )
    if base.get("stale"):
        changed.append(
            {
                "text": f"Стали устаревшими: {num(base['stale'])}",
                "link": "/candidates?fresh=stale",
            }
        )
    if base.get("embedded"):
        changed.append(
            {
                "text": f"Подготовили к поиску по смыслу: {num(base['embedded'])} резюме",
                "link": "/candidates",
            }
        )
    found = [
        ("Новые кандидаты по вакансиям", fresh),
        ("Нужно решить", todo),
        ("Что изменилось в базе", changed),
        ("Не получилось", summary.get("problems", [])),
    ]
    return [{"title": title, "rows": rows} for title, rows in found if rows]


def empty_line(summary: dict) -> str | None:
    """«За ночь новых подходящих кандидатов нет…», если ни по одной вакансии никого."""
    if any(v["fit"] or v["maybe"] for v in summary.get("vacancies", [])):
        return None
    n, m = summary.get("checked_vacancies", 0), summary.get("checked_resumes", 0)
    return (
        "За ночь новых подходящих кандидатов нет. "
        f"Проверили {_n(n, 'вакансию', 'вакансии', 'вакансий')} по {num(m)} резюме."
    )


def view(s, now: datetime | None = None) -> dict:
    """Что показать на «Утре»: предупреждение первой строкой, разделы, пустое утро."""
    now = now or datetime.now()
    run = s.scalar(select(NightRun).order_by(NightRun.id.desc()).limit(1))
    alert, run_now = None, False
    expected = schedule.previous_run(now)
    if run is None:
        alert, run_now = "Ночного прогона ещё не было.", True
    elif run.status in ("failed", "missed"):
        alert, run_now = run.error, True
    elif run.status in ("queued", "running") and run.id not in night.working_runs(s):
        alert, run_now = night.BROKEN, True  # «Запустить сейчас» поставит новый прогон
    elif run.status in ("queued", "running"):
        alert = "Ночной прогон идёт. Отчёт появится здесь, когда он закончится."
    elif expected and (run.planned_at or run.started_at) < expected - timedelta(minutes=1):
        alert = (
            f"Ночной прогон в {expected:%H:%M} не состоялся: приложение не было запущено. "
            f"Ниже — отчёт за {day(run.started_at)}."
        )
        run_now = True
    elif run.summary.get("late"):
        alert = run.summary["late"]
    done = run if run is not None and run.status == "done" else None
    if done is None and run is not None:  # сорвался — показываем последний удачный
        done = s.scalar(
            select(NightRun).where(NightRun.status == "done").order_by(NightRun.id.desc()).limit(1)
        )
    return {
        "run": done,
        "alert": alert,
        "run_now": run_now,
        "sections": sections(done.summary) if done else [],
        "empty": empty_line(done.summary) if done else None,
        "next": next_line(s, now),
    }


def next_line(s, now: datetime | None = None) -> str:
    """«Этой ночью в 02:00: 2 вакансии и 15 новых резюме. Компьютер должен быть включён.»"""
    now = now or datetime.now()
    upcoming = schedule.next_run(now)
    if upcoming - now <= timedelta(hours=24) and upcoming.hour < 12:
        when = f"Этой ночью в {upcoming:%H:%M}"
    else:
        weekday = WEEKDAYS[upcoming.weekday()]
        when = f"Ближайший ночной прогон — {weekday}, {day(upcoming)} в {upcoming:%H:%M}"
    last = s.scalar(
        select(NightRun.started_at)
        .where(NightRun.status == "done")
        .order_by(NightRun.id.desc())
        .limit(1)
    )
    active = Candidate.duplicate_of.is_(None)
    fresh = s.scalar(
        select(func.count(Candidate.id)).where(
            active, Candidate.imported_at >= (last or datetime.min)
        )
    )
    vacancies = s.scalar(select(func.count(Vacancy.id)).where(Vacancy.schedule_enabled.is_(True)))
    return (
        f"{when}: {_n(vacancies, 'вакансия', 'вакансии', 'вакансий')} и "
        f"{_n(fresh, 'новое резюме', 'новых резюме', 'новых резюме')}. "
        "Компьютер должен быть включён."
    )
