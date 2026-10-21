"""Служебный «сборщик» «Инвентаризация» для ввода остатков

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "Инвентаризация"
SERVICE_PIN_HASH = "*служебный сотрудник, вход запрещён*"


def upgrade() -> None:
    """Зеркала, заведённые при вводе остатков, видны в карточке и журнале как «Инвентаризация»."""
    op.execute(
        sa.text(
            "INSERT INTO users (name, role, pin_hash, active, failed_logins, is_service) "
            "SELECT :name, 'assembler', :pin, true, 0, true "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE name = :name)"
        ).bindparams(name=NAME, pin=SERVICE_PIN_HASH)
    )


def downgrade() -> None:
    """Служебного сотрудника не удаляем: на него ссылаются зеркала."""
