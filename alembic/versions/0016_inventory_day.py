"""Доступ к инвентаризации на день: администратор открывает его грузчикам

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("inventory_day", sa.Date))


def downgrade() -> None:
    op.drop_column("users", "inventory_day")
