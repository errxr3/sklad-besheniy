"""Ввод остатков (инвентаризация на старте): этикетки сразу на склад, лишние — убрать сканом.

На втором этаже склада нет интернета, поэтому этикетки печатаются заранее с запасом и сразу
записываются на склад от служебного «сборщика» «Инвентаризация» — сканировать наклеенные не
нужно. Неиспользованные этикетки потом сканируются там, где есть связь, и убираются со склада
движением «Исправление по инвентаризации» — в брак они не попадают. Убрать можно только
этикетку, заведённую при вводе остатков: настоящие зеркала так не спишешь по ошибке.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Site, Unit, User
from app.scan import clean_code, is_own_label
from app.seed import INVENTORY_SERVICE_NAME
from app.services.task_labels import BUTTON_ACCEPT_NOTE
from app.services.units import MAX_PRINT_QTY, ScanResult, UnitError, create_printed_units, stock_count
from app.statuses import IN_STOCK, MV_ACCEPT, MV_INVENTORY_FIX, STATUS_LABELS, WRITTEN_OFF
from app.transitions import apply_transition, lock_unit

MAX_ENTRY_QTY = 300
ENTRY_NOTE = "ввод остатков (инвентаризация)"
REMOVE_NOTE = "лишняя этикетка: не наклеена"


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


def _removable(session: Session, unit: Unit) -> bool:
    """Лишней может быть только этикетка, на склад которой не попала сканом.

    Это этикетки «Ввода остатков» и этикетки задач, сданных кнопкой «Собрал всё»: сайт не
    знает, какие из них наклеены. Зеркало, принятое сканом, так не убрать.
    """
    if unit.assembler is not None and unit.assembler.name == INVENTORY_SERVICE_NAME:
        return True
    last_accept_note = session.scalar(
        select(Movement.note)
        .where(Movement.unit_id == unit.id, Movement.type == MV_ACCEPT)
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .limit(1)
    )
    return last_accept_note == BUTTON_ACCEPT_NOTE


def remove_extra(session: Session, *, code: str, actor: User) -> ScanResult:
    """Скан неиспользованной этикетки (ввод остатков или «Собрал всё») — убрать со склада."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO", "Отсканируйте лишнюю этикетку")
    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)
    article_name = unit.article.name
    if not _removable(session, unit):
        return ScanResult(
            False,
            "Эту этикетку так убрать нельзя",
            "только из «Ввода остатков» или сданную кнопкой «Собрал всё»",
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
