"""Расписание ночного прогона: APScheduler в процессе приложения (раздел 4 плана).

В «Настройках» хранятся время («02:00») и дни («daily» или «weekdays»); cron-выражение
собирается здесь, рекрутер его не видит. coalesce=True: сколько бы запусков ни проспал
компьютер, при пробуждении выполнится один. misfire_grace_time — 6 часов: если компьютер
проснулся позже, прогон не идёт, а в `night_runs` остаётся запись «пропущен» с причиной,
и «Утро» начинается с неё и кнопки «Запустить сейчас».
"""

import re
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config

GRACE = 6 * 3600  # секунды: проснулся позже — прогон пропущен
JOB_ID = "night"
DAYS = {"daily": "*", "weekdays": "mon-fri"}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_scheduler: BackgroundScheduler | None = None


def parse_time(value: str) -> tuple[int, int] | None:
    match = TIME_RE.match((value or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def trigger(time_value: str | None = None, days: str | None = None, tz=None) -> CronTrigger:
    """Cron из настроек: «02:00» и «weekdays» → minute=0 hour=2 day_of_week=mon-fri."""
    hour, minute = parse_time(time_value or config.get("night_time")) or (2, 0)
    return CronTrigger(
        day_of_week=DAYS.get(days or config.get("night_days"), "*"),
        hour=hour,
        minute=minute,
        timezone=tz or _local_tz(),
    )


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(_local_tz()).replace(tzinfo=None) if moment.tzinfo else moment


def previous_run(now: datetime | None = None) -> datetime | None:
    """Последнее время по расписанию не позже `now` (за последнюю неделю)."""
    now = now or datetime.now()
    trig = trigger()
    moment = trig.get_next_fire_time(None, (now - timedelta(days=8)).astimezone())
    last = None
    while moment and _naive(moment) <= now:
        last = _naive(moment)
        moment = trig.get_next_fire_time(moment, moment + timedelta(seconds=1))
    return last


def next_run(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return _naive(trigger().get_next_fire_time(None, now.astimezone()))


def fire() -> None:
    """Сработало расписание: ставим ночной прогон. Время по плану — последнее по
    расписанию: при пробуждении после сна это тот запуск, который проспали."""
    from app import night

    fired = datetime.now()
    night.enqueue(planned_at=previous_run(fired) or fired, fired_at=fired)


def _missed(event) -> None:
    from app import night

    night.record_missed(_naive(event.scheduled_run_time), datetime.now())


def configure(scheduler) -> None:
    scheduler.add_job(
        fire,
        trigger(),
        id=JOB_ID,
        coalesce=True,
        misfire_grace_time=GRACE,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.add_listener(_missed, EVENT_JOB_MISSED)


def start() -> None:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=_local_tz())
    configure(_scheduler)
    _scheduler.start()


def reschedule() -> None:
    """После сохранения «Настроек»: новое время или дни вступают в силу сразу."""
    if _scheduler is not None:
        _scheduler.reschedule_job(JOB_ID, trigger=trigger())


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
