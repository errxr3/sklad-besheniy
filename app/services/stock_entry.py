"""Ввод остатков (инвентаризация на старте): этикетки сразу на склад, лишние — убрать сканом.

На втором этаже склада нет интернета, поэтому этикетки печатаются заранее с запасом и сразу
записываются на склад от служебного «сборщика» «Инвентаризация» — сканировать наклеенные не
нужно. Неиспользованные этикетки потом сканируются там, где есть связь, и убираются со склада
движением «Исправление по инвентаризации» — в брак они не попадают. Убрать можно только
этикетку, заведённую при вводе остатков: настоящие зеркала так не спишешь по ошибке.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Site, Unit, User
from app.scan import clean_code, is_own_label
from app.seed import INVENTORY_SERVICE_NAME
from app.services.units import MAX_PRINT_QTY, ScanResult, UnitError, create_printed_units, stock_count
from app.statuses import IN_STOCK, MV_ACCEPT, MV_INVENTORY_FIX, STATUS_LABELS, WRITTEN_OFF
from app.transitions import apply_transition, lock_unit

MAX_ENTRY_QTY = 300
ENTRY_NOTE = "ввод остатков (инвентаризация)"
REMOVE_NOTE = "лишняя этикетка при вводе остатков"


def inventory_user(session: Session) -> User:
    """Служебный «сборщик» «Инвентаризация»."""
    user = session.scalar(select(User).where(User.name == INVENTORY_SERVICE_NAME))
    if user is None:
        raise UnitError("Нет служебного сотрудника «Инвентаризация» — обратитесь к администратору")
    return user


def enter_stock(
    session: Session, *, article_id: int, qty: int, site_id: int, actor: User
) -> list[Unit]:
    """Выдать номера и сразу поставить зеркала на склад площадки."""
    if not 1 <= qty <= MAX_ENTRY_QTY:
        raise UnitError(f"Количество — от 1 до {MAX_ENTRY_QTY}")
    site = session.get(Site, site_id)
    if site is None or not site.active:
        raise UnitError("Выберите площадку")
    service = inventory_user(session)
    units: list[Unit] = []
    left = qty
    while left > 0:
        chunk = min(left, MAX_PRINT_QTY)
        units += create_printed_units(
            session,
            article_id=article_id,
            qty=chunk,
            site_id=site.id,
            assembler_id=service.id,
            actor_id=actor.id,
        )
        left -= chunk
    for unit in units:
        apply_transition(
            session,
            unit,
            IN_STOCK,
            movement_type=MV_ACCEPT,
            user_id=actor.id,
            to_site_id=site.id,
            note=ENTRY_NOTE,
        )
    return units


def remove_extra(session: Session, *, code: str, actor: User) -> ScanResult:
    """Скан неиспользованной этикетки ввода остатков — убрать её со склада."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO", "Отсканируйте лишнюю этикетку")
    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)
    article_name = unit.article.name
    if unit.assembler is None or unit.assembler.name != INVENTORY_SERVICE_NAME:
        return ScanResult(
            False,
            "Это не этикетка ввода остатков",
            "убрать можно только этикетку, напечатанную в «Вводе остатков»",
            number=number,
            article=article_name,
        )
    if unit.status != IN_STOCK:
        current = STATUS_LABELS.get(unit.status, unit.status)
        return ScanResult(
            False, f"Уже «{current}»", f"этикетка {number}", number=number, article=article_name
        )
    apply_transition(
        session,
        unit,
        WRITTEN_OFF,
        movement_type=MV_INVENTORY_FIX,
        user_id=actor.id,
        note=REMOVE_NOTE,
    )
    return ScanResult(
        True,
        article_name,
        "Лишняя этикетка убрана",
        number=number,
        article=article_name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )
