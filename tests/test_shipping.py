"""SPEC §12 п. 6 и §6.4: погрузка на паллету, ШК маркетплейса, отмена отгрузки."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Movement, Pallet, Unit
from app.services.shipping import (
    ShippingError,
    cancel_ship,
    close_pallet,
    next_pallet_number,
    open_new_pallet,
    pallet_loaders,
    pallet_summary,
    scan_loading,
)
from app.services.units import accept_unit, create_printed_units, stock_count
from app.statuses import IN_STOCK, MV_SHIP, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD, SHIPPED
from app.timeutil import now_utc, today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def _stocked_units(db: Session, qty: int = 2, site_name: str = "Коминтерна") -> list[Unit]:
    """Зеркала на складе выбранной площадки."""
    worker = db.scalar(select(Unit).limit(0))  # заглушка не нужна, оставляем явный код ниже
    assembler = make_user(db, name=f"Сборщик {qty}{site_name}", role=ROLE_ASSEMBLER)
    site = site_by_name(db, site_name)
    article = make_article(db)
    units = create_printed_units(
        db,
        article_id=article.id,
        qty=qty,
        site_id=site.id,
        assembler_id=assembler.id,
        actor_id=assembler.id,
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()
    assert worker is None
    return units


def _loader_with_pallet(db: Session, channel: str = "Ozon", name: str = "Грузчик") -> tuple:
    """Грузчик с открытой паллетой."""
    loader = make_user(db, name=name, role=ROLE_LOADER)
    site = site_by_name(db)
    pallet = open_new_pallet(db, user=loader, site=site, channel=channel)
    db.commit()
    return loader, pallet


def test_pallet_number_format(db: Session) -> None:
    """Номер паллеты — P + дата + порядковый за день."""
    number = next_pallet_number(db)
    assert number.startswith(f"P{today_msk():%Y%m%d}-")
    loader, pallet = _loader_with_pallet(db)
    assert pallet.number == number
    close_pallet(db, pallet_id=pallet.id, user=loader, note="Озон1")
    db.commit()
    second = open_new_pallet(db, user=loader, site=site_by_name(db), channel="WB")
    db.commit()
    assert second.number.endswith("-02")


def test_scan_ships_unit(db: Session) -> None:
    """Скан зеркала на погрузке отгружает его и уменьшает остаток."""
    units = _stocked_units(db, 2)
    loader, pallet = _loader_with_pallet(db)
    article_id, site_id = units[0].article_id, units[0].site_id
    assert stock_count(db, article_id, site_id) == 2

    result = scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()
    db.refresh(units[0])

    assert result.ok, result.message
    assert units[0].status == SHIPPED
    assert units[0].pallet_id == pallet.id
    assert units[0].channel == "Ozon"
    assert result.pallet_count == 1
    assert stock_count(db, article_id, site_id) == 1


def test_second_scan_of_shipped_unit_refused(db: Session) -> None:
    """Повторный скан отгруженного зеркала — отказ с паллетой и временем."""
    units = _stocked_units(db, 1)
    loader, pallet = _loader_with_pallet(db)
    scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()

    again = scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()
    assert not again.ok
    assert "Уже отгружено" in again.message
    assert pallet.number in again.detail


def test_unit_from_other_site_refused(db: Session) -> None:
    """Зеркало с другой площадки отгрузить нельзя."""
    other_units = _stocked_units(db, 1, site_name="Учительская")
    loader, pallet = _loader_with_pallet(db)
    result = scan_loading(db, code=other_units[0].number, pallet=pallet, actor=loader)
    assert not result.ok
    assert "Учительской" in result.message or "Учительская" in result.message


def test_printed_unit_cannot_be_shipped(db: Session) -> None:
    """Непринятое зеркало отгрузить нельзя."""
    assembler = make_user(db, name="Сборщик печати", role=ROLE_ASSEMBLER)
    site = site_by_name(db)
    article = make_article(db)
    unit = create_printed_units(
        db, article_id=article.id, qty=1, site_id=site.id, assembler_id=assembler.id, actor_id=assembler.id
    )[0]
    db.commit()
    loader, pallet = _loader_with_pallet(db)
    result = scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    assert not result.ok
    assert "Напечатано" in result.message


def test_marketplace_code_without_unit_refused(db: Session) -> None:
    """ШК маркетплейса без предыдущего скана этикетки — отказ (SPEC §6.4)."""
    loader, pallet = _loader_with_pallet(db)
    result = scan_loading(db, code="4603746901234", pallet=pallet, actor=loader)
    assert not result.ok
    assert "код маркетплейса" in result.message
    assert "этикетку BLAGO" in result.detail  # на канал без партии сканируют только BLAGO


def test_mp_barcode_unique_in_database(db: Session) -> None:
    """Уникальность ШК маркетплейса обеспечена индексом в базе."""
    units = _stocked_units(db, 2)
    units[0].mp_barcode = "4603746900777"
    db.commit()
    units[1].mp_barcode = "4603746900777"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_marketplace_code_without_batch_is_refused(db: Session) -> None:
    """На канал без партии из BLAGO OTGRUZKI код маркетплейса не нужен — экран так и говорит."""
    units = _stocked_units(db, 1)
    loader, pallet = _loader_with_pallet(db)
    result = scan_loading(
        db, code="4603746900555", pallet=pallet, actor=loader, unit_number=units[0].number
    )
    db.commit()
    db.refresh(units[0])
    assert not result.ok and result.message == "Это код маркетплейса"
    assert units[0].status == IN_STOCK


def test_close_pallet_and_summary(db: Session) -> None:
    """Закрытая паллета показывает итог по артикулам и не дополняется."""
    units = _stocked_units(db, 3)
    loader, pallet = _loader_with_pallet(db)
    for unit in units[:2]:
        scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    db.commit()

    summary = pallet_summary(db, pallet.id)
    assert summary == [(units[0].article.name, 2)]

    close_pallet(db, pallet_id=pallet.id, user=loader, note="Озон1")
    db.commit()
    with pytest.raises(ShippingError):
        close_pallet(db, pallet_id=pallet.id, user=loader, note="Озон1")


def test_loaders_share_channel_of_site(db: Session) -> None:
    """Ozon на площадке один: второй грузчик встаёт в тот же канал и грузит вместе (2026-10-21)."""
    units = _stocked_units(db, 2)
    first, pallet = _loader_with_pallet(db, name="Грузчик 1")
    second = make_user(db, name="Грузчик 2", role=ROLE_LOADER)
    joined = open_new_pallet(db, user=second, site=site_by_name(db), channel="Ozon")
    db.commit()
    assert joined.id == pallet.id
    assert scan_loading(db, code=units[0].number, pallet=pallet, actor=first).ok
    assert scan_loading(db, code=units[1].number, pallet=joined, actor=second).ok
    db.commit()
    assert pallet_loaders(db, pallet.id) == [("Грузчик 1", 1), ("Грузчик 2", 1)]
    # Закрыть может любой из них, канал закрывается для всех
    close_pallet(db, pallet_id=pallet.id, user=second, note="Озон2")
    db.commit()
    assert pallet_summary(db, pallet.id) == [(units[0].article.name, 2)]
    # Другой канал и другая площадка — отдельные каналы
    wb = open_new_pallet(db, user=first, site=site_by_name(db), channel="WB")
    other = open_new_pallet(db, user=second, site=site_by_name(db, "Учительская"), channel="WB")
    assert wb.id != other.id


def test_cancel_ship_returns_unit_to_stock(db: Session) -> None:
    """Отмена отгрузки — новое движение, а не удаление (SPEC §6.4)."""
    units = _stocked_units(db, 1)
    loader, pallet = _loader_with_pallet(db)
    lead = make_user(db, name="Начальник отмены", role=ROLE_SHIFT_LEAD)
    scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()

    result = cancel_ship(db, number=units[0].number, actor=lead)
    db.commit()
    db.refresh(units[0])

    assert result.ok
    assert units[0].status == IN_STOCK
    assert units[0].pallet_id is None
    movements = db.scalars(
        select(Movement).where(Movement.unit_id == units[0].id).order_by(Movement.id)
    ).all()
    assert [m.type for m in movements] == ["print", "accept", "ship", "ship_cancel"]


def test_cancel_ship_after_timeout_refused(db: Session) -> None:
    """Через отведённое время отмена отгрузки уже невозможна."""
    units = _stocked_units(db, 1)
    loader, pallet = _loader_with_pallet(db)
    lead = make_user(db, name="Начальник поздний", role=ROLE_SHIFT_LEAD)
    scan_loading(db, code=units[0].number, pallet=pallet, actor=loader)
    db.commit()

    from sqlalchemy import text

    db.execute(text("ALTER TABLE movements DISABLE TRIGGER USER"))
    db.execute(
        text("UPDATE movements SET created_at = :moment WHERE unit_id = :unit AND type = 'ship'"),
        {"moment": now_utc() - timedelta(minutes=30), "unit": units[0].id},
    )
    db.execute(text("ALTER TABLE movements ENABLE TRIGGER USER"))
    db.commit()

    result = cancel_ship(db, number=units[0].number, actor=lead)
    assert not result.ok
    assert "в течение" in result.message
    db.refresh(units[0])
    assert units[0].status == SHIPPED


def test_loading_through_http(client: TestClient, db: Session) -> None:
    """Полный путь грузчика: паллета, скан, закрытие."""
    units = _stocked_units(db, 2)
    loader = make_user(db, name="Грузчик веб 2", role=ROLE_LOADER, pin="1234")
    open_shift(db, loader)
    login(client, loader)

    token = get_csrf(client, "/loading")
    client.post("/loading/new-pallet", data={"csrf_token": token, "channel": "WB"})
    pallet = db.scalar(select(Pallet).where(Pallet.opened_by == loader.id))
    assert pallet is not None

    response = client.post(
        "/loading/scan",
        json={"code": units[0].number, "csrf_token": token, "pallet_id": pallet.id},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    data = response.json()
    assert data["ok"] is True
    assert data["pallet_count"] == 1

    closed = client.post(
        "/loading/close",
        data={"csrf_token": token, "pallet_id": str(pallet.id), "note": "машина 7"},
        follow_redirects=True,
    )
    assert closed.status_code == 200
    assert "Канал закрыт" in closed.text
    assert "машина 7" in closed.text
    db.expire_all()
    pallet_fresh = db.get(Pallet, pallet.id)
    assert pallet_fresh is not None and pallet_fresh.closed_at is not None


def test_loader_without_shift_cannot_scan(client: TestClient, db: Session) -> None:
    """Грузчик без смены не может открыть паллету и сканировать (SPEC §12 п. 11)."""
    loader = make_user(db, name="Грузчик без смены", role=ROLE_LOADER, pin="1234")
    login(client, loader)
    token = get_csrf(client, "/")
    response = client.post(
        "/loading/new-pallet", data={"csrf_token": token, "channel": "Ozon"}, follow_redirects=False
    )
    assert response.status_code == 403

    scan = client.post(
        "/loading/scan",
        json={"code": "Z0000001", "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert scan.status_code == 403
    assert "начните смену" in scan.json()["error"]


def test_ship_movements_count(db: Session) -> None:
    """Каждая отгрузка пишет ровно одно движение."""
    units = _stocked_units(db, 3)
    loader, pallet = _loader_with_pallet(db)
    for unit in units:
        scan_loading(db, code=unit.number, pallet=pallet, actor=loader)
    db.commit()
    count = db.scalar(
        select(func.count()).select_from(Movement).where(Movement.type == MV_SHIP)
    )
    assert count == 3


def test_ozon_trucks_are_separate_channels(db: Session) -> None:
    """Озон 1, Озон 2, Озон 3 — разные каналы: машина выбирается при открытии (2026-10-22)."""
    site = site_by_name(db)
    loader = make_user(db, name="Грузчик Ozon", role=ROLE_LOADER)
    with pytest.raises(ShippingError):
        open_new_pallet(db, user=loader, site=site, channel="Ozon", truck="Озон9")
    first = open_new_pallet(db, user=loader, site=site, channel="Ozon", truck="Озон1")
    second = open_new_pallet(db, user=loader, site=site, channel="Ozon", truck="Озон2")
    wb = open_new_pallet(db, user=loader, site=site, channel="WB")
    assert len({first.id, second.id, wb.id}) == 3
    assert (first.title, second.title, wb.title) == ("Озон 1", "Озон 2", "WB")
    # Повторное нажатие «Озон 2» — тот же канал
    assert open_new_pallet(db, user=loader, site=site, channel="Ozon", truck="Озон2").id == second.id
    closed = close_pallet(db, pallet_id=second.id, user=loader)
    assert closed.note == "Озон2" and closed.closed_at is not None
    db.commit()


def test_mirror_not_shipped_twice_across_channels(db: Session) -> None:
    """Зеркало, отсканированное в один канал, не пикается во второй."""
    units = _stocked_units(db, 1)
    site = site_by_name(db)
    loader = make_user(db, name="Грузчик двух каналов", role=ROLE_LOADER)
    ozon2 = open_new_pallet(db, user=loader, site=site, channel="Ozon", truck="Озон2")
    wb = open_new_pallet(db, user=loader, site=site, channel="WB")
    assert scan_loading(db, code=units[0].number, pallet=ozon2, actor=loader).ok
    again = scan_loading(db, code=units[0].number, pallet=wb, actor=loader)
    assert not again.ok and "Уже отгружено" in again.message


def test_all_channels_available(db: Session) -> None:
    """Каналы погрузки: Ozon, WB, СГТ, КГТ, СДЭК; описание у не-Ozon — по желанию."""
    site = site_by_name(db)
    loader = make_user(db, name="Грузчик каналов", role=ROLE_LOADER)
    for channel in ("WB", "СГТ", "КГТ", "СДЭК"):
        pallet = open_new_pallet(db, user=loader, site=site, channel=channel)
        closed = close_pallet(db, pallet_id=pallet.id, user=loader)
        assert closed.channel == channel and closed.note is None
    with pytest.raises(ShippingError):
        open_new_pallet(db, user=loader, site=site, channel="Почта")


def test_channel_of_other_site_refused_over_http(client: TestClient, db: Session) -> None:
    """Канал Коминтерна не принимает сканы грузчика на смене на Учительской."""
    units = _stocked_units(db, 1, site_name="Учительская")
    kom_loader = make_user(db, name="Грузчик Коминтерна", role=ROLE_LOADER)
    kom_pallet = open_new_pallet(db, user=kom_loader, site=site_by_name(db, "Коминтерна"), channel="Ozon")
    loader = make_user(db, name="Грузчик Учительская", role=ROLE_LOADER, pin="1234")
    db.commit()
    open_shift(db, loader, site_by_name(db, "Учительская"))
    login(client, loader)
    token = get_csrf(client, "/loading")
    headers = {"X-CSRF-Token": token, "X-Requested-With": "fetch"}
    refused = client.post(
        "/loading/scan", json={"code": units[0].number, "pallet_id": kom_pallet.id}, headers=headers
    ).json()
    assert refused["ok"] is False
    assert "Коминтерна" in refused["message"]

    # Нажал Ozon у себя — открылся свой канал Учительской, скан проходит
    client.post("/loading/new-pallet", data={"csrf_token": token, "channel": "Ozon"})
    own = db.scalar(
        select(Pallet).where(Pallet.site_id == site_by_name(db, "Учительская").id, Pallet.closed_at.is_(None))
    )
    assert own is not None and own.id != kom_pallet.id
    data = client.post(
        "/loading/scan", json={"code": units[0].number, "pallet_id": own.id}, headers=headers
    ).json()
    assert data["ok"] is True, data


def test_scan_into_closed_channel_refused(client: TestClient, db: Session) -> None:
    """Канал закрыл напарник — следующий скан получает отказ и возврат к выбору канала."""
    units = _stocked_units(db, 1)
    loader = make_user(db, name="Грузчик поздний", role=ROLE_LOADER, pin="1234")
    open_shift(db, loader)
    _, pallet = _loader_with_pallet(db, channel="WB", name="Напарник")
    close_pallet(db, pallet_id=pallet.id, user=pallet.opener, note="")
    db.commit()
    login(client, loader)
    token = get_csrf(client, "/loading")
    data = client.post(
        "/loading/scan",
        json={"code": units[0].number, "pallet_id": pallet.id},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    ).json()
    assert data["ok"] is False
    assert data["closed"] is True
    assert "уже закрыт" in data["message"]


def test_loading_page_without_shift_asks_to_start(client: TestClient, db: Session) -> None:
    """Без смены вместо каналов — «Смена не начата» и кнопка на главную."""
    loader = make_user(db, name="Грузчик без смены 2", role=ROLE_LOADER, pin="1234")
    login(client, loader)
    page = client.get("/loading").text
    assert "Смена не начата" in page
    assert "channel-btn" not in page


def test_two_loaders_open_same_channel_at_once(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Гонка: второй не видел открытый канал — уникальный индекс не пустит, он встанет в тот же."""
    import app.services.shipping as shipping

    site = site_by_name(db)
    first = make_user(db, name="Грузчик гонка 1", role=ROLE_LOADER)
    second = make_user(db, name="Грузчик гонка 2", role=ROLE_LOADER)
    pallet = open_new_pallet(db, user=first, site=site, channel="WB")
    db.commit()

    real = shipping.open_channel_at
    calls = {"n": 0}

    def late(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(shipping, "open_channel_at", late)
    joined = open_new_pallet(db, user=second, site=site, channel="WB")
    assert joined.id == pallet.id
    assert calls["n"] == 2
