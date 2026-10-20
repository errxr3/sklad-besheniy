"""Роль «Возвраты» и перемещение грузчиком (решение владельца 2026-10-20)."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, Unit, User
from app.seed import RETURNS_SERVICE_NAME
from app.services.shipping import open_new_pallet, scan_loading
from app.services.units import accept_unit, create_printed_units
from app.statuses import (
    IN_STOCK,
    IN_TRANSIT,
    MV_ACCEPT,
    ROLE_ASSEMBLER,
    ROLE_LOADER,
    ROLE_RETURNS,
)
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def _post_scan(client: TestClient, url: str, token: str, **payload: object) -> dict:
    return client.post(
        url,
        json={**payload, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()


def _stocked(db: Session, site_name: str, qty: int = 1) -> list[Unit]:
    assembler = make_user(db, name=f"Сборщик {site_name}", role=ROLE_ASSEMBLER)
    article = make_article(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=qty,
        site_id=site_by_name(db, site_name).id,
        assembler_id=assembler.id,
        actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()
    return units


def test_returns_user_sees_only_returns(client: TestClient, db: Session) -> None:
    worker = make_user(db, name="Анна Возвраты", role=ROLE_RETURNS)
    login(client, worker)
    response = client.get("/")
    assert str(response.url).endswith("/returns")
    assert "Принять возврат" in response.text
    assert 'href="/returns/print"' in response.text
    for path in ("/board", "/labels", "/stock", "/loading", "/tasks"):
        assert client.get(path).status_code == 403, path


def test_print_unmarked_return_goes_to_kominterna_stock(client: TestClient, db: Session) -> None:
    """Возврат без нашей этикетки: «Печать ШК» сразу ставит зеркало на склад Коминтерна."""
    worker = make_user(db, name="Анна Возвраты", role=ROLE_RETURNS)
    article = make_article(db, name="Zerkalo_50x130")
    login(client, worker)
    token = get_csrf(client, "/returns/print")
    pdf = client.post(
        "/returns/print", data={"csrf_token": token, "article_id": str(article.id), "qty": "2"}
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"

    units = db.scalars(select(Unit).order_by(Unit.id)).all()
    service = db.scalar(select(User).where(User.name == RETURNS_SERVICE_NAME))
    assert service is not None and service.is_service
    kominterna = site_by_name(db, "Коминтерна")
    assert len(units) == 2
    for unit in units:
        assert unit.status == IN_STOCK
        assert unit.site_id == kominterna.id
        assert unit.assembler_id == service.id
    accept = db.scalars(select(Movement).where(Movement.type == MV_ACCEPT)).all()
    assert len(accept) == 2 and all("возврат без маркировки" in (m.note or "") for m in accept)


def test_assembler_label_is_not_accepted_on_returns(client: TestClient, db: Session) -> None:
    """Обычная этикетка сборщика («Напечатано») не принимается как возврат."""
    assembler = make_user(db, name="Сборщик", role=ROLE_ASSEMBLER)
    article = make_article(db)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site_by_name(db).id,
        assembler_id=assembler.id, actor_id=assembler.id,
    )[0]
    db.commit()
    worker = make_user(db, name="Анна Возвраты", role=ROLE_RETURNS)
    login(client, worker)
    token = get_csrf(client, "/returns")
    assert _post_scan(client, "/returns/scan", token, code=unit.number)["ok"] is False


def test_return_scan_always_books_to_kominterna(client: TestClient, db: Session) -> None:
    """Отгруженное с Учительской зеркало при возврате записывается на Коминтерна."""
    unit = _stocked(db, "Учительская")[0]
    loader = make_user(db, name="Грузчик Учительской", role=ROLE_LOADER)
    pallet = open_new_pallet(db, user=loader, site=site_by_name(db, "Учительская"), channel="WB")
    assert scan_loading(db, code=unit.number, pallet=pallet, actor=loader).ok
    db.commit()

    worker = make_user(db, name="Анна Возвраты", role=ROLE_RETURNS)
    login(client, worker)
    token = get_csrf(client, "/returns")
    result = _post_scan(client, "/returns/scan", token, code=unit.number)
    assert result["ok"] is True
    db.expire_all()
    fresh = db.get(Unit, unit.id)
    assert fresh is not None
    assert fresh.status == IN_STOCK
    assert fresh.site_id == site_by_name(db, "Коминтерна").id


def test_loader_moves_mirrors_between_sites(client: TestClient, db: Session) -> None:
    """Грузчик Коминтерна отправляет, грузчик Учительской принимает — зеркало на Учительской."""
    unit = _stocked(db, "Коминтерна")[0]
    kominterna = site_by_name(db, "Коминтерна")
    uchitelskaya = site_by_name(db, "Учительская")
    sender = make_user(db, name="Грузчик Коминтерна", role=ROLE_LOADER)
    receiver = make_user(db, name="Грузчик Учительской", role=ROLE_LOADER)
    open_shift(db, sender, kominterna)
    open_shift(db, receiver, uchitelskaya)

    with TestClient(client.app) as sender_client:
        login(sender_client, sender)
        token = get_csrf(sender_client, "/transfer")
        sent = _post_scan(
            sender_client, "/transfer/scan", token, code=unit.number, to_site_id=uchitelskaya.id
        )
        assert sent["ok"] is True
    db.expire_all()
    assert db.get(Unit, unit.id).status == IN_TRANSIT  # type: ignore[union-attr]

    login(client, receiver)
    token = get_csrf(client, "/transfer/in")
    received = _post_scan(client, "/transfer/in/scan", token, code=unit.number)
    assert received["ok"] is True
    db.expire_all()
    fresh = db.get(Unit, unit.id)
    assert fresh is not None
    assert fresh.status == IN_STOCK and fresh.site_id == uchitelskaya.id


def test_loader_without_shift_cannot_transfer(client: TestClient, db: Session) -> None:
    unit = _stocked(db, "Коминтерна")[0]
    loader = make_user(db, name="Грузчик без смены", role=ROLE_LOADER)
    login(client, loader)
    token = get_csrf(client, "/transfer")
    response = client.post(
        "/transfer/scan",
        json={"code": unit.number, "to_site_id": 1, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
