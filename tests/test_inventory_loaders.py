"""Инвентаризацию сканируют грузчики в день, который открыл администратор (2026-10-30)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import InventoryScan, User
from app.services.units import accept_unit, create_printed_units
from app.services.warehouse import start_inventory
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER, ROLE_LOADER
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _world(db: Session):  # type: ignore[no-untyped-def]
    site = site_by_name(db)
    admin = make_user(db, name="Админ Инвент", role=ROLE_ADMIN)
    loader = make_user(db, name="Грузчик Инвент", role=ROLE_LOADER, home_site_id=site.id)
    other = make_user(db, name="Грузчик Другой", role=ROLE_LOADER, home_site_id=site.id)
    worker = make_user(db, name="Сборщик Инвент", role=ROLE_ASSEMBLER)
    article = make_article(db, name="Zerkalo_50x130")
    unit = create_printed_units(db, article_id=article.id, qty=1, site_id=site.id, assembler_id=worker.id,
                                actor_id=worker.id)[0]
    db.commit()
    accept_unit(db, code=unit.number, actor=worker)
    inventory = start_inventory(db, site_id=site.id, actor=admin)
    db.commit()
    return admin, loader, other, unit, inventory


def test_loader_without_access_sees_nothing(client: TestClient, db: Session) -> None:
    _, loader, _, _, inventory = _world(db)
    login(client, loader)
    assert "/inventory" not in client.get("/").text
    assert client.get("/inventory").status_code == 403
    assert client.get(f"/inventory/{inventory.id}").status_code == 403


def test_admin_opens_day_loader_scans_but_cannot_finish(client: TestClient, db: Session) -> None:
    admin, loader, other, unit, inventory = _world(db)
    login(client, admin)
    page = client.get("/inventory").text
    assert "Кто сканирует сегодня" in page and "Грузчик Инвент" in page
    token = get_csrf(client, "/inventory")
    response = client.post("/inventory/access", data={"csrf_token": token, "user_id": [str(loader.id)]})
    assert "открыта на сегодня: 1" in response.text
    db.expire_all()
    assert db.get(User, loader.id).inventory_day == today_msk()  # type: ignore[union-attr]
    assert db.get(User, other.id).inventory_day is None  # type: ignore[union-attr]

    client.cookies.clear()
    login(client, loader)
    assert 'href="/inventory"' in client.get("/").text
    home = client.get("/inventory").text
    assert f"/inventory/{inventory.id}" in home and "Начать инвентаризацию" not in home
    screen = client.get(f"/inventory/{inventory.id}").text
    assert "Учтено зеркал" in screen and "Закончить пересчёт" not in screen
    token = get_csrf(client, f"/inventory/{inventory.id}")
    result = client.post(f"/inventory/{inventory.id}/scan", json={"code": unit.number},
                         headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"}).json()
    assert result["ok"]
    assert db.query(InventoryScan).count() == 1
    assert client.post(f"/inventory/{inventory.id}/finish", data={"csrf_token": token}).status_code == 403

    # На следующий день доступ закрыт сам
    db.get(User, loader.id).inventory_day = today_msk() - timedelta(days=1)  # type: ignore[union-attr]
    db.commit()
    assert client.get("/inventory").status_code == 403
