"""Уведомления начальника: красные счётчики на плитках, вкладках и в меню (2026-11-12).

Что считается: застряли в пути, этикетки без зеркал, вопросы по нехватке, «не хватит»
в плане отгрузки и каналы, не закрытые к вечеру. Только для админа и начальника смены,
один раз за страницу.
"""

from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Movement, Pallet, Unit
from app.statuses import IN_TRANSIT, MV_TRANSFER_OUT, PRINTED
from app.timeutil import now_utc, to_moscow, today_msk

STALE_HOURS = 24

# Плитки группы «Расхождения» и что на них считается
DISCREPANCY_URLS = ("/warehouse/in-transit", "/warehouse/printed-old")

# С этого часа (Москва) открытый канал считается забытым
CHANNELS_EVENING_HOUR = 19

# Что попадает в счётчик пункта меню: «Склад» у начальника, «Начальник смены» у админа
WAREHOUSE_ALERT_URLS = ("/shipments/plan", "/ship/questions", *DISCREPANCY_URLS)
MENU_ALERT_URLS: dict[str, tuple[str, ...]] = {
    "/warehouse": WAREHOUSE_ALERT_URLS,
    "/board": ("/board",),
    "/admin/section/lead": ("/board", *WAREHOUSE_ALERT_URLS),
}


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


def evening_open_channels(session: Session, *, site_ids: list[int] | None = None) -> int:
    """Каналы, не закрытые к вечеру: с 19:00 по Москве или открытые ещё вчера."""
    now = now_utc()
    query = select(Pallet.opened_at).where(Pallet.closed_at.is_(None))
    if site_ids is not None:
        query = query.where(Pallet.site_id.in_(site_ids or [0]))
    opened = list(session.scalars(query).all())
    if to_moscow(now).hour >= CHANNELS_EVENING_HOUR:
        return len(opened)
    return sum(1 for moment in opened if today_msk(moment) < today_msk(now))


_PLAN_CACHE: dict[tuple[str, str], int] = {}
_STAMP_SQL = text("SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM live_stamp_seq")


def plan_short_count(session: Session) -> int:
    """Сколько артикулов в плане отгрузки на сегодня «не хватит».

    План считается долго, поэтому результат запоминается до любого изменения склада
    (метка live_stamp сдвигается триггерами при каждом скане, задаче, отгрузке и плане).
    """
    from app.services.ship_plan import build_ship_plan

    key = (today_msk().isoformat(), str(session.scalar(_STAMP_SQL) or ""))
    if key not in _PLAN_CACHE:
        plan = build_ship_plan(session, day=today_msk())
        _PLAN_CACHE.clear()
        _PLAN_CACHE[key] = sum(1 for row in plan.rows if not row.is_header and row.state == "no")
    return _PLAN_CACHE[key]


def lead_alerts(session: Session, *, site_ids: list[int] | None = None) -> dict[str, int]:
    """Все счётчики начальника: {адрес вкладки или плитки: сколько}. Нулевые не возвращаются.

    `site_ids` — площадка начальника (вопросы и каналы только его); None — все (админ).
    """
    from app.services.batches import count_open_questions

    counts = dict(discrepancy_counts(session))
    counts["/ship/questions"] = count_open_questions(session, site_ids=site_ids)
    counts["/shipments/plan"] = plan_short_count(session)
    counts["/board"] = evening_open_channels(session, site_ids=site_ids)
    return {url: n for url, n in counts.items() if n}


def menu_badge(alerts: dict[str, int], url: str) -> int:
    """Число на пункте меню: сумма счётчиков, которые под ним лежат."""
    return sum(alerts.get(u, 0) for u in MENU_ALERT_URLS.get(url, ()))
