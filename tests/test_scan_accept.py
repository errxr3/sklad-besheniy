"""SPEC §12 п. 2 и §6.3: приёмка сканом, двойной скан, права сборщика."""

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Movement, Unit
from app.services.tasks import create_task
from app.services.units import accept_unit, create_printed_units, stock_count
from app.statuses import IN_STOCK, PRINTED, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD, ROLE_STOREKEEPER
from app.timeutil import today_msk
from tests.helpers import get_csrf, login, make_article, make_user, open_shift, site_by_name


def _printed_unit(db: Session, assembler, qty: int = 1) -> list[Unit]:
    """Напечатать этикетки на сборщика."""
    site = site_by_name(db)
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
    return units


def test_accept_moves_to_stock(db: Session) -> None:
    """Скан этикетки переводит зеркало на склад и увеличивает остаток."""
    worker = make_user(db, name="Сборщик 1", role=ROLE_ASSEMBLER)
    unit = _printed_unit(db, worker)[0]
    assert stock_count(db, unit.article_id, unit.site_id) == 0

    result = accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    assert result.ok, result.message
    assert result.stock == 1
    db.refresh(unit)
    assert unit.status == IN_STOCK
    assert unit.accepted_at is not None


def test_second_scan_does_not_change_stock(db: Session) -> None:
    """Повторный скан принятой этикетки отклоняется, остаток не меняется (SPEC §12 п. 2)."""
    worker = make_user(db, name="Сборщик 2", role=ROLE_ASSEMBLER)
    unit = _printed_unit(db, worker)[0]

    first = accept_unit(db, code=unit.number, actor=worker)
    db.commit()
    second = accept_unit(db, code=unit.number, actor=worker)
    db.commit()

    assert first.ok
    assert not second.ok
    assert "Уже на складе" in second.message
    assert "принято" in second.detail
    assert stock_count(db, unit.article_id, unit.site_id) == 1
    movements = db.scalar(
        select(func.count()).select_from(Movement).where(Movement.unit_id == unit.id)
    )
    # печать + приёмка, второй скан ничего не записал
    assert movements == 2


def test_unknown_number_refused(db: Session) -> None:
    """Неизвестный номер — отказ."""
    worker = make_user(db, name="Сборщик 3", role=ROLE_ASSEMBLER)
    result = accept_unit(db, code="Z9999999", actor=worker)
    assert not result.ok
    assert "не найден" in result.message


def test_marketplace_barcode_refused(db: Session) -> None:
    """Код маркетплейса не принимается как этикетка склада."""
    worker = make_user(db, name="Сборщик 4", role=ROLE_ASSEMBLER)
    result = accept_unit(db, code="4603746901234", actor=worker)
    assert not result.ok
    assert "не этикетка СКЛАД" in result.message


