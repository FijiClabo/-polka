"""«Читаем вместе»: копия плана друга без текста, метки начала и конца отрезка, флаг пары.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("books") as b:
        b.add_column(sa.Column("shared_from_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_books_shared_from_id_books", "books", ["shared_from_id"], ["id"], ondelete="SET NULL")
    op.add_column("segments", sa.Column("start_words", sa.String(length=200), nullable=True))
    op.add_column("segments", sa.Column("end_words", sa.String(length=200), nullable=True))
    op.add_column("pairs", sa.Column("together", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("pairs", "together")
    op.drop_column("segments", "end_words")
    op.drop_column("segments", "start_words")
    with op.batch_alter_table("books") as b:
        b.drop_constraint("fk_books_shared_from_id_books", type_="foreignkey")
        b.drop_column("shared_from_id")
