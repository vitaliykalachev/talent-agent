"""этап 4: ночные прогоны, обратимое слияние дублей

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "night_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("planned_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
    )
    with op.batch_alter_table("duplicates") as t:
        t.add_column(sa.Column("snapshot", sa.JSON(), nullable=True))
        t.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
        t.add_column(sa.Column("postponed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("duplicates") as t:
        t.drop_column("postponed_at")
        t.drop_column("created_at")
        t.drop_column("snapshot")
    op.drop_table("night_runs")
