"""Вход по логину и паролю: users.login, имя и фамилия отдельно

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Старые сотрудники: имя делится по первому пробелу, логин admin / user<номер>."""
    op.add_column("users", sa.Column("first_name", sa.String(60), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.String(60), nullable=True))
    op.add_column("users", sa.Column("login", sa.String(40), nullable=True))
    op.execute(
        sa.text(
            "UPDATE users SET first_name = split_part(name, ' ', 1),"
            " last_name = nullif(trim(substr(name, length(split_part(name, ' ', 1)) + 1)), '')"
        )
    )
    # Первый администратор получает логин admin, остальные — user<номер>
    op.execute(
        sa.text(
            "UPDATE users SET login = CASE WHEN id = (SELECT min(id) FROM users WHERE role = 'admin'"
            " AND NOT is_service) THEN 'admin' ELSE 'user' || id END WHERE NOT is_service"
        )
    )
    op.create_index("uq_users_login", "users", [sa.text("lower(login)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_login", table_name="users")
    op.drop_column("users", "login")
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
