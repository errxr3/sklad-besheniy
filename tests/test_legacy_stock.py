"""Старый остаток без этикеток: вносится числом, грузится по наклейке маркетплейса (2026-11-03)."""

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ShipItem, Unit
from app.services.batches import receive_batch
from app.services.legacy_stock import apply_legacy, legacy_counts, preview_legacy, read_legacy_xlsx
from app.services.shipping import join_channel, scan_loading
from app.services.units import accept_unit, create_printed_units
from app.services.warehouse import inventory_diff, start_inventory
from app.statuses import (
    IN_STOCK,
    ROLE_ADMIN,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_SHIFT_LEAD,
    SHIP_LOADED,
    SHIPPED,
)
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name

KOM = "Коминтерна"


def _xlsx(rows: list[tuple]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append(["Артикул", "Площадка", "Количество"])
    for row in rows:
        sheet.append(list(row))
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def _world(db: Session):  # type: ignore[no-untyped-def]
    site = site_by_name(db, KOM)
    admin = make_user(db, name="Админ Остатков", role=ROLE_ADMIN)
    worker = make_user(db, name="Сборщик Остатков", role=ROLE_ASSEMBLER, home_site_id=site.id)
    loader = make_user(db, name="Грузчик Остатков", role=ROLE_LOADER, home_site_id=site.id)
    a = make_article(db, name="Zerkalo_60x160", sort_order=1)
    b = make_article(db, name="Zerkalo_70x200", sort_order=2)
    # Сборщик уже сдал 2 зеркала 60x160 с этикетками — их в Excel тоже посчитали «на полке»
    for unit in create_printed_units(db, article_id=a.id, qty=2, site_id=site.id, assembler_id=worker.id,
                                     actor_id=worker.id):
        db.commit()
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    return site, admin, loader, a, b


def test_excel_sets_exact_old_count(db: Session) -> None:
    site, admin, _, a, b = _world(db)  # 2 новых с этикетками — в файл их не пишут
    lines = read_legacy_xlsx(_xlsx([("Zerkalo_60x160", KOM, 5), ("Zerkalo_70х200", KOM, 3),
                                    ("Нет_такого", KOM, 4)]))
    rows = {r.name: r for r in preview_legacy(db, lines)}
    assert rows["Zerkalo_60x160"].to_create == 5
    assert rows["Zerkalo_70x200"].to_create == 3  # кириллическая «х» в файле — тот же артикул
    assert rows["Нет_такого"].problem and rows["Нет_такого"].to_create == 0
    assert apply_legacy(db, list(rows.values()), actor=admin) == (8, 0)
    db.commit()
    assert legacy_counts(db) == {(a.id, site.id): 5, (b.id, site.id): 3}
    # Тот же файл ещё раз — ничего не задвоилось
    assert sum(r.to_create + r.to_remove for r in preview_legacy(db, lines)) == 0
    # Пересчёт через месяц: 60x160 на полке 2, 70x200 не осталось (0), новые с этикетками не трогаются
    recount = preview_legacy(db, read_legacy_xlsx(_xlsx([("Zerkalo_60x160", KOM, 2),
                                                          ("Zerkalo_70x200", KOM, 0)])))
    assert apply_legacy(db, recount, actor=admin) == (0, 6)
    db.commit()
    assert legacy_counts(db) == {(a.id, site.id): 2}
    assert len(_labeled(db, site, a)) == 2


def _ozon_plan(db: Session, n: int) -> None:
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": f"OZ-{i}", "article": "Zerkalo_60x160", "production": KOM, "codes": [f"%OZ{i}"]}
        for i in range(1, n + 1)
    ])
    db.commit()


def _labeled(db: Session, site, article) -> list[Unit]:  # type: ignore[no-untyped-def]
    return list(db.scalars(select(Unit).where(Unit.article_id == article.id, Unit.unlabeled.is_(False),
                                              Unit.status == IN_STOCK).order_by(Unit.id)).all())


def test_ozon_label_alone_ships_old_then_asks_for_ours(db: Session) -> None:
    site, admin, loader, a, _ = _world(db)  # 2 новых с этикетками уже на складе
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 1)]), actor=admin)  # 1 старое
    _ozon_plan(db, 3)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    # Старое есть — одной наклейки Озона хватает, хотя новые тоже есть
    done = scan_loading(db, code="%OZ1", pallet=pallet, actor=loader)
    db.commit()
    assert done.ok and not done.await_unit and "Старое" in done.message
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and item.state == SHIP_LOADED
    shipped = db.get(Unit, item.unit_id)
    assert shipped is not None and shipped.unlabeled and shipped.status == SHIPPED
    # Старых больше нет — склад просит нашу этикетку
    need = scan_loading(db, code="%OZ2", pallet=pallet, actor=loader)
    assert need.ok and need.await_unit
    new = _labeled(db, site, a)[0]
    second = scan_loading(db, code=new.number, pallet=pallet, actor=loader, ship_code="%OZ2")
    db.commit()
    assert second.ok and db.get(Unit, new.id).status == SHIPPED  # type: ignore[union-attr]


