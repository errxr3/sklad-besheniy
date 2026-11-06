"""«Нет на полке»: начальник снимает этикетку сканом и списывает зеркало-призрак (2026-11-06)."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, ShipItem, Unit
from app.services.batches import receive_batch, return_items
from app.services.legacy_stock import apply_legacy, legacy_counts, preview_legacy
from app.services.not_on_shelf import NOT_ON_SHELF, not_on_shelf
from app.services.units import accept_unit, create_printed_units, stock_count
from app.statuses import (
    IN_STOCK,
    MV_INVENTORY_FIX,
    ROLE_ADMIN,
    ROLE_ASSEMBLER,
    ROLE_SHIFT_LEAD,
    SHIP_REMOVED,
    SHIP_WAIT,
    WRITTEN_OFF,
)
from app.timeutil import today_msk
from tests.helpers import login, make_article, make_user, site_by_name

KOM = "Коминтерна"


def _world(db: Session, labeled: int = 2):  # type: ignore[no-untyped-def]
    site = site_by_name(db, KOM)
    lead = make_user(db, name="Начальник Полки", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    worker = make_user(db, name="Сборщик Полки", role=ROLE_ASSEMBLER, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_40x130", sort_order=1)
    units = create_printed_units(db, article_id=article.id, qty=labeled, site_id=site.id,
                                 assembler_id=worker.id, actor_id=worker.id) if labeled else []
    for unit in units:
        db.commit()
        accept_unit(db, code=unit.number, actor=worker)
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": f"OZ-{i}", "article": "Zerkalo_40x130", "production": KOM, "codes": [f"%OZ{i}"]}
        for i in range(1, 4)
    ])
    db.commit()
    return site, lead, article


def _item(db: Session, posting: str) -> ShipItem:
    item = db.scalar(select(ShipItem).where(ShipItem.posting == posting))
    assert item is not None
    return item


def test_removes_label_and_writes_off_phantom(db: Session) -> None:
    site, lead, article = _world(db, labeled=2)
    result = not_on_shelf(db, code="%OZ1", actor=lead, site_ids=[site.id])
    db.commit()
    assert result.ok and result.detail == "списано со склада: 1" and result.stock == 1
    item = _item(db, "OZ-1")
    assert item.state == SHIP_REMOVED and item.removed_reason == NOT_ON_SHELF
    gone = db.get(Unit, item.unit_id)
    assert gone is not None and gone.status == WRITTEN_OFF
    move = db.scalar(select(Movement).where(Movement.unit_id == gone.id).order_by(Movement.id.desc()))
    assert move is not None and move.type == MV_INVENTORY_FIX and "OZ-1" in (move.note or "")
    # Повторный скан той же наклейки ничего больше не списывает
    again = not_on_shelf(db, code="%OZ1", actor=lead, site_ids=[site.id])
    assert not again.ok and "снята" in again.message
    assert stock_count(db, article.id, site.id) == 1


def test_nothing_in_stock_only_removes_label(db: Session) -> None:
    site, lead, article = _world(db, labeled=0)
    result = not_on_shelf(db, code="%OZ2", actor=lead, site_ids=[site.id])
    db.commit()
    assert result.ok and "не числилось" in result.detail
    assert _item(db, "OZ-2").state == SHIP_REMOVED and _item(db, "OZ-2").unit_id is None


def test_old_without_label_goes_first(db: Session) -> None:
    site, lead, article = _world(db, labeled=1)
    admin = make_user(db, name="Админ Полки", role=ROLE_ADMIN)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_40x130", KOM, 2)]), actor=admin)
    db.commit()
    not_on_shelf(db, code="%OZ1", actor=lead, site_ids=[site.id])
    db.commit()
    assert legacy_counts(db) == {(article.id, site.id): 1}
    labeled = db.scalars(select(Unit).where(Unit.unlabeled.is_(False), Unit.article_id == article.id)).all()
    assert [u.status for u in labeled] == [IN_STOCK]


def test_return_brings_mirror_back(db: Session) -> None:
    site, lead, article = _world(db, labeled=2)
    not_on_shelf(db, code="%OZ3", actor=lead, site_ids=[site.id])
    db.commit()
    item = _item(db, "OZ-3")
    unit_id = item.unit_id
    assert return_items(db, item_ids=[item.id], site_ids=[site.id], actor_id=lead.id) == 1
    db.commit()
    assert item.state == SHIP_WAIT and item.unit_id is None
    assert db.get(Unit, unit_id).status == IN_STOCK  # type: ignore[union-attr]
    assert stock_count(db, article.id, site.id) == 2


def test_other_site_and_unknown_code(db: Session) -> None:
    site, lead, _ = _world(db, labeled=1)
    other = site_by_name(db, "Учительская")
    assert not not_on_shelf(db, code="%OZ1", actor=lead, site_ids=[other.id]).ok
    assert not not_on_shelf(db, code="%NOPE", actor=lead, site_ids=[site.id]).ok
    assert _item(db, "OZ-1").state == SHIP_WAIT


def test_page_scan_for_lead(client: TestClient, db: Session) -> None:
    site, lead, article = _world(db, labeled=1)
    login(client, lead)
    assert "Нет на полке" in client.get("/shipments/not-on-shelf").text
    assert "Нет на полке" in client.get("/shipments/batches").text
    page = client.get("/shipments/not-on-shelf").text
    token = page.split("csrf: '")[1].split("'")[0]
    done = client.post("/shipments/not-on-shelf/scan", json={"code": "%OZ2"},
                       headers={"X-CSRF-Token": token})
    assert done.status_code == 200 and done.json()["ok"], done.text
    db.expire_all()
    assert stock_count(db, article.id, site.id) == 0
