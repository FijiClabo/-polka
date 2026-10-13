"""Профиль: своё фото или без фото, ссылка на Telegram для друзей, напарника и клуба.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar", sa.String(length=16), nullable=False, server_default="telegram"))
    op.add_column("users", sa.Column("avatar_token", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("show_username", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("users", "show_username")
    op.drop_column("users", "avatar_token")
    op.drop_column("users", "avatar")
