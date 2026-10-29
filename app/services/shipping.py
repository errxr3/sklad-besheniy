"""Погрузка по каналам и отгрузка (SPEC §6.4).

Грузчик открывает канал (Ozon, WB, СГТ, КГТ, СДЭК), сканирует зеркала и закрывает канал
с описанием (для Ozon — машина Озон1…Озон3). Внутри это по-прежнему запись `pallets`:
одна «отгрузка канала» = одна запись (решение владельца 2026-10-20).
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.articles import normalize_article
from app.models import Article, Movement, MpShipment, Pallet, ShipBatch, ShipCode, ShipItem, Site, Unit, User
from app.scan import clean_code, is_own_label
from app.services.batches import clean_ship_code
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
    SHIP_LOADED,
    SHIP_REMOVED,
    SHIP_WAIT,
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
    # Партии: первой отсканирована этикетка маркетплейса — ждём этикетку BLAGO
    await_unit: bool = False
    ship_code: str = ""
    level: str = ""  # «warn» — погружено, но не в свою машину (жёлтый экран)


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
    # Двое одновременно нажали «Озон 1» — канал откроется один: второго не пустит уникальный
    # индекс открытого канала, и он встанет в уже открытый. Блокировку строки здесь не берём:
    # под нагрузкой (100 человек) ожидание блокировки занимало все потоки сервера и он зависал
    for _ in range(5):
        existing = open_channel_at(session, site.id, channel, truck)
        if existing is not None:
            return existing
        try:
            with session.begin_nested():
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
        except IntegrityError:
            # Канал уже открыл другой грузчик или номер отгрузки заняли — пробуем снова
            continue
    raise ShippingError("Не удалось открыть канал, нажмите ещё раз")


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
    ship_code: str | None = None,
) -> LoadResult:
    """Скан на погрузке: этикетка зеркала или ШК маркетплейса (SPEC §6.4)."""
    value = clean_code(code)
    if not value:
        return LoadResult(False, "Пустой скан")
    if batch_mode(session, pallet):
        return scan_batch(
            session, value=value, pallet=pallet, actor=actor, shift_id=shift_id,
            unit_number=unit_number, ship_code=ship_code,
        )

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


# ---------- погрузка по партиям BLAGO OTGRUZKI ----------


def pallet_truck(pallet: Pallet) -> str:
    """Машина Ozon канала: «Озон2» (или пусто для остальных каналов)."""
    if pallet.channel != OZON or not pallet.note:
        return ""
    return pallet.note.split(",")[0].strip()


def batch_mode(session: Session, pallet: Pallet) -> bool:
    """В канал грузят по партиям: сегодня есть партия этого канала с этикетками этой площадки.

    У Ozon — любая сегодняшняя машина: в «Озон 4» добивают остатки Озон 1–3.
    """
    found = session.scalar(
        select(ShipItem.id)
        .join(ShipBatch, ShipBatch.id == ShipItem.batch_id)
        .where(
            ShipBatch.day == today_msk(),
            ShipBatch.channel == pallet.channel,
            ShipItem.site_id == pallet.site_id,
        )
        .limit(1)
    )
    return found is not None


def find_ship_item(session: Session, code: str) -> ShipItem | None:
    """Строка партии по любому коду с этикетки маркетплейса."""
    owner = session.get(ShipCode, clean_ship_code(code))
    return session.get(ShipItem, owner.item_id) if owner else None


@dataclass
class BatchLine:
    """Строка списка «что грузить» в канале: артикул и сколько погружено."""

    article: str
    total: int
    loaded: int
    removed: int
    in_stock: int

    @property
    def left(self) -> int:
        return max(0, self.total - self.loaded - self.removed)


def channel_batch_lines(session: Session, pallet: Pallet) -> list[BatchLine]:
    """Что грузить в канал: этикетки сегодняшней партии этой машины на этой площадке."""
    truck = pallet_truck(pallet)
    items = session.scalars(
        select(ShipItem)
        .join(ShipBatch, ShipBatch.id == ShipItem.batch_id)
        .where(
            ShipBatch.day == today_msk(),
            ShipBatch.channel == pallet.channel,
            ShipBatch.truck == truck,
            ShipItem.site_id == pallet.site_id,
        )
    ).all()
    # Погружено в этот канал из других машин — тоже показываем, это уже сделанная работа
    moved = session.scalars(
        select(ShipItem).where(ShipItem.pallet_id == pallet.id, ShipItem.moved_note.is_not(None))
    ).all()
    stock = {
        int(article_id): int(count)
        for article_id, count in session.execute(
            select(Unit.article_id, func.count())
            .where(Unit.status == IN_STOCK, Unit.site_id == pallet.site_id)
            .group_by(Unit.article_id)
        ).all()
    }
    lines: dict[str, BatchLine] = {}
    for item in [*items, *[m for m in moved if m not in items]]:
        name = item.article.name if item.article else item.article_name
        line = lines.setdefault(
            name, BatchLine(name, 0, 0, 0, stock.get(item.article_id or 0, 0))
        )
        line.total += 1
        line.loaded += item.state == SHIP_LOADED
        line.removed += item.state == SHIP_REMOVED
    return sorted(lines.values(), key=lambda line: (line.left == 0, line.article))


def scan_batch(
    session: Session,
    *,
    value: str,
    pallet: Pallet,
    actor: User,
    shift_id: int | None,
    unit_number: str | None,
    ship_code: str | None,
) -> LoadResult:
    """Двойной скан: этикетка BLAGO + этикетка маркетплейса, в любом порядке."""
    if is_own_label(value):
        unit = lock_unit(session, value)
        problem = _unit_problem(session, unit, value, pallet)
        if problem is not None:
            return problem
        assert unit is not None
        if ship_code:
            item = find_ship_item(session, ship_code)
            if item is None:
                return LoadResult(False, "Этикетка маркетплейса потерялась", "отсканируйте обе ещё раз")
            return _ship_by_item(session, unit=unit, item=item, code=ship_code, pallet=pallet,
                                 actor=actor, shift_id=shift_id)
        return LoadResult(
            True, unit.article.name, "Теперь этикетку маркетплейса на этом зеркале",
            number=unit.number, article=unit.article.name, await_mp=True,
            pallet_count=pallet_count(session, pallet.id),
        )

    item = find_ship_item(session, value)
    problem = _item_problem(item, value, pallet)
    if problem is not None:
        return problem
    assert item is not None
    article = item.article.name if item.article else item.article_name
    if unit_number:
        unit = lock_unit(session, clean_code(unit_number))
        problem = _unit_problem(session, unit, unit_number, pallet)
        if problem is not None:
            return problem
        assert unit is not None
        return _ship_by_item(session, unit=unit, item=item, code=value, pallet=pallet,
                             actor=actor, shift_id=shift_id)
    return LoadResult(
        True, f"Нужно: {article}", "Теперь этикетку BLAGO на этом зеркале",
        article=article, await_unit=True, ship_code=value, pallet_count=pallet_count(session, pallet.id),
    )


def _unit_problem(session: Session, unit: Unit | None, number: str, pallet: Pallet) -> LoadResult | None:
    """Зеркало нельзя грузить: нет такого, уже отгружено, не на складе, другая площадка."""
    if unit is None:
        return LoadResult(False, "Номер не найден", f"Этикетка {number} не выдавалась", number=number)
    if unit.status == SHIPPED:
        shipped = session.get(Pallet, unit.pallet_id) if unit.pallet_id else None
        return LoadResult(
            False, "Уже отгружено", f"канал {shipped.title if shipped else '—'}",
            number=unit.number, article=unit.article.name,
        )
    if unit.status != IN_STOCK:
        return LoadResult(
            False, f"Нельзя отгрузить: статус «{STATUS_LABELS.get(unit.status, unit.status)}»",
            number=unit.number, article=unit.article.name,
        )
    if unit.site_id != pallet.site_id:
        return LoadResult(
            False, f"Зеркало числится на площадке {unit.site.name}",
            f"а канал открыт на площадке {pallet.site.name}", number=unit.number, article=unit.article.name,
        )
    return None


def _item_problem(item: ShipItem | None, code: str, pallet: Pallet) -> LoadResult | None:
    """Этикетку маркетплейса нельзя грузить в этот канал."""
    if item is None:
        return LoadResult(False, "Этикетки нет в сегодняшних отгрузках", f"код {code}")
    article = item.article.name if item.article else item.article_name
    if item.state == SHIP_LOADED:
        when = fmt_dt(item.loaded_at)
        return LoadResult(False, "Эта этикетка уже погружена", when, article=article)
    if item.state == SHIP_REMOVED:
        return LoadResult(False, "Снято с отгрузки", item.removed_reason or "", article=article)
    if item.batch.channel != pallet.channel:
        hint = f"грузите её в канал {item.batch.channel}"
        return LoadResult(False, f"Это этикетка {item.batch.title}", hint, article=article)
    if item.site_id is not None and item.site_id != pallet.site_id:
        site = item.site.name if item.site else "другой площадки"
        return LoadResult(False, f"Этикетка производства {site}", "грузите на той площадке", article=article)
    return None


def _ship_by_item(
    session: Session,
    *,
    unit: Unit,
    item: ShipItem,
    code: str,
    pallet: Pallet,
    actor: User,
    shift_id: int | None,
) -> LoadResult:
    """Сверить артикул и отгрузить зеркало по этикетке маркетплейса."""
    article = item.article.name if item.article else item.article_name
    same = (
        unit.article_id == item.article_id
        if item.article_id is not None
        else normalize_article(unit.article.name) == normalize_article(item.article_name)
    )
    if not same:
        return LoadResult(
            False, f"Зеркало {unit.article.name}, а этикетка на {article}", "зеркало осталось на складе",
            number=unit.number, article=unit.article.name,
        )
    moved = ""
    truck = pallet_truck(pallet)
    if item.batch.truck != truck or item.batch.day != today_msk():
        source = item.batch.title + ("" if item.batch.day == today_msk() else f" от {item.batch.day:%d.%m}")
        moved = f"{source} → {pallet.title}"
    result = _do_ship(session, unit=unit, pallet=pallet, actor=actor, shift_id=shift_id,
                      mp_barcode=clean_ship_code(code)[:64])
    if not result.ok:
        return result
    item.state = SHIP_LOADED
    item.unit_id = unit.id
    item.pallet_id = pallet.id
    item.loaded_at = now_utc()
    item.loaded_by = actor.id
    item.moved_note = moved or None
    session.flush()
    if moved:
        result.level = "warn"
        result.detail = f"{moved} · в канале: {result.pallet_count}"
    return result


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
    # Этикетка маркетплейса снова ждёт погрузки
    for item in session.scalars(select(ShipItem).where(ShipItem.unit_id == unit.id)).all():
        item.state, item.unit_id, item.pallet_id = SHIP_WAIT, None, None
        item.loaded_at = item.loaded_by = item.moved_note = None
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
