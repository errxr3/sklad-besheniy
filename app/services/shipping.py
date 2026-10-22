"""Погрузка по каналам и отгрузка (SPEC §6.4).

Грузчик открывает канал (Ozon, WB, СГТ, КГТ, СДЭК), сканирует зеркала и закрывает канал
с описанием (для Ozon — машина Озон1…Озон3). Внутри это по-прежнему запись `pallets`:
одна «отгрузка канала» = одна запись (решение владельца 2026-10-20).
"""

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
    OZON,
    OZON_TRUCKS,
    ROLE_ADMIN,
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


def open_channel_at(session: Session, site_id: int, channel: str, truck: str = "") -> Pallet | None:
    """Открытый канал площадки (Озон 2 на Учительской, WB на Коминтерна и т.п.)."""
    query = select(Pallet).where(
        Pallet.site_id == site_id, Pallet.channel == channel, Pallet.closed_at.is_(None)
    )
    query = query.where(Pallet.note == truck) if truck else query.where(Pallet.note.is_(None))
    return session.scalar(query)


# Кнопки каналов на экране погрузки: у Ozon каждая машина — отдельный канал
CHANNEL_BUTTONS: tuple[tuple[str, str, str], ...] = (
    *((OZON, truck, "Озон " + truck.removeprefix("Озон")) for truck in OZON_TRUCKS),
    *((channel, "", channel) for channel in CHANNELS if channel != OZON),
)


def site_channels(session: Session, site_id: int) -> dict[str, Pallet]:
    """Открытые каналы площадки по названию кнопки («Озон 2», «WB»)."""
    pallets = session.scalars(
        select(Pallet).where(Pallet.site_id == site_id, Pallet.closed_at.is_(None))
    ).all()
    return {pallet.title: pallet for pallet in pallets}


def pallet_loaders(session: Session, pallet_id: int) -> list[tuple[str, int]]:
    """Кто сколько зеркал загрузил в канал — грузят несколько человек одновременно."""
    rows = session.execute(
        select(User.name, func.count(func.distinct(Unit.id)))
        .join(Movement, Movement.user_id == User.id)
        .join(Unit, Unit.id == Movement.unit_id)
        .where(Unit.pallet_id == pallet_id, Movement.type == MV_SHIP)
        .group_by(User.name)
        .order_by(func.count(func.distinct(Unit.id)).desc(), User.name)
    ).all()
    return [(str(name), int(count)) for name, count in rows]


def check_channel_site(pallet: Pallet, site: Site | None, user: User) -> None:
    """Грузить и закрывать канал можно только на площадке своей смены."""
    if pallet.closed_at is not None:
        raise ShippingError(f"Канал {pallet.title} уже закрыт — выберите канал заново")
    if user.role == ROLE_ADMIN:
        return
    if site is None or pallet.site_id != site.id:
        raise ShippingError(f"Канал {pallet.title} открыт на площадке {pallet.site.name}, а вы на другой")


def next_pallet_number(session: Session) -> str:
    """Номер отгрузки: P + дата + порядковый за день (напр. P20261013-03)."""
    today = today_msk()
    prefix = f"P{today:%Y%m%d}"
    count = session.scalar(
        select(func.count()).select_from(Pallet).where(Pallet.number.like(f"{prefix}-%"))
    )
    index = int(count or 0) + 1
    while session.scalar(select(Pallet).where(Pallet.number == f"{prefix}-{index:02d}")) is not None:
        index += 1
    return f"{prefix}-{index:02d}"


def join_channel(
    session: Session, *, user: User, site: Site, channel: str, truck: str = ""
) -> Pallet:
    """Встать на канал: присоединиться к открытому на площадке или открыть его.

    Канал общий — «Озон 2» на Учительской один, и все грузчики на смене там сканируют в него.
    Разные каналы (Озон 1 и Озон 2, WB, СДЭК) можно грузить одновременно.
    """
    if channel not in CHANNELS:
        raise ShippingError("Выберите канал: " + ", ".join(CHANNELS))
    if channel == OZON:
        # Раньше машину выбирали при закрытии; без машины — «Озон 1»
        truck = truck or OZON_TRUCKS[0]
        if truck not in OZON_TRUCKS:
            raise ShippingError("Выберите машину Ozon: " + ", ".join(OZON_TRUCKS))
    else:
        truck = ""
    # Блокировка площадки: двое одновременно нажали «Озон 1» — канал откроется один
    session.execute(select(Site.id).where(Site.id == site.id).with_for_update())
    existing = open_channel_at(session, site.id, channel, truck)
    if existing is not None:
        return existing
    pallet = Pallet(
        number=next_pallet_number(session),
        site_id=site.id,
        channel=channel,
        note=truck or None,
        opened_by=user.id,
        opened_at=now_utc(),
    )
    session.add(pallet)
    session.flush()
    return pallet


