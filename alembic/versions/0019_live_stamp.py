"""Метка живых таблиц: любое изменение склада, плана, каналов, смен и задач сдвигает счётчик

Счётчик — sequence, а не строка таблицы: сканы не ждут друг друга на одной блокировке.
Триггеры на уровне запроса (FOR EACH STATEMENT): массовая операция сдвигает счётчик один раз.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "units", "movements", "ship_batches", "ship_items", "ship_codes", "ship_demand",
    "pallets", "shifts", "manual_channels", "tasks",
)


def upgrade() -> None:
    op.execute("CREATE SEQUENCE live_stamp_seq")
    op.execute(
        "CREATE FUNCTION live_stamp_bump() RETURNS trigger LANGUAGE plpgsql AS $$"
        " BEGIN PERFORM nextval('live_stamp_seq'); RETURN NULL; END $$"
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_live AFTER INSERT OR UPDATE OR DELETE ON {table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION live_stamp_bump()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER trg_{table}_live ON {table}")
    op.execute("DROP FUNCTION live_stamp_bump()")
    op.execute("DROP SEQUENCE live_stamp_seq")
