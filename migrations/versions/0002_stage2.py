"""этап 2: партия записи, контакты из тела резюме, ошибка разбора

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("candidates") as t:
        t.add_column(sa.Column("batch_id", sa.Integer(), nullable=True))
        t.add_column(sa.Column("body_contacts", sa.JSON(), nullable=False, server_default="[]"))
        t.add_column(sa.Column("parse_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidates") as t:
        t.drop_column("parse_error")
        t.drop_column("body_contacts")
        t.drop_column("batch_id")
