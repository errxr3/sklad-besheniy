"""Перемещения, инвентаризация, возвраты (SPEC §6.5–6.7)."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Inventory, InventoryScan, Movement, Site, Unit, User
from app.scan import clean_code, is_own_label
from app.seed import RETURNS_SERVICE_NAME, RETURNS_SITE_NAME
from app.services.units import ScanResult, UnitError, create_printed_units, stock_count
from app.statuses import (
    IN_STOCK,
    IN_TRANSIT,
    MV_ACCEPT,
    MV_INVENTORY_FIX,
    MV_RETURN_BAD,
    MV_RETURN_OK,
    MV_TRANSFER_IN,
    MV_TRANSFER_OUT,
    SHIPPED,
    STATUS_LABELS,
    WRITTEN_OFF,
)
from app.timeutil import now_utc
from app.transitions import TransitionError, apply_transition, lock_unit


class WarehouseError(Exception):
    """Ошибка складской операции, текст показывается пользователю."""


# --- Перемещение между площадками (SPEC §6.5) ---


def transfer_out(
    session: Session, *, code: str, actor: User, to_site_id: int, shift_id: int | None = None
) -> ScanResult:
    """Отправка зеркала на другую площадку: «На складе» → «В пути»."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO")
    target = session.get(Site, to_site_id)
    if target is None or not target.active:
        return ScanResult(False, "Площадка-получатель не найдена")

    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", number=number)
    if unit.site_id == target.id:
        return ScanResult(
            False, f"Зеркало уже на площадке {target.name}", number=number, article=unit.article.name
        )
    if unit.status != IN_STOCK:
        return ScanResult(
            False,
            f"Нельзя отправить: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
            number=number,
            article=unit.article.name,
        )

    try:
        apply_transition(
            session,
            unit,
            IN_TRANSIT,
            movement_type=MV_TRANSFER_OUT,
            user_id=actor.id,
            shift_id=shift_id,
            journal_to_site_id=target.id,
            note=f"отправлено на {target.name}",
        )
    except TransitionError as exc:
        return ScanResult(False, str(exc), number=number, article=unit.article.name)

    return ScanResult(
        True,
        unit.article.name,
        f"В пути на {target.name}",
        number=number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )


def transfer_in(
    session: Session, *, code: str, actor: User, site_id: int, shift_id: int | None = None
) -> ScanResult:
    """Приёмка перемещения на площадке-получателе: «В пути» → «На складе»."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO")
    site = session.get(Site, site_id)
    if site is None:
        return ScanResult(False, "Площадка не найдена")

    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", number=number)
    if unit.status != IN_TRANSIT:
        return ScanResult(
            False,
            f"Нельзя принять: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
            number=number,
            article=unit.article.name,
        )

    destination = session.scalar(
        select(Movement.to_site_id)
        .where(Movement.unit_id == unit.id, Movement.type == MV_TRANSFER_OUT)
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .limit(1)
    )
    if destination is not None and destination != site.id:
        other = session.get(Site, destination)
        return ScanResult(
            False,
            f"Зеркало отправлено на {other.name if other else 'другую площадку'}",
            number=number,
            article=unit.article.name,
        )

    apply_transition(
        session,
        unit,
        IN_STOCK,
        movement_type=MV_TRANSFER_IN,
        user_id=actor.id,
        shift_id=shift_id,
        to_site_id=site.id,
        note=f"принято на {site.name}",
    )
    return ScanResult(
        True,
        unit.article.name,
        f"Принято · {site.name}",
        number=number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, site.id),
    )


def units_in_transit(session: Session, *, hours: int = 24) -> list[Unit]:
    """Зеркала, которые в пути дольше суток (SPEC §6.5)."""
    threshold = now_utc() - timedelta(hours=hours)
    sent = (
        select(Unit.id, func.max(Movement.created_at).label("sent_at"))
        .join(Movement, Movement.unit_id == Unit.id)
        .where(Unit.status == IN_TRANSIT, Movement.type == MV_TRANSFER_OUT)
        .group_by(Unit.id)
        .subquery()
    )
    ids = session.scalars(select(sent.c.id).where(sent.c.sent_at < threshold)).all()
    if not ids:
        return []
    return list(session.scalars(select(Unit).where(Unit.id.in_(ids)).order_by(Unit.number)).all())


# --- Возвраты (SPEC §6.7) ---


def return_unit(
    session: Session, *, code: str, actor: User, good: bool, site_id: int, shift_id: int | None = None
) -> ScanResult:
    """Возврат от покупателя: годное — на склад, негодное — в списание."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO")
    unit = lock_unit(session, number)
    if unit is None:
        return ScanResult(False, "Номер не найден", number=number)
    if unit.status != SHIPPED:
        current = STATUS_LABELS.get(unit.status, unit.status)
        return ScanResult(
            False,
            f"Возврат возможен только для отгруженного (сейчас «{current}»)",
            number=number,
            article=unit.article.name,
        )

    target = IN_STOCK if good else WRITTEN_OFF
    apply_transition(
        session,
        unit,
        target,
        movement_type=MV_RETURN_OK if good else MV_RETURN_BAD,
        user_id=actor.id,
        shift_id=shift_id,
        to_site_id=site_id if good else None,
        note="возврат от покупателя: годное" if good else "возврат от покупателя: негодное",
    )
    return ScanResult(
        True,
        unit.article.name,
        "Возвращено на склад" if good else "Возврат списан",
        number=number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )


