"""SPEC §6.5–6.7: перемещения между площадками, инвентаризация, возвраты."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, Movement, Unit
from app.services.shipping import open_new_pallet, scan_loading
from app.services.units import accept_unit, create_printed_units, stock_count
from app.services.warehouse import (
    WarehouseError,
    active_inventory,
    finish_inventory,
    inventory_diff,
    return_unit,
    scan_inventory,
    start_inventory,
    transfer_in,
    transfer_out,
    units_in_transit,
)
from app.statuses import (
    IN_STOCK,
    IN_TRANSIT,
    MV_INVENTORY_FIX,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_SHIFT_LEAD,
    SHIPPED,
    WRITTEN_OFF,
)
from tests.helpers import make_article, make_user, site_by_name


def _stocked(db: Session, qty: int = 2, site_name: str = "Коминтерна", tag: str = "") -> list[Unit]:
    """Зеркала на складе площадки."""
    assembler = make_user(db, name=f"Сборщик {site_name}{tag}{qty}", role=ROLE_ASSEMBLER)
    site = site_by_name(db, site_name)
    article = make_article(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=qty,
        site_id=site.id,
        assembler_id=assembler.id,
        actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()
    return units


def test_transfer_between_sites(db: Session) -> None:
    """Перемещение: остаток уходит у отправителя и появляется у получателя."""
    units = _stocked(db, 2)
    lead = make_user(db, name="Начальник перемещения", role=ROLE_SHIFT_LEAD)
    source = site_by_name(db)
    target = site_by_name(db, "Учительская")
    article_id = units[0].article_id

    assert stock_count(db, article_id, source.id) == 2

    sent = transfer_out(db, code=units[0].number, actor=lead, to_site_id=target.id)
    db.commit()
    db.refresh(units[0])
    assert sent.ok
    assert units[0].status == IN_TRANSIT
    assert stock_count(db, article_id, source.id) == 1
    assert stock_count(db, article_id, target.id) == 0

    received = transfer_in(db, code=units[0].number, actor=lead, site_id=target.id)
    db.commit()
    db.refresh(units[0])
    assert received.ok
    assert units[0].status == IN_STOCK
    assert units[0].site_id == target.id
    assert stock_count(db, article_id, target.id) == 1


def test_transfer_in_wrong_site_refused(db: Session) -> None:
    """Принять перемещение можно только на площадке назначения."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник 2 перемещения", role=ROLE_SHIFT_LEAD)
    source = site_by_name(db)
    target = site_by_name(db, "Учительская")

    transfer_out(db, code=units[0].number, actor=lead, to_site_id=target.id)
    db.commit()

    wrong = transfer_in(db, code=units[0].number, actor=lead, site_id=source.id)
    assert not wrong.ok
    assert "отправлено на" in wrong.message


def test_transfer_requires_stock_status(db: Session) -> None:
    """Отправить можно только то, что лежит на складе."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник 3", role=ROLE_SHIFT_LEAD)
    loader = make_user(db, name="Грузчик перемещения", role=ROLE_LOADER)
    pallet = open_new_pallet(db, user=loader, site=site_by_name(db), channel="Ozon")
    scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()

    result = transfer_out(
        db, code=units[0].number, actor=lead, to_site_id=site_by_name(db, "Учительская").id
    )
    assert not result.ok
    assert "Отгружено" in result.message


def test_in_transit_list(db: Session) -> None:
    """Зеркала в пути дольше суток попадают в отдельный список."""
    from datetime import timedelta

    from sqlalchemy import text

    from app.timeutil import now_utc

    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник 4", role=ROLE_SHIFT_LEAD)
    transfer_out(db, code=units[0].number, actor=lead, to_site_id=site_by_name(db, "Учительская").id)
    db.commit()
    assert units_in_transit(db) == []

    db.execute(text("ALTER TABLE movements DISABLE TRIGGER USER"))
    db.execute(
        text("UPDATE movements SET created_at = :moment WHERE type = 'transfer_out'"),
        {"moment": now_utc() - timedelta(days=2)},
    )
    db.execute(text("ALTER TABLE movements ENABLE TRIGGER USER"))
    db.commit()
    assert len(units_in_transit(db)) == 1


def test_return_good_and_bad(db: Session) -> None:
    """Годный возврат возвращается на склад, негодный списывается (SPEC §6.7)."""
    units = _stocked(db, 2)
    lead = make_user(db, name="Начальник возвратов", role=ROLE_SHIFT_LEAD)
    loader = make_user(db, name="Грузчик возвратов", role=ROLE_LOADER)
    site = site_by_name(db)
    pallet = open_new_pallet(db, user=loader, site=site, channel="Ozon")
    for unit in units:
        scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    db.commit()

    good = return_unit(db, code=units[0].number, actor=lead, good=True, site_id=site.id)
    bad = return_unit(db, code=units[1].number, actor=lead, good=False, site_id=site.id)
    db.commit()
    db.refresh(units[0])
    db.refresh(units[1])

    assert good.ok and units[0].status == IN_STOCK
    assert bad.ok and units[1].status == WRITTEN_OFF
    assert stock_count(db, units[0].article_id, site.id) == 1


def test_return_only_for_shipped(db: Session) -> None:
    """Возврат возможен только для отгруженного зеркала."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник 5", role=ROLE_SHIFT_LEAD)
    result = return_unit(
        db, code=units[0].number, actor=lead, good=True, site_id=site_by_name(db).id
    )
    assert not result.ok
    assert "только для отгруженного" in result.message


