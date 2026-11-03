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
from app.statuses import IN_STOCK, ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER, SHIP_LOADED, SHIPPED
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


def test_excel_subtracts_labeled_and_creates_unlabeled(db: Session) -> None:
    site, admin, _, a, b = _world(db)
    lines = read_legacy_xlsx(_xlsx([("Zerkalo_60x160", KOM, 5), ("Zerkalo_70х200", KOM, 3),
                                    ("Нет_такого", KOM, 4)]))
    rows = {r.name: r for r in preview_legacy(db, lines)}
    assert rows["Zerkalo_60x160"].labeled == 2 and rows["Zerkalo_60x160"].to_create == 3  # 5 − 2 с этикетками
    assert rows["Zerkalo_70x200"].to_create == 3  # кириллическая «х» в файле — тот же артикул
    assert rows["Нет_такого"].problem and rows["Нет_такого"].to_create == 0
    assert apply_legacy(db, list(rows.values()), actor=admin) == 6
    db.commit()
    assert legacy_counts(db) == {(a.id, site.id): 3, (b.id, site.id): 3}
    # Загрузили тот же файл ещё раз — ничего не задвоилось
    again = preview_legacy(db, lines)
    assert sum(r.to_create for r in again) == 0


def test_loader_ships_old_mirror_by_ozon_label(db: Session) -> None:
    site, admin, loader, a, _ = _world(db)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 3)]), actor=admin)  # 1 старое
    receive_batch(db, day=today_msk(), market="ozon", channel="Ozon", truck="Озон1", items=[
        {"posting": "OZ-1", "article": "Zerkalo_60x160", "production": KOM, "codes": ["%OZ1"]},
        {"posting": "OZ-2", "article": "Zerkalo_60x160", "production": KOM, "codes": ["%OZ2"]},
    ])
    db.commit()
    pallet = join_channel(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    db.commit()
    need = scan_loading(db, code="%OZ1", pallet=pallet, actor=loader)
    assert need.ok and need.await_unit
    done = scan_loading(db, code="%OZ1", pallet=pallet, actor=loader, ship_code="%OZ1", legacy=True)
    db.commit()
    assert done.ok and "без этикетки" in done.detail
    item = db.scalar(select(ShipItem).where(ShipItem.posting == "OZ-1"))
    assert item is not None and item.state == SHIP_LOADED
    shipped = db.get(Unit, item.unit_id)
    assert shipped is not None and shipped.unlabeled and shipped.status == SHIPPED
    # Старых больше нет — склад не даст «отгрузить» то, чего не вносили
    none_left = scan_loading(db, code="%OZ2", pallet=pallet, actor=loader, ship_code="%OZ2", legacy=True)
    assert not none_left.ok and "не осталось" in none_left.message
    assert db.scalar(select(func.count()).select_from(Unit).where(Unit.status == IN_STOCK)) == 2


def test_channel_without_batch_ships_old_by_article(db: Session) -> None:
    site, admin, loader, a, _ = _world(db)
    apply_legacy(db, preview_legacy(db, [("Zerkalo_60x160", KOM, 4)]), actor=admin)  # 2 старых
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
    assert "будет заведено 5" in page
    assert legacy_counts(db) == {}  # проверка ничего не записывает
    raw = page.split('name="payload" value="')[1].split('"')[0]
    payload = raw.replace("&#34;", '"').replace("&quot;", '"')
    form = {"csrf_token": token, "site_id": "0", "payload": payload}
    done = client.post("/stock-entry/legacy/apply", data=form)
    assert "Заведено старых зеркал без этикеток: 5" in done.text
    db.expire_all()
    assert legacy_counts(db) == {(a.id, site.id): 5}
    assert "5 старых зеркал без этикеток" in client.get("/stock").text