# Старое имя: тесты и скрипты открывали канал так
open_new_pallet = join_channel


def close_pallet(
    session: Session, *, pallet_id: int, user: User, note: str = "", site: Site | None = None
) -> Pallet:
    """Закрыть канал: всё отсканировано и готово к отгрузке, дополнять больше нельзя.

    Закрыть может любой грузчик на смене на площадке канала. Для Ozon обязательно указать
    машину (Озон1…Озон3), для остальных описание — по желанию.
    """
    pallet = session.scalar(select(Pallet).where(Pallet.id == pallet_id).with_for_update(of=Pallet))
    if pallet is None:
        raise ShippingError("Отгрузка не найдена")
    check_channel_site(pallet, site if site is not None else pallet.site, user)
    text = note.strip()
    if pallet.channel == OZON:
        # Машина выбрана при открытии канала; старые каналы без машины — выбрать при закрытии
        if pallet.note not in OZON_TRUCKS:
            if text not in OZON_TRUCKS:
                raise ShippingError("Для Ozon выберите машину: " + ", ".join(OZON_TRUCKS))
            pallet.note = text
    else:
        pallet.note = text[:200] or None
    pallet.closed_at = now_utc()
    session.flush()
    return pallet


AUTO_CLOSE_NOTE = "закрыт автоматически"


def open_channels(session: Session) -> list[Pallet]:
    """Каналы, которые грузчики ещё не закрыли: их зеркала не попали в таблицу отгрузки."""
    return list(
        session.scalars(select(Pallet).where(Pallet.closed_at.is_(None)).order_by(Pallet.opened_at)).all()
    )


def auto_close_channels(session: Session) -> int:
    """Закрыть забытые каналы в конце дня, чтобы отгрузка попала в таблицу.

    У Ozon машина не выбрана — такая отгрузка считается в колонке «Озон 1».
    """
    moment = now_utc()
    closed = 0
    for pallet in open_channels(session):
        pallet.closed_at = moment
        # У Ozon машина остаётся в пометке: «Озон2, закрыт автоматически»
        truck = pallet.note if pallet.channel == OZON and pallet.note in OZON_TRUCKS else ""
        pallet.note = f"{truck}, {AUTO_CLOSE_NOTE}" if truck else AUTO_CLOSE_NOTE
        closed += 1
    session.flush()
    return closed


def pallet_summary(session: Session, pallet_id: int) -> list[tuple[str, int]]:
    """Итог отгрузки: количество по артикулам."""
    rows = session.execute(
        select(Article.name, func.count())
        .join(Unit, Unit.article_id == Article.id)
        .where(Unit.pallet_id == pallet_id)
        .group_by(Article.name)
        .order_by(Article.name)
    ).all()
    return [(str(name), int(count)) for name, count in rows]


def pallet_count(session: Session, pallet_id: int) -> int:
    """Сколько зеркал в отгрузке."""
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
            "Сначала отсканируйте этикетку BLAGO",
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
    """Скан этикетки зеркала при погрузке."""
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
            f"отгрузка {pallet_number}",
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
            f"Зеркало числится на площадке {unit.site.name}",
            f"а канал открыт на площадке {pallet.site.name}: грузить можно только оттуда",
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
        return LoadResult(False, "Зеркало не найдено", "отсканируйте этикетку BLAGO заново")

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
        # Зеркало уже отгружено первым сканом — просто привязываем код
        unit.mp_barcode = mp_barcode
        session.flush()
        return LoadResult(
            True,
            unit.article.name,
            f"ШК привязан · в канале: {pallet_count(session, pallet.id)}",
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
    """Перевести зеркало в «Отгружено» и привязать к отгрузке канала."""
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
            note=f"канал {pallet.title}, отгрузка {pallet.number}",
        )
    except TransitionError as exc:
        return LoadResult(False, str(exc), number=unit.number, article=unit.article.name)

    count = pallet_count(session, pallet.id)
    return LoadResult(
        True,
        unit.article.name,
        f"В канале {pallet.title}: {count}",
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
        note=f"отмена отгрузки {pallet_number}",
    )
    return ScanResult(
        True,
        unit.article.name,
        "Отгрузка отменена, зеркало снова на складе",
        number=unit.number,
        article=unit.article.name,
        stock=stock_count(session, unit.article_id, unit.site_id),
    )
