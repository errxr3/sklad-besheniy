"""SPEC §10: API для BLAGO OTGRUZKI — доступ по ключу, остатки, отправления."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MpShipment
from app.services.apikeys import create_key, deactivate_key
from app.services.units import accept_unit, create_printed_units
from app.statuses import ROLE_ASSEMBLER
from tests.helpers import make_article, make_user, site_by_name


def _key(db: Session, name: str = "BLAGO OTGRUZKI") -> str:
    """Создать ключ и вернуть его открытое значение."""
    _, plain = create_key(db, name=name, created_by=None)
    db.commit()
    return plain


def test_without_key_unauthorized(client: TestClient, db: Session) -> None:
    """Без ключа API отвечает 401."""
    response = client.get("/api/v1/stock")
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["error"]


def test_wrong_key_unauthorized(client: TestClient, db: Session) -> None:
    """Чужой ключ не принимается."""
    _key(db)
    response = client.get("/api/v1/stock", headers={"X-API-Key": "sklad_wrong_key_value"})
    assert response.status_code == 401


def test_deactivated_key_refused(client: TestClient, db: Session) -> None:
    """Отключённый ключ перестаёт работать."""
    record, plain = create_key(db, name="Временный", created_by=None)
    db.commit()
    assert client.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 200

    deactivate_key(db, record.id)
    db.commit()
    assert client.get("/api/v1/ping", headers={"X-API-Key": plain}).status_code == 401


def test_stock_endpoint(client: TestClient, db: Session) -> None:
    """GET /api/v1/stock отдаёт остатки по артикулам и площадкам."""
    plain = _key(db)
    assembler = make_user(db, name="Сборщик API", role=ROLE_ASSEMBLER)
    article = make_article(db)
    site = site_by_name(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=2,
        site_id=site.id,
        assembler_id=assembler.id,
        actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()

    response = client.get("/api/v1/stock", headers={"X-API-Key": plain})
    assert response.status_code == 200
    data = response.json()
    assert {"article": article.name, "site": site.name, "qty": 2} in data


def test_shipments_upsert_is_idempotent(client: TestClient, db: Session) -> None:
    """POST /api/v1/shipments принимает список и не плодит дубли по ШК."""
    plain = _key(db)
    payload = [
        {"mp_barcode": "4603746900001", "article": "Zerkalo_100х200", "date": "2026-10-14"},
        {"mp_barcode": "4603746900002", "article": "Alum rama 200x100 black"},
    ]
    first = client.post("/api/v1/shipments", json=payload, headers={"X-API-Key": plain})
    assert first.status_code == 200
    assert first.json()["added"] == 2

    second = client.post("/api/v1/shipments", json=payload, headers={"X-API-Key": plain})
    assert second.json()["added"] == 0
    assert second.json()["updated"] == 2

    rows = db.scalars(select(MpShipment)).all()
    assert len(rows) == 2
    stored = db.scalar(select(MpShipment).where(MpShipment.mp_barcode == "4603746900001"))
    assert stored is not None
    # Артикул хранится и в исходном написании, и нормализованным (кириллическая «х»)
    assert stored.article_name == "Zerkalo_100х200"
    assert stored.article_name_norm == "zerkalo_100x200"


def test_shipments_rejects_non_list(client: TestClient, db: Session) -> None:
    """Неверное тело запроса — понятная ошибка, а не 500."""
    plain = _key(db)
    response = client.post("/api/v1/shipments", json={"mp_barcode": "1"}, headers={"X-API-Key": plain})
    assert response.status_code == 400
    assert "список" in response.json()["error"]


def test_api_does_not_need_csrf(client: TestClient, db: Session) -> None:
    """Внешняя программа шлёт POST без CSRF-токена — это допустимо для API с ключом."""
    plain = _key(db)
    response = client.post(
        "/api/v1/shipments",
        json=[{"mp_barcode": "4603746900003", "article": "Zerkalo_60x160"}],
        headers={"X-API-Key": plain},
    )
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
