"""Дата отправления может быть пустой

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """В списке отправлений дата не обязательна: программа отгрузок может её не прислать."""
    op.execute("ALTER TABLE mp_shipments ALTER COLUMN date DROP NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE mp_shipments ALTER COLUMN date SET NOT NULL")
