"""Экраны «Вакансии», «Новая вакансия», карточка вакансии и «Результат по вакансии»."""

import math
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app import db, export
from app import evaluate as ev
from app.llm import LLMError
from app.models import Candidate, Feedback, Job, Match, Vacancy
from app.web import present
from app.web.routes import render, templates

router = APIRouter()

FEEDBACK_KINDS = {
    "not_in_resume": "Этого нет в резюме",
    "misread": "Опыт понят неправильно",
    "irrelevant": "Для этой вакансии неважно",
    "other": "Другое…",
}
REJECT_REASONS = ["Нет нужного опыта", "Зарплата", "Город", "Данные устарели", "Другое"]
DECISIONS = {"invite": "Позвать", "maybe": "Под вопросом", "reject": "Не подходит"}
templates.env.globals["FEEDBACK_KINDS"] = FEEDBACK_KINDS  # «Агент запомнил» в «Настройках»


def _vacancy(s, vacancy_id: int) -> Vacancy:
    v = s.get(Vacancy, vacancy_id)
    if not v:
        raise HTTPException(404)
    return v


# Потолки полей: больше — ошибка ввода, а не условие (иначе карточка вакансии падает).
LIMITS = {
    "min_years": (60, "Стаж — не больше 60 лет."),
    "max_salary": (100_000_000, "Зарплата — не больше 100 000 000 ₽."),
    "top_n": (500, "Сколько кандидатов показать — не больше 500."),
}


def _number(value, field: str | None = None) -> float | None:
    """Число из поля формы; пусто или не число — None; «inf», «1e999» и больше потолка
    поля — ответ 400 с русским текстом."""
    try:
        number = float(str(value).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(number):
        raise HTTPException(400, "Укажите обычное число, например 3 или 150 000.")
    if field in LIMITS and number > LIMITS[field][0]:
        raise HTTPException(400, LIMITS[field][1])
    return number


def _candidate_id(form) -> int | None:
    value = str(form.get("candidate_id", "")).strip()
    if not value:
        return None
    if not value.isdigit() or len(value) > 12:
        raise HTTPException(400, "Не понял, о каком кандидате речь: обновите страницу.")
    return int(value)


def _filters(form) -> dict:
    """Жёсткие условия из формы: пустое поле — условия нет."""
    years = _number(form.get("min_years", ""), "min_years")
    salary = _number(form.get("max_salary", ""), "max_salary")
    found = {
        "city": str(form.get("city", "")).strip(),
        "min_years": years if years is not None and years >= 0 else None,
        "max_salary": int(salary) if salary is not None and salary > 0 else None,
        "fresh": form.get("fresh") == "1",
    }
    return {k: v for k, v in found.items() if v not in (None, "", False)}


def _top_n(form, default: int = 40) -> int:
    n = _number(form.get("top_n", ""), "top_n")
    return max(int(n), 1) if n is not None else default


def _live_job(s, v: Vacancy) -> Job | None:
    jobs = s.scalars(
        select(Job)
        .where(Job.kind == "evaluate", Job.status.in_(("queued", "running", "paused")))
        .order_by(Job.id)
    )
    return next((j for j in jobs if j.payload.get("vacancy_id") == v.id), None)


# ── Список и новая вакансия ─────────────────────────────────────────────────


@router.get("/vacancies", response_class=HTMLResponse)
def vacancies(request: Request):
    with db.SessionLocal() as s:
        rows = s.execute(
            select(Vacancy, func.count(Match.candidate_id))
            .outerjoin(Match, (Match.vacancy_id == Vacancy.id) & (Match.status == "ok"))
            .group_by(Vacancy.id)
            .order_by(Vacancy.id.desc())
        ).all()
    return render(request, "vacancies.html", rows=rows)


@router.get("/vacancies/new", response_class=HTMLResponse)
def vacancy_new(request: Request):
    return render(request, "vacancy_new.html", form={}, error=None)


@router.post("/vacancies/new", response_class=HTMLResponse)
async def vacancy_create(request: Request):
    form = await request.form()
    description = str(form.get("description", "")).strip()
    if len(description) < 20:
        return render(
            request,
            "vacancy_new.html",
            status_code=400,
            form=form,
            error="Опишите вакансию хотя бы парой предложений: что делать, какой нужен опыт.",
        )
    with db.SessionLocal() as s:
        v = Vacancy(
            title=str(form.get("title", "")).strip(),
            description=description,
            hard_filters=_filters(form),
            top_n=_top_n(form),
            schedule_enabled=form.get("schedule") == "1",
        )
        s.add(v)
        s.commit()
        try:
            ev.parse_vacancy(s, v)
        except LLMError as exc:
            s.delete(v)
            s.commit()
            return render(
                request,
                "vacancy_new.html",
                status_code=502,
                form=form,
                error=f"Не получилось разобрать описание: {exc}. Попробуйте ещё раз.",
            )
    return RedirectResponse(f"/vacancies/{v.id}", status_code=303)


# ── Карточка вакансии: портрет, условия, запуск ─────────────────────────────


@router.get("/vacancies/{vacancy_id}", response_class=HTMLResponse)
def vacancy(request: Request, vacancy_id: int):
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        job = _live_job(s, v) or s.scalar(
            select(Job).where(Job.kind == "evaluate").order_by(Job.id.desc()).limit(1)
        )
        if job and job.payload.get("vacancy_id") != v.id:
            job = None
        offers = []
        if v.requirements and ev.scored(v):
            order = ev.pool(s, v)
            for limit in (ev.TRIAL, v.top_n):
                est = ev.estimate(s, v, limit, order)
                offers.append(
                    {
                        "limit": limit,
                        "count": est["count"],
                        "price": present.rub_range(est["rub_low"], est["rub_high"]),
                        "duration": present.duration(est["seconds_low"], est["seconds_high"], True),
                    }
                )
        evaluated = s.scalar(select(func.count(Match.candidate_id)).where(Match.vacancy_id == v.id))
        memory = list(
            s.scalars(
                select(Feedback).where(Feedback.vacancy_id == v.id).order_by(Feedback.id.desc())
            )
        )
        names = _names(s, [f.candidate_id for f in memory])
    return render(
        request,
        "vacancy.html",
        v=v,
        job=job,
        offers=offers,
        evaluated=evaluated,
        memory=memory,
        names=names,
        KINDS=ev.KINDS,
        MANY=ev.MANY,
        FEEDBACK_KINDS=FEEDBACK_KINDS,
        FAIR_HINT=ev.FAIR_HINT,
        VAGUE_HINT=ev.VAGUE_HINT,
    )


def _names(s, ids) -> dict[int, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = s.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(ids)))
    return {cid: name or "Без имени" for cid, name in rows}


