"""API для BLAGO OTGRUZKI — доступ по ключу, заказы дня, партии."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.apikeys import create_key, deactivate_key
from tests.helpers import make_article


def _key(db: Session, name: str = "BLAGO OTGRUZKI") -> str:
    """Создать ключ и вернуть его открытое значение."""
    _, plain = create_key(db, name=name, created_by=None)
    db.commit()
    return plain


def test_without_key_unauthorized(client: TestClient, db: Session) -> None:
    """Без ключа API отвечает 401."""
    response = client.get("/api/v1/ping")
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["error"]


def test_wrong_key_unauthorized(client: TestClient, db: Session) -> None:
    """Чужой ключ не принимается."""
    _key(db)
    response = client.get("/api/v1/ping", headers={"X-API-Key": "sklad_wrong_key_value"})
    assert response.status_code == 401


def test_deactivated_key_refused(client: TestClient, db: Session) -> None:
    """Отключённый ключ перестаёт работать."""
    record, plain = create_key(db, name="Временный", created_by=None)
    db.commit()
    assert client.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 200

    deactivate_key(db, record.id)
    db.commit()
    assert client.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 401


def test_old_endpoints_removed(client: TestClient, db: Session) -> None:
    """Старые выдача остатков и приём отправлений убраны: BLAGO OTGRUZKI шлёт заказы дня и партии."""
    plain = _key(db)
    assert client.get("/api/v1/stock", headers={"X-API-Key": plain}).status_code == 404
    assert client.post("/api/v1/shipments", json=[], headers={"X-API-Key": plain}).status_code == 404


def test_api_does_not_need_csrf(client: TestClient, db: Session) -> None:
    """Внешняя программа шлёт POST без CSRF-токена — это допустимо для API с ключом."""
    plain = _key(db)
    response = client.post("/api/v1/demand", json={"lines": []}, headers={"X-API-Key": plain})
    assert response.status_code == 200


def test_demand_and_batch_api(client: TestClient, db: Session) -> None:
    """BLAGO OTGRUZKI: заказы дня → решения; партия этикеток → строки для погрузки."""
    plain = _key(db)
    make_article(db, name="Zerkalo_50x130")
    headers = {"X-API-Key": plain}
    line = {"market": "ozon", "channel": "Ozon", "truck": "Озон1", "posting": "API-1",
            "article": "Zerkalo_50x130", "qty": 1, "production": "Коминтерна"}
    demand = client.post("/api/v1/demand", json={"lines": [line]}, headers=headers).json()
    assert demand["ok"] and demand["postings"]["API-1"]["decision"] == "no"  # на складе пусто
    batch = client.post("/api/v1/batches", headers=headers, json={
        "market": "ozon", "channel": "Ozon", "truck": "Озон2", "source": "тест",
        "items": [{"posting": "API-1", "article": "Zerkalo_50x130", "production": "Коминтерна",
                   "codes": ["%1234"]}],
    }).json()
    assert batch == {"ok": True, "batch": "Озон 2", "added": 1, "updated": 0, "kept": [], "unknown": []}
    wrong = {"market": "ozon", "channel": "Луна", "items": []}
    bad = client.post("/api/v1/batches", headers=headers, json=wrong)
    assert bad.status_code == 400 and "Неизвестный канал" in bad.json()["error"]
    assert client.post("/api/v1/demand", json={"lines": [line]}).status_code == 401
