"""Погрузка на паллету и отгрузка (SPEC §6.4)."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.articles import normalize_article
from app.models import Article, Movement, MpShipment, Pallet, Site, Unit, User
from app.scan import clean_code, is_own_label
from app.services.units import ScanResult, stock_count
from app.settings_store import get_flag, get_int
from app.statuses import (
    CHANNELS,
    IN_STOCK,
    MV_SHIP,
    MV_SHIP_CANCEL,
    SHIPPED,
    STATUS_LABELS,
)
from app.timeutil import fmt_dt, now_utc, to_moscow, today_msk
from app.transitions import TransitionError, apply_transition, lock_unit


class ShippingError(Exception):
    """Ошибка отгрузки, текст показывается пользователю."""


@dataclass
class LoadResult(ScanResult):
    """Результат скана на погрузке: может требовать второй скан."""

    await_mp: bool = False
    pallet_count: int = 0


def open_pallet_for(session: Session, user_id: int) -> Pallet | None:
    """Открытая паллета грузчика."""
    return session.scalars(
        select(Pallet).where(Pallet.opened_by == user_id, Pallet.closed_at.is_(None)).limit(1)
    ).first()


def next_pallet_number(session: Session) -> str:
    """Номер паллеты: P + дата + порядковый за день (напр. P20261013-03)."""
    today = today_msk()
    prefix = f"P{today:%Y%m%d}"
    count = session.scalar(
        select(func.count()).select_from(Pallet).where(Pallet.number.like(f"{prefix}-%"))
    )
    index = int(count or 0) + 1
    while session.scalar(select(Pallet).where(Pallet.number == f"{prefix}-{index:02d}")) is not None:
        index += 1
    return f"{prefix}-{index:02d}"


def open_new_pallet(session: Session, *, user: User, site: Site, channel: str) -> Pallet:
    """Открыть новую паллету выбранного канала."""
    if channel not in CHANNELS:
        raise ShippingError("Выберите канал: Ozon, WB или СДЭК")
    if open_pallet_for(session, user.id) is not None:
        raise ShippingError("У вас уже есть открытая паллета — закройте её или продолжайте")
    pallet = Pallet(
        number=next_pallet_number(session),
        site_id=site.id,
        channel=channel,
        opened_by=user.id,
        opened_at=now_utc(),
    )
    session.add(pallet)
    session.flush()
    return pallet


def close_pallet(session: Session, *, pallet_id: int, user: User) -> Pallet:
    """Закрыть паллету: дополнять её после этого нельзя."""
    pallet = session.get(Pallet, pallet_id)
    if pallet is None:
        raise ShippingError("Паллета не найдена")
    if pallet.closed_at is not None:
        raise ShippingError("Паллета уже закрыта")
    pallet.closed_at = now_utc()
    session.flush()
    return pallet


def pallet_summary(session: Session, pallet_id: int) -> list[tuple[str, int]]:
    """Итог паллеты: количество по артикулам."""
    rows = session.execute(
        select(Article.name, func.count())
        .join(Unit, Unit.article_id == Article.id)
        .where(Unit.pallet_id == pallet_id)
        .group_by(Article.name)
        .order_by(Article.name)
    ).all()
    return [(str(name), int(count)) for name, count in rows]


def pallet_count(session: Session, pallet_id: int) -> int:
    """Сколько зеркал на паллете."""
    value = session.scalar(select(func.count()).select_from(Unit).where(Unit.pallet_id == pallet_id))
    return int(value or 0)


def check_order_match(session: Session, *, mp_barcode: str, unit: Unit) -> None:
    """Сверка с отправлением из BLAGO OTGRUZKI (этап 3, SPEC §6.4)."""
    shipment = session.scalar(select(MpShipment).where(MpShipment.mp_barcode == mp_barcode))
    if shipment is None:
        raise ShippingError(f"ШК {mp_barcode} не найден в списке отправлений")
    expected = shipment.article_name_norm
    actual = normalize_article(unit.article.name)
    if expected != actual:
        raise ShippingError(
            f"Зеркало {unit.article.name}, а заказ на {shipment.article_name or expected}"
        )


def scan_loading(
    session: Session,
    *,
    code: str,
    pallet: Pallet,
    actor: User,
    shift_id: int | None = None,
    unit_number: str | None = None,
) -> LoadResult:
    """Скан на погрузке: этикетка зеркала или ШК маркетплейса (SPEC §6.4)."""
    value = clean_code(code)
    if not value:
        return LoadResult(False, "Пустой скан")

    require_mp = get_flag(session, "require_mp_barcode")
    verify_order = get_flag(session, "verify_order")

    if is_own_label(value):
        return _scan_unit_label(
            session,
            number=value,
            pallet=pallet,
            actor=actor,
            shift_id=shift_id,
            require_mp=require_mp,
        )

    # Это код маркетплейса
    if not unit_number:
        return LoadResult(
            False,
            "Это код маркетплейса",
            "Сначала отсканируйте этикетку СКЛАД",
        )
    return _scan_mp_barcode(
        session,
        mp_barcode=value,
        unit_number=unit_number,
        pallet=pallet,
        actor=actor,
        shift_id=shift_id,
        require_mp=require_mp,
        verify_order=verify_order,
    )


def _scan_unit_label(
    session: Session,
    *,
    number: str,
    pallet: Pallet,
    actor: User,
    shift_id: int | None,
    require_mp: bool,
) -> LoadResult:
    """Скан этикетки зеркала при укладке на паллету."""
    unit = lock_unit(session, number)
    if unit is None:
        return LoadResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)

    article_name = unit.article.name
    if unit.status == SHIPPED:
        when = fmt_dt(
            session.scalar(
                select(func.max(Movement.created_at)).where(
                    Movement.unit_id == unit.id, Movement.type == MV_SHIP
                )
            )
        )
        shipped_pallet = session.get(Pallet, unit.pallet_id) if unit.pallet_id else None
        pallet_number = shipped_pallet.number if shipped_pallet else "—"
        return LoadResult(
            False,
            f"Уже отгружено {when}",
            f"паллета {pallet_number}",
            number=number,
            article=article_name,
        )
    if unit.status != IN_STOCK:
        return LoadResult(
            False,
            f"Нельзя отгрузить: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
            number=number,
            article=article_name,
        )
    if unit.site_id != pallet.site_id:
        return LoadResult(
            False,
            f"Зеркало числится на {unit.site.name}",
            "отгрузка возможна только со своей площадки",
            number=number,
            article=article_name,
        )

    if require_mp:
        # Второй скан обязателен: отгружаем только после кода маркетплейса
        return LoadResult(
            True,
            article_name,
            "Отсканируйте ШК маркетплейса",
            number=number,
            article=article_name,
            await_mp=True,
            pallet_count=pallet_count(session, pallet.id),
        )

    return _do_ship(session, unit=unit, pallet=pallet, actor=actor, shift_id=shift_id)


def _scan_mp_barcode(
    session: Session,
    *,
    mp_barcode: str,
    unit_number: str,
    pallet: Pallet,
    actor: User,
    shift_id: int | None,
    require_mp: bool,
    verify_order: bool,
) -> LoadResult:
    """Скан ШК маркетплейса, который наклеен на только что отсканированное зеркало."""
    unit = lock_unit(session, clean_code(unit_number))
    if unit is None:
        return LoadResult(False, "Зеркало не найдено", "отсканируйте этикетку СКЛАД заново")

    existing = session.scalar(
        select(Unit).where(Unit.mp_barcode == mp_barcode, Unit.id != unit.id)
    )
    if existing is not None:
        return LoadResult(
            False,
            "Этот ШК маркетплейса уже привязан",
            f"к зеркалу {existing.number}",
            number=unit.number,
            article=unit.article.name,
        )

    if verify_order:
        try:
            check_order_match(session, mp_barcode=mp_barcode, unit=unit)
        except ShippingError as exc:
            return LoadResult(
                False,
                str(exc),
                "зеркало осталось на складе",
                number=unit.number,
                article=unit.article.name,
            )

    if unit.status == IN_STOCK:
        return _do_ship(
            session,
            unit=unit,
            pallet=pallet,
            actor=actor,
            shift_id=shift_id,
            mp_barcode=mp_barcode,
        )

    if unit.status == SHIPPED and unit.pallet_id == pallet.id:
        # Зеркало уже уехало на паллету первым сканом — просто привязываем код
        unit.mp_barcode = mp_barcode
        session.flush()
        return LoadResult(
            True,
            unit.article.name,
            f"ШК привязан · на паллете: {pallet_count(session, pallet.id)}",
            number=unit.number,
            article=unit.article.name,
            pallet_count=pallet_count(session, pallet.id),
        )

    return LoadResult(
        False,
        f"Нельзя: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
        number=unit.number,
        article=unit.article.name,
    )


def _do_ship(
    session: Session,
    *,
    unit: Unit,
    pallet: Pallet,
    actor: User,
    shift_id: int | None,
    mp_barcode: str | None = None,
) -> LoadResult:
    """Перевести зеркало в «Отгружено» и привязать к паллете."""
    try:
        apply_transition(
            session,
            unit,
            SHIPPED,
            movement_type=MV_SHIP,
            user_id=actor.id,
            shift_id=shift_id,
            channel=pallet.channel,
            pallet_id=pallet.id,
            mp_barcode=mp_barcode,
            ref_id=pallet.id,
            note=f"паллета {pallet.number}, канал {pallet.channel}",
        )
    except TransitionError as exc:
        return LoadResult(False, str(exc), number=unit.number, article=unit.article.name)

    count = pallet_count(session, pallet.id)
    return LoadResult(
        True,
        unit.article.name,
        f"На паллете: {count}",
        number=unit.number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, unit.site_id),
        pallet_count=count,
    )


def cancel_ship(session: Session, *, number: str, actor: User, shift_id: int | None = None) -> ScanResult:
    """Отменить ошибочную отгрузку последних минут (SPEC §6.4)."""
    minutes = get_int(session, "ship_cancel_minutes")
    unit = lock_unit(session, clean_code(number))
    if unit is None:
        return ScanResult(False, "Номер не найден", number=number)
    if unit.status != SHIPPED:
        return ScanResult(
            False,
            f"Зеркало не отгружено (статус «{STATUS_LABELS.get(unit.status, unit.status)}»)",
            number=unit.number,
        )

    shipped_at = session.scalar(
        select(func.max(Movement.created_at)).where(
            Movement.unit_id == unit.id, Movement.type == MV_SHIP
        )
    )
    if shipped_at is not None and now_utc() - shipped_at > timedelta(minutes=minutes):
        when = to_moscow(shipped_at).strftime("%d.%m %H:%M")
        return ScanResult(
            False,
            f"Отменить можно в течение {minutes} мин после отгрузки",
            f"отгружено {when}",
            number=unit.number,
        )

    current_pallet = session.get(Pallet, unit.pallet_id) if unit.pallet_id else None
    pallet_number = current_pallet.number if current_pallet else "—"
    apply_transition(
        session,
        unit,
        IN_STOCK,
        movement_type=MV_SHIP_CANCEL,
        user_id=actor.id,
        shift_id=shift_id,
        note=f"отмена отгрузки, паллета {pallet_number}",
    )
    return ScanResult(
        True,
        unit.article.name,
        "Отгрузка отменена, зеркало снова на складе",
        number=unit.number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )
