"""Роль «Возвраты», описание закрытия канала, служебный сборщик для зеркал без маркировки

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES_NEW = ("admin", "shift_lead", "storekeeper", "assembler", "loader", "returns")
ROLES_OLD = ("admin", "shift_lead", "storekeeper", "assembler", "loader")
RETURNS_SERVICE_NAME = "Возврат без ШК"
SERVICE_PIN_HASH = "*служебный сотрудник, вход запрещён*"


def _role_check(roles: tuple[str, ...]) -> None:
    values = ", ".join(f"'{role}'" for role in roles)
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ({values}))")


def upgrade() -> None:
    _role_check(ROLES_NEW)
    # Описание при закрытии канала погрузки (для Ozon — машина: Озон1…Озон3)
    op.add_column("pallets", sa.Column("note", sa.Text(), nullable=True))
    # Служебный «сборщик» для возвратов без нашей этикетки: в базе видно, что зеркало
    # пришло с возвратов без личного ШК
    op.execute(
        sa.text(
            "INSERT INTO users (name, role, pin_hash, active, failed_logins, is_service) "
            "SELECT :name, 'assembler', :pin, true, 0, true "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE name = :name)"
        ).bindparams(name=RETURNS_SERVICE_NAME, pin=SERVICE_PIN_HASH)
    )


def downgrade() -> None:
    op.drop_column("pallets", "note")
    op.execute("UPDATE users SET active = false, role = 'loader' WHERE role = 'returns'")
    _role_check(ROLES_OLD)
