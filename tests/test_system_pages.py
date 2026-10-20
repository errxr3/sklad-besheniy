"""Этап 6: ключи API в настройках, страница «Состояние», документация API."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiKey
from app.statuses import ROLE_ADMIN, ROLE_SHIFT_LEAD
from tests.helpers import get_csrf, login, make_user


def test_admin_creates_key_and_it_works(client: TestClient, db: Session) -> None:
    """Ключ показывается один раз, в базе — только хеш, и ключ пускает в API."""
    admin = make_user(db, name="Админ ключей", role=ROLE_ADMIN)
    login(client, admin)
    token = get_csrf(client, "/admin/api-keys")
    page = client.post("/admin/api-keys/create", data={"csrf_token": token, "name": "BLAGO OTGRUZKI"})
    assert page.status_code == 200
    assert "больше он показан не будет" in page.text

    import re

    match = re.search(r'value="(sklad_[^"]+)"', page.text)
    assert match
    plain = match.group(1)

    record = db.scalar(select(ApiKey).where(ApiKey.name == "BLAGO OTGRUZKI"))
    assert record is not None
    assert plain not in record.key_hash

    with TestClient(client.app) as outside:
        assert outside.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 200

    client.post("/admin/api-keys/deactivate", data={"csrf_token": token, "key_id": str(record.id)})
    with TestClient(client.app) as outside:
        assert outside.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 401


def test_status_page(client: TestClient, db: Session) -> None:
    """Страница «Состояние» показывает базу, гугл-таблицу и бэкапы."""
    admin = make_user(db, name="Админ состояния", role=ROLE_ADMIN)
    login(client, admin)
    response = client.get("/status")
    assert response.status_code == 200
    for text in ("База данных", "Гугл-таблица", "Ночной бэкап", "На складе всего зеркал"):
        assert text in response.text


def test_docs_only_for_admin(client: TestClient, db: Session) -> None:
    """Документация API открыта только администратору."""
    lead = make_user(db, name="Начальник без доков", role=ROLE_SHIFT_LEAD)
    login(client, lead)
    assert client.get("/docs").status_code == 403
    assert client.get("/openapi.json").status_code == 403

    admin = make_user(db, name="Админ доков", role=ROLE_ADMIN)
    with TestClient(client.app) as admin_client:
        login(admin_client, admin)
        assert "swagger-ui-bundle.js" in admin_client.get("/docs").text
        schema = admin_client.get("/openapi.json").json()
        assert "/api/v1/stock" in schema["paths"]
