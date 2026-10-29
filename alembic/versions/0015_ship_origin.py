"""Исходный номер отправления: Ozon при сборке даёт отправлению новый номер

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ship_items", sa.Column("origin", sa.String(64)))
    op.create_index("ix_ship_items_origin", "ship_items", ["market", "origin"])


def downgrade() -> None:
    op.drop_index("ix_ship_items_origin", table_name="ship_items")
    op.drop_column("ship_items", "origin")
