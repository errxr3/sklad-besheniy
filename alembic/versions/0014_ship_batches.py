"""Партии BLAGO OTGRUZKI, заказы дня, вопросы начальнику, срочные задачи

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ship_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("truck", sa.String(10), nullable=False, server_default=""),
        sa.Column("source", sa.String(160)),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("day", "channel", "truck", name="uq_ship_batches_kind"),
    )
    op.create_table(
        "ship_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("ship_batches.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("posting", sa.String(64), nullable=False),
        sa.Column("article_name", sa.String(200), nullable=False),
        sa.Column("article_id", sa.Integer, sa.ForeignKey("articles.id")),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id")),
        sa.Column("ordered_at", TS),
        sa.Column("state", sa.String(10), nullable=False, server_default="wait"),
        sa.Column("unit_id", sa.BigInteger, sa.ForeignKey("units.id")),
        sa.Column("pallet_id", sa.Integer, sa.ForeignKey("pallets.id")),
        sa.Column("loaded_at", TS),
        sa.Column("loaded_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("moved_note", sa.String(120)),
        sa.Column("removed_reason", sa.String(200)),
        sa.Column("removed_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("removed_at", TS),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("market", "posting", name="uq_ship_items_posting"),
        sa.CheckConstraint("state IN ('wait', 'loaded', 'removed')", name="ck_ship_items_state"),
    )
    op.create_index("ix_ship_items_batch", "ship_items", ["batch_id"])
    op.create_index("ix_ship_items_site_state", "ship_items", ["site_id", "state"])
    op.create_table(
        "ship_codes",
        sa.Column("code", sa.String(80), primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("ship_items.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_ship_codes_item", "ship_codes", ["item_id"])
    op.create_table(
        "ship_demand",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("truck", sa.String(10), nullable=False, server_default=""),
        sa.Column("posting", sa.String(64), nullable=False),
        sa.Column("article_name", sa.String(200), nullable=False),
        sa.Column("article_id", sa.Integer, sa.ForeignKey("articles.id")),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id")),
        sa.Column("qty", sa.Integer, nullable=False, server_default="1"),
        sa.Column("ordered_at", TS),
        sa.Column("decision", sa.String(10), nullable=False, server_default="no"),
        sa.Column("reason", sa.String(200)),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("market", "posting", "article_name", name="uq_ship_demand_line"),
    )
    op.create_index("ix_ship_demand_day", "ship_demand", ["day"])
    op.create_table(
        "ship_questions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("article_id", sa.Integer, sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("need", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stock", sa.Integer, nullable=False, server_default="0"),
        sa.Column("in_work", sa.Integer, nullable=False, server_default="0"),
        sa.Column("short", sa.Integer, nullable=False, server_default="0"),
        sa.Column("channels", sa.String(120)),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("promised", sa.Integer, nullable=False, server_default="0"),
        sa.Column("due_at", TS),
        sa.Column("answered_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("answered_at", TS),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("day", "site_id", "article_id", name="uq_ship_questions_kind"),
    )
    op.add_column("tasks", sa.Column("urgent_qty", sa.Integer))
    op.add_column("tasks", sa.Column("urgent_due", TS))
    op.add_column("tasks", sa.Column("urgent_since", TS))
    op.add_column("tasks", sa.Column("urgent_note", sa.String(120)))


def downgrade() -> None:
    for column in ("urgent_note", "urgent_since", "urgent_due", "urgent_qty"):
        op.drop_column("tasks", column)
    op.drop_table("ship_questions")
    op.drop_index("ix_ship_demand_day", table_name="ship_demand")
    op.drop_table("ship_demand")
    op.drop_index("ix_ship_codes_item", table_name="ship_codes")
    op.drop_table("ship_codes")
    op.drop_index("ix_ship_items_site_state", table_name="ship_items")
    op.drop_index("ix_ship_items_batch", table_name="ship_items")
    op.drop_table("ship_items")
    op.drop_table("ship_batches")