def returns_site(session: Session) -> Site:
    """Площадка, на которую всегда записываются возвраты (Коминтерна)."""
    site = session.scalar(select(Site).where(Site.name == RETURNS_SITE_NAME, Site.active.is_(True)))
    if site is None:
        site = session.scalars(
            select(Site).where(Site.active.is_(True)).order_by(Site.sort_order, Site.id).limit(1)
        ).first()
    if site is None:
        raise WarehouseError("Нет ни одной активной площадки")
    return site


def print_unmarked_returns(
    session: Session, *, article_id: int, qty: int, actor: User
) -> list[Unit]:
    """Возврат без нашей этикетки: выдать номера и сразу поставить зеркала на склад.

    Зеркало записывается на площадку возвратов, «сборщиком» — служебный «Возврат без ШК»:
    в карточке и журнале видно, что оно пришло с возвратов без личного ШК.
    """
    service = session.scalar(select(User).where(User.name == RETURNS_SERVICE_NAME))
    if service is None:
        raise UnitError("Нет служебного сотрудника «Возврат без ШК» — обратитесь к администратору")
    site = returns_site(session)
    units = create_printed_units(
        session,
        article_id=article_id,
        qty=qty,
        site_id=site.id,
        assembler_id=service.id,
        actor_id=actor.id,
    )
    for unit in units:
        apply_transition(
            session,
            unit,
            IN_STOCK,
            movement_type=MV_ACCEPT,
            user_id=actor.id,
            to_site_id=site.id,
            note="возврат без маркировки: ШК напечатан на возвратах",
        )
    return units


# --- Инвентаризация (SPEC §6.6) ---


@dataclass
class InventoryDiff:
    """Расхождения инвентаризации."""

    missing: list[Unit] = field(default_factory=list)  # по учёту есть, не отсканировано
    extra: list[Unit] = field(default_factory=list)  # отсканировано, по учёту не на складе
    scanned: int = 0


def active_inventory(session: Session, site_id: int) -> Inventory | None:
    """Идущая инвентаризация площадки."""
    return session.scalars(
        select(Inventory)
        .where(Inventory.site_id == site_id, Inventory.finished_at.is_(None))
        .limit(1)
    ).first()


def start_inventory(
    session: Session, *, site_id: int, actor: User, article_ids: list[int] | None = None
) -> Inventory:
    """Начать инвентаризацию площадки или группы артикулов."""
    if active_inventory(session, site_id) is not None:
        raise WarehouseError("На этой площадке уже идёт инвентаризация")
    site = session.get(Site, site_id)
    if site is None or not site.active:
        raise WarehouseError("Площадка не найдена")
    inventory = Inventory(
        site_id=site_id,
        started_by=actor.id,
        started_at=now_utc(),
        article_ids=article_ids or None,
    )
    session.add(inventory)
    session.flush()
    return inventory


