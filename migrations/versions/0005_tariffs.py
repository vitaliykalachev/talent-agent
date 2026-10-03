"""тарифы по списаниям ClaudeHub на 03.10.2026 вместо сохранённых прежних умолчаний

«Сохранить» в «Настройках» записывает все поля формы, в том числе цены, поэтому у тех,
кто нажимал его в 0.3, в базе остались прежние умолчания $1/$5 (разбор) и $2/$10
(оценка), завышавшие цену на экранах в 4–12 раз. Миграция идёт один раз при первом
старте новой версии и меняет только значения, равные прежним умолчаниям: цену, которую
рекрутер поставил сам, не трогает. Каждая замена пишется в журнал сервера.

Revision ID: 0005
Revises: 0004
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

log = logging.getLogger("app.config")
OLD = {"price_parse_in": 1, "price_parse_out": 5, "price_eval_in": 2, "price_eval_out": 10}
NEW = {  # app/config.py, DEFAULTS на 03.10.2026
    "price_parse_in": "0.44",
    "price_parse_out": "0.44",
    "price_eval_in": "0.49",
    "price_eval_out": "0.49",
}


def _same(value: str | None, old: float) -> bool:
    try:
        return float(str(value).replace(",", ".")) == old
    except ValueError:
        return False


def upgrade() -> None:
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    conn = op.get_bind()
    rows = conn.execute(sa.select(settings.c.key, settings.c.value).where(settings.c.key.in_(OLD)))
    for key, value in rows.all():
        if _same(value, OLD[key]):
            conn.execute(settings.update().where(settings.c.key == key).values(value=NEW[key]))
            log.warning(
                "Цена %s: прежнее умолчание %s заменено на %s по списаниям ClaudeHub",
                key,
                value,
                NEW[key],
            )


def downgrade() -> None:
    """Прежние умолчания не возвращаем: они завышали цену."""