def test_own_label_after_ozon_swaps_old_for_new(db: Session) -> None:
    site, admin, loader, a, _ = _world(db)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 2)]), actor=admin)  # 2 старых
    _ozon_plan(db, 1)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert scan_loading(db, code="%OZ1", pallet=pallet, actor=loader).ok  # склад списал старое
    db.commit()
    new = _labeled(db, site, a)[0]
    swap = scan_loading(db, code=new.number, pallet=pallet, actor=loader)  # а в руках было новое
    db.commit()
    assert swap.ok and "вместо старого" in swap.message
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and item.unit_id == new.id and item.state == SHIP_LOADED
    assert legacy_counts(db) == {(a.id, site.id): 2}  # старое вернулось
    assert db.scalar(select(func.count()).select_from(Unit).where(Unit.status == SHIPPED)) == 1


def test_swap_refuses_other_article(db: Session) -> None:
    site, admin, loader, a, b = _world(db)
    worker = make_user(db, name="Сборщик Б", role=ROLE_ASSEMBLER, home_site_id=site.id)
    other = create_printed_units(db, article_id=b.id, qty=1, site_id=site.id, assembler_id=worker.id,
                                 actor_id=worker.id)[0]
    db.commit()
    accept_unit(db, code=other.number, actor=worker)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 3)]), actor=admin)
    _ozon_plan(db, 1)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert scan_loading(db, code="%OZ1", pallet=pallet, actor=loader).ok
    db.commit()
    # Наша этикетка другого артикула — не замена, а начало следующего зеркала
    result = scan_loading(db, code=other.number, pallet=pallet, actor=loader)
    assert result.ok and result.await_mp
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and db.get(Unit, item.unit_id).unlabeled  # type: ignore[union-attr]


def test_lead_adds_found_old(client: TestClient, db: Session) -> None:
    site, admin, loader, a, _ = _world(db)
    lead = make_user(db, name="Начальник Остатков", role=ROLE_SHIFT_LEAD, home_site_id=site.id)
    _ozon_plan(db, 1)
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    assert scan_loading(db, code="%OZ1", pallet=pallet, actor=loader).await_unit  # старых нет
    login(client, lead)
    token = get_csrf(client, "/shipments/batches")
    assert "Нашлось старое зеркало" in client.get("/shipments/batches").text
    form = {"csrf_token": token, "article": "zerkalo_60х160", "site_id": str(site.id), "qty": "1"}
    done = client.post("/shipments/found-old", data=form)
    assert "Zerkalo_60x160 +1" in done.text
    bad = client.post("/shipments/found-old", data={**form, "qty": "50"})
    assert "Старый остаток" in bad.text
    db.expire_all()
    assert scan_loading(db, code="%OZ1", pallet=pallet, actor=loader).ok  # теперь списалось старое


def test_channel_without_batch_ships_old_by_article(db: Session) -> None:
    site, admin, loader, a, _ = _world(db)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 2)]), actor=admin)  # 2 старых
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="СДЭК")
    db.commit()
    result = scan_loading(db, code="СТАРОЕ", pallet=pallet, actor=loader, legacy_article_id=a.id)
    db.commit()
    assert result.ok
    assert legacy_counts(db) == {(a.id, site.id): 1}


def test_inventory_does_not_write_off_unlabeled(db: Session) -> None:
    site, admin, _, a, _ = _world(db)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 5)]), actor=admin)
    inventory = start_inventory(db, site_id=site.id, actor=admin)
    db.commit()
    diff = inventory_diff(db, inventory)
    assert len(diff.missing) == 2  # только 2 с этикетками, 3 старых сканом не ищем
    assert all(not u.unlabeled for u in diff.missing)


def test_admin_page_preview_and_apply(client: TestClient, db: Session) -> None:
    site, admin, _, a, _ = _world(db)
    login(client, admin)
    token = get_csrf(client, "/stock-entry/legacy")
    page = client.post("/stock-entry/legacy/preview", data={"csrf_token": token, "site_id": "0"},
                       files={"file": ("ost.xlsx", _xlsx([("Zerkalo_60x160", KOM, 7)]))}).text
    assert "добавить 7, списать 0" in page
    assert legacy_counts(db) == {}  # проверка ничего не записывает
    raw = page.split('name="payload" value="')[1].split('"')[0]
    payload = raw.replace("&#34;", '"').replace("&quot;", '"')
    form = {"csrf_token": token, "site_id": "0", "payload": payload}
    done = client.post("/stock-entry/legacy/apply", data=form)
    assert "добавлено 7, списано 0" in done.text
    db.expire_all()
    assert legacy_counts(db) == {(a.id, site.id): 7}
    assert "7 старых зеркал без этикеток" in client.get("/stock").text