@router.post("/vacancies/{vacancy_id}/requirements")
async def requirement_change(request: Request, vacancy_id: int):
    """Правка портрета: add, edit, delete, accept (заменить на предложенное)."""
    form = await request.form()
    action = str(form.get("action", ""))
    rid, name, kind = (str(form.get(k, "")) for k in ("rid", "name", "kind"))
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        if action == "add":
            ev.add_requirement(s, v, name, kind)
        elif action == "edit":
            ev.edit_requirement(s, v, rid, name, kind)
        elif action == "delete":
            ev.delete_requirement(s, v, rid)
        elif action == "accept":
            ev.accept_suggestion(s, v, rid)
    return RedirectResponse(f"/vacancies/{vacancy_id}#portrait", status_code=303)


@router.post("/vacancies/{vacancy_id}/conditions")
async def conditions_save(request: Request, vacancy_id: int):
    form = await request.form()
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        v.hard_filters = _filters(form)
        v.top_n = _top_n(form, v.top_n)
        v.schedule_enabled = form.get("schedule") == "1"
        s.commit()
    return RedirectResponse(f"/vacancies/{vacancy_id}", status_code=303)


@router.post("/vacancies/{vacancy_id}/evaluate")
async def evaluate_start(request: Request, vacancy_id: int):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()] or None
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        limit = ev.TRIAL if form.get("scope") == "trial" else v.top_n
        if ev.scored(v):
            ev.start(s, v, limit, ids)
    return RedirectResponse(f"/vacancies/{vacancy_id}/results", status_code=303)


@router.get("/vacancies/{vacancy_id}/job", response_class=HTMLResponse)
def evaluate_progress(request: Request, vacancy_id: int):
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        job = _live_job(s, v)
    return render(request, "eval_job.html", v=v, job=job)


# ── Результат по вакансии ───────────────────────────────────────────────────


def _card(v: Vacancy, m: Match, c: Candidate) -> dict:
    fresh_run = bool(v.last_run_at and m.evaluated_at >= v.last_run_at)
    changed = None
    if fresh_run and m.prev_score is not None and m.score is not None and m.prev_score != m.score:
        why = ev.CHANGE.get(m.change_reason or "", "")
        if m.change_reason == "resume_updated" and c.resume_date:
            why = f"резюме обновлено {present.short_date(c.resume_date)}"
        changed = {"text": f"Оценка изменилась: {m.prev_score} → {m.score}", "why": why}
    return {
        "m": m,
        "c": c,
        "category": ev.category(m),
        "counter": ev.counter(m),
        "new": fresh_run and m.change_reason == "new",
        "changed": changed,
        "stale": present.stale_note(c) if c.stale else None,
        "tie": False,
    }


