"""Погрузка по партиям BLAGO OTGRUZKI: двойной скан, перенос между машинами, снятие (2026-10-29)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShipItem, Unit
from app.services.batches import receive_batch
from app.services.shipping import (
    batch_mode,
    cancel_ship,
    channel_batch_lines,
    join_channel,
    scan_loading,
)
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, SHIP_LOADED, SHIP_WAIT, SHIPPED
from app.timeutil import today_msk
from tests.helpers import make_article, make_user, site_by_name

KOM = "Коминтерна"


def _setup(db: Session, qty: int = 3) -> tuple:
    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик Партий", role=ROLE_ASSEMBLER)
    loader = make_user(db, name="Грузчик Партий", role=ROLE_LOADER)
    a = make_article(db, name="Zerkalo_50x130", sort_order=1)
    b = make_article(db, name="Zerkalo_60x160", sort_order=2)
    units = create_printed_units(db, article_id=a.id, qty=qty, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)
    other = create_printed_units(db, article_id=b.id, qty=1, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)
    db.commit()
    for unit in [*units, *other]:
        accept_unit(db, code=unit.number, actor=worker)
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": "OZ-1", "article": "Zerkalo_50x130", "production": KOM, "codes": ["%101"]},
        {"posting": "OZ-2", "article": "Zerkalo_50x130", "production": KOM, "codes": ["%102"]},
    ])
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон2", items=[
        {"posting": "OZ-3", "article": "Zerkalo_50x130", "production": KOM, "codes": ["%103"]},
    ])
    receive_batch(db, day=today_msk(), market="wb", channel="WB", items=[
        {"posting": "WB-1", "article": "Zerkalo_50x130", "production": KOM, "codes": ["*WB1"]},
    ])
    db.commit()
    return site, loader, units, other


def _scan(db: Session, pallet, loader, code: str, unit: str | None = None, ship: str | None = None):
    result = scan_loading(db, code=code, pallet=pallet, actor=loader, unit_number=unit, ship_code=ship)
    db.commit()
    return result


def test_both_orders_of_double_scan(db: Session) -> None:
    site, loader, units, _ = _setup(db)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert batch_mode(db, pallet)

    first = _scan(db, pallet, loader, units[0].number)
    assert first.ok and first.await_mp
    done = _scan(db, pallet, loader, "%101", unit=units[0].number)
    assert done.ok and done.level == ""

    # Наоборот: сначала этикетка маркетплейса — экран подсказывает, какое зеркало нужно
    need = _scan(db, pallet, loader, "%102")
    assert need.ok and need.await_unit and need.message == "Нужно: Zerkalo_50x130"
    done = _scan(db, pallet, loader, units[1].number, ship=need.ship_code)
    assert done.ok

    items = {i.posting: i for i in db.scalars(select(ShipItem)).all()}
    assert items["OZ-1"].state == SHIP_LOADED and items["OZ-1"].unit_id == units[0].id
    assert db.get(Unit, units[0].id).mp_barcode == "%101"  # type: ignore[union-attr]
    lines = channel_batch_lines(db, pallet)
    assert [(line.article, line.loaded, line.left) for line in lines] == [("Zerkalo_50x130", 2, 0)]


def test_red_cases(db: Session) -> None:
    site, loader, units, other = _setup(db)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    wrong = _scan(db, pallet, loader, "%101", unit=other[0].number)
    assert not wrong.ok and wrong.message == "Зеркало Zerkalo_60x160, а этикетка на Zerkalo_50x130"
    assert db.get(Unit, other[0].id).status != SHIPPED  # type: ignore[union-attr]
    assert not _scan(db, pallet, loader, "%999").ok  # нет в сегодняшних отгрузках
    wb = _scan(db, pallet, loader, "*WB1")
    assert not wb.ok and wb.message == "Это этикетка WB"
    _scan(db, pallet, loader, "%101", unit=units[0].number)
    again = _scan(db, pallet, loader, "%101", unit=units[1].number)
    assert not again.ok and again.message == "Эта этикетка уже погружена"


def test_other_truck_is_yellow_and_ozon4_finishes(db: Session) -> None:
    site, loader, units, _ = _setup(db)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон2")
    db.commit()
    moved = _scan(db, pallet, loader, "%101", unit=units[0].number)
    assert moved.ok and moved.level == "warn" and moved.detail.startswith("Озон 1 → Озон 2")
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and item.moved_note == "Озон 1 → Озон 2"

    fourth = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон4")
    db.commit()
    assert batch_mode(db, fourth)  # своей партии нет, но остатки Озон 1–3 догружаются
    late = _scan(db, fourth, loader, "%102", unit=units[1].number)
    assert late.ok and late.level == "warn" and "Озон 1 → Озон 4" in late.detail


def test_cancel_ship_returns_label_to_wait(db: Session) -> None:
    site, loader, units, _ = _setup(db)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    _scan(db, pallet, loader, "%101", unit=units[0].number)
    cancel_ship(db, number=units[0].number, actor=loader)
    db.commit()
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and item.state == SHIP_WAIT and item.unit_id is None


def test_channel_without_batch_works_as_before(db: Session) -> None:
    site, loader, units, _ = _setup(db)
    pallet = join_channel(db, user=loader, site=site, channel="СДЭК")
    db.commit()
    assert not batch_mode(db, pallet)
    assert _scan(db, pallet, loader, units[2].number).ok
