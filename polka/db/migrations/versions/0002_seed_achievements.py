"""Справочник ачивок (9 штук). Названия можно менять без изменения условий.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    achievements = sa.table(
        "achievements",
        sa.column("code", sa.String),
        sa.column("title", sa.String),
        sa.column("description", sa.String),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        achievements,
        [
            {"code": "first_page", "title": "Первая страница", "description": "Первый засчитанный пересказ", "sort_order": 1},
            {"code": "week", "title": "Неделя", "description": "Стрик 7 дней", "sort_order": 2},
            {"code": "two_weeks", "title": "Две недели", "description": "Стрик 14 дней", "sort_order": 3},
            {"code": "iron", "title": "Железный", "description": "Весь план без единой заморозки и пропуска", "sort_order": 4},
            {"code": "duet", "title": "Дуэт", "description": "Общий стрик пары 7 дней", "sort_order": 5},
            {"code": "kept_word", "title": "Месяц вдвоём", "description": "Общий стрик пары 30 дней", "sort_order": 6},
            {"code": "comeback", "title": "Возвращение", "description": "Пересказ на следующий день после сгоревшего стрика", "sort_order": 7},
            {"code": "brought_friend", "title": "Друг в деле", "description": "Друг по твоей ссылке сдал первый пересказ", "sort_order": 8},
            {"code": "finish", "title": "Финиш", "description": "Первая дочитанная книга", "sort_order": 9},
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM achievements")