def results_view(s, v: Vacancy, hidden: bool = False) -> dict:
    rows = s.execute(
        select(Match, Candidate)
        .join(Candidate, Candidate.id == Match.candidate_id)
        .where(Match.vacancy_id == v.id)
    ).all()
    visible = [(m, c) for m, c in rows if m.decision != "reject"]
    rejected = [(m, c) for m, c in rows if m.decision == "reject"]
    groups: dict[str, list] = {key: [] for key in ev.CATEGORIES}
    for m, c in visible:
        card = _card(v, m, c)
        groups[card["category"]].append(card)
    for key, cards in groups.items():
        # внутри группы по баллу; устаревшие резюме — ниже в своей группе
        cards.sort(key=lambda d: (bool(d["c"].stale), -(d["m"].score or 0), d["m"].rank or 0))
        if key != ev.FAILED:
            for card, tie in zip(cards, ev.tied([d["m"].score for d in cards]), strict=True):
                card["tie"] = tie
    return {
        "groups": groups,
        "hidden": [_card(v, m, c) for m, c in rejected] if hidden else [],
        "hidden_count": len(rejected),
        "total": len(rows),
    }


@router.get("/vacancies/{vacancy_id}/results", response_class=HTMLResponse)
def results(request: Request, vacancy_id: int, hidden: int = 0, undo: int | None = None):
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        view = results_view(s, v, bool(hidden))
        job = _live_job(s, v)
        toast = s.get(Feedback, undo) if undo else None
        recount = _recount(s, v, toast) if toast else 0
    return render(
        request,
        "results.html",
        v=v,
        job=job,
        toast=toast,
        recount=recount,
        show_hidden=bool(hidden),
        CATEGORIES=ev.CATEGORIES,
        VERDICTS=ev.VERDICTS,
        KINDS=ev.KINDS,
        FEEDBACK_KINDS=FEEDBACK_KINDS,
        REJECT_REASONS=REJECT_REASONS,
        DECISIONS=DECISIONS,
        **view,
    )


def _recount(s, v: Vacancy, f: Feedback) -> int:
    """Скольких кандидатов пересчитает поправка: одного или всю вакансию."""
    if f.kind != "irrelevant":
        return 1
    return s.scalar(
        select(func.count(Match.candidate_id)).where(
            Match.vacancy_id == v.id, Match.decision.is_distinct_from("reject")
        )
    )


@router.post("/vacancies/{vacancy_id}/decision")
async def decision(request: Request, vacancy_id: int):
    form = await request.form()
    value = str(form.get("decision", ""))
    with db.SessionLocal() as s:
        m = s.get(Match, (vacancy_id, _candidate_id(form) or 0))
        if not m:
            raise HTTPException(404)
        m.decision = value if value in DECISIONS else None
        reason = str(form.get("reason", ""))
        m.decision_reason = reason if value == "reject" and reason in REJECT_REASONS else None
        s.commit()
    return RedirectResponse(f"/vacancies/{vacancy_id}/results#c{m.candidate_id}", status_code=303)


@router.post("/vacancies/{vacancy_id}/feedback")
async def feedback(request: Request, vacancy_id: int):
    """«Неверно» у довода: запись в feedback и уведомление с «Отменить»."""
    form = await request.form()
    kind = str(form.get("kind", ""))
    if kind not in FEEDBACK_KINDS:
        raise HTTPException(400, "Выберите, что не так")
    text = str(form.get("text", "")).strip()
    if kind == "other" and (note := str(form.get("note", "")).strip()):
        text = f"{text}: {note}"
    scope = "all" if form.get("scope") == "all" else "vacancy"
    with db.SessionLocal() as s:
        _vacancy(s, vacancy_id)
        f = Feedback(
            candidate_id=_candidate_id(form),
            vacancy_id=vacancy_id if scope == "vacancy" else None,
            target=str(form.get("target", "reason"))
            if form.get("target") in ("reason", "concern", "field")
            else "reason",
            text=text[:500],
            kind=kind,
            scope=scope,
        )
        s.add(f)
        s.commit()
    return RedirectResponse(f"/vacancies/{vacancy_id}/results?undo={f.id}", status_code=303)


@router.post("/feedback/{feedback_id}/delete")
async def feedback_delete(request: Request, feedback_id: int):
    """«Отменить» в уведомлении и «Удалить» в блоке «Агент запомнил»."""
    form = await request.form()
    with db.SessionLocal() as s:
        f = s.get(Feedback, feedback_id)
        if f:
            s.delete(f)
            s.commit()
    back = str(form.get("back", "/vacancies"))
    safe = back.startswith("/") and not back.startswith("//") and "\\" not in back
    return RedirectResponse(back if safe else "/vacancies", status_code=303)


@router.post("/vacancies/{vacancy_id}/notice")
def notice_seen(vacancy_id: int):
    with db.SessionLocal() as s:
        _vacancy(s, vacancy_id).notice_seen = True
        s.commit()
    return RedirectResponse(f"/vacancies/{vacancy_id}/results", status_code=303)


@router.get("/vacancies/{vacancy_id}/export")
def export_xlsx(vacancy_id: int, who: str = "invite", audience: str = "client"):
    with db.SessionLocal() as s:
        v = _vacancy(s, vacancy_id)
        data = export.shortlist(s, v, who, audience)
    name = f"{v.title or 'Вакансия'} — {date.today():%d.%m.%Y}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )
