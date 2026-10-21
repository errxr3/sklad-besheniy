"""Какие артикулы делают на площадке (начало названия)

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Учительская делает Ambient, Коминтерна — Zerkalo и Alum (решение владельца 2026-10-21)."""
    op.add_column("sites", sa.Column("article_prefixes", sa.Text(), nullable=True))
    op.execute("UPDATE sites SET article_prefixes = 'Ambient' WHERE name = 'Учительская'")
    op.execute("UPDATE sites SET article_prefixes = 'Zerkalo, Alum' WHERE name = 'Коминтерна'")


def downgrade() -> None:
    op.drop_column("sites", "article_prefixes")
