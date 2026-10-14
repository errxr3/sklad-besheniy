"""Площадки, служебный сборщик и настройки по умолчанию

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# PIN служебного сотрудника заведомо невалиден: войти под ним нельзя
SERVICE_PIN_HASH = "*служебный сотрудник, вход запрещён*"

SETTINGS = (
    ("label_width_mm", "58"),
    ("label_height_mm", "40"),
    ("require_mp_barcode", "0"),
    ("verify_order", "0"),
    ("ship_cancel_minutes", "10"),
)


def upgrade() -> None:
    sites = sa.table(
        "sites",
        sa.column("name", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("sort_order", sa.Integer),
        sa.column("geo_radius_m", sa.Integer),
        sa.column("geo_check_enabled", sa.Boolean),
        sa.column("labeling_done", sa.Boolean),
    )
    op.bulk_insert(
        sites,
        [
            {
                "name": "Учительская",
                "active": True,
                "sort_order": 1,
                "geo_radius_m": 300,
                "geo_check_enabled": False,
                "labeling_done": False,
            },
            {
                "name": "Коминтерна",
                "active": True,
                "sort_order": 2,
                "geo_radius_m": 300,
                "geo_check_enabled": False,
                "labeling_done": False,
            },
        ],
    )

    users = sa.table(
        "users",
        sa.column("name", sa.String),
        sa.column("role", sa.String),
        sa.column("pin_hash", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("failed_logins", sa.Integer),
        sa.column("is_service", sa.Boolean),
    )
    op.bulk_insert(
        users,
        [
            {
                "name": "Остаток на запуск",
                "role": "assembler",
                "pin_hash": SERVICE_PIN_HASH,
                "active": True,
                "failed_logins": 0,
                "is_service": True,
            }
        ],
    )

    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    op.bulk_insert(settings, [{"key": key, "value": value} for key, value in SETTINGS])


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key IN ('label_width_mm', 'label_height_mm', "
               "'require_mp_barcode', 'verify_order', 'ship_cancel_minutes')")
    op.execute("DELETE FROM users WHERE is_service = true AND name = 'Остаток на запуск'")
    op.execute("DELETE FROM sites WHERE name IN ('Учительская', 'Коминтерна')")
