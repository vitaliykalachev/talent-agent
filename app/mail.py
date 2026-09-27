"""Письмо с отчётом за ночь по SMTP из «Настроек».

В письме те же разделы, что на «Утре», но только числа и названия вакансий со ссылками:
имён, телефонов и почты кандидатов в нём нет — персональные данные в почтовый ящик не
уходят. Без SMTP письмо не отправляется, «Утро» работает как обычно.
"""

import smtplib
import socket
import ssl
from email.message import EmailMessage

from app import config
from app.models import NightRun


def configured() -> bool:
    return bool(config.get("smtp_host") and config.get("smtp_to"))


def _send(subject: str, body: str) -> None:
    host, port = config.get("smtp_host"), int(config.number("smtp_port"))
    user, password = config.get("smtp_user"), config.get("smtp_password")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user if "@" in user else config.get("smtp_to")
    msg["To"] = config.get("smtp_to")
    msg.set_content(body)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        server.ehlo()
        if port != 465 and server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        if user:
            server.login(user, password)
        server.send_message(msg)


def _error(exc: Exception) -> str:
    """Причина по-русски, без имён исключений: рекрутёру «gaierror» ничего не говорит."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "почтовый сервер не принял логин или пароль"
    if isinstance(exc, socket.gaierror):
        return "почтовый сервер с таким адресом не найден, проверьте поле «Почтовый сервер»"
    if isinstance(exc, ConnectionRefusedError):
        return "почтовый сервер не принимает соединения на этом порту, проверьте поле «Порт»"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "почтовый сервер не ответил вовремя, проверьте адрес и порт"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "почтовый сервер не принял адрес получателя"
    return "почтовый сервер не ответил, проверьте адрес и порт"


def send_test() -> str | None:
    """«Отправить пробное письмо»: None — ушло, иначе — что случилось."""
    if not configured():
        return "Заполните адрес почтового сервера и адрес получателя."
    try:
        _send(
            "Кадровый агент: пробное письмо",
            "Почта настроена. После ночного прогона сюда придёт отчёт: сколько кандидатов "
            "подходят по каждой вакансии и что нужно решить.",
        )
    except Exception as exc:
        return f"Письмо не ушло: {_error(exc)}."
    return None


def report_body(run: NightRun) -> str:
    from app.morning import day, empty_line, sections

    base = config.get("public_url").rstrip("/")
    lines = [f"Отчёт за ночь, {day(run.started_at or run.planned_at)}", ""]
    if run.status != "done":
        lines += [
            run.error or "Ночной прогон не получился.",
            f"Запустить сейчас: {base}/morning",
            "",
        ]
    if run.summary.get("late"):
        lines += [run.summary["late"], ""]
    if run.status == "done" and (empty := empty_line(run.summary)):
        lines += [empty, ""]
    for section in sections(run.summary) if run.status == "done" else []:
        lines.append(section["title"])
        lines += [f"— {row['text']}: {base}{row['link']}" for row in section["rows"]]
        lines.append("")
    lines.append(f"Весь отчёт: {base}/morning")
    return "\n".join(lines)


def send_report(run: NightRun) -> str | None:
    """После ночного прогона: «sent», текст ошибки или None, если почта не настроена."""
    if not configured():
        return None
    try:
        _send(f"Кадровый агент: отчёт за ночь, {run.started_at:%d.%m}", report_body(run))
    except Exception as exc:
        return f"Письмо с отчётом не ушло: {_error(exc)}."
    return "sent"
