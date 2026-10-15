"""SPEC §12 п. 7: сверка с заказом BLAGO OTGRUZKI при отгрузке."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import MpShipment
from app.services.apikeys import create_key
from app.services.shipping import open_new_pallet, scan_loading
from app.services.units import accept_unit, create_printed_units, stock_count
from app.settings_store import set_value
from app.statuses import IN_STOCK, ROLE_ASSEMBLER, ROLE_LOADER, SHIPPED
from tests.helpers import make_article, make_user, site_by_name


def _setup(db: Session, article_name: str = "Zerkalo_60x160"):  # type: ignore[no-untyped-def]
    """Зеркало на складе, грузчик с паллетой, включённая сверка."""
    assembler = make_user(db, name="Сборщик сверки", role=ROLE_ASSEMBLER)
    loader = make_user(db, name="Грузчик сверки", role=ROLE_LOADER)
    site = site_by_name(db)
    article = make_article(db, name=article_name)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site.id, assembler_id=assembler.id, actor_id=assembler.id
    )[0]
    db.commit()
    accept_unit(db, code=unit.number, actor=assembler)
    set_value(db, "require_mp_barcode", "1")
    set_value(db, "verify_order", "1")
    pallet = open_new_pallet(db, user=loader, site=site, channel="Ozon")
    db.commit()
    return unit, loader, pallet


def test_mismatch_refused_and_unit_stays_in_stock(db: Session) -> None:
    """Артикул не совпал с заказом → отказ, зеркало остаётся на складе (SPEC §12 п. 7)."""
    unit, loader, pallet = _setup(db)
    db.add(MpShipment(mp_barcode="4600000000001", article_name="Zerkalo_60x170",
                      article_name_norm="zerkalo_60x170"))
    db.commit()

    first = scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    assert first.ok and first.await_mp

    second = scan_loading(db, code="4600000000001", pallet=pallet, actor=loader, unit_number=unit.number)
    db.commit()
    db.refresh(unit)

    assert not second.ok
    assert second.message == "Зеркало Zerkalo_60x160, а заказ на Zerkalo_60x170"
    assert unit.status == IN_STOCK
    assert unit.mp_barcode is None
    assert stock_count(db, unit.article_id, unit.site_id) == 1


def test_match_ships_unit(db: Session) -> None:
    """Совпадение (в том числе с кириллической «х» в заказе) → отгрузка."""
    unit, loader, pallet = _setup(db, "Zerkalo_100x200")
    db.add(MpShipment(mp_barcode="4600000000002", article_name="Zerkalo_100х200",
                      article_name_norm="zerkalo_100x200"))
    db.commit()

    scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    result = scan_loading(db, code="4600000000002", pallet=pallet, actor=loader, unit_number=unit.number)
    db.commit()
    db.refresh(unit)

    assert result.ok, result.message
    assert unit.status == SHIPPED
    assert unit.mp_barcode == "4600000000002"


def test_unknown_order_refused(db: Session) -> None:
    """ШК, которого нет в списке отправлений, — отказ, зеркало на складе."""
    unit, loader, pallet = _setup(db)
    scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    result = scan_loading(db, code="4600000000999", pallet=pallet, actor=loader, unit_number=unit.number)
    db.commit()
    db.refresh(unit)
    assert not result.ok
    assert "не найден в списке отправлений" in result.message
    assert unit.status == IN_STOCK


def test_api_shipments_feed_the_check(client: TestClient, db: Session) -> None:
    """Список, присланный через API, сразу используется сверкой."""
    unit, loader, pallet = _setup(db)
    _, key = create_key(db, name="BLAGO", created_by=None)
    db.commit()
    response = client.post(
        "/api/v1/shipments",
        json=[{"mp_barcode": "4600000000003", "article": "Zerkalo_60x160"}],
        headers={"X-API-Key": key},
    )
    assert response.json()["added"] == 1

    scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    result = scan_loading(db, code="4600000000003", pallet=pallet, actor=loader, unit_number=unit.number)
    db.commit()
    assert result.ok, result.message
