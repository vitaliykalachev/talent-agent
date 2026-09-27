"""Ночной прогон — задача `night` (раздел 4 и этап 4 плана).

По порядку: помечает устаревшие резюме (дата старше 18 месяцев или её нет), ищет
возможные дубли, считает смысловые отпечатки для новых разобранных, оценивает все
вакансии с «Оценивать каждую ночь» — только новых и изменившихся кандидатов. Итог
пишется в `night_runs.summary`: из него строятся «Утро» и письмо. Если прогон начался
с опозданием, причина — в `summary.late`; если сорвался — в `error`.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import db, mail
from app import evaluate as ev
from app.embed import run_embed
from app.importer.dedup import find_possible, open_pairs
from app.importer.normalize import is_stale
from app.jobs import enqueue as enqueue_job
from app.models import Candidate, Duplicate, Job, Match, NightRun, Vacancy

LATE = timedelta(minutes=10)


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def enqueue(planned_at: datetime | None = None, fired_at: datetime | None = None) -> NightRun:
    """Ставит ночной прогон; `planned_at` пусто — «Запустить сейчас». Пока прогон в
    очереди или идёт, второй не ставится."""
    with db.SessionLocal() as s:
        working = {
            j.payload.get("run_id")
            for j in s.scalars(
                select(Job).where(Job.kind == "night", Job.status.in_(("queued", "running")))
            )
        }
        for live in s.scalars(select(NightRun).where(NightRun.status.in_(("queued", "running")))):
            if live.id in working:
                return live
            # задача уже закончилась или пропала, а запись осталась «идёт» — прогон оборвался
            live.status = "failed"
            live.error = "Ночной прогон оборвался: приложение закрыли или компьютер выключился."
            live.summary = {**live.summary, "error": live.error}
        run = NightRun(
            planned_at=planned_at,
            status="queued",
            summary={"fired_at": (fired_at or datetime.now()).isoformat(timespec="seconds")},
        )
        s.add(run)
        s.flush()
        enqueue_job(s, "night", {"run_id": run.id})
        s.commit()
        return run


def record_missed(planned_at: datetime, woke_at: datetime) -> None:
    reason = (
        f"Ночной прогон в {hhmm(planned_at)} не состоялся: компьютер спал или "
        f"был выключен до {hhmm(woke_at)}."
    )
    with db.SessionLocal() as s:
        s.add(
            NightRun(
                planned_at=planned_at, status="missed", error=reason, summary={"error": reason}
            )
        )
        s.commit()


def late_reason(run: NightRun, started: datetime) -> str | None:
    if run.planned_at is None:
        return None
    fired = datetime.fromisoformat(run.summary.get("fired_at") or started.isoformat())
    when = (
        hhmm(started)
        if started.date() == run.planned_at.date()
        else started.strftime("%d.%m в %H:%M")
    )
    if fired - run.planned_at > LATE:
        return f"Ночной прогон начался в {when}, а не в {hhmm(run.planned_at)}: компьютер спал."
    if started - fired > LATE:
        return (
            f"Ночной прогон начался в {when}, а не в {hhmm(run.planned_at)}: "
            "ждал, пока закончится другая задача."
        )
    return None


def _since(s, run: NightRun) -> datetime:
    """Начало прошлого удачного прогона: что появилось после него — новое."""
    prev = s.scalar(
        select(NightRun.started_at)
        .where(NightRun.status == "done", NightRun.id != run.id)
        .order_by(NightRun.id.desc())
        .limit(1)
    )
    return prev or datetime.min


def _mark_stale(s) -> int:
    newly = 0
    for c in s.scalars(select(Candidate).where(Candidate.duplicate_of.is_(None))):
        stale = is_stale(c.resume_date)
        newly += stale and not c.stale
        c.stale = stale
    s.commit()
    return newly


def _subjob(s, kind: str, payload: dict, handler) -> Job:
    job = enqueue_job(s, kind, payload)
    s.commit()
    handler(job.id)
    s.expire_all()
    return s.get(Job, job.id)


def _evaluate(s, v: Vacancy, started: datetime) -> tuple[dict, list[dict]]:
    job = ev.start(s, v, v.top_n)
    if job.status in ("queued", "running"):
        ev.run_evaluate(job.id)
    s.expire_all()
    job = s.get(Job, job.id)
    rows = list(
        s.scalars(
            select(Match).where(
                Match.vacancy_id == v.id,
                Match.evaluated_at >= started,
                Match.decision.is_distinct_from("reject"),
            )
        )
    )
    new = [m for m in rows if m.change_reason == "new"]
    count = {cat: sum(ev.category(m) == cat for m in new) for cat in ev.CATEGORIES}
    item = {
        "id": v.id,
        "title": v.title or "Без названия",
        "fit": count[ev.FIT],
        "maybe": count[ev.MAYBE],
        "unfit": count[ev.UNFIT],
        "changed": sum(m.change_reason != "new" and m.status == "ok" for m in rows),
        "failed": sum(m.status == "failed" for m in rows),
    }
    problems = []
    if job.status == "failed":
        problems.append(
            {
                "text": f"Оценка «{item['title']}» не получилась: {job.error}",
                "link": f"/vacancies/{v.id}",
            }
        )
    elif item["failed"]:
        reasons = sorted({m.error or "причина не записана" for m in rows if m.status == "failed"})
        problems.append(
            {
                "text": f"«{item['title']}»: не удалось оценить {item['failed']} — "
                + "; ".join(reasons[:3]),
                "link": f"/vacancies/{v.id}/results",
            }
        )
    return item, problems


def run_night(job_id: int) -> None:
    with db.SessionLocal() as s:
        job = s.get(Job, job_id)
        run = s.get(NightRun, job.payload["run_id"])
        started = datetime.now()
        summary = dict(run.summary)
        try:
            run.status, run.started_at = "running", started
            job.status, job.total = "running", 4
            summary["late"] = late_reason(run, started)
            run.summary = summary
            s.commit()
            since = _since(s, run)
            newly_stale = _mark_stale(s)
            job.progress = 1
            new_pairs = find_possible(s)
            job.progress = 2
            s.commit()
            embedded = _subjob(s, "embed", {}, run_embed).total
            job.progress = 3
            s.commit()
            vacancies, problems = [], []
            for v in s.scalars(select(Vacancy).where(Vacancy.schedule_enabled.is_(True))):
                item, trouble = _evaluate(s, v, started)
                vacancies.append(item)
                problems += trouble
            active = Candidate.duplicate_of.is_(None)
            count = lambda *w: s.scalar(select(func.count(Candidate.id)).where(*w))  # noqa: E731
            failed_parse = count(
                active, Candidate.parse_status == "failed", Candidate.imported_at >= since
            )
            if failed_parse:
                parse_job = s.scalar(
                    select(Job.id)
                    .where(Job.kind == "parse", Job.error.is_not(None))
                    .order_by(Job.id.desc())
                    .limit(1)
                )
                problems.append(
                    {
                        "text": f"Не удалось разобрать новых резюме: {failed_parse}",
                        "link": f"/jobs/{parse_job}/problems" if parse_job else "/",
                    }
                )
            summary.update(
                vacancies=vacancies,
                checked_vacancies=len(vacancies),
                checked_resumes=count(active),
                base={
                    "new": count(active, Candidate.imported_at >= since),
                    "merged": s.scalar(
                        select(func.count(Duplicate.id)).where(
                            Duplicate.status == "merged", Duplicate.created_at >= since
                        )
                    ),
                    "stale": newly_stale,
                    "embedded": embedded,
                },
                decide={
                    "duplicates": len(open_pairs(s)),
                    "new_duplicates": new_pairs,
                    "stale": count(active, Candidate.stale.is_(True)),
                },
                problems=problems,
            )
            run.status = "done"
        except Exception as exc:  # причина уходит в «Утро» первой строкой
            s.rollback()
            run = s.get(NightRun, run.id)
            run.status, run.error = "failed", f"Ночной прогон не получился: {exc}"
            summary["error"] = run.error
        finally:  # запись прогона и задача закрываются, что бы ни случилось выше
            run.finished_at = datetime.now()
            run.summary = dict(summary)  # письмо строится из итога, поэтому сначала итог
            run.summary = {**summary, "mail": mail.send_report(run)}
            job = s.get(Job, job_id)
            job.status, job.progress, job.finished_at = "done", job.total, datetime.now()
            s.commit()
