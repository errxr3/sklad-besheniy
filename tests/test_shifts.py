"""Смены кнопкой (без QR, решение владельца 2026-10-21), автозакрытие, табель, табло."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, Site
from app.services.shifts import (
    ShiftError,
    auto_close_shifts,
    edit_shift,
    end_shift,
    shift_sites,
    start_shift,
    timesheet,
)
from app.services.units import accept_unit, create_printed_units
from app.statuses import END_AUTO, END_MANUAL, END_USER, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import MOSCOW, now_utc, to_moscow
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _site_with_id(db: Session, name: str = "Коминтерна") -> tuple[Site, int]:
    """Площадка и её id (смена начинается кнопкой на площадке)."""
    site = site_by_name(db, name)
    return site, site.id


def test_start_shift_by_button(db: Session) -> None:
    """Смена начинается кнопкой на площадке сотрудника и запоминает устройство."""
    site = site_by_name(db)
    worker = make_user(db, name="Сборщик смены", role=ROLE_ASSEMBLER, home_site_id=site.id)

    shift = start_shift(db, user=worker, site_id=site.id, ip="10.0.0.5", user_agent="Mobile Safari")
    db.commit()

    assert shift.site_id == site.id
    assert shift.ended_at is None
    assert shift.start_ip == "10.0.0.5"
    assert shift.start_ua == "Mobile Safari"


def test_assembler_only_on_own_site_loader_chooses(db: Session) -> None:
    """Сборщик — только на своей площадке; грузчик выбирает Коминтерна или Учительскую."""
    kom = site_by_name(db, "Коминтерна")
    uch = site_by_name(db, "Учительская")
    worker = make_user(db, name="Сборщик Коминтерна", role=ROLE_ASSEMBLER, home_site_id=kom.id)
    with pytest.raises(ShiftError):
        start_shift(db, user=worker, site_id=uch.id)

    loader = make_user(db, name="Грузчик выбор", role=ROLE_LOADER, home_site_id=kom.id)
    assert {s.id for s in shift_sites(db, loader)} == {kom.id, uch.id}
    shift = start_shift(db, user=loader, site_id=uch.id)
    db.commit()
    assert shift.site_id == uch.id


def test_second_open_shift_impossible(db: Session) -> None:
    """Вторая открытая смена у сотрудника невозможна (SPEC §12 п. 12)."""
    site = site_by_name(db)
    worker = make_user(db, name="Сборщик 4", role=ROLE_ASSEMBLER)
    start_shift(db, user=worker, site_id=site.id)
    db.commit()

    with pytest.raises(ShiftError) as err:
        start_shift(db, user=worker, site_id=site.id)
    assert "уже начата" in str(err.value)
    assert len(db.scalars(select(Shift)).all()) == 1


def test_end_shift_by_button(db: Session) -> None:
    site = site_by_name(db)
    worker = make_user(db, name="Сборщик 7", role=ROLE_ASSEMBLER)
    with pytest.raises(ShiftError):
        end_shift(db, user=worker)
    start_shift(db, user=worker, site_id=site.id)
    db.commit()
    shift = end_shift(db, user=worker)
    db.commit()
    assert shift.ended_at is not None
    assert shift.end_reason == END_USER


def test_auto_close_uses_last_scan_time(db: Session) -> None:
    """Автозакрытие ставит время последнего скана в смену (SPEC §12 п. 12)."""
    site, code = _site_with_id(db)
    worker = make_user(db, name="Сборщик 8", role=ROLE_ASSEMBLER)
    article = make_article(db)

    yesterday_start = datetime(2026, 10, 13, 5, 0, tzinfo=MOSCOW)
    shift = start_shift(db, user=worker, site_id=code)
    shift.started_at = yesterday_start
    db.commit()

    unit = create_printed_units(
        db,
        article_id=article.id,
        qty=1,
        site_id=site.id,
        assembler_id=worker.id,
        actor_id=worker.id,
        shift_id=shift.id,
    )[0]
    db.commit()
    accept_unit(db, code=unit.number, actor=worker, shift_id=shift.id)
    db.commit()

    last_scan = datetime(2026, 10, 13, 17, 42, tzinfo=MOSCOW)
    from sqlalchemy import text

    db.execute(
        text("ALTER TABLE movements DISABLE TRIGGER USER"),
    )
    db.execute(
        text("UPDATE movements SET created_at = :moment WHERE shift_id = :shift"),
        {"moment": last_scan, "shift": shift.id},
    )
    db.execute(text("ALTER TABLE movements ENABLE TRIGGER USER"))
    db.commit()

    closed = auto_close_shifts(db, moment=datetime(2026, 10, 14, 0, 5, tzinfo=MOSCOW))
    db.commit()
    db.refresh(shift)

    assert closed == 1
    assert shift.end_reason == END_AUTO
    assert to_moscow(shift.ended_at).strftime("%H:%M") == "17:42"


def test_auto_close_without_scans_uses_start_time(db: Session) -> None:
    """Если сканов не было — время окончания равно времени начала."""
    _, code = _site_with_id(db)
    worker = make_user(db, name="Сборщик 9", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, site_id=code)
    shift.started_at = datetime(2026, 10, 13, 8, 15, tzinfo=MOSCOW)
    db.commit()

    auto_close_shifts(db, moment=datetime(2026, 10, 14, 0, 5, tzinfo=MOSCOW))
    db.commit()
    db.refresh(shift)
    assert shift.ended_at == shift.started_at
    assert shift.end_reason == END_AUTO


def test_auto_close_does_not_touch_today_shift(db: Session) -> None:
    """Сегодняшняя открытая смена автозакрытием не трогается."""
    _, code = _site_with_id(db)
    worker = make_user(db, name="Сборщик 10", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, site_id=code)
    db.commit()

    closed = auto_close_shifts(db, moment=now_utc())
    db.commit()
    db.refresh(shift)
    assert closed == 0
    assert shift.ended_at is None


def test_manual_edit_requires_note(db: Session) -> None:
    """Правка смены требует комментарий и сохраняет прежние значения."""
    _, code = _site_with_id(db)
    worker = make_user(db, name="Сборщик 11", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник правок", role=ROLE_SHIFT_LEAD)
    shift = start_shift(db, user=worker, site_id=code)
    db.commit()

    with pytest.raises(ShiftError):
        edit_shift(db, shift_id=shift.id, actor=lead, ended_at=now_utc(), note="  ")

    edit_shift(
        db, shift_id=shift.id, actor=lead, ended_at=now_utc(), note="забыл закрыть, ушёл в 18:00"
    )
    db.commit()
    db.refresh(shift)
    assert shift.end_reason == END_MANUAL
    assert shift.edited_by == lead.id
    assert "было:" in (shift.edit_note or "")
    assert "забыл закрыть" in (shift.edit_note or "")


def test_timesheet_counts_hours(db: Session) -> None:
    """Табель считает часы за период (SPEC §5.6)."""
    _, code = _site_with_id(db)
    worker = make_user(db, name="Сборщик 12", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, site_id=code)
    shift.started_at = datetime(2026, 10, 13, 8, 0, tzinfo=MOSCOW)
    shift.ended_at = datetime(2026, 10, 13, 17, 30, tzinfo=MOSCOW)
    shift.end_reason = END_USER
    db.commit()

    rows = timesheet(
        db, user_id=worker.id, date_from=shift.started_at.date(), date_to=shift.started_at.date()
    )
    assert len(rows) == 1
    assert rows[0].hours == 9.5
    assert rows[0].closed_label == "сотрудником"


def test_shift_buttons_through_http(client: TestClient, db: Session) -> None:
    """Начало и окончание смены кнопками; грузчик видит выбор площадки."""
    kom = site_by_name(db, "Коминтерна")
    worker = make_user(db, name="Грузчик веб", role=ROLE_LOADER, pin="1234")
    login(client, worker)
    home = client.get("/").text
    assert "Коминтерна" in home and "Учительская" in home
    assert "camera-btn" not in home  # QR-кода больше нет
    token = get_csrf(client, "/")

    started = client.post("/shift/start", data={"csrf_token": token, "site_id": str(kom.id)})
    assert started.status_code == 200
    assert "На смене с" in started.text

    again = client.post(
        "/shift/start", data={"csrf_token": token, "site_id": str(kom.id)}, follow_redirects=False
    )
    assert "err=" in again.headers["location"]

    ended = client.post("/shift/end", data={"csrf_token": token})
    assert ended.status_code == 200
    assert "Смена не начата" in ended.text


def test_no_qr_shift_code_pages(client: TestClient, db: Session) -> None:
    """QR смены убран совсем: листа с кодом и перевыпуска кода больше нет."""
    from app.services.admin import create_user
    from app.statuses import ROLE_ADMIN

    admin = create_user(
        db, actor_id=None, first_name="Админ", last_name="Кодов", login="codes", role=ROLE_ADMIN, pin="1234"
    )
    db.commit()
    login(client, admin)
    site = site_by_name(db)
    assert client.get(f"/admin/sites/{site.id}/code.pdf").status_code == 404
    assert "QR" not in client.get("/admin/sites").text


def test_board_counts_only_chosen_site(db: Session) -> None:
    """Табло показывает смены и выработку выбранной площадки (SPEC §5.5)."""
    from app.services.shifts import board_rows
    from app.services.units import accept_unit, create_printed_units
    from app.timeutil import today_msk

    site, code = _site_with_id(db)
    other, other_code = _site_with_id(db, "Учительская")
    here = make_user(db, name="Работает здесь", role=ROLE_ASSEMBLER)
    there = make_user(db, name="Работает там", role=ROLE_ASSEMBLER)
    nobody = make_user(db, name="Не вышел совсем", role=ROLE_LOADER)

    start_shift(db, user=here, site_id=code)
    start_shift(db, user=there, site_id=other_code)
    db.commit()

    article = make_article(db)
    units = create_printed_units(
        db, article_id=article.id, qty=2, site_id=site.id, assembler_id=here.id, actor_id=here.id
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=here)
    db.commit()

    rows = {row.user.name: row for row in board_rows(db, site_id=site.id, day=today_msk())}

    assert "Работает там" not in rows, "сотрудник со сменой на другой площадке сюда не попадает"
    assert rows["Работает здесь"].state == "на смене"
    assert rows["Работает здесь"].accepted == 2
    assert rows[nobody.name].state == "не вышел"
    assert rows[nobody.name].accepted == 0


def test_board_ignores_other_site_movements(db: Session) -> None:
    """Выработка на другой площадке не попадает в табло этой."""
    from app.services.shifts import board_rows
    from app.services.units import accept_unit, create_printed_units
    from app.timeutil import today_msk

    site, code = _site_with_id(db)
    other = site_by_name(db, "Учительская")
    worker = make_user(db, name="Сборщик двух площадок", role=ROLE_ASSEMBLER)
    start_shift(db, user=worker, site_id=code)
    db.commit()

    article = make_article(db)
    units = create_printed_units(
        db, article_id=article.id, qty=3, site_id=other.id, assembler_id=worker.id, actor_id=worker.id
    )
    db.commit()
    for unit in units:
        accept_unit(db, code=unit.number, actor=worker)
    db.commit()

    rows = {row.user.name: row for row in board_rows(db, site_id=site.id, day=today_msk())}
    assert rows["Сборщик двух площадок"].accepted == 0
