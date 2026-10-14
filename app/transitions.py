"""Переходы статусов зеркала — единственная точка изменения статуса (SPEC §3, §8).

Таблица разрешённых переходов задана одним словарём `ALLOWED_TRANSITIONS`.
Статус зеркала меняется только функцией `apply_transition`, которая в той же
транзакции пишет запись в журнал `movements`. Прямое присваивание `unit.status`
в любом другом модуле запрещено и проверяется тестом.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.statuses import (
    IN_STOCK,
    IN_TRANSIT,
    MV_ACCEPT,
    MV_INVENTORY_FIX,
    MV_LABEL_VOID,
    MV_RETURN_BAD,
    MV_RETURN_OK,
    MV_SHIP,
    MV_SHIP_CANCEL,
    MV_TRANSFER_IN,
    MV_TRANSFER_OUT,
    MV_WRITE_OFF,
    PRINTED,
    SHIPPED,
    STATUS_LABELS,
    WRITTEN_OFF,
)
from app.timeutil import fmt_dt

# Ровно 8 разрешённых переходов (SPEC §3). Значение — типы движений,
# которыми этот переход может быть выполнен.
ALLOWED_TRANSITIONS: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    (PRINTED, IN_STOCK): (MV_ACCEPT,),
    (PRINTED, WRITTEN_OFF): (MV_LABEL_VOID,),
    (IN_STOCK, IN_TRANSIT): (MV_TRANSFER_OUT,),
    (IN_TRANSIT, IN_STOCK): (MV_TRANSFER_IN,),
    (IN_STOCK, SHIPPED): (MV_SHIP,),
    (IN_STOCK, WRITTEN_OFF): (MV_WRITE_OFF,),
    (SHIPPED, IN_STOCK): (MV_RETURN_OK, MV_SHIP_CANCEL),
    (SHIPPED, WRITTEN_OFF): (MV_RETURN_BAD,),
}

# Исключение SPEC §3: исправления по итогам инвентаризации могут переводить зеркало
# в «На складе» или «Списано» из любого статуса, кроме уже совпадающего.
INVENTORY_FIX_TARGETS: Final[tuple[str, ...]] = (IN_STOCK, WRITTEN_OFF)


class TransitionError(Exception):
    """Переход запрещён. Текст предназначен пользователю."""


def is_allowed(from_status: str, to_status: str, movement_type: str) -> bool:
    """Разрешён ли переход данным типом движения."""
    if movement_type == MV_INVENTORY_FIX:
        return to_status in INVENTORY_FIX_TARGETS and from_status != to_status
    return movement_type in ALLOWED_TRANSITIONS.get((from_status, to_status), ())


def describe_refusal(unit: Unit, to_status: str) -> str:
    """Понятный текст отказа с текущим статусом зеркала."""
    current = STATUS_LABELS.get(unit.status, unit.status)
    if unit.status == to_status:
        return f"Уже «{current}»"
    return f"Нельзя: зеркало в статусе «{current}»"


def lock_unit(session: Session, number: str) -> Unit | None:
    """Найти зеркало по номеру и заблокировать строку до конца транзакции.

    Блокировка `SELECT ... FOR UPDATE` гарантирует, что два одновременных скана
    одного зеркала дадут ровно один успех (SPEC §8).
    """
    # `of=Unit` — блокируем строку таблицы units: сопутствующие таблицы подтягиваются
    # внешним соединением, а его PostgreSQL блокировать не умеет.
    return session.scalars(
        select(Unit).where(Unit.number == number).with_for_update(of=Unit).limit(1)
    ).first()


def lock_unit_by_id(session: Session, unit_id: int) -> Unit | None:
    """То же, но по идентификатору."""
    return session.scalars(
        select(Unit).where(Unit.id == unit_id).with_for_update(of=Unit).limit(1)
    ).first()


def apply_transition(
    session: Session,
    unit: Unit,
    to_status: str,
    *,
    movement_type: str,
    user_id: int | None,
    shift_id: int | None = None,
    to_site_id: int | None = None,
    note: str | None = None,
    ref_id: int | None = None,
    channel: str | None = None,
    pallet_id: int | None = None,
    mp_barcode: str | None = None,
    moment: datetime | None = None,
) -> Movement:
    """Перевести зеркало в новый статус и записать движение в одной транзакции.

    Единственное место в системе, где присваивается `unit.status`.
    """
    from_status = unit.status
    if not is_allowed(from_status, to_status, movement_type):
        raise TransitionError(
            f"Переход «{STATUS_LABELS.get(from_status, from_status)}» → "
            f"«{STATUS_LABELS.get(to_status, to_status)}» "
            f"типом «{movement_type}» не разрешён"
        )

    from_site_id = unit.site_id
    unit.status = to_status

    if to_site_id is not None:
        unit.site_id = to_site_id
    if to_status == IN_STOCK:
        unit.accepted_at = moment or _now()
        unit.pallet_id = None
        unit.channel = None
    if channel is not None:
        unit.channel = channel
    if pallet_id is not None:
        unit.pallet_id = pallet_id
    if mp_barcode is not None:
        unit.mp_barcode = mp_barcode

    movement = Movement(
        unit_id=unit.id,
        type=movement_type,
        from_status=from_status,
        to_status=to_status,
        from_site_id=from_site_id,
        to_site_id=to_site_id if to_site_id is not None else unit.site_id,
        user_id=user_id,
        shift_id=shift_id,
        note=note,
        ref_id=ref_id,
    )
    if moment is not None:
        movement.created_at = moment
    session.add(movement)
    session.flush()
    return movement


def _now() -> datetime:
    """Текущий момент (отдельной функцией — её подменяют тесты границы суток)."""
    from app.timeutil import now_utc

    return now_utc()


def accepted_at_text(unit: Unit) -> str:
    """Текст «принято 10.10 в 14:20» для отказа при повторном скане."""
    if unit.accepted_at is None:
        return ""
    moment = fmt_dt(unit.accepted_at)
    day, time_part = moment.split(" ")
    return f"принято {day[:5]} в {time_part}"
