"""Гибкий темп: «перескажу ещё один отрезок» сегодня.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enrollments", sa.Column("more_day", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("enrollments", "more_day")
