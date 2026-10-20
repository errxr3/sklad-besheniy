"""Роль «Кладовщик» и привязка этикетки к задаче

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES_NEW = ("admin", "shift_lead", "storekeeper", "assembler", "loader")
ROLES_OLD = ("admin", "shift_lead", "assembler", "loader")


def _role_check(roles: tuple[str, ...]) -> None:
    values = ", ".join(f"'{role}'" for role in roles)
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ({values}))")


def upgrade() -> None:
    """Кладовщик печатает этикетки по задачам; этикетка помнит свою задачу."""
    _role_check(ROLES_NEW)
    op.add_column(
        "units",
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL", name="fk_units_task_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_units_task", "units", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_units_task", table_name="units")
    op.drop_column("units", "task_id")
    op.execute("UPDATE users SET active = false, role = 'assembler' WHERE role = 'storekeeper'")
    _role_check(ROLES_OLD)
