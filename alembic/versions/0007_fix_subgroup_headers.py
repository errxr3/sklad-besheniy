"""Строки «ПОЛОТНА без креплений» и «АЛЮМИНИЙ на ноге» — заголовки, а не артикулы

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_header(name: str) -> bool:
    """То же правило, что в app.articles.is_subgroup_header (миграция не зависит от кода)."""
    text = name.strip()
    if not text:
        return False
    letters = [ch for ch in text.split()[0] if ch.isalpha()]
    return len(letters) >= 2 and all(ch.isupper() for ch in letters)


def upgrade() -> None:
    """Ранний импорт считал заголовком только строку целиком заглавными буквами.

    Ошибочно созданные «артикулы» превращаются в заголовки, если на них нет ни зеркал,
    ни задач (иначе строка остаётся как есть — её разберёт администратор).
    """
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, name FROM articles WHERE kind = 'article'")).all()
    for article_id, name in rows:
        if not _is_header(str(name)):
            continue
        used = conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM units WHERE article_id = :id)"
                " OR EXISTS (SELECT 1 FROM tasks WHERE article_id = :id)"
            ),
            {"id": article_id},
        ).scalar()
        if used:
            continue
        conn.execute(
            sa.text(
                "UPDATE articles SET kind = 'header', name_norm = NULL, prefix = NULL,"
                " subgroup = NULL, color_hex = NULL, active = true WHERE id = :id"
            ),
            {"id": article_id},
        )


def downgrade() -> None:
    """Обратно не превращаем: заголовок без нормализованного имени артикулом не станет."""