def scan_inventory(session: Session, *, inventory_id: int, code: str, actor: User) -> ScanResult:
    """Скан зеркала при пересчёте. Повторный скан — не ошибка."""
    number = clean_code(code)
    if not is_own_label(number):
        return ScanResult(False, "Это не этикетка BLAGO")
    inventory = session.get(Inventory, inventory_id)
    if inventory is None or inventory.finished_at is not None:
        return ScanResult(False, "Инвентаризация уже завершена")

    unit = session.scalar(select(Unit).where(Unit.number == number))
    if unit is None:
        return ScanResult(False, "Номер не найден", number=number)
    if inventory.article_ids and unit.article_id not in inventory.article_ids:
        return ScanResult(
            False,
            "Этот артикул не входит в инвентаризацию",
            number=number,
            article=unit.article.name,
        )

    existing = session.scalar(
        select(InventoryScan).where(
            InventoryScan.inventory_id == inventory.id, InventoryScan.unit_id == unit.id
        )
    )
    if existing is not None:
        return ScanResult(
            True, unit.article.name, "Уже учтено", number=number, article=unit.article.name
        )

    session.add(
        InventoryScan(inventory_id=inventory.id, unit_id=unit.id, user_id=actor.id)
    )
    session.flush()
    count = session.scalar(
        select(func.count())
        .select_from(InventoryScan)
        .where(InventoryScan.inventory_id == inventory.id)
    )
    return ScanResult(
        True,
        unit.article.name,
        f"Учтено · всего {int(count or 0)}",
        number=number,
        article=unit.article.name,
    )


def inventory_diff(session: Session, inventory: Inventory) -> InventoryDiff:
    """Расхождения: не отсканировано и лишнее.

    Зеркала, которые двигались после старта инвентаризации (отгрузка, перемещение),
    из расхождений исключаются — сравнение идёт по времени движений (SPEC §6.6).
    """
    scanned_ids = set(
        session.scalars(
            select(InventoryScan.unit_id).where(InventoryScan.inventory_id == inventory.id)
        ).all()
    )
    moved_after = set(
        session.scalars(
            select(Movement.unit_id).where(Movement.created_at >= inventory.started_at)
        ).all()
    )

    expected_query = select(Unit).where(Unit.status == IN_STOCK, Unit.site_id == inventory.site_id)
    if inventory.article_ids:
        expected_query = expected_query.where(Unit.article_id.in_(inventory.article_ids))
    expected = list(session.scalars(expected_query.order_by(Unit.number)).all())

    diff = InventoryDiff(scanned=len(scanned_ids))
    diff.missing = [
        unit for unit in expected if unit.id not in scanned_ids and unit.id not in moved_after
    ]
    if scanned_ids:
        extra_units = session.scalars(
            select(Unit).where(Unit.id.in_(scanned_ids)).order_by(Unit.number)
        ).all()
        diff.extra = [
            unit
            for unit in extra_units
            if (unit.status != IN_STOCK or unit.site_id != inventory.site_id)
            and unit.id not in moved_after
        ]
    return diff


def finish_inventory(session: Session, *, inventory_id: int, actor: User) -> tuple[int, int]:
    """Подтвердить инвентаризацию: списать ненайденное, оприходовать лишнее."""
    inventory = session.get(Inventory, inventory_id)
    if inventory is None:
        raise WarehouseError("Инвентаризация не найдена")
    if inventory.finished_at is not None:
        raise WarehouseError("Инвентаризация уже завершена")

    diff = inventory_diff(session, inventory)
    written_off = 0
    restored = 0

    for unit in diff.missing:
        locked = lock_unit(session, unit.number)
        if locked is None or locked.status == WRITTEN_OFF:
            continue
        apply_transition(
            session,
            locked,
            WRITTEN_OFF,
            movement_type=MV_INVENTORY_FIX,
            user_id=actor.id,
            ref_id=inventory.id,
            note="инвентаризация: не найдено на складе",
        )
        written_off += 1

    for unit in diff.extra:
        locked = lock_unit(session, unit.number)
        if locked is None or (locked.status == IN_STOCK and locked.site_id == inventory.site_id):
            continue
        apply_transition(
            session,
            locked,
            IN_STOCK,
            movement_type=MV_INVENTORY_FIX,
            user_id=actor.id,
            to_site_id=inventory.site_id,
            ref_id=inventory.id,
            note="инвентаризация: найдено на складе",
        )
        restored += 1

    inventory.finished_at = now_utc()
    inventory.finished_by = actor.id
    inventory.note = f"списано {written_off}, оприходовано {restored}"
    session.flush()
    return written_off, restored


def inventory_articles(session: Session, inventory: Inventory) -> list[Article]:
    """Артикулы, попавшие в инвентаризацию (для заголовка экрана)."""
    if not inventory.article_ids:
        return []
    return list(
        session.scalars(
            select(Article).where(Article.id.in_(inventory.article_ids)).order_by(Article.sort_order)
        ).all()
    )
