"""Озон и ВБ без плана BLAGO не грузят, пока начальник не нажал «Грузить без плана» (2026-11-03)."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Unit
from app.services.batches import receive_batch
from app.services.shipping import NO_PLAN_MESSAGE, join_channel, scan_loading, set_manual, waits_for_plan
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name

KOM = "Коминтерна"


def _world(db: Session, qty: int = 3):  # type: ignore[no-untyped-def]
    site = site_by_name(db, KOM)
    lead = make_user(db, name="Начальник Плана", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    worker = make_user(db, name="Сборщик Плана", role=ROLE_ASSEMBLER, home_site_id=site.id)
    loader = make_user(db, name="Грузчик Плана", role=ROLE_LOADER, home_site_id=site.id)
    article = make_article(db, name="Zerkalo_60x160", sort_order=1)
    units: list[Unit] = []
    for unit in create_printed_units(db, article_id=article.id, qty=qty, site_id=site.id,
                                     assembler_id=worker.id, actor_id=worker.id):
        db.commit()
        accept_unit(db, code=unit.number, actor=worker)
        units.append(unit)
    db.commit()
    return site, lead, loader, units


def test_ozon_without_plan_is_closed_until_lead_allows(db: Session) -> None:
    site, lead, loader, units = _world(db)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert waits_for_plan(db, pallet)
    refused = scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    assert not refused.ok and refused.message == NO_PLAN_MESSAGE
    # Старое зеркало без этикетки тоже не проскочит
    article_id = units[0].article_id
    legacy = scan_loading(db, code="СТАРОЕ", pallet=pallet, actor=loader, legacy_article_id=article_id)
    assert not legacy.ok and legacy.message == NO_PLAN_MESSAGE

    assert set_manual(db, site_id=site.id, channel="Ozon", actor=lead, on=True)
    assert not set_manual(db, site_id=site.id, channel="Ozon", actor=lead, on=True)  # уже открыт
    db.commit()
    assert scan_loading(db, code=units[0].number, pallet=pallet, actor=loader).ok
    db.commit()

    set_manual(db, site_id=site.id, channel="Ozon", actor=lead, on=False)
    db.commit()
    assert not scan_loading(db, code=units[1].number, pallet=pallet, actor=loader).ok


def test_cdek_needs_no_plan_and_other_channel_stays_closed(db: Session) -> None:
    site, lead, loader, units = _world(db)
    set_manual(db, site_id=site.id, channel="Ozon", actor=lead, on=True)
    cdek = join_channel(db, user=loader, site=site, channel="СДЭК")
    wb = join_channel(db, user=loader, site=site, channel="WB")
    db.commit()
    assert scan_loading(db, code=units[0].number, pallet=cdek, actor=loader).ok
    assert not scan_loading(db, code=units[1].number, pallet=wb, actor=loader).ok  # разрешён только Озон


def test_plan_from_blago_opens_channel_with_label_check(db: Session) -> None:
    site, _, loader, units = _world(db)
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": "OZ-1", "article": "Zerkalo_60x160", "production": KOM, "codes": ["%OZ1"]},
    ])
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert not waits_for_plan(db, pallet)
    first = scan_loading(db, code="%OZ1", pallet=pallet, actor=loader)
    assert first.ok and first.await_unit


def test_lead_page_button_and_loader_banner(client: TestClient, db: Session) -> None:
    site, lead, loader, _ = _world(db)
    open_shift(db, loader, site)
    pallet = join_channel(db, user=loader, site=site, channel="WB")
    db.commit()

    login(client, loader)
    assert "Плана BLAGO на этот канал сегодня нет" in client.get(f"/loading?pallet_id={pallet.id}").text
    client.post("/logout", data={"csrf_token": get_csrf(client, "/")})

    login(client, lead)
    token = get_csrf(client, "/shipments/batches")
    page = client.get("/shipments/batches").text
    assert "плана нет, погрузка закрыта" in page
    form = {"csrf_token": token, "site_id": str(site.id), "channel": "WB", "on": "1"}
    done = client.post("/shipments/manual", data=form)
    assert "сегодня грузят без плана" in done.text
    assert "без плана <span" in client.get("/shipments/batches").text
    db.expire_all()
    assert not waits_for_plan(db, pallet)
