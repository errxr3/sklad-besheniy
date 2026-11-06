"""Несколько зеркал под одной этикеткой: заказ СДЭК из двух зеркал на один трек (2026-11-06)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShipCode, ShipDemand, ShipItem, Unit
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


def _scan(db: Session, pallet, loader, code: str, unit: str | None = None,  # type: ignore[no-untyped-def]
          ship: str | None = None):
    result = scan_loading(db, code=code, pallet=pallet, actor=loader, unit_number=unit, ship_code=ship)
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


def test_old_big_order_holds_stock(db: Session) -> None:
    """Большой заказ ждёт со вчера: 13 зеркал копятся под него, новые заказы их не забирают."""
    from datetime import timedelta

    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик Копит", role=ROLE_ASSEMBLER)
    article = make_article(db, name="Zerkalo_40x130", sort_order=1)
    _units(db, site, worker, article, 13)
    big = [{"market": "ozon", "channel": "Ozon", "truck": "Озон1", "posting": "BIG-1",
            "article": "Zerkalo_40x130", "qty": 15, "production": KOM}]
    small = [{"market": "wb", "channel": "WB", "posting": "S1", "article": "Zerkalo_40x130", "qty": 1,
              "production": KOM}]
    yesterday = today_msk() - timedelta(days=1)
    # Вчера: большой не едет, мелкий забирает своё — как раньше
    assert receive_demand(db, day=yesterday, lines=big)[0].decision == "no"
    db.commit()
    # Сегодня большой ждёт со вчера: склад держит под него всё, новый мелкий не едет
    decided = receive_demand(db, day=today_msk(), lines=small)
    db.commit()
    assert decided[0].decision == "no"
    order = db.scalar(select(ShipDemand).where(ShipDemand.posting == "BIG-1"))
    assert order is not None and order.decision == "no" and "копит" in (order.reason or "")


def test_colored_orders_stay_in_plan_but_dont_go(db: Session) -> None:
    """Покрасил «не едет» — заказ в плане остаётся (видно, сколько упало), но не едет и зеркала не держит."""
    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик Красный", role=ROLE_ASSEMBLER)
    article = make_article(db, name="Zerkalo_40x130", sort_order=1)
    _units(db, site, worker, article, 1)
    art = "Zerkalo_40x130"
    lines = [{"market": "ozon", "channel": "Ozon", "truck": "Озон3", "posting": p, "article": art,
              "qty": 1, "production": KOM} for p in ("R-1", "R-2")]
    receive_demand(db, day=today_msk(), lines=lines)
    db.commit()
    # Покрасил R-1 красным уже после отправки: BLAGO присылает drop
    receive_demand(db, day=today_msk(), lines=[], drop=[("ozon", "R-1")])
    db.commit()
    plan = {d.posting: d for d in db.scalars(select(ShipDemand)).all()}
    assert set(plan) == {"R-1", "R-2"}  # оба в плане
    assert plan["R-1"].decision == "no" and "убрано" in (plan["R-1"].reason or "")
    assert plan["R-2"].decision == "go"  # единственное зеркало досталось R-2
    # Сняли покраску и прислали заново — снова едет (зеркал нет — значит «не хватает», но не «покрашено»)
    receive_demand(db, day=today_msk(), lines=lines[:1])
    db.commit()
    assert "убрано" not in (db.scalar(select(ShipDemand).where(ShipDemand.posting == "R-1")).reason or "")


def test_unknown_kgt_sticker_learns_code(db: Session) -> None:
    """КГТ Ozon: на наклейке только «ii…» — склад привязывает её к заказу артикула (2026-11-06)."""
    from app.services.legacy_stock import apply_legacy, preview_legacy

    site = site_by_name(db, KOM)
    worker = make_user(db, name="Сборщик КГТ", role=ROLE_ASSEMBLER)
    loader = make_user(db, name="Оператор КГТ", role=ROLE_LOADER)
    admin = make_user(db, name="Админ КГТ", role="admin")
    a = make_article(db, name="Zerkalo_50x130", sort_order=1)
    make_article(db, name="Zerkalo_60x160", sort_order=2)
    units = _units(db, site, worker, a, 1)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 1)]), actor=admin)
    receive_batch(db, day=today_msk(), market="ozon", channel="КГТ", items=[
        {"posting": "K-1", "article": "Zerkalo_50x130", "production": KOM, "codes": ["651668005236000"]},
        {"posting": "K-2", "article": "Zerkalo_60x160", "production": KOM, "codes": ["502184971349000"]},
    ])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="КГТ")
    db.commit()
    # Новое зеркало: наклейка → «код новый» → наша этикетка → погружено, код запомнен
    first = _scan(db, pallet, loader, "ii50196073009")
    assert first.ok and first.await_unit and first.unknown
    done = _scan(db, pallet, loader, units[0].number, ship="II50196073009")
    assert done.ok, done.message
    assert _items(db)["K-1"].state == SHIP_LOADED
    again = _scan(db, pallet, loader, "ii50196073009")
    assert not again.ok and "уже погружена" in again.message
    # Старое без этикетки: наклейка → выбрать артикул → погружено старое
    _scan(db, pallet, loader, "ii50196071349")
    old_id = db.scalar(select(ShipItem.article_id).where(ShipItem.posting == "K-2"))
    old = scan_loading(db, code="СТАРОЕ", pallet=pallet, actor=loader, ship_code="II50196071349",
                       legacy_article_id=old_id)
    db.commit()
    assert old.ok and "Старое" in old.message, old.message
    assert _items(db)["K-2"].state == SHIP_LOADED
