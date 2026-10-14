"""Отдельный код ссылки «читать вместе» (обычную ссылку в напарники нельзя превратить в доступ к книге)
и настоящая пауза: дни паузы помечаются отдельно, а не как сданные заранее.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enrollments", sa.Column("together_code", sa.String(length=16), nullable=True))
    op.create_unique_constraint("uq_enrollments_together_code", "enrollments", ["together_code"])
    op.add_column("enrollments", sa.Column("paused_from", sa.Date(), nullable=True))
    op.add_column("enrollments", sa.Column("paused_until", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("enrollments", "paused_until")
    op.drop_column("enrollments", "paused_from")
    op.drop_constraint("uq_enrollments_together_code", "enrollments", type_="unique")
    op.drop_column("enrollments", "together_code")
