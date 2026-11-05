"""Метка живых таблиц: меняется, когда на складе что-то случилось, — экраны сразу обновляются."""

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.shipping import set_manual
from app.statuses import ROLE_SHIFT_LEAD
from tests.helpers import login, make_user


def test_stamp_changes_after_warehouse_event(client: TestClient, db: Session) -> None:
    """Пока ничего не происходит, метка та же; после изменения — новая."""
    lead = make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD, pin="1234")
    login(client, lead)
    first = client.get("/live/stamp")
    assert first.status_code == 200
    assert client.get("/live/stamp").text == first.text
    set_manual(db, channel="Ozon", actor=lead, on=True)
    db.commit()
    assert client.get("/live/stamp").text != first.text


def test_stamp_changes_on_plain_update(client: TestClient, db: Session) -> None:
    """Правка существующей строки (без новой записи) тоже сдвигает метку."""
    lead = make_user(db, name="Начальник", role=ROLE_SHIFT_LEAD, pin="1234")
    login(client, lead)
    first = client.get("/live/stamp").text
    db.execute(text("UPDATE users SET name = name WHERE id = :id"), {"id": lead.id})
    db.commit()
    assert client.get("/live/stamp").text == first  # сотрудники не влияют на таблицы склада
    db.execute(text("UPDATE ship_items SET id = id"))
    db.commit()
    assert client.get("/live/stamp").text != first
