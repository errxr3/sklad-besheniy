"""SPEC §6.10: журнал с фильтрами, выгрузка CSV и отчёт «Выработка»."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.journal import JournalFilters, journal_csv, journal_page, output_report
from app.services.units import accept_unit, create_printed_units, write_off_unit
from app.statuses import MV_ACCEPT, ROLE_ASSEMBLER, ROLE_SHIFT_LEAD
from app.timeutil import today_msk
from tests.helpers import login, make_article, make_user, site_by_name


def _work(db: Session, qty: int = 3) -> tuple:
    """Сборщик, сделавший несколько зеркал."""
    assembler = make_user(db, name="Сборщик журнала", role=ROLE_ASSEMBLER)
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
    for unit in units:
        accept_unit(db, code=unit.number, actor=assembler)
    db.commit()
    return assembler, article, units


def test_journal_filters_by_type_and_user(db: Session) -> None:
    """Журнал фильтруется по типу операции и сотруднику."""
    assembler, _, _ = _work(db, 2)
    rows, total = journal_page(db, JournalFilters(movement_type=MV_ACCEPT))
    assert total == 2
    assert all(row.type == MV_ACCEPT for row in rows)

    rows_user, total_user = journal_page(db, JournalFilters(user_id=assembler.id))
    assert total_user == 4  # 2 печати + 2 приёмки
    assert rows_user


def test_journal_filters_by_number_and_article(db: Session) -> None:
    """Журнал фильтруется по номеру зеркала и артикулу."""
    _, article, units = _work(db, 2)
    _, total = journal_page(db, JournalFilters(number=units[0].number))
    assert total == 2

    _, total_article = journal_page(db, JournalFilters(article_id=article.id))
    assert total_article == 4


def test_journal_filters_by_dates(db: Session) -> None:
    """Фильтр по датам отсекает вчерашний день."""
    _work(db, 1)
    today = today_msk()
    _, total_today = journal_page(db, JournalFilters(date_from=today, date_to=today))
    assert total_today == 2
    yesterday = today - timedelta(days=1)
    _, total_yesterday = journal_page(db, JournalFilters(date_from=yesterday, date_to=yesterday))
    assert total_yesterday == 0


def test_journal_csv_has_bom_and_semicolons(db: Session) -> None:
    """CSV открывается в Excel: BOM и разделитель «;»."""
    _work(db, 1)
    data = journal_csv(db, JournalFilters())
    text = data.decode("utf-8")
    assert text.startswith("﻿")
    assert "Когда;Номер;Артикул" in text
    assert "Приёмка после изготовления" in text


def test_output_report_counts_defects(db: Session) -> None:
    """«Выработка»: сделано, брак и процент брака."""
    assembler, article, units = _work(db, 4)
    lead = make_user(db, name="Начальник выработки", role=ROLE_SHIFT_LEAD)
    write_off_unit(db, code=units[0].number, actor=lead, reason="бой")
    db.commit()

    today = today_msk()
    rows = output_report(db, date_from=today, date_to=today)
    assert len(rows) == 1
    row = rows[0]
    assert row.assembler == assembler.name
    assert row.article == article.name
    assert row.made == 4
    assert row.scrapped == 1
    assert row.defect_percent == 25.0


def test_journal_page_through_http(client: TestClient, db: Session) -> None:
    """Страница журнала и выгрузка CSV открываются начальником смены."""
    _work(db, 2)
    lead = make_user(db, name="Начальник страницы", role=ROLE_SHIFT_LEAD, pin="1234")
    login(client, lead)

    page_response = client.get("/journal")
    assert page_response.status_code == 200
    assert "Журнал движений" in page_response.text

    csv_response = client.get("/journal.csv")
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]

    output_response = client.get("/reports/output")
    assert output_response.status_code == 200
    assert "Выработка" in output_response.text
