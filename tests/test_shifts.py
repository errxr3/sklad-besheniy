"""SPEC §12 п. 11 и 12: смены по коду площадки, геопроверка, автозакрытие."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, Site
from app.services.shifts import (
    ShiftError,
    auto_close_shifts,
    check_code,
    edit_shift,
    end_shift,
    make_shift_code,
    start_shift,
    timesheet,
)
from app.services.units import accept_unit, create_printed_units
from app.statuses import END_AUTO, END_MANUAL, END_USER, ROLE_ASSEMBLER, ROLE_LOADER, ROLE_SHIFT_LEAD
from app.timeutil import MOSCOW, now_utc, to_moscow
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _site_with_code(db: Session, name: str = "Коминтерна") -> tuple[Site, str]:
    """Площадка с выпущенным кодом смены."""
    site = site_by_name(db, name)
    code = make_shift_code(db, site)
    db.commit()
    return site, code


def test_start_shift_with_valid_code(db: Session) -> None:
    """Верный код начинает смену и запоминает устройство."""
    site, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик смены", role=ROLE_ASSEMBLER)

    shift = start_shift(db, user=worker, code=code, ip="10.0.0.5", user_agent="Mobile Safari")
    db.commit()

    assert shift.site_id == site.id
    assert shift.ended_at is None
    assert shift.start_ip == "10.0.0.5"
    assert shift.start_ua == "Mobile Safari"


def test_wrong_code_refused(db: Session) -> None:
    """Неверный код смену не начинает (SPEC §12 п. 12)."""
    site, _ = _site_with_code(db)
    worker = make_user(db, name="Сборщик 2", role=ROLE_ASSEMBLER)

    with pytest.raises(ShiftError) as err:
        start_shift(db, user=worker, code=f"SHIFT:{site.id}:чужой-секрет")
    assert "устарел" in str(err.value)

    with pytest.raises(ShiftError) as err_site:
        start_shift(db, user=worker, code="SHIFT:999:секрет")
    assert "не распознан" in str(err_site.value)

    with pytest.raises(ShiftError):
        start_shift(db, user=worker, code="просто текст")

    assert db.scalars(select(Shift)).all() == []


def test_rotated_code_breaks_old_one(db: Session) -> None:
    """После перевыпуска старый код перестаёт работать (SPEC §12 п. 12)."""
    site, old_code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 3", role=ROLE_ASSEMBLER)

    new_code = make_shift_code(db, site)
    db.commit()
    assert new_code != old_code

    with pytest.raises(ShiftError) as err:
        start_shift(db, user=worker, code=old_code)
    assert "устарел" in str(err.value)

    shift = start_shift(db, user=worker, code=new_code)
    db.commit()
    assert shift.id is not None


def test_second_open_shift_impossible(db: Session) -> None:
    """Вторая открытая смена у сотрудника невозможна (SPEC §12 п. 12)."""
    _, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 4", role=ROLE_ASSEMBLER)
    start_shift(db, user=worker, code=code)
    db.commit()

    with pytest.raises(ShiftError) as err:
        start_shift(db, user=worker, code=code)
    assert "уже начата" in str(err.value)
    assert len(db.scalars(select(Shift)).all()) == 1


def test_geo_check_refuses_outside_radius(db: Session) -> None:
    """Вне радиуса площадки смена не начинается (SPEC §12 п. 12)."""
    site, code = _site_with_code(db)
    site.geo_check_enabled = True
    site.geo_lat = 56.3269
    site.geo_lon = 44.0059
    site.geo_radius_m = 300
    db.commit()
    worker = make_user(db, name="Сборщик 5", role=ROLE_ASSEMBLER)

    with pytest.raises(ShiftError) as err:
        start_shift(db, user=worker, code=code, lat=56.3500, lon=44.0500)
    assert "от площадки" in str(err.value)

    with pytest.raises(ShiftError) as err_no_geo:
        start_shift(db, user=worker, code=code, lat=None, lon=None)
    assert "геопозиции" in str(err_no_geo.value)

    shift = start_shift(db, user=worker, code=code, lat=56.3270, lon=44.0060)
    db.commit()
    assert shift.start_lat == pytest.approx(56.3270)


def test_geo_check_disabled_by_default(db: Session) -> None:
    """По умолчанию геопроверка выключена и координаты не требуются."""
    site, code = _site_with_code(db)
    assert site.geo_check_enabled is False
    worker = make_user(db, name="Сборщик 6", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, code=code)
    db.commit()
    assert shift.id is not None


def test_end_shift_requires_same_site_code(db: Session) -> None:
    """Смена закрывается кодом своей площадки."""
    _, code = _site_with_code(db)
    other_site, other_code = _site_with_code(db, "Учительская")
    worker = make_user(db, name="Сборщик 7", role=ROLE_ASSEMBLER)
    start_shift(db, user=worker, code=code)
    db.commit()

    with pytest.raises(ShiftError) as err:
        end_shift(db, user=worker, code=other_code)
    assert other_site.name in str(err.value)

    shift = end_shift(db, user=worker, code=code)
    db.commit()
    assert shift.ended_at is not None
    assert shift.end_reason == END_USER


def test_auto_close_uses_last_scan_time(db: Session) -> None:
    """Автозакрытие ставит время последнего скана в смену (SPEC §12 п. 12)."""
    site, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 8", role=ROLE_ASSEMBLER)
    article = make_article(db)

    yesterday_start = datetime(2026, 10, 13, 5, 0, tzinfo=MOSCOW)
    shift = start_shift(db, user=worker, code=code)
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
    _, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 9", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, code=code)
    shift.started_at = datetime(2026, 10, 13, 8, 15, tzinfo=MOSCOW)
    db.commit()

    auto_close_shifts(db, moment=datetime(2026, 10, 14, 0, 5, tzinfo=MOSCOW))
    db.commit()
    db.refresh(shift)
    assert shift.ended_at == shift.started_at
    assert shift.end_reason == END_AUTO


def test_auto_close_does_not_touch_today_shift(db: Session) -> None:
    """Сегодняшняя открытая смена автозакрытием не трогается."""
    _, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 10", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, code=code)
    db.commit()

    closed = auto_close_shifts(db, moment=now_utc())
    db.commit()
    db.refresh(shift)
    assert closed == 0
    assert shift.ended_at is None


def test_manual_edit_requires_note(db: Session) -> None:
    """Правка смены требует комментарий и сохраняет прежние значения."""
    _, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 11", role=ROLE_ASSEMBLER)
    lead = make_user(db, name="Начальник правок", role=ROLE_SHIFT_LEAD)
    shift = start_shift(db, user=worker, code=code)
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
    _, code = _site_with_code(db)
    worker = make_user(db, name="Сборщик 12", role=ROLE_ASSEMBLER)
    shift = start_shift(db, user=worker, code=code)
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


def test_shift_scan_through_http(client: TestClient, db: Session) -> None:
    """Начало и окончание смены через веб: камера шлёт код тем же обработчиком."""
    _, code = _site_with_code(db)
    worker = make_user(db, name="Грузчик веб", role=ROLE_LOADER, pin="1234")
    login(client, worker)
    token = get_csrf(client, "/")

    started = client.post(
        "/shift/start",
        json={"code": code, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert started.json()["ok"] is True
    assert "Смена начата" in started.json()["message"]

    again = client.post(
        "/shift/start",
        json={"code": code, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert again.json()["ok"] is False
    assert "уже начата" in again.json()["message"]

    ended = client.post(
        "/shift/end",
        json={"code": code, "csrf_token": token},
        headers={"X-CSRF-Token": token, "X-Requested-With": "fetch"},
    )
    assert ended.json()["ok"] is True


def test_start_shift_page_has_no_text_input(client: TestClient, db: Session) -> None:
    """На экране начала смены нет текстового поля — только камера (SPEC §5.2)."""
    _site_with_code(db)
    worker = make_user(db, name="Сборщик 13", role=ROLE_ASSEMBLER, pin="1234")
    login(client, worker)
    page_text = client.get("/shift/start").text
    assert "camera-btn" in page_text
    assert 'type="text"' not in page_text
    assert "scan-input" not in page_text


def test_shift_code_pdf_contains_qr(client: TestClient, db: Session) -> None:
    """Лист A4 с кодом смены печатается и QR читается."""
    import pypdfium2
    import zxingcpp

    from app.services.admin import create_user
    from app.statuses import ROLE_ADMIN

    admin = create_user(db, actor_id=None, name="Админ кодов", role=ROLE_ADMIN, pin="1234")
    db.commit()
    login(client, admin)
    site = site_by_name(db)

    response = client.get(f"/admin/sites/{site.id}/code.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"

    document = pypdfium2.PdfDocument(response.content)
    image = document[0].render(scale=3).to_pil().convert("L")
    codes = [result.text for result in zxingcpp.read_barcodes(image)]
    assert len(codes) == 1
    assert codes[0].startswith(f"SHIFT:{site.id}:")

    db.expire_all()
    site_fresh = site_by_name(db)
    assert check_code(db, codes[0]).id == site_fresh.id
