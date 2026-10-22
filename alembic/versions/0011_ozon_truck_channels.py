"""Озон 1, Озон 2, Озон 3 — отдельные каналы: машина выбирается при открытии

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WHERE_OPEN = sa.text("closed_at IS NULL")


def upgrade() -> None:
    """Две машины Ozon можно грузить одновременно; открытый Ozon без машины — «Озон1»."""
    op.drop_index("uq_pallets_one_open_per_channel", table_name="pallets", postgresql_where=WHERE_OPEN)
    op.execute(sa.text("UPDATE pallets SET note = 'Озон1' WHERE closed_at IS NULL AND channel = 'Ozon'"))
    op.create_index(
        "uq_pallets_one_open_per_channel",
        "pallets",
        ["site_id", "channel", sa.text("coalesce(note, '')")],
        unique=True,
        postgresql_where=WHERE_OPEN,
    )


def downgrade() -> None:
    """Один открытый Ozon на площадку (не сработает, если открыто несколько машин)."""
    op.drop_index("uq_pallets_one_open_per_channel", table_name="pallets", postgresql_where=WHERE_OPEN)
    op.create_index(
        "uq_pallets_one_open_per_channel",
        "pallets",
        ["site_id", "channel"],
        unique=True,
        postgresql_where=WHERE_OPEN,
    )
