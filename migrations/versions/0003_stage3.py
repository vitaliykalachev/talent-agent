"""этап 3: требования вакансии, оценка по чек-листу, решения рекрутера, «Неверно»

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vacancies") as t:
        t.add_column(sa.Column("requirements", sa.JSON(), nullable=False, server_default="[]"))
        t.add_column(
            sa.Column("requirements_version", sa.Integer(), nullable=False, server_default="1")
        )
        t.add_column(sa.Column("last_run_at", sa.DateTime(), nullable=True))
        t.add_column(
            sa.Column("notice_seen", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    # Результатов этапа 2 нет: таблица пересоздаётся по новой схеме.
    op.drop_table("matches")
    op.create_table(
        "matches",
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("prev_score", sa.Integer(), nullable=True),
        sa.Column("change_reason", sa.String(16), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("concerns", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("fit_summary", sa.Text(), nullable=True),
        sa.Column("requirements_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resume_hash", sa.String(40), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_reason", sa.String(64), nullable=True),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), nullable=True),
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), nullable=True),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="vacancy"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_feedback_vacancy_id", "feedback", ["vacancy_id"])


def downgrade() -> None:
    op.drop_index("ix_feedback_vacancy_id", "feedback")
    op.drop_table("feedback")
    op.drop_table("matches")
    op.create_table(
        "matches",
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), primary_key=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("concerns", sa.JSON(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
    )
    with op.batch_alter_table("vacancies") as t:
        t.drop_column("notice_seen")
        t.drop_column("last_run_at")
        t.drop_column("requirements_version")
        t.drop_column("requirements")
