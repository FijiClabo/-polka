"""Книжные клубы: ведущий делится планом своей книги, участники идут по нему со своими экземплярами.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clubs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("host_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id", ondelete="SET NULL"), nullable=True),
        sa.Column("meet_date", sa.Date(), nullable=True),
        sa.Column("promo_code", sa.String(length=32), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_clubs_code", "clubs", ["code"], unique=True)
    op.create_index("ix_clubs_host_user_id", "clubs", ["host_user_id"])
    with op.batch_alter_table("enrollments") as b:
        b.add_column(sa.Column("club_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_enrollments_club_id_clubs", "clubs", ["club_id"], ["id"], ondelete="SET NULL")
        b.create_index("ix_enrollments_club_id", ["club_id"])


def downgrade() -> None:
    with op.batch_alter_table("enrollments") as b:
        b.drop_index("ix_enrollments_club_id")
        b.drop_constraint("fk_enrollments_club_id_clubs", type_="foreignkey")
        b.drop_column("club_id")
    op.drop_index("ix_clubs_host_user_id", table_name="clubs")
    op.drop_index("ix_clubs_code", table_name="clubs")
    op.drop_table("clubs")
