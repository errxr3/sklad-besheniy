"""Полный список типов движений в ограничении movements.type

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES = (
    "print",
    "accept",
    "label_void",
    "transfer_out",
    "transfer_in",
    "ship",
    "ship_cancel",
    "write_off",
    "return_ok",
    "return_bad",
    "inventory_fix",
    "assembler_change",
)


def upgrade() -> None:
    """Пересоздать ограничение, чтобы базы, созданные ранней версией миграции 0001,
    тоже знали типы `print` и `assembler_change`."""
    values = ", ".join(f"'{value}'" for value in TYPES)
    op.execute("ALTER TABLE movements DROP CONSTRAINT IF EXISTS ck_movements_type")
    op.execute(f"ALTER TABLE movements ADD CONSTRAINT ck_movements_type CHECK (type IN ({values}))")


def downgrade() -> None:
    op.execute("ALTER TABLE movements DROP CONSTRAINT IF EXISTS ck_movements_type")
