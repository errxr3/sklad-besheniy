"""Ввод остатков: этикетки сразу на склад, лишние убираются сканом (2026-10-21)."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Unit
from app.seed import INVENTORY_SERVICE_NAME
from app.services.units import accept_unit, create_printed_units, stock_count
from app.statuses import IN_STOCK, MV_INVENTORY_FIX, ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_STOREKEEPER, WRITTEN_OFF
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _scan(client: TestClient, token: str, code: str) -> dict:
    return client.post(
        "/stock-entry/remove/scan",
        json={"code": code, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()


def test_entry_prints_into_stock_and_extra_removed(client: TestClient, db: Session) -> None:
    site = site_by_name(db, "Учительская")
    admin = make_user(db, name="Админ ввода", role=ROLE_ADMIN)
    keeper = make_user(db, name="Кладовщик ввода", role=ROLE_STOREKEEPER, home_site_id=site.id)
    article = make_article(db, name="Ambient 90 krug multi")
    login(client, admin)  # ввод остатков — только у администратора (2026-11-06)
    token = get_csrf(client, "/stock-entry")

    pdf = client.post(
        "/stock-entry/print",
        data={"csrf_token": token, "article_id": str(article.id), "qty": "5", "site_id": str(site.id)},
    )
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    units = db.scalars(select(Unit).order_by(Unit.id)).all()
    assert len(units) == 5
    assert all(u.status == IN_STOCK and u.site_id == site.id for u in units)
    assert {u.assembler.name for u in units if u.assembler} == {INVENTORY_SERVICE_NAME}
    assert stock_count(db, article.id, site.id) == 5

    # По факту 2 — три лишние этикетки приносят кладовщице, она их сканирует и убирает
    client.cookies.clear()
    login(client, keeper)
    token = get_csrf(client, "/stock-entry/remove")
    for unit in units[2:]:
        assert _scan(client, token, unit.number)["ok"] is True
    again = _scan(client, token, units[4].number)
    assert again["ok"] is False
    db.expire_all()
    assert stock_count(db, article.id, site.id) == 2
    removed = db.scalars(select(Movement).where(Movement.type == MV_INVENTORY_FIX)).all()
    assert len(removed) == 3
    assert {db.get(Unit, u.id).status for u in units[2:]} == {WRITTEN_OFF}  # type: ignore[union-attr]


def test_real_mirror_cannot_be_removed_as_extra(client: TestClient, db: Session) -> None:
    """Убрать можно только этикетку ввода остатков — настоящее зеркало так не спишешь."""
    site = site_by_name(db)
    worker = make_user(db, name="Сборщик", role=ROLE_ASSEMBLER)
    article = make_article(db)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site.id, assembler_id=worker.id, actor_id=worker.id
    )[0]
    db.commit()
    accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    keeper = make_user(db, name="Кладовщик", role=ROLE_STOREKEEPER, home_site_id=site.id)
    login(client, keeper)
    token = get_csrf(client, "/stock-entry/remove")
    result = _scan(client, token, unit.number)
    assert result["ok"] is False and "так убрать нельзя" in result["message"]


def test_storekeeper_only_removes_extra(client: TestClient, db: Session) -> None:
    """У кладовщицы нет ввода остатков, только «Лишние этикетки» (решение владельца 2026-11-06)."""
    own = site_by_name(db, "Учительская")
    keeper = make_user(db, name="Кладовщик Учит", role=ROLE_STOREKEEPER, home_site_id=own.id)
    article = make_article(db)
    login(client, keeper)
    assert client.get("/stock-entry").status_code == 403
    token = get_csrf(client, "/stock-entry/remove")
    response = client.post(
        "/stock-entry/print",
        data={"csrf_token": token, "article_id": str(article.id), "qty": "2", "site_id": str(own.id)},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert db.scalars(select(Unit)).all() == []
    menu = client.get("/labels").text
    assert 'href="/stock-entry"' not in menu and 'href="/stock-entry/remove"' in menu
