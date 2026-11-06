"""«Нет на полке»: начальник смены сканирует этикетку маркетплейса, под которую не нашли зеркало.

Решение владельца 2026-11-06. Оператор склада подходит с этикеткой, а зеркала нет ни на полке,
ни в задачах. Начальник сканирует наклейку Ozon/WB/СДЭК: этикетка снимается с отгрузки, а если
зеркала этого артикула ещё числятся на складе площадки — одно списывается как недостача.
Иначе склад снова отдал бы «зеркало-призрак» следующему заказу.

Сначала списывается старое без этикетки (его и так не видно поштучно), потом самое раннее с нашей
этикеткой. Ошиблись — «Вернуть в отгрузку» на странице партий возвращает и этикетку, и зеркало.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShipItem, Unit, User
from app.services.batches import _lock, allocate
from app.services.legacy_stock import pick_legacy_unit
from app.services.shipping import find_ship_item
from app.services.units import ScanResult, stock_count
from app.statuses import IN_STOCK, MV_INVENTORY_FIX, SHIP_LOADED, SHIP_REMOVED, SHIP_WAIT, WRITTEN_OFF
from app.timeutil import now_utc
from app.transitions import apply_transition

NOT_ON_SHELF = "нет на полке"
WRITE_OFF_NOTE = "нет на полке при отгрузке, этикетка {posting}"
BACK_NOTE = "вернули в отгрузку после «нет на полке», этикетка {posting}"


def _phantom(session: Session, *, article_id: int, site_id: int) -> Unit | None:
    """Зеркало, которое числится на складе, но которого нет: сначала старое, потом самое раннее."""
    old = pick_legacy_unit(session, article_id=article_id, site_id=site_id)
    if old is not None:
        return old
    return session.scalars(
        select(Unit)
        .where(Unit.status == IN_STOCK, Unit.article_id == article_id, Unit.site_id == site_id)
        .order_by(Unit.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Unit)
    ).first()


def not_on_shelf(session: Session, *, code: str, actor: User, site_ids: list[int] | None) -> ScanResult:
    """Снять этикетку с отгрузки и списать одно зеркало-призрак, если оно числится."""
    _lock(session)  # по одному с приёмом партий и пересчётом «что едет»
    item = find_ship_item(session, code)
    if item is None:
        return ScanResult(False, "Этикетки нет в отгрузках", f"код {code}")
    item = session.scalar(select(ShipItem).where(ShipItem.id == item.id).with_for_update(of=ShipItem))
    assert item is not None
    article = item.article.name if item.article else item.article_name
    if site_ids is not None and item.site_id not in site_ids:
        return ScanResult(False, "Это этикетка другой площадки", article=article)
    if item.state == SHIP_LOADED:
        return ScanResult(False, "Уже погружена", "снимать нечего", article=article)
    if item.state == SHIP_REMOVED:
        return ScanResult(False, "Уже снята с отгрузки", item.removed_reason or "", article=article)
    assert item.state == SHIP_WAIT

    item.state = SHIP_REMOVED
    item.removed_reason = NOT_ON_SHELF
    item.removed_by = actor.id
    item.removed_at = now_utc()
    detail = "на складе и так не числилось"
    stock = None
    if item.article_id is not None and item.site_id is not None:
        unit = _phantom(session, article_id=item.article_id, site_id=item.site_id)
        if unit is not None:
            apply_transition(
                session, unit, WRITTEN_OFF, movement_type=MV_INVENTORY_FIX, user_id=actor.id,
                note=WRITE_OFF_NOTE.format(posting=item.posting),
            )
            item.unit_id = unit.id  # чтобы «Вернуть в отгрузку» вернуло и это зеркало
            detail = "списано со склада: 1"
        stock = stock_count(session, item.article_id, item.site_id)
    session.flush()
    allocate(session)
    return ScanResult(True, f"Снято: {article}", detail, number=item.posting, article=article, stock=stock)


def restore_written_off(session: Session, item: ShipItem, *, actor_id: int | None) -> None:
    """Вернули этикетку в отгрузку — вернуть на склад и списанное по ней зеркало."""
    if item.removed_reason != NOT_ON_SHELF or item.unit_id is None:
        return
    unit = session.scalar(select(Unit).where(Unit.id == item.unit_id).with_for_update(of=Unit))
    if unit is not None and unit.status == WRITTEN_OFF:
        apply_transition(
            session, unit, IN_STOCK, movement_type=MV_INVENTORY_FIX, user_id=actor_id,
            note=BACK_NOTE.format(posting=item.posting),
        )
    item.unit_id = None
