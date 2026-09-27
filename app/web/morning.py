"""Экран «Утро» и кнопка «Запустить сейчас»."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, morning, night
from app.web.routes import render

router = APIRouter()


@router.get("/morning", response_class=HTMLResponse)
def morning_page(request: Request):
    with db.SessionLocal() as s:
        view = morning.view(s)
    return render(request, "morning.html", **view)


@router.post("/night/run")
def run_now():
    night.enqueue()
    return RedirectResponse("/morning", status_code=303)