def test_assembler_cannot_accept_foreign_unit(db: Session) -> None:
    """Сборщик принимает только свои зеркала, начальник смены — любые."""
    owner = make_user(db, name="Хозяин", role=ROLE_ASSEMBLER)
    stranger = make_user(db, name="Чужой", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник смены", role=ROLE_SHIFT_LEAD)
    unit = _printed_unit(db, owner)[0]

    refused = accept_unit(db, code=unit.number, actor=stranger)
    assert not refused.ok
    assert "другого сборщика" in refused.message

    allowed = accept_unit(db, code=unit.number, actor=lead)
    db.commit()
    assert allowed.ok


def test_scan_is_case_insensitive_and_trimmed(db: Session) -> None:
    """Пробелы и регистр в отсканированной строке не мешают."""
    worker = make_user(db, name="Сборщик 5", role=ROLE_ASSEMBLER)
    unit = _printed_unit(db, worker)[0]
    result = accept_unit(db, code=f"  {unit.number.lower()} \r\n", actor=worker)
    db.commit()
    assert result.ok


def test_scan_through_http(client: TestClient, db: Session) -> None:
    """Полный путь: задача → кладовщик печатает на сборщика → сборщик со сменой принимает сканом."""
    site = site_by_name(db)
    worker = make_user(db, name="Сборщик веб", role=ROLE_ASSEMBLER, pin="1234")
    keeper = make_user(db, name="Кладовщик веб", role=ROLE_STOREKEEPER, home_site_id=site.id)
    lead = make_user(db, name="Начальник веб", role=ROLE_SHIFT_LEAD)
    open_shift(db, worker, site)
    article = make_article(db)
    create_task(
        db, actor=lead, day=today_msk(), site_id=site.id, user_id=worker.id, kind="article",
        article_id=article.id, qty_plan=2,
    )
    db.commit()

    with TestClient(client.app) as keeper_client:
        login(keeper_client, keeper)
        keeper_token = get_csrf(keeper_client, "/labels")
        pdf = keeper_client.post(
            "/labels/print",
            data={"csrf_token": keeper_token, "user_id": str(worker.id), "day": today_msk().isoformat()},
        )
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"

    numbers = [u.number for u in db.scalars(select(Unit).order_by(Unit.id)).all()]
    assert len(numbers) == 2

    login(client, worker)
    token = get_csrf(client, "/")
    response = client.post(
        "/scan/accept",
        json={"code": numbers[0], "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["stock"] == 1

    again = client.post(
        "/scan/accept",
        json={"code": numbers[0], "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert again.json()["ok"] is False


def test_assembler_cannot_print(client: TestClient, db: Session) -> None:
    """Сборщик не печатает этикетки: их заранее печатает кладовщик (решение владельца)."""
    worker = make_user(db, name="Сборщик без печати", role=ROLE_ASSEMBLER, pin="1234")
    open_shift(db, worker)
    login(client, worker)
    token = get_csrf(client, "/")
    assert client.get("/labels").status_code == 403
    response = client.post("/labels/print", data={"csrf_token": token, "user_id": str(worker.id)})
    assert response.status_code == 403
    # Экрана «Сделал зеркало» больше нет ни у кого
    assert client.get("/make").status_code == 404
    assert db.scalar(select(func.count()).select_from(Unit)) == 0


def test_scan_without_shift_refused(client: TestClient, db: Session) -> None:
    """Сборщик без открытой смены не может сканировать (SPEC §12 п. 11)."""
    worker = make_user(db, name="Без смены", role=ROLE_ASSEMBLER, pin="1234")
    login(client, worker)
    token = get_csrf(client, "/")
    response = client.post(
        "/scan/accept",
        json={"code": "Z0000001", "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
    assert "начните смену" in response.json()["error"]


def test_scan_uses_few_queries(db: Session) -> None:
    """Один скан — немного запросов к базе (SPEC §11: ответ ≤ 1 с)."""
    worker = make_user(db, name="Сборщик 6", role=ROLE_ASSEMBLER)
    unit = _printed_unit(db, worker)[0]

    queries: list[str] = []

    def before_cursor(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        queries.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before_cursor)
    try:
        accept_unit(db, code=unit.number, actor=worker)
        db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor)

    selects = [q for q in queries if q.strip().upper().startswith("SELECT")]
    assert len(selects) <= 4, f"слишком много запросов на скан: {queries}"


def test_printed_label_can_be_written_off(db: Session) -> None:
    """Испорченная этикетка списывается переходом printed → written_off."""
    from app.services.units import write_off_unit
    from app.statuses import WRITTEN_OFF

    lead = make_user(db, name="Начальник 2", role=ROLE_SHIFT_LEAD)
    worker = make_user(db, name="Сборщик 7", role=ROLE_ASSEMBLER)
    unit = _printed_unit(db, worker)[0]
    assert unit.status == PRINTED

    result = write_off_unit(db, code=unit.number, actor=lead, reason="ошибочная этикетка")
    db.commit()
    db.refresh(unit)
    assert result.ok
    assert unit.status == WRITTEN_OFF
