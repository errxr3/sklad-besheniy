"""Старый остаток без этикеток: зеркала, заведённые числом, наклейку не получают

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("units", sa.Column("unlabeled", sa.Boolean, nullable=False, server_default="false"))
    op.create_index(
        "ix_units_unlabeled_stock", "units", ["site_id", "article_id"],
        postgresql_where=sa.text("unlabeled AND status = 'in_stock'"),
    )


def downgrade() -> None:
    op.drop_index("ix_units_unlabeled_stock", table_name="units")
    op.drop_column("units", "unlabeled")
