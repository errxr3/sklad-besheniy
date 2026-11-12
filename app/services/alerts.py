"""Уведомления начальника: счётчики «Расхождений» на плитках, вкладках и в меню (2026-11-12).

Считаются одним запросом на страницу и только для админа и начальника смены.
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.statuses import IN_TRANSIT, MV_TRANSFER_OUT, PRINTED
from app.timeutil import now_utc

STALE_HOURS = 24

# Плитки группы «Расхождения» и что на них считается
DISCREPANCY_URLS = ("/warehouse/in-transit", "/warehouse/printed-old")


def stuck_in_transit_count(session: Session, *, hours: int = STALE_HOURS) -> int:
    """Зеркала в пути дольше суток (то же, что на странице «Застряли в пути»)."""
    threshold = now_utc() - timedelta(hours=hours)
    sent = (
        select(Unit.id, func.max(Movement.created_at).label("sent_at"))
        .join(Movement, Movement.unit_id == Unit.id)
        .where(Unit.status == IN_TRANSIT, Movement.type == MV_TRANSFER_OUT)
        .group_by(Unit.id)
        .subquery()
    )
    return int(session.scalar(select(func.count()).select_from(sent).where(sent.c.sent_at < threshold)) or 0)


def printed_old_count(session: Session, *, hours: int = STALE_HOURS) -> int:
    """Этикетки «Напечатано» старше суток (то же, что на странице «Этикетки без зеркал»)."""
    threshold = now_utc() - timedelta(hours=hours)
    return int(
        session.scalar(
            select(func.count(Unit.id)).where(Unit.status == PRINTED, Unit.made_at < threshold)
        )
        or 0
    )


def discrepancy_counts(session: Session) -> dict[str, int]:
    """{адрес плитки: сколько висит}. Нулевые не возвращаются."""
    counts = {
        "/warehouse/in-transit": stuck_in_transit_count(session),
        "/warehouse/printed-old": printed_old_count(session),
    }
    return {url: n for url, n in counts.items() if n}