def test_inventory_finds_missing_and_extra(db: Session) -> None:
    """Инвентаризация находит ненайденное и лишнее (SPEC §6.6)."""
    units = _stocked(db, 3)
    lead = make_user(db, name="Начальник инвентаризации", role=ROLE_SHIFT_LEAD)
    site = site_by_name(db)

    # Одно зеркало «потерялось» — его не отсканируют
    inventory = start_inventory(db, site_id=site.id, actor=lead)
    db.commit()

    scan_inventory(db, inventory_id=inventory.id, code=units[0].number, actor=lead)
    scan_inventory(db, inventory_id=inventory.id, code=units[1].number, actor=lead)
    db.commit()

    diff = inventory_diff(db, inventory)
    assert [u.id for u in diff.missing] == [units[2].id]
    assert diff.extra == []
    assert diff.scanned == 2

    written_off, restored = finish_inventory(db, inventory_id=inventory.id, actor=lead)
    db.commit()
    db.refresh(units[2])

    assert written_off == 1
    assert restored == 0
    assert units[2].status == WRITTEN_OFF
    fix = db.scalar(
        select(Movement).where(Movement.unit_id == units[2].id, Movement.type == MV_INVENTORY_FIX)
    )
    assert fix is not None
    assert fix.ref_id == inventory.id


def test_inventory_restores_extra_unit(db: Session) -> None:
    """Отсканированное, но списанное зеркало возвращается на склад."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник инвентаризации 2", role=ROLE_SHIFT_LEAD)
    site = site_by_name(db)

    from app.services.units import write_off_unit

    write_off_unit(db, code=units[0].number, actor=lead, reason="потеря")
    db.commit()
    db.refresh(units[0])
    assert units[0].status == WRITTEN_OFF

    inventory = start_inventory(db, site_id=site.id, actor=lead)
    db.commit()
    scan_inventory(db, inventory_id=inventory.id, code=units[0].number, actor=lead)
    db.commit()

    diff = inventory_diff(db, inventory)
    assert [u.id for u in diff.extra] == [units[0].id]

    written_off, restored = finish_inventory(db, inventory_id=inventory.id, actor=lead)
    db.commit()
    db.refresh(units[0])
    assert restored == 1
    assert written_off == 0
    assert units[0].status == IN_STOCK


def test_repeated_inventory_scan_is_not_an_error(db: Session) -> None:
    """Повторный скан в инвентаризации — «уже учтено», без ошибки."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник инвентаризации 3", role=ROLE_SHIFT_LEAD)
    inventory = start_inventory(db, site_id=site_by_name(db).id, actor=lead)
    db.commit()

    first = scan_inventory(db, inventory_id=inventory.id, code=units[0].number, actor=lead)
    db.commit()
    second = scan_inventory(db, inventory_id=inventory.id, code=units[0].number, actor=lead)
    db.commit()

    assert first.ok
    assert second.ok
    assert "Уже учтено" in second.detail


def test_only_one_active_inventory_per_site(db: Session) -> None:
    """На площадке одновременно идёт только одна инвентаризация."""
    lead = make_user(db, name="Начальник инвентаризации 4", role=ROLE_SHIFT_LEAD)
    site = site_by_name(db)
    start_inventory(db, site_id=site.id, actor=lead)
    db.commit()
    with pytest.raises(WarehouseError):
        start_inventory(db, site_id=site.id, actor=lead)
    db.rollback()
    assert active_inventory(db, site.id) is not None


def test_units_shipped_during_inventory_are_excluded(db: Session) -> None:
    """Зеркала, отгруженные после старта инвентаризации, в расхождения не попадают."""
    units = _stocked(db, 2)
    lead = make_user(db, name="Начальник инвентаризации 5", role=ROLE_SHIFT_LEAD)
    loader = make_user(db, name="Грузчик инвентаризации", role=ROLE_LOADER)
    site = site_by_name(db)

    inventory = start_inventory(db, site_id=site.id, actor=lead)
    db.commit()

    # Первое зеркало успели отгрузить уже во время пересчёта
    pallet = open_new_pallet(db, user=loader, site=site, channel="СДЭК")
    scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()

    scan_inventory(db, inventory_id=inventory.id, code=units[1].number, actor=lead)
    db.commit()

    diff = inventory_diff(db, inventory)
    assert diff.missing == []
    assert diff.extra == []

    written_off, restored = finish_inventory(db, inventory_id=inventory.id, actor=lead)
    db.commit()
    db.refresh(units[0])
    assert (written_off, restored) == (0, 0)
    assert units[0].status == SHIPPED


def test_finished_inventory_cannot_be_scanned(db: Session) -> None:
    """После завершения инвентаризации сканы не принимаются."""
    units = _stocked(db, 1)
    lead = make_user(db, name="Начальник инвентаризации 6", role=ROLE_SHIFT_LEAD)
    inventory = start_inventory(db, site_id=site_by_name(db).id, actor=lead)
    db.commit()
    finish_inventory(db, inventory_id=inventory.id, actor=lead)
    db.commit()

    result = scan_inventory(db, inventory_id=inventory.id, code=units[0].number, actor=lead)
    assert not result.ok
    assert "завершена" in result.message
    stored = db.get(Inventory, inventory.id)
    assert stored is not None and stored.finished_at is not None
