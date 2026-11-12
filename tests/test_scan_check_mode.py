"""Режим проверки сканов: склад не решает по остаткам, всё присланное едет (2026-11-10)."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ShipDemand
from app.services.batches import CHECK_REASON, receive_demand
from app.services.ship_plan import build_ship_plan
from app.settings_store import SCAN_CHECK_KEY, scan_check_on, set_value
from app.statuses import ROLE_ADMIN, ROLE_LOADER
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user


def _lines() -> list[dict]:
    return [
        {"market": "ozon", "channel": "Ozon", "truck": "Озон1", "posting": p, "article": "Zerkalo_60x160",
         "qty": 1, "production": "Коминтерна"}
        for p in ("C-1", "C-2", "C-3")
    ]


def test_everything_goes_without_stock_but_colored_stays_out(db: Session) -> None:
    make_article(db, name="Zerkalo_60x160")
    set_value(db, SCAN_CHECK_KEY, "1")
    lines = receive_demand(db, day=today_msk(), lines=_lines(), drop=[("ozon", "C-3")])
    db.commit()
    decisions = {line.posting: (line.decision, line.reason) for line in lines}
    assert decisions["C-1"] == ("go", CHECK_REASON) and decisions["C-2"] == ("go", CHECK_REASON)
    assert decisions["C-3"][0] == "no"           # покрашено в BLAGO «не едет» — не едет и в проверке
    plan = build_ship_plan(db, day=today_msk())
    row = next(r for r in plan.rows if r.name == "Zerkalo_60x160")
    assert row.need_total == 2 and row.state != "no"


def test_without_mode_no_stock_means_no(db: Session) -> None:
    make_article(db, name="Zerkalo_60x160")
    lines = receive_demand(db, day=today_msk(), lines=_lines())
    db.commit()
    assert {line.decision for line in lines} == {"no"}


def test_admin_toggles_mode_and_plan_is_recalculated(client: TestClient, db: Session) -> None:
    make_article(db, name="Zerkalo_60x160")
    receive_demand(db, day=today_msk(), lines=_lines())
    admin = make_user(db, name="Админ Проверки", role=ROLE_ADMIN)
    db.commit()
    login(client, admin)
    token = get_csrf(client, "/admin/settings")
    form = {"csrf_token": token, "label_width_mm": "58", "label_height_mm": "40", "ship_cancel_minutes": "10"}
    client.post("/admin/settings", data={**form, "scan_check": "1"})
    db.expire_all()
    assert scan_check_on(db)
    assert {line.decision for line in db.query(ShipDemand)} == {"go"}
    assert "check-dot" in client.get("/admin/settings").text
    client.post("/admin/settings", data=form)
    db.expire_all()
    assert not scan_check_on(db)
    assert {line.decision for line in db.query(ShipDemand)} == {"no"}
    assert "check-dot" not in client.get("/admin/settings").text


def test_loader_sees_banner(client: TestClient, db: Session) -> None:
    set_value(db, SCAN_CHECK_KEY, "1")
    loader = make_user(db, name="Грузчик Проверки", role=ROLE_LOADER)
    db.commit()
    login(client, loader)
    assert "check-dot" in client.get("/").text


def test_wipe_removes_everything_done_during_check(client: TestClient, db: Session) -> None:
    """Пробный день: заказы → партия → пик-пики → «Отгрузка» видна. «Стереть» — как не было."""
    from sqlalchemy import select

    from app.models import Pallet, ShipBatch, ShipItem, Unit
    from app.services.batches import receive_batch
    from app.services.scan_check import check_since, set_check_mode, wipe_check
    from app.services.shipment_report import shipped_by_article_column
    from app.services.shipping import join_channel, scan_loading
    from app.services.units import accept_unit, create_printed_units, stock_count
    from app.statuses import IN_STOCK, ROLE_ASSEMBLER
    from tests.helpers import site_by_name

    site = site_by_name(db, "Коминтерна")
    art = make_article(db, name="Zerkalo_60x160")
    worker = make_user(db, name="Сборщик Пробы", role=ROLE_ASSEMBLER, home_site_id=site.id)
    loader = make_user(db, name="Грузчик Пробы", role=ROLE_LOADER, home_site_id=site.id)
    admin = make_user(db, name="Админ Пробы", role=ROLE_ADMIN)
    units = create_printed_units(db, article_id=art.id, qty=2, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()

    set_check_mode(db, True)
    db.commit()
    assert check_since(db) is not None
    receive_demand(db, day=today_msk(), lines=_lines())
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": "C-1", "article": "Zerkalo_60x160", "production": "Коминтерна", "codes": ["%C1"]},
        {"posting": "C-2", "article": "Zerkalo_60x160", "production": "Коминтерна", "codes": ["%C2"]},
    ])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert scan_loading(db, code=units[0].number, pallet=pallet, actor=loader, ship_code="%C1").ok
    db.commit()
    assert stock_count(db, art.id, site.id) == 1
    from collections import defaultdict

    pending: dict = defaultdict(int)
    shipped_by_article_column(db, day=today_msk(), site_id=site.id, pending=pending)
    assert sum(pending.values()) == 1                     # погрузка видна в «Отгрузке»

    done = wipe_check(db, actor=admin)
    db.commit()
    assert (done.shipments, done.batches, done.items, done.demand, done.pallets) == (1, 1, 2, 3, 1)
    db.expire_all()
    assert db.get(Unit, units[0].id).status == IN_STOCK   # type: ignore[union-attr]
    assert stock_count(db, art.id, site.id) == 2           # остаток как до проверки
    assert db.scalars(select(ShipBatch)).all() == [] and db.scalars(select(ShipItem)).all() == []
    assert db.query(ShipDemand).count() == 0               # плана нет
    assert db.get(Pallet, pallet.id).closed_at is not None  # type: ignore[union-attr]
    assert not scan_check_on(db) and check_since(db) is None
    pending = defaultdict(int)
    closed = shipped_by_article_column(db, day=today_msk(), site_id=site.id, pending=pending)
    assert not closed and not pending                      # в «Отгрузке» пусто


def test_wipe_button_on_settings_page(client: TestClient, db: Session) -> None:
    admin = make_user(db, name="Админ Кнопки", role=ROLE_ADMIN)
    db.commit()
    login(client, admin)
    token = get_csrf(client, "/admin/settings")
    assert "Стереть проверку" not in client.get("/admin/settings").text
    form = {"csrf_token": token, "label_width_mm": "58", "label_height_mm": "40", "ship_cancel_minutes": "10"}
    client.post("/admin/settings", data={**form, "scan_check": "1"})
    page = client.get("/admin/settings").text
    assert "Стереть проверку" in page
    client.post("/admin/settings", data=form)              # выключили — стереть всё ещё можно
    assert "Стереть проверку" in client.get("/admin/settings").text
    answer = client.post("/admin/scan-check/wipe", data={"csrf_token": token})
    assert "Проверка стёрта" in answer.text and "Стереть проверку" not in answer.text
