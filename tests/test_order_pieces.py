"""Несколько зеркал под одной этикеткой: заказ СДЭК из двух зеркал на один трек (2026-11-06)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShipCode, ShipItem, Unit
from app.services.batches import receive_batch, receive_demand
from app.services.not_on_shelf import not_on_shelf
from app.services.shipping import join_channel, scan_loading
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD, SHIP_LOADED, SHIP_REMOVED, SHIP_WAIT
from app.timeutil import today_msk
from tests.helpers import make_article, make_user, site_by_name

KOM = "Коминтерна"


def _units(db: Session, site, worker, article, qty: int) -> list[Unit]:  # type: ignore[no-untyped-def]
    units = create_printed_units(db, article_id=article.id, qty=qty, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    return units


def _row(article: str, track: str = "1001", task: str = "T1") -> dict:
    return {"posting": track, "origin": task, "article": article, "production": KOM,
            "codes": [f"[CDK]{track}", track]}


def _setup(db: Session, second: str = "Zerkalo_50x130"):  # type: ignore[no-untyped-def]
    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик Мест", role=ROLE_ASSEMBLER)
    loader = make_user(db, name="Оператор Мест", role=ROLE_LOADER)
    a = make_article(db, name="Zerkalo_50x130", sort_order=1)
    b = make_article(db, name="Zerkalo_60x160", sort_order=2)
    units_a = _units(db, site, worker, a, 3)
    units_b = _units(db, site, worker, b, 1)
    rows = [_row("Zerkalo_50x130"), _row(second, task="T2")]
    receive_batch(db, day=today_msk(), market="cdek", channel="СДЭК", items=rows)
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="СДЭК")
    db.commit()
    return site, loader, pallet, units_a, units_b, rows


def _scan(db: Session, pallet, loader, code: str, unit: str | None = None):  # type: ignore[no-untyped-def]
    result = scan_loading(db, code=code, pallet=pallet, actor=loader, unit_number=unit)
    db.commit()
    return result


def _items(db: Session) -> dict[str, ShipItem]:
    return {i.posting: i for i in db.scalars(select(ShipItem)).all()}


def test_two_mirrors_on_one_track(db: Session) -> None:
    site, loader, pallet, units, _, rows = _setup(db)
    items = _items(db)
    assert sorted(items) == ["1001", "1001#2"]
    assert {c.item_id for c in db.scalars(select(ShipCode)).all()} == {items["1001"].id}
    # Повторная отправка той же партии ничего не задваивает
    receive_batch(db, day=today_msk(), market="cdek", channel="СДЭК", items=rows)
    db.commit()
    assert len(_items(db)) == 2

    first = _scan(db, pallet, loader, "[CDK]1001", unit=units[0].number)
    assert first.ok and "заказ из 2 зеркал: погружено 1" in first.detail
    need = _scan(db, pallet, loader, "[CDK]1001")
    assert need.ok and need.await_unit and "погружено 1" in need.detail
    second = _scan(db, pallet, loader, "[CDK]1001", unit=units[1].number)
    assert second.ok and "заказ из 2 зеркал: погружено 2" in second.detail
    assert {i.state for i in _items(db).values()} == {SHIP_LOADED}
    barcodes = {db.get(Unit, u.id).mp_barcode for u in units[:2]}  # type: ignore[union-attr]
    assert barcodes == {"[CDK]1001", "[CDK]1001#2"}
    third = _scan(db, pallet, loader, "[CDK]1001", unit=units[2].number)
    assert not third.ok and "уже погружена" in third.message


def test_different_articles_on_one_track(db: Session) -> None:
    site, loader, pallet, _, units_b, _ = _setup(db, second="Zerkalo_60x160")
    # Сначала зеркало второго артикула: склад берёт место заказа с этим артикулом
    done = _scan(db, pallet, loader, units_b[0].number)
    assert done.ok and done.await_mp
    done = _scan(db, pallet, loader, "[CDK]1001", unit=units_b[0].number)
    assert done.ok, done.message
    items = _items(db)
    assert items["1001#2"].state == SHIP_LOADED and items["1001"].state == SHIP_WAIT


def test_order_waits_until_all_pieces_loaded(db: Session) -> None:
    site, loader, pallet, units, _, _ = _setup(db)
    lines = [{"market": "cdek", "channel": "СДЭК", "posting": "1001", "article": "Zerkalo_50x130", "qty": 2,
              "production": KOM}]
    decided = receive_demand(db, day=today_msk(), lines=lines)
    db.commit()
    assert decided[0].decision == "go"
    _scan(db, pallet, loader, "[CDK]1001", unit=units[0].number)
    # Половина погружена: заказ ещё не «погружено», второе зеркало держится, третье свободно
    other = [{"market": "wb", "channel": "WB", "posting": "W1", "article": "Zerkalo_50x130", "qty": 1,
              "production": KOM}]
    got = receive_demand(db, day=today_msk(), lines=other)
    db.commit()
    assert got[0].decision == "go"
    db.expire_all()
    order = receive_demand(db, day=today_msk(), lines=lines)[0]
    assert order.reason != "погружено"


def test_not_on_shelf_takes_one_piece(db: Session) -> None:
    site, _, _, _, _, _ = _setup(db)
    lead = make_user(db, name="Начальник Мест", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    assert not_on_shelf(db, code="[CDK]1001", actor=lead, site_ids=[site.id]).ok
    db.commit()
    states = sorted(i.state for i in _items(db).values())
    assert states == [SHIP_REMOVED, SHIP_WAIT]


def test_ozon_split_order_holds_only_unloaded(db: Session) -> None:
    """Ozon разделил заказ на 3 грузоместа с новыми номерами: погружено 2 — держим одно зеркало."""
    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик Озон", role=ROLE_ASSEMBLER)
    loader = make_user(db, name="Оператор Озон", role=ROLE_LOADER)
    a = make_article(db, name="Zerkalo_50x130", sort_order=1)
    units = _units(db, site, worker, a, 4)
    order = [{"market": "ozon", "channel": "Ozon", "truck": "Озон1", "posting": "777-0001-1",
              "article": "Zerkalo_50x130", "qty": 3, "production": KOM}]
    assert receive_demand(db, day=today_msk(), lines=order)[0].decision == "go"
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": f"777-0001-{n}", "origin": "777-0001-1", "article": "Zerkalo_50x130", "production": KOM,
         "codes": [f"%77{n}"]} for n in (2, 3, 4)
    ])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    for n, unit in zip((2, 3), units[:2], strict=True):
        assert _scan(db, pallet, loader, f"%77{n}", unit=unit.number).ok
    # На складе осталось 2, заказ держит одно — второе достаётся ровно одному новому заказу
    extra = [{"market": "wb", "channel": "WB", "posting": p, "article": "Zerkalo_50x130", "qty": 1,
              "production": KOM} for p in ("W9", "W10")]
    decided = receive_demand(db, day=today_msk(), lines=extra)
    assert sorted(d.decision for d in decided) == ["go", "no"]
