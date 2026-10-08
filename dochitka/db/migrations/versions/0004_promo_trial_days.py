"""Пробный доступ по промокоду: срок в днях.

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
    op.add_column("promo_codes", sa.Column("trial_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_codes", "trial_days")
