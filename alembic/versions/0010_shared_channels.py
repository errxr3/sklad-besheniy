"""Общие каналы погрузки: один открытый канал на площадку, грузят все грузчики площадки

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Канал больше не принадлежит грузчику: Ozon на Учительской открыт один на всех."""
    op.drop_index(
        "uq_pallets_one_open_per_loader",
        table_name="pallets",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # Если на площадке открыто несколько одинаковых каналов — остаётся самый ранний,
    # зеркала остальных переносятся в него, пустые дубли закрываются
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id, first_value(id) OVER (
                    PARTITION BY site_id, channel ORDER BY opened_at, id
                ) AS keep_id
                FROM pallets WHERE closed_at IS NULL
            )
            UPDATE units u SET pallet_id = r.keep_id
            FROM ranked r WHERE u.pallet_id = r.id AND r.id <> r.keep_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id, first_value(id) OVER (
                    PARTITION BY site_id, channel ORDER BY opened_at, id
                ) AS keep_id
                FROM pallets WHERE closed_at IS NULL
            )
            DELETE FROM pallets p USING ranked r
            WHERE p.id = r.id AND r.id <> r.keep_id
              AND NOT EXISTS (SELECT 1 FROM units u WHERE u.pallet_id = p.id)
            """
        )
    )
    op.create_index(
        "uq_pallets_one_open_per_channel",
        "pallets",
        ["site_id", "channel"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    """Вернуть канал на грузчика (если у грузчика открыто несколько каналов — не сработает)."""
    op.drop_index(
        "uq_pallets_one_open_per_channel",
        table_name="pallets",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "uq_pallets_one_open_per_loader",
        "pallets",
        ["opened_by"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
