"""Режим проверки сканов (решение владельца 2026-11-10).

Пробный день: склад не решает по остаткам — едет всё, что прислал BLAGO. Погрузка, «Отгрузка» и
«План отгрузки» работают по-настоящему. После проверки «Стереть проверку» убирает всё, что
накопилось с момента включения: отгрузки отменяются (зеркала снова на складе — журнал движений
не удаляется по правилам склада, в нём остаётся пара «отгрузка + отмена»), партии, заказы дня
и вопросы удаляются, открытые каналы закрываются, режим выключается.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Movement, Pallet, ShipBatch, ShipDemand, ShipItem, ShipQuestion, User
from app.settings_store import SCAN_CHECK_KEY, get_value, scan_check_on, set_value
from app.statuses import IN_STOCK, MV_SHIP, MV_SHIP_CANCEL, SHIPPED
from app.timeutil import now_utc, to_moscow
from app.transitions import apply_transition, lock_unit_by_id

SINCE_KEY = "scan_check_since"
WIPE_NOTE = "стёрта проверка сканов"


def check_since(session: Session) -> datetime | None:
    """Когда включили режим проверки (None — не включали или уже стёрли)."""
    raw = get_value(session, SINCE_KEY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def set_check_mode(session: Session, on: bool) -> bool:
    """Включить/выключить режим. Возвращает True, если состояние изменилось.

    Время включения запоминается при первом включении и сохраняется после выключения,
    чтобы проверку можно было стереть и потом.
    """
    was = scan_check_on(session)
    set_value(session, SCAN_CHECK_KEY, "1" if on else "0")
    if on and not was and check_since(session) is None:
        set_value(session, SINCE_KEY, now_utc().isoformat())
    if was != on:
        from app.services.batches import allocate

        allocate(session)  # план пересчитывается сразу: всё едет / снова по остаткам
    return was != on


@dataclass
class WipeResult:
    shipments: int = 0
    items: int = 0
    batches: int = 0
    demand: int = 0
    pallets: int = 0


def wipe_check(session: Session, *, actor: User) -> WipeResult:
    """Стереть всё, что сделано с момента включения режима проверки."""
    since = check_since(session)
    result = WipeResult()
    if since is None:
        return result
    # 1. Отгрузки проверки: зеркала снова на складе («Отгрузка» их больше не считает)
    shipped = session.scalars(
        select(Movement.unit_id).where(Movement.type == MV_SHIP, Movement.created_at >= since).distinct()
    ).all()
    for unit_id in shipped:
        unit = lock_unit_by_id(session, unit_id)
        if unit is None or unit.status != SHIPPED:
            continue
        apply_transition(
            session, unit, IN_STOCK, movement_type=MV_SHIP_CANCEL, user_id=actor.id, note=WIPE_NOTE
        )
        result.shipments += 1
    # 2. Партии этикеток и заказы дня, пришедшие за проверку
    batch_ids = list(session.scalars(select(ShipBatch.id).where(ShipBatch.created_at >= since)).all())
    if batch_ids:
        result.items = session.execute(delete(ShipItem).where(ShipItem.batch_id.in_(batch_ids))).rowcount or 0
        result.batches = session.execute(delete(ShipBatch).where(ShipBatch.id.in_(batch_ids))).rowcount or 0
    # этикетки старых партий, погруженные за проверку, снова ждут погрузки
    for item in session.scalars(select(ShipItem).where(ShipItem.loaded_at >= since)).all():
        item.state, item.unit_id, item.pallet_id = "wait", None, None
        item.loaded_at = item.loaded_by = item.moved_note = None
    result.demand = session.execute(delete(ShipDemand).where(ShipDemand.updated_at >= since)).rowcount or 0
    session.execute(delete(ShipQuestion).where(ShipQuestion.day >= to_moscow(since).date()))
    # 3. Каналы, открытые за проверку, закрываются (удалять нельзя: на них ссылается журнал)
    for pallet in session.scalars(
        select(Pallet).where(Pallet.opened_at >= since, Pallet.closed_at.is_(None))
    ).all():
        pallet.closed_at = now_utc()
        pallet.note = ((pallet.note or "") + ", " + WIPE_NOTE).strip(", ")[:120]
        result.pallets += 1
    # 4. Режим выключен, отметка о начале снята
    set_value(session, SCAN_CHECK_KEY, "0")
    set_value(session, SINCE_KEY, "")
    session.flush()
    from app.services.batches import allocate

    allocate(session)
    return result

